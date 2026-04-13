#!/usr/bin/env python3
"""
Headless test for dual camera ring buffer + timestamp matching.

Builds minimal capture-only GStreamer pipelines (no preview decode),
performs multiple captures, and reports frame sync statistics.

Usage:
    python3 scirpts/headless_sync_test.py                                  # auto-detect, 20 captures
    python3 scirpts/headless_sync_test.py --captures 10                    # quick test
    python3 scirpts/headless_sync_test.py --devices /dev/video0,/dev/video2
    python3 scirpts/headless_sync_test.py --framerate 10/1                 # 10 Hz mode
"""

import argparse
import os
import sys
import signal
import threading
import time
from collections import deque

# Ensure src/ is on the path for camera_pipeline.find_uvc_cameras
_src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _src_dir)

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from loguru import logger

from camera_pipeline import find_uvc_cameras, StampedSample, RING_BUFFER_SIZE, _validate_jpeg

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Same-trigger frames arrive within ~2-3 ms across separate USB controllers.
# Adjacent triggers at 27 Hz are ~37 ms apart, at 10 Hz ~100 ms.
SYNC_THRESHOLD_MS = 10.0

# ---------------------------------------------------------------------------
# Lightweight capture-only pipeline (no preview decode)
# ---------------------------------------------------------------------------

class HeadlessCaptureSource:
    """
    Minimal GStreamer pipeline for one camera: v4l2src -> appsink.

    No jpegparse, no mppjpegdec, no preview — just raw MJPEG frames
    into a ring buffer with wall-clock arrival timestamps.
    """

    def __init__(self, device: str, framerate: str = "55/2"):
        self.device = device
        self.framerate = framerate
        self._pipeline: Gst.Pipeline | None = None
        self._sample_ring: deque[StampedSample] = deque(maxlen=RING_BUFFER_SIZE)
        self._lock = threading.Lock()
        self._probe_timestamps: dict[int, int] = {}
        self._probe_lock = threading.Lock()
        self._frame_count = 0
        self._error: str | None = None

    def start(self) -> bool:
        pipe_str = (
            f"v4l2src device={self.device} io-mode=mmap ! "
            f"image/jpeg,width=5120,height=3840,framerate={self.framerate} ! "
            "queue name=q leaky=downstream max-size-buffers=1 ! "
            "appsink name=sink drop=true max-buffers=1 emit-signals=true"
        )
        logger.info("[{}] Pipeline: {}", self.device, pipe_str)

        try:
            self._pipeline = Gst.parse_launch(pipe_str)
        except Exception as exc:
            self._error = str(exc)
            logger.error("[{}] Parse failed: {}", self.device, exc)
            return False

        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)

        # Attach timestamp probe on queue's sink pad (v4l2src thread)
        q = self._pipeline.get_by_name("q")
        if q is not None:
            q_sink = q.get_static_pad("sink")
            q_sink.add_probe(Gst.PadProbeType.BUFFER, self._stamp_probe)
            logger.info("[{}] Timestamp probe attached on queue sink pad", self.device)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            bus = self._pipeline.get_bus()
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg:
                err, debug = msg.parse_error()
                self._error = f"{err.message} | {debug}"
            else:
                self._error = "Failed to set PLAYING"
            logger.error("[{}] Start failed: {}", self.device, self._error)
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            return False

        logger.success("[{}] Streaming", self.device)
        return True

    def stop(self):
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline.get_state(3 * Gst.SECOND)
            self._pipeline = None
        with self._lock:
            self._sample_ring.clear()
        with self._probe_lock:
            self._probe_timestamps.clear()
        logger.info("[{}] Stopped ({} frames received)", self.device, self._frame_count)

    def _stamp_probe(self, pad, info) -> Gst.PadProbeReturn:
        """Record wall-clock timestamp keyed by PTS on v4l2src's thread."""
        buf = info.get_buffer()
        if buf is not None:
            pts = buf.pts
            if pts != Gst.CLOCK_TIME_NONE:
                ts = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                with self._probe_lock:
                    self._probe_timestamps[pts] = ts
                    if len(self._probe_timestamps) > 50:
                        keys = list(self._probe_timestamps.keys())
                        for k in keys[:25]:
                            del self._probe_timestamps[k]
        return Gst.PadProbeReturn.OK

    def _on_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        pts = buf.pts
        ts = None
        if pts != Gst.CLOCK_TIME_NONE:
            with self._probe_lock:
                ts = self._probe_timestamps.pop(pts, None)
        if ts is None:
            ts = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        self._frame_count += 1
        with self._lock:
            self._sample_ring.append(StampedSample(ts, sample))
        return Gst.FlowReturn.OK

    def snapshot_ring(self) -> list[StampedSample]:
        with self._lock:
            return list(self._sample_ring)

    def check_bus_errors(self) -> str | None:
        """Poll the bus once; return error string or None."""
        if self._pipeline is None:
            return None
        bus = self._pipeline.get_bus()
        msg = bus.pop_filtered(Gst.MessageType.ERROR)
        if msg:
            err, debug = msg.parse_error()
            self._error = f"{err.message} | {debug}"
            return self._error
        return None

    @property
    def frame_count(self) -> int:
        return self._frame_count


