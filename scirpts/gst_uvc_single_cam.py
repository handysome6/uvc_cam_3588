#!/usr/bin/env python3
"""Interactive single-camera launcher for RK3588 UVC MJPEG preview.

Uses Rockchip MPP hardware JPEG decoder (mppjpegdec) and xvimagesink
for display.  The pipeline:

  v4l2src -> jpegparse -> mppjpegdec (HW decode + resize) -> xvimagesink
"""

import argparse
import fcntl
import re
import signal
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Sequence

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

# ---------------------------------------------------------------------------
# Preview resolution — mppjpegdec does HW downscale in the decoder itself
# ---------------------------------------------------------------------------
PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720


@dataclass(frozen=True)
class CameraDevice:
    path: str
    label: str


@dataclass(frozen=True)
class CaptureMode:
    width: int
    height: int
    fps: Fraction

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def gst_framerate(self) -> str:
        limited = self.fps.limit_denominator(1000)
        return f"{limited.numerator}/{limited.denominator}"

    @property
    def display_fps(self) -> str:
        return f"{float(self.fps):.3f}"


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Missing required command: {name}")


def run_checked(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stderr.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        raise SystemExit(exc.returncode or 1)

    return completed.stdout


# V4L2 capability check
_VIDIOC_QUERYCAP = 0x80685600
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _is_capture_device(dev_path: str) -> bool:
    """Return True if the device supports V4L2 video capture."""
    try:
        with open(dev_path, "rb") as f:
            buf = b"\x00" * 104
            info = fcntl.ioctl(f, _VIDIOC_QUERYCAP, buf)
        device_caps = struct.unpack_from("I", info, 88)[0]
        return bool(device_caps & _V4L2_CAP_VIDEO_CAPTURE)
    except Exception:
        return False


def scan_camera_devices() -> List[CameraDevice]:
    output = run_checked(["v4l2-ctl", "--list-devices"])
    devices: List[CameraDevice] = []
    current_label = ""
    seen = set()

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        if not raw_line.startswith((" ", "\t")):
            current_label = stripped.rstrip(":")
            continue

        if re.fullmatch(r"/dev/video\d+", stripped) and stripped not in seen and _is_capture_device(stripped):
            devices.append(CameraDevice(path=stripped, label=current_label or "Unknown device"))
            seen.add(stripped)

    return devices


def select_camera_device(devices: Sequence[CameraDevice]) -> str:
    if not devices:
        fallback = Path("/dev/video0")
        if fallback.exists():
            print("No camera devices were listed by v4l2-ctl, falling back to /dev/video0.", file=sys.stderr)
            return str(fallback)
        raise SystemExit("No V4L2 camera devices found.")

    print("Available camera devices:")
    for index, device in enumerate(devices, start=1):
        print(f"  [{index}] {device.path} ({device.label})")

    if not sys.stdin.isatty():
        raise SystemExit("Interactive device selection requires a terminal. Pass a device path explicitly.")

    prompt = f"Select a device to stream [1-{len(devices)}] (default 1): "
    while True:
        selection = input(prompt).strip()
        if not selection:
            return devices[0].path
        if selection.isdigit():
            selected_index = int(selection)
            if 1 <= selected_index <= len(devices):
                return devices[selected_index - 1].path
        print(f"Invalid selection. Please enter a number between 1 and {len(devices)}.", file=sys.stderr)


def parse_fps(line: str) -> Optional[Fraction]:
    fps_match = re.search(r"\(([0-9.]+)\s+fps\)", line)
    if fps_match:
        return Fraction(fps_match.group(1))

    seconds_match = re.search(r"Interval:\s+\w+\s+([0-9.]+)s", line)
    if not seconds_match:
        return None

    seconds = Fraction(seconds_match.group(1))
    if seconds <= 0:
        return None

    return Fraction(1, 1) / seconds


def detect_best_mjpeg_mode(device: str) -> CaptureMode:
    output = run_checked(["v4l2-ctl", "-d", device, "--list-formats-ext"])
    in_mjpeg_section = False
    current_size = None
    best_mode: Optional[CaptureMode] = None

    for line in output.splitlines():
        if re.search(r"\[\d+\]:", line):
            in_mjpeg_section = "'MJPG'" in line or "'JPEG'" in line
            current_size = None
            continue

        if not in_mjpeg_section:
            continue

        discrete_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if discrete_match:
            current_size = (int(discrete_match.group(1)), int(discrete_match.group(2)))
            continue

        stepwise_match = re.search(r"Size:\s+Stepwise\s+\d+x\d+\s*-\s*(\d+)x(\d+)", line)
        if stepwise_match:
            current_size = (int(stepwise_match.group(1)), int(stepwise_match.group(2)))
            continue

        if current_size is None or "Interval:" not in line:
            continue

        fps = parse_fps(line)
        if fps is None:
            continue

        candidate = CaptureMode(width=current_size[0], height=current_size[1], fps=fps)
        if best_mode is None:
            best_mode = candidate
            continue

        if candidate.area > best_mode.area or (candidate.area == best_mode.area and candidate.fps > best_mode.fps):
            best_mode = candidate

    if best_mode is None:
        raise SystemExit(f"No MJPEG mode found for {device}")

    return best_mode


def validate_jpeg(
    data,
    check_soi: bool = True,
    check_eoi: bool = True,
    check_walk: bool = True,
) -> bool:
    """Validate JPEG structural integrity.

    Checks are independently toggleable:
      check_soi  -- verify SOI (0xFFD8) at start
      check_eoi  -- verify EOI (0xFFD9) at end
      check_walk -- walk marker segments from SOI to SOS, verifying each
                   marker type and segment length

    Does NOT validate entropy-coded scan data -- corruption there produces
    visual artifacts but will not crash jpegparse.
    """
    size = len(data)
    if size < 4:
        return False

    if check_soi:
        if data[0] != 0xFF or data[1] != 0xD8:
            return False

    if check_eoi:
        if data[size - 2] != 0xFF or data[size - 1] != 0xD9:
            return False

    if check_walk:
        offset = 2
        while offset < size - 1:
            if data[offset] != 0xFF:
                return False

            while offset < size - 1 and data[offset + 1] == 0xFF:
                offset += 1
            if offset + 1 >= size:
                return False

            marker = data[offset + 1]
            offset += 2

            if marker == 0xD9:
                return True

            if marker == 0xDA:
                return True

            if (0xD0 <= marker <= 0xD7) or marker == 0x01:
                continue

            if marker == 0xD8:
                return False

            if marker == 0x00:
                return False

            if offset + 2 > size:
                return False
            seg_len = (data[offset] << 8) | data[offset + 1]
            if seg_len < 2:
                return False

            offset += seg_len
            if offset > size:
                return False

        return False

    return True


def _make_jpeg_probe(
    check_soi: bool, check_eoi: bool, check_walk: bool
):
    """Create a pad probe callback with the given validation settings."""
    drop_count = 0

    def probe(pad, info):
        nonlocal drop_count

        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.DROP

        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.DROP

        try:
            if validate_jpeg(map_info.data, check_soi, check_eoi, check_walk):
                return Gst.PadProbeReturn.OK
            drop_count += 1
            print(
                f"\rDropped corrupted JPEG frame #{drop_count} "
                f"({buf.get_size()} bytes)",
                end="",
                flush=True,
            )
            return Gst.PadProbeReturn.DROP
        finally:
            buf.unmap(map_info)

    return probe


# ---------------------------------------------------------------------------
# RK3588 GStreamer pipeline
# ---------------------------------------------------------------------------

def build_pipeline_desc(device: str, mode: CaptureMode, sink: str) -> str:
    """Build a GStreamer pipeline string for RK3588.

    mppjpegdec has built-in width/height properties that perform HW-accelerated
    downscale inside the decoder, eliminating the need for a separate videoscale
    or nvvidconv element.
    """
    return (
        f"v4l2src device={device} io-mode=mmap "
        f"! image/jpeg,width={mode.width},height={mode.height},"
        f"framerate={mode.gst_framerate} "
        f"! jpegparse name=parser "
        f"! mppjpegdec width={PREVIEW_WIDTH} height={PREVIEW_HEIGHT} format=NV12 "
        f"! {sink} sync=false"
    )


def run_preview_pipeline(
    device: str,
    mode: CaptureMode,
    sink: str = "xvimagesink",
    check_soi: bool = True,
    check_eoi: bool = True,
    check_walk: bool = True,
) -> int:
    """Build and run a GStreamer preview pipeline with JPEG validation."""
    Gst.init(None)

    pipeline_desc = build_pipeline_desc(device, mode, sink)

    any_checks = check_soi or check_eoi or check_walk
    enabled = []
    if check_soi:
        enabled.append("soi")
    if check_eoi:
        enabled.append("eoi")
    if check_walk:
        enabled.append("walk")
    print(
        f"Validation: {', '.join(enabled) if enabled else 'OFF'}",
        flush=True,
    )
    print(f"Pipeline: {pipeline_desc}", flush=True)
    pipeline = Gst.parse_launch(pipeline_desc)

    if any_checks:
        parser = pipeline.get_by_name("parser")
        sink_pad = parser.get_static_pad("sink")
        probe_fn = _make_jpeg_probe(check_soi, check_eoi, check_walk)
        sink_pad.add_probe(Gst.PadProbeType.BUFFER, probe_fn)

    loop = GLib.MainLoop()
    exit_code = 0

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(_bus, message):
        nonlocal exit_code
        if message.type == Gst.MessageType.EOS:
            print("\nEnd of stream", flush=True)
            loop.quit()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"\nERROR: {err.message}", flush=True)
            if debug:
                print(f"Debug: {debug}", flush=True)
            exit_code = 1
            loop.quit()

    bus.connect("message", on_bus_message)

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, loop.quit)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    return exit_code


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select a V4L2 camera, detect its best MJPEG mode, and launch an RK3588 preview pipeline.",
    )
    parser.add_argument(
        "device",
        nargs="?",
        help="V4L2 device path to use directly, for example /dev/video0.",
    )
    parser.add_argument(
        "--sink",
        default="xvimagesink",
        help="GStreamer video sink element (default: xvimagesink).",
    )
    parser.add_argument(
        "--no-check-soi",
        action="store_true",
        help="Disable SOI (0xFFD8) check at frame start.",
    )
    parser.add_argument(
        "--no-check-eoi",
        action="store_true",
        help="Disable EOI (0xFFD9) check at frame end.",
    )
    parser.add_argument(
        "--no-check-walk",
        action="store_true",
        default=True,
        help="Disable marker segment walk (disabled by default; use --check-walk to enable).",
    )
    parser.add_argument(
        "--check-walk",
        action="store_true",
        help="Enable marker segment walk (header structure validation).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv[1:])

    require_command("v4l2-ctl")

    if args.device:
        device = args.device
    else:
        device = select_camera_device(scan_camera_devices())

    if not Path(device).exists():
        raise SystemExit(f"V4L2 device not found: {device}")

    mode = detect_best_mjpeg_mode(device)

    print(f"Using device: {device}", flush=True)
    print(
        f"Detected MJPEG mode: {mode.width}x{mode.height} "
        f"@ {mode.gst_framerate} fps ({mode.display_fps} fps)",
        flush=True,
    )

    return run_preview_pipeline(
        device,
        mode,
        sink=args.sink,
        check_soi=not args.no_check_soi,
        check_eoi=not args.no_check_eoi,
        check_walk=args.check_walk,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(130)
