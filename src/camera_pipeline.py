"""
CameraPipeline — GStreamer pipeline for one UVC MJPEG camera on RK3588.

Pipeline topology (tee-split):

    v4l2src (MJPEG 5120x3840 @ 27.5 fps, io-mode=mmap)
      -> tee
         ├─ Preview branch: jpegparse -> mppjpegdec (HW decode + resize) -> preview sink
         └─ Capture branch: queue(leaky) -> appsink (raw MJPEG, latest only)

Two preview modes controlled by `use_overlay`:
  True  — VideoOverlay: GStreamer renders directly into a Qt widget window handle
  False — appsink fallback: decoded RGB frames are emitted as QImage via signal

Platform selection:
  RK3588  — mppjpegdec + xvimagesink (HW accelerated, built-in resize)
  Dev     — jpegdec + videoconvert + autovideosink (software, test source)
"""

import fcntl
import glob
import os
import struct
import threading

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo
from loguru import logger

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage


# ---------------------------------------------------------------------------
# UVC device auto-detection
# ---------------------------------------------------------------------------

# v4l2 ioctl constants (from <linux/videodev2.h>)
_VIDIOC_QUERYCAP = 0x80685600
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _is_capture_device(dev_path: str) -> bool:
    """Return True if the device supports V4L2 video capture (VIDIOC_QUERYCAP)."""
    try:
        with open(dev_path, "rb") as f:
            buf = b"\x00" * 104  # sizeof(struct v4l2_capability)
            info = fcntl.ioctl(f, _VIDIOC_QUERYCAP, buf)
        device_caps = struct.unpack_from("I", info, 88)[0]  # .device_caps field
        return bool(device_caps & _V4L2_CAP_VIDEO_CAPTURE)
    except Exception:
        return False


def find_uvc_cameras() -> list[str]:
    """
    Scan sysfs to find all USB UVC capture devices.

    Criteria:
      - sysfs path resolves through a 'usb' bus (i.e. it is a USB device)
      - VIDIOC_QUERYCAP reports V4L2_CAP_VIDEO_CAPTURE

    Returns a list of /dev/videoX paths ordered by device number.
    """
    found: list[str] = []
    candidates = sorted(glob.glob("/sys/class/video4linux/video*"))
    for video_dir in candidates:
        try:
            real = os.path.realpath(video_dir)
        except OSError:
            continue
        if "usb" not in real.lower():
            continue
        dev = "/dev/" + os.path.basename(video_dir)
        logger.debug("Probing {} (sysfs: {})", dev, real)
        if _is_capture_device(dev):
            logger.info("UVC camera found: {}", dev)
            found.append(dev)
    if not found:
        logger.warning("No USB UVC capture device found in /sys/class/video4linux/")
    return found


def find_uvc_camera() -> str | None:
    """Return the first USB UVC capture device, or None."""
    devices = find_uvc_cameras()
    return devices[0] if devices else None


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_rk3588() -> bool:
    """
    Return True if running on RK3588 with Rockchip MPP HW-accelerated GStreamer elements.

    Detection: check for mppjpegdec element factory — the specific element we
    use for HW JPEG decode + resize.
    """
    result = Gst.ElementFactory.find("mppjpegdec") is not None
    logger.info("Platform: {}", "RK3588 (HW accelerated)" if result else "Dev machine (SW path)")
    return result


# ---------------------------------------------------------------------------
# JPEG validation probe — drop corrupted frames before jpegparse
# ---------------------------------------------------------------------------

def _validate_jpeg(data) -> bool:
    """Check SOI (0xFFD8) at start and EOI (0xFFD9) at end."""
    size = len(data)
    if size < 4:
        return False
    if data[0] != 0xFF or data[1] != 0xD8:
        return False
    if data[size - 2] != 0xFF or data[size - 1] != 0xD9:
        return False
    return True


def _make_jpeg_probe(device: str):
    """Create a pad probe callback that drops invalid JPEG buffers."""
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
            if _validate_jpeg(map_info.data):
                return Gst.PadProbeReturn.OK
            drop_count += 1
            logger.debug(
                "Dropped corrupted JPEG #{} ({} bytes) on {}",
                drop_count, buf.get_size(), device,
            )
            return Gst.PadProbeReturn.DROP
        finally:
            buf.unmap(map_info)

    return probe


# ---------------------------------------------------------------------------
# File-write worker (runs on QThreadPool, never on the UI thread)
# ---------------------------------------------------------------------------