# ---------------------------------------------------------------------------
# Frame matching (same logic as DualCameraManager.capture)
# ---------------------------------------------------------------------------

def match_rings(ring_a: list[StampedSample],
                ring_d: list[StampedSample]) -> tuple[int, int, float]:
    """
    Find the pair with minimum arrival-time delta.

    Returns (index_a, index_d, delta_ms).
    """
    best_delta = float("inf")
    best_ai = best_di = 0
    for ai, a in enumerate(ring_a):
        for di, d in enumerate(ring_d):
            delta = abs(a.timestamp_ns - d.timestamp_ns)
            if delta < best_delta:
                best_delta = delta
                best_ai, best_di = ai, di
    return best_ai, best_di, best_delta / 1_000_000


# ---------------------------------------------------------------------------
# Capture + file write
# ---------------------------------------------------------------------------

def save_sample(sample, path: str) -> bool:
    """Write raw MJPEG buffer bytes to file. Returns True on success."""
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return False
    try:
        data = bytes(info.data)
        if not _validate_jpeg(data):
            logger.warning("Invalid JPEG skipped: {}", path)
            return False
        with open(path, "wb") as f:
            f.write(data)
        return True
    finally:
        buf.unmap(info)


# ---------------------------------------------------------------------------
# Main test loop
# ---------------------------------------------------------------------------

def run_test(devices, num_captures, interval_s, save_dir, framerate):
    Gst.init(None)

    src_a = HeadlessCaptureSource(devices[0], framerate)
    src_d = HeadlessCaptureSource(devices[1], framerate)

    if not src_a.start() or not src_d.start():
        src_a.stop()
        src_d.stop()
        sys.exit(1)

    # Warm-up: wait for ring buffers to fill
    warmup_s = 3
    logger.info("Warming up {}s...", warmup_s)
    time.sleep(warmup_s)

    # Check for streaming errors after warmup
    for label, src in [("A", src_a), ("D", src_d)]:
        err = src.check_bus_errors()
        if err:
            logger.error("[{}] Streaming error: {}", label, err)
            src_a.stop()
            src_d.stop()
            sys.exit(1)
        if src.frame_count == 0:
            logger.error("[{}] No frames received after {}s warmup — "
                         "is the camera streaming / trigger active?", label, warmup_s)
            src_a.stop()
            src_d.stop()
            sys.exit(1)

    logger.info("Warmup done — A: {} frames, D: {} frames",
                src_a.frame_count, src_d.frame_count)

    os.makedirs(save_dir, exist_ok=True)
    deltas: list[float] = []
    saved_pairs: list[tuple[str, str]] = []

    for n in range(1, num_captures + 1):
        ring_a = src_a.snapshot_ring()
        ring_d = src_d.snapshot_ring()

        if not ring_a or not ring_d:
            logger.warning("[{:2d}/{}] Ring empty (A={}, D={})",
                           n, num_captures, len(ring_a), len(ring_d))
            time.sleep(interval_s)
            continue

        ai, di, delta_ms = match_rings(ring_a, ring_d)
        deltas.append(delta_ms)

        synced = "SYNC  " if delta_ms < SYNC_THRESHOLD_MS else "DESYNC"
        logger.info(
            "[{:2d}/{}] {} | delta={:.3f}ms | ring[{}]+ring[{}] | sizes={}/{}",
            n, num_captures, synced, delta_ms, ai, di,
            len(ring_a), len(ring_d),
        )

        # Dump ring for first 3 captures and any desyncs
        if delta_ms >= SYNC_THRESHOLD_MS or n <= 3:
            _dump_rings(ring_a, ring_d, ai, di)

        # Save matched pair
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        ms = int((time.time() % 1) * 1000)
        base = f"{ts_str}_{ms:03d}"

        # Include buffer PTS for debugging
        pts_a = ring_a[ai].sample.get_buffer().pts
        pts_d = ring_d[di].sample.get_buffer().pts
        pts_a_str = f"_pts{pts_a}" if pts_a != Gst.CLOCK_TIME_NONE else ""
        pts_d_str = f"_pts{pts_d}" if pts_d != Gst.CLOCK_TIME_NONE else ""

        path_a = os.path.join(save_dir, f"A_{base}{pts_a_str}.jpg")
        path_d = os.path.join(save_dir, f"D_{base}{pts_d_str}.jpg")

        ok_a = save_sample(ring_a[ai].sample, path_a)
        ok_d = save_sample(ring_d[di].sample, path_d)

        if ok_a and ok_d:
            saved_pairs.append((os.path.basename(path_a), os.path.basename(path_d)))
            sz_a = os.path.getsize(path_a) / 1024
            sz_d = os.path.getsize(path_d) / 1024
            logger.info("  Saved: {:.0f}KB + {:.0f}KB", sz_a, sz_d)

        time.sleep(interval_s)

    # Shutdown
    src_a.stop()
    src_d.stop()

    print_report(deltas, framerate, save_dir, saved_pairs)


