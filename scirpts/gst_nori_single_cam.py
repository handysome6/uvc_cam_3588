#!/usr/bin/env python3
"""Single-camera launcher using Nori SDK source (norisrc) on RK3588.

Uses the Nori Xvision camera SDK via the ``norisrc`` GStreamer element
instead of the kernel v4l2/UVC driver.  The downstream decode pipeline
is identical to the v4l2src variant:

  norisrc -> jpegparse -> mppjpegdec (HW decode + resize) -> xvimagesink
"""

import argparse
import sys
from fractions import Fraction
from typing import Optional, Sequence

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from gst_common import PREVIEW_WIDTH, PREVIEW_HEIGHT, run_preview  # noqa: E402

# ---------------------------------------------------------------------------
# Default capture mode — Nori Xvision 20MP sensor
# ---------------------------------------------------------------------------
DEFAULT_WIDTH = 5120
DEFAULT_HEIGHT = 3840
# NOTE: norisrc get_caps truncates the SDK's float fps to int, so 27.5 fps
# becomes 27/1 in GstCaps.  We omit framerate from the caps filter by default
# and let GStreamer negotiate from the element's advertised modes.


def parse_fps(value: str) -> Fraction:
    """Parse '27', '27.5', or '55/2' into a Fraction."""
    if "/" in value:
        num, den = value.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(value).limit_denominator(1000)


def build_pipeline_desc(
    device_index: int,
    width: int,
    height: int,
    fps: Optional[Fraction],
    sink: str,
    trigger_mode: Optional[str] = None,
    sensor_shutter: Optional[int] = None,
    sensor_gain: Optional[int] = None,
) -> str:
    """Build a GStreamer pipeline string for norisrc on RK3588."""
    # Source element with optional SDK / camera-control properties
    src_parts = [f"norisrc device-index={device_index}"]
    if trigger_mode is not None:
        src_parts.append(f"trigger-mode={trigger_mode}")
    if sensor_shutter is not None:
        src_parts.append(f"sensor-shutter={sensor_shutter}")
    if sensor_gain is not None:
        src_parts.append(f"sensor-gain={sensor_gain}")
    src = " ".join(src_parts)

    # Caps filter — framerate included only when explicitly requested
    caps = f"image/jpeg,width={width},height={height}"
    if fps is not None:
        limited = fps.limit_denominator(1000)
        caps += f",framerate={limited.numerator}/{limited.denominator}"

    return (
        f"{src} "
        f"! {caps} "
        f"! jpegparse name=parser "
        f"! mppjpegdec width={PREVIEW_WIDTH} height={PREVIEW_HEIGHT} format=NV12 "
        f"! {sink} sync=false"
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a Nori SDK camera preview pipeline on RK3588.",
    )
    parser.add_argument(
        "--device-index",
        type=int,
        default=0,
        help="Nori camera index (default: 0).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"Capture width (default: {DEFAULT_WIDTH}).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_HEIGHT,
        help=f"Capture height (default: {DEFAULT_HEIGHT}).",
    )
    parser.add_argument(
        "--fps",
        type=str,
        default=None,
        help="Framerate as integer, decimal, or fraction e.g. '30', '27.5', '55/2'. "
        "Omitted by default (auto-negotiated from SDK).",
    )
    parser.add_argument(
        "--trigger-mode",
        choices=["none", "software", "hardware", "command"],
        default=None,
        help="Camera trigger mode (default: free-running).",
    )
    parser.add_argument(
        "--sensor-shutter",
        type=int,
        default=None,
        help="Sensor shutter / exposure time in microseconds.",
    )
    parser.add_argument(
        "--sensor-gain",
        type=int,
        default=None,
        help="Sensor analogue gain multiplier.",
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

    Gst.init(None)

    if not Gst.ElementFactory.find("norisrc"):
        raise SystemExit(
            "GStreamer element 'norisrc' not found. Is gst-nori installed?"
        )

    fps = parse_fps(args.fps) if args.fps else None

    print(f"Device index: {args.device_index}", flush=True)
    fps_display = f" @ {float(fps):.1f} fps" if fps else " (fps auto)"
    print(
        f"Capture mode: {args.width}x{args.height}{fps_display}",
        flush=True,
    )
    if args.trigger_mode:
        print(f"Trigger mode: {args.trigger_mode}", flush=True)

    pipeline_desc = build_pipeline_desc(
        device_index=args.device_index,
        width=args.width,
        height=args.height,
        fps=fps,
        sink=args.sink,
        trigger_mode=args.trigger_mode,
        sensor_shutter=args.sensor_shutter,
        sensor_gain=args.sensor_gain,
    )

    return run_preview(
        pipeline_desc,
        check_soi=not args.no_check_soi,
        check_eoi=not args.no_check_eoi,
        check_walk=args.check_walk,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(130)
