"""
Entry point for the UVC dual-camera preview/capture application on RK3588.

Usage:
    python main.py [--devices auto|/dev/videoX,/dev/videoY] [--no-overlay]

Flags:
    --devices PATHS    Comma-separated camera device paths, or 'auto' (default: auto)
    --no-overlay       Force appsink-to-QImage fallback (skips VideoOverlay)
"""

import os
import signal
import sys
import argparse

# Force Qt to use X11 (xcb) backend — xvimagesink requires X11 window IDs,
# which are unavailable under the native Wayland Qt backend.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst
from loguru import logger

from PySide6.QtWidgets import QApplication

from camera_pipeline import find_uvc_cameras, is_rk3588
from dual_camera_manager import DualCameraManager
from main_window import MainWindow


def _setup_signals(app: QApplication) -> None:
    """Route SIGINT/SIGTERM through Qt's event loop so closeEvent runs cleanly."""
    def _handler(signum, _frame):
        logger.info("Signal {} received — quitting", signal.Signals(signum).name)
        app.quit()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main():
    Gst.init(None)
    logger.info("GStreamer initialized")

    parser = argparse.ArgumentParser(description="UVC dual-camera preview/capture (RK3588)")
    parser.add_argument(
        "--devices",
        default="auto",
        help="Comma-separated camera device paths, or 'auto' to detect (default: auto)",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Force appsink-to-QImage preview instead of VideoOverlay",
    )
    args = parser.parse_args()

    # Resolve device paths
    if args.devices == "auto":
        devices = find_uvc_cameras()
        if not devices:
            logger.error(
                "No UVC cameras found. Connect cameras or use --devices /dev/videoX,/dev/videoY"
            )
            sys.exit(1)
        logger.info("Auto-detected {} camera(s): {}", len(devices), devices)
    else:
        devices = [d.strip() for d in args.devices.split(",") if d.strip()]

    # VideoOverlay is the preferred path on RK3588; on dev machines fall back
    use_overlay = is_rk3588() and not args.no_overlay
    logger.info(
        "Config | devices={} preview={}",
        devices,
        "VideoOverlay" if use_overlay else "appsink fallback",
    )

    app = QApplication(sys.argv)
    _setup_signals(app)

    manager = DualCameraManager(devices=devices, use_overlay=use_overlay)

    logger.info("Launching MainWindow")
    window = MainWindow(manager=manager)

    # Safety net: also stop pipelines on any quit path that bypasses closeEvent
    app.aboutToQuit.connect(manager.stop)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