def _dump_rings(ring_a, ring_d, best_ai, best_di):
    """Print arrival timestamps of both rings for visual inspection."""
    ref = min(ring_a[0].timestamp_ns, ring_d[0].timestamp_ns)

    def _fmt(ring, label, best_idx):
        parts = []
        for i, s in enumerate(ring):
            t_ms = (s.timestamp_ns - ref) / 1_000_000
            marker = " <-- matched" if i == best_idx else ""
            parts.append(f"    [{i}] +{t_ms:8.3f}ms{marker}")
        return f"  {label} ring:\n" + "\n".join(parts)

    logger.debug("Ring dump:\n{}\n{}", _fmt(ring_a, "A", best_ai), _fmt(ring_d, "D", best_di))


def print_report(deltas, framerate, save_dir, saved_pairs):
    if not deltas:
        print("\nNo captures completed.")
        return

    synced = sum(1 for d in deltas if d < SYNC_THRESHOLD_MS)
    total = len(deltas)
    sorted_d = sorted(deltas)

    print()
    print("=" * 60)
    print("  RING BUFFER FRAME SYNC TEST REPORT")
    print("=" * 60)
    print(f"  Framerate:       {framerate}")
    print(f"  Threshold:       {SYNC_THRESHOLD_MS} ms")
    print(f"  Save dir:        {save_dir}")
    print(f"  Total captures:  {total}")
    print(f"  Synced:          {synced}/{total} ({100 * synced / total:.0f}%)")
    print(f"  Desynced:        {total - synced}/{total}")
    print(f"  Min delta:       {sorted_d[0]:.3f} ms")
    print(f"  Max delta:       {sorted_d[-1]:.3f} ms")
    print(f"  Mean delta:      {sum(deltas) / total:.3f} ms")
    print(f"  Median delta:    {sorted_d[total // 2]:.3f} ms")
    print("-" * 60)

    # Delta histogram
    print("  Delta distribution:")
    brackets = [
        (0, 2, "0-2ms   "),
        (2, 5, "2-5ms   "),
        (5, 10, "5-10ms  "),
        (10, 20, "10-20ms "),
        (20, 40, "20-40ms "),
        (40, float("inf"), ">40ms   "),
    ]
    for lo, hi, label in brackets:
        count = sum(1 for d in deltas if lo <= d < hi)
        bar = "#" * count
        print(f"    {label}: {count:3d}  {bar}")

    print("=" * 60)

    # Verdict
    if synced == total:
        print("  PASS: all frames matched within threshold")
    elif synced / total >= 0.9:
        print(f"  WARN: {total - synced} frame(s) desynced")
    else:
        print(f"  FAIL: {total - synced}/{total} frames desynced")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Headless dual camera ring buffer sync test"
    )
    parser.add_argument(
        "--devices", default="auto",
        help="Comma-separated device paths or 'auto' (default: auto)",
    )
    parser.add_argument(
        "--captures", type=int, default=20,
        help="Number of captures to perform (default: 20)",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Seconds between captures (default: 2.0)",
    )
    parser.add_argument(
        "--save-dir", default="/tmp/sync_test",
        help="Directory to save captured images (default: /tmp/sync_test)",
    )
    parser.add_argument(
        "--framerate", default="55/2",
        help="GStreamer framerate fraction (default: 55/2 = 27Hz)",
    )
    args = parser.parse_args()

    # Early device detection (needs Gst.init)
    if args.devices == "auto":
        Gst.init(None)
        devices = find_uvc_cameras()
        if len(devices) < 2:
            print(f"Need 2 cameras, found {len(devices)}: {devices}")
            sys.exit(1)
        print(f"Detected cameras: {devices}")
    else:
        devices = [d.strip() for d in args.devices.split(",")]

    run_test(devices, args.captures, args.interval, args.save_dir, args.framerate)
