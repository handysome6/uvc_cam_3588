"""Shared utilities for GStreamer single-camera test scripts on RK3588.

Common JPEG validation, pad probe factory, and preview pipeline runner
used by both the v4l2src and norisrc single-camera launchers.
"""

import signal

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

# ---------------------------------------------------------------------------
# Preview resolution — mppjpegdec does HW downscale in the decoder itself
# ---------------------------------------------------------------------------
PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720


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


def make_jpeg_probe(
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


def run_preview(
    pipeline_desc: str,
    check_soi: bool = True,
    check_eoi: bool = True,
    check_walk: bool = True,
) -> int:
    """Parse-launch a GStreamer pipeline and run until EOS or error.

    Expects Gst.init() to have been called already.  If the pipeline
    contains an element named ``parser``, a JPEG validation probe is
    attached to its sink pad according to the *check_** flags.

    Returns 0 on clean shutdown, 1 on error.
    """
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
        if parser:
            sink_pad = parser.get_static_pad("sink")
            probe_fn = make_jpeg_probe(check_soi, check_eoi, check_walk)
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