class _FrameWriter(QRunnable):
    """Writes raw GstBuffer bytes to a .jpg file on a worker thread."""

    def __init__(self, sample: Gst.Sample, path: str):
        super().__init__()
        self._sample = sample
        self._path = path

    def run(self):
        buf = self._sample.get_buffer()
        result, map_info = buf.map(Gst.MapFlags.READ)
        if not result:
            logger.error("Failed to map GstBuffer for capture write: {}", self._path)
            return
        try:
            with open(self._path, "wb") as f:
                f.write(bytes(map_info.data))
            logger.success("Frame saved → {} ({:.1f} KB)", self._path, len(map_info.data) / 1024)
        except OSError as exc:
            logger.error("Failed to write capture file {}: {}", self._path, exc)
        finally:
            buf.unmap(map_info)


# ---------------------------------------------------------------------------
# CameraPipeline
# ---------------------------------------------------------------------------

class CameraPipeline(QObject):
    """
    Manages one GStreamer camera pipeline.

    Signals:
        pipeline_error(str)   — emitted on GStreamer ERROR bus message
        pipeline_eos()        — emitted on EOS
        preview_frame(QImage) — emitted per frame in appsink fallback mode
    """

    pipeline_error = Signal(str)
    pipeline_eos = Signal()
    preview_frame = Signal(QImage)

    # Preview dimensions for both modes
    PREVIEW_W = 1280
    PREVIEW_H = 720

    def __init__(
        self,
        device: str = "/dev/video0",
        use_overlay: bool = True,
        parent: QObject = None,
    ):
        super().__init__(parent)
        self._device = device
        self._use_overlay = use_overlay
        self._on_rk3588 = is_rk3588()

        # Runtime state
        self._pipeline: Gst.Pipeline | None = None
        self._preview_sink: Gst.Element | None = None
        self._capture_sink: Gst.Element | None = None
        self._window_handle: int | None = None

        # Latest MJPEG sample cache — updated on GStreamer streaming thread
        self._latest_sample: Gst.Sample | None = None
        self._sample_lock = threading.Lock()

        # State: "stopped" | "playing" | "error"
        self._state = "stopped"
        self._error_message: str | None = None

        # Qt timer polls the GStreamer bus so we never run a GLib main loop
        self._bus_timer = QTimer(self)
        self._bus_timer.setInterval(50)  # 50 ms ≈ 20 polls/s
        self._bus_timer.timeout.connect(self._poll_bus)

    # ------------------------------------------------------------------
    # Pipeline string construction
    # ------------------------------------------------------------------

    def _build_pipeline_string(self) -> str:
        W, H = self.PREVIEW_W, self.PREVIEW_H

        if self._on_rk3588:
            # RK3588: v4l2src with mmap, mppjpegdec does HW decode + resize.
            # Framerate must be explicit — RK3588 v4l2src won't auto-negotiate.
            src = (
                f"v4l2src device={self._device} io-mode=mmap ! "
                "image/jpeg,width=5120,height=3840,framerate=55/2 ! "
                "tee name=t "
            )
            capture_branch = (
                "t. ! queue leaky=downstream max-size-buffers=1 ! "
                "appsink name=capture_sink drop=true max-buffers=1 emit-signals=true"
            )
            if self._use_overlay:
                # HW decode + resize -> VideoOverlay sink (xvimagesink)
                preview_branch = (
                    f"t. ! queue ! jpegparse name=parser ! "
                    f"mppjpegdec width={W} height={H} format=NV12 ! "
                    "xvimagesink name=preview_sink sync=false "
                )
            else:
                # HW decode + resize -> RGB -> appsink (CPU copy for QImage)
                preview_branch = (
                    f"t. ! queue ! jpegparse name=parser ! "
                    f"mppjpegdec width={W} height={H} ! "
                    f"videoconvert ! video/x-raw,format=RGB,width={W},height={H} ! "
                    "appsink name=preview_sink drop=true max-buffers=1 "
                    "emit-signals=true sync=false "
                )
        else:
            # Dev machine: software decode with a test source
            src = (
                "videotestsrc is-live=true pattern=ball ! "
                f"video/x-raw,width={W},height={H},framerate=27/1 ! "
                "jpegenc ! image/jpeg ! "
                "tee name=t "
            )
            capture_branch = (
                "t. ! queue leaky=downstream max-size-buffers=1 ! "
                "appsink name=capture_sink drop=true max-buffers=1 emit-signals=true"
            )
            if self._use_overlay:
                preview_branch = (
                    f"t. ! queue ! jpegdec ! videoconvert ! "
                    f"video/x-raw,width={W},height={H} ! "
                    "autovideosink name=preview_sink sync=false "
                )
            else:
                preview_branch = (
                    f"t. ! queue ! jpegdec ! videoconvert ! "
                    f"video/x-raw,format=RGB,width={W},height={H} ! "
                    "appsink name=preview_sink drop=true max-buffers=1 "
                    "emit-signals=true sync=false "
                )

        pipeline_str = src + preview_branch + capture_branch
        logger.debug("Pipeline string: {}", pipeline_str)
        return pipeline_str

    # ------------------------------------------------------------------
    # Lifecycle: start / stop
    # ------------------------------------------------------------------

    def start(self, window_handle: int | None = None) -> bool:
        """
        Build and start the pipeline.

        Args:
            window_handle: Native window ID (winId()) of the preview widget.
                           Required when use_overlay=True.
        Returns:
            True if the pipeline reached PLAYING state (or ASYNC).
        """
        self._window_handle = window_handle
        logger.info(
            "Starting pipeline | device={} overlay={} rk3588={}",
            self._device, self._use_overlay, self._on_rk3588,
        )

        pipeline_str = self._build_pipeline_string()
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except Exception as exc:
            self._state = "error"
            self._error_message = str(exc)
            logger.error("Pipeline parse failed: {}", exc)
            self.pipeline_error.emit(self._error_message)
            return False

        if self._pipeline is None:
            self._state = "error"
            self._error_message = "Gst.parse_launch returned None"
            logger.error(self._error_message)
            self.pipeline_error.emit(self._error_message)
            return False

        # Named element references
        self._capture_sink = self._pipeline.get_by_name("capture_sink")
        self._preview_sink = self._pipeline.get_by_name("preview_sink")

        if self._capture_sink is None:
            self._state = "error"
            self._error_message = "capture_sink element not found in pipeline"
            logger.error(self._error_message)
            self.pipeline_error.emit(self._error_message)
            return False

        # Attach JPEG validation probe on jpegparse sink pad (RK3588 only)
        if self._on_rk3588:
            parser = self._pipeline.get_by_name("parser")
            if parser is not None:
                sink_pad = parser.get_static_pad("sink")
                sink_pad.add_probe(
                    Gst.PadProbeType.BUFFER, _make_jpeg_probe(self._device)
                )
                logger.info("JPEG validation probe attached | device={}", self._device)

        # Connect capture appsink new-sample
        self._capture_sink.connect("new-sample", self._on_new_capture_sample)

        # Connect preview appsink new-sample in fallback mode
        if not self._use_overlay and self._preview_sink is not None:
            self._preview_sink.connect("new-sample", self._on_new_preview_sample)

        # Bus setup
        bus = self._pipeline.get_bus()
        if self._use_overlay:
            # sync-message needed so we can call set_window_handle()
            # before the first frame is rendered
            bus.enable_sync_message_emission()
            bus.connect("sync-message::element", self._on_sync_message)

        # Start pipeline
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            # Pull the actual error from the bus
            bus_msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if bus_msg:
                err, debug = bus_msg.parse_error()
                self._error_message = f"{err.message} | {debug}"
            else:
                self._error_message = "Failed to set pipeline to PLAYING"
            self._state = "error"
            logger.error("Pipeline start failed: {}", self._error_message)
            self.pipeline_error.emit(self._error_message)
            return False

        self._state = "playing"
        self._bus_timer.start()

        logger.success("Pipeline playing | device={} overlay={}", self._device, self._use_overlay)
        return True

    def stop(self):
        """Stop the pipeline and release all resources.

        Blocks until:
          - GStreamer reaches NULL state (up to 3 s)
          - Any in-flight QThreadPool file-write tasks finish (up to 2 s)
        Safe to call multiple times or from a signal handler.
        """
        if self._state == "stopped" and self._pipeline is None:
            return
        logger.info("Stopping pipeline | device={}", self._device)
        self._bus_timer.stop()
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline.get_state(3 * Gst.SECOND)
            self._pipeline = None
        self._preview_sink = None
        self._capture_sink = None
        with self._sample_lock:
            self._latest_sample = None
        self._state = "stopped"
        QThreadPool.globalInstance().waitForDone(2000)
        logger.info("Pipeline stopped")

    # ------------------------------------------------------------------
    # Capture appsink callback — GStreamer streaming thread
    # ------------------------------------------------------------------

    def _on_new_capture_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """Cache latest MJPEG sample; called on GStreamer streaming thread."""
        sample = appsink.emit("pull-sample")
        if sample is not None:
            with self._sample_lock:
                self._latest_sample = sample
        return Gst.FlowReturn.OK

    # ------------------------------------------------------------------
    # Preview appsink callback — appsink fallback mode
    # ------------------------------------------------------------------

    def _on_new_preview_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """Pull decoded RGB frame, convert to QImage, emit signal."""
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")

        buf = sample.get_buffer()
        result, map_info = buf.map(Gst.MapFlags.READ)
        if not result:
            return Gst.FlowReturn.OK
        try:
            # Make a copy so the QImage owns the bytes after buf.unmap()
            image = QImage(
                bytes(map_info.data),
                width,
                height,
                width * 3,
                QImage.Format.Format_RGB888,
            ).copy()
        finally:
            buf.unmap(map_info)

        self.preview_frame.emit(image)
        return Gst.FlowReturn.OK

    # ------------------------------------------------------------------
    # VideoOverlay sync-message handler
    # ------------------------------------------------------------------

    def _on_sync_message(self, bus: Gst.Bus, message: Gst.Message):
        """Set the native window handle the moment GStreamer asks for it."""
        structure = message.get_structure()
        if structure is None:
            return
        if structure.get_name() == "prepare-window-handle":
            if self._window_handle is not None:
                logger.info("VideoOverlay: setting window handle 0x{:x}", self._window_handle)
                GstVideo.VideoOverlay.set_window_handle(message.src, self._window_handle)
            else:
                logger.warning("VideoOverlay: prepare-window-handle received but no window handle set")

    # ------------------------------------------------------------------
    # GStreamer bus polling — Qt main thread
    # ------------------------------------------------------------------

    @Slot()
    def _poll_bus(self):
        """Poll the GStreamer bus for error / EOS messages (no GLib main loop)."""
        if self._pipeline is None:
            return
        bus = self._pipeline.get_bus()
        while True:
            msg = bus.pop()
            if msg is None:
                break
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self._state = "error"
                self._error_message = str(err)
                logger.error("GStreamer error: {} | debug: {}", err, debug)
                self.pipeline_error.emit(self._error_message)
                self._bus_timer.stop()
            elif msg.type == Gst.MessageType.EOS:
                self._state = "stopped"
                logger.warning("GStreamer EOS — stream ended")
                self.pipeline_eos.emit()
                self._bus_timer.stop()

    # ------------------------------------------------------------------
    # VideoOverlay expose on resize
    # ------------------------------------------------------------------

    def expose(self):
        """
        Call when the preview widget is resized (VideoOverlay mode only).
        Tells xvimagesink to repaint to the new widget dimensions.
        """
        if self._use_overlay and self._preview_sink is not None:
            try:
                self._preview_sink.expose()
            except Exception:
                pass  # not all sinks implement expose; safe to ignore

    def set_window_handle(self, window_handle: int | None):
        """
        Dynamically change the window handle for VideoOverlay rendering.
        Note: xvimagesink does not reliably support this while running;
        stop the pipeline first, then restart with the new handle.
        """
        if self._use_overlay and self._preview_sink is not None and window_handle is not None:
            try:
                self._preview_sink.set_window_handle(window_handle)
                self._window_handle = window_handle
                self._preview_sink.expose()
            except Exception as e:
                logger.warning("Failed to set window handle: {}", e)

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    def capture_to_file(self, path: str) -> bool:
        """
        Schedule a write of the latest cached MJPEG frame to `path`.

        The write happens on QThreadPool (never blocks the UI thread).
        Returns True if a cached sample was available, False otherwise.
        """
        with self._sample_lock:
            sample = self._latest_sample

        if sample is None:
            logger.warning("capture_to_file: no cached sample available yet")
            return False

        logger.info("Capture queued → {}", path)
        QThreadPool.globalInstance().start(_FrameWriter(sample, path))
        return True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """One of: 'stopped', 'playing', 'error'."""
        return self._state

    @property
    def error_message(self) -> str | None:
        return self._error_message

    @property
    def use_overlay(self) -> bool:
        return self._use_overlay
