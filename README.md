# UVC Dual Camera on Rockchip 3588(s)

Dual 20MP UVC camera preview and synchronized capture application for RK3588. Two hardware-triggered cameras stream MJPEG at 5120x3840, with 720p live previews and single-click full-resolution capture of matched frame pairs.

## Hardware Setup

- **SoC**: Rockchip RK3588 (aarch64, Linux 6.1.84)
- **Cameras**: 2x DECXIN Camera (20MP, 5120x3840, USB UVC, MJPEG)
- **Trigger**: External hardware trigger, shared TRG/GND pins (27 Hz)
- **USB**: Each camera on a separate USB host controller (`xhci-hcd.3.auto`, `xhci-hcd.11.auto`)

## Requirements

### System dependencies

```bash
sudo apt update && sudo apt install libxcb-cursor0
```

GStreamer 1.20+ with Rockchip MPP plugins (`mppjpegdec`) and PyGObject (`gi`) must be available system-wide.

### UV (recommended)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv, skip if already available
uv sync                                             # install Python dependencies
```

Set `include-system-site-packages = true` in `.venv/pyvenv.cfg` so the venv can access system-installed GStreamer bindings.

### Manual venv

```bash
sudo apt install python3-venv
python3 -m venv .venv
# Set include-system-site-packages = true in .venv/pyvenv.cfg
pip install loguru pyside6==6.8.0.2
```

## Usage

### Dual camera GUI

```bash
cd src
python main.py                                          # auto-detect cameras
python main.py --devices /dev/video0,/dev/video2        # explicit devices
python main.py --no-overlay                             # appsink fallback (no VideoOverlay)
python main.py --pts-filename                           # embed timestamps in filenames
```

GUI features:
- Dual 720p live preview with VideoOverlay (xvimagesink)
- Single capture or auto-capture (20 pairs at 2s intervals)
- Configurable save path and framerate presets (27 Hz / 10 Hz)
- Camera swap (left/right reassignment)
- Optional timestamp suffix in filenames for sync debugging

### Headless sync test

Run without a display to validate frame sync (useful over SSH):

```bash
python3 scirpts/headless_sync_test.py                   # auto-detect, 20 captures
python3 scirpts/headless_sync_test.py --captures 5      # quick test
python3 scirpts/headless_sync_test.py --framerate 10/1  # 10 Hz mode
```

### Single camera test

```bash
python3 scirpts/gst_uvc_single_cam.py                  # interactive device selection
python3 scirpts/gst_uvc_single_cam.py /dev/video0      # direct device
python3 scirpts/gst_uvc_single_cam.py --sink fakesink  # decode benchmark
```

### Image pair viewer

Compare captured A/D image pairs side-by-side with zoom, pan, and thumbnail navigation:

```bash
python3 src/image_compare.py /path/to/captures/
```

## Frame Sync

Both cameras are hardware-triggered simultaneously, but USB delivery skew causes frames to arrive 1-2 trigger pulses apart. The application solves this with:

1. **Ring buffer**: Each camera caches the 5 most recent frames (not just the latest)
2. **Pad probe timestamps**: A GStreamer pad probe on the tee's sink pad stamps `CLOCK_MONOTONIC` wall-clock time on v4l2src's streaming thread — before the tee fans out to preview/capture branches, eliminating decode contention jitter
3. **Minimum-delta matching**: On capture, both ring buffers are snapshot and the frame pair with the smallest timestamp delta is selected (~0.2ms for same-trigger frames)

See `docs/dual-camera-sync-investigation.md` for the full investigation and `docs/frame-sync-future-directions.md` for the evolution of the timestamp approach.

## Project Structure

```
src/
  main.py                  # Entry point, CLI args, GStreamer init
  camera_pipeline.py       # GStreamer pipeline per camera, pad probe, ring buffer
  dual_camera_manager.py   # Two-camera coordination, frame matching
  main_window.py           # PySide6 GUI
  image_compare.py         # Side-by-side capture viewer

scirpts/                   # (typo is intentional — matches existing paths)
  headless_sync_test.py    # Headless dual camera sync validation
  gst_uvc_single_cam.py   # Single camera GStreamer test

docs/
  dual-camera-sync-investigation.md   # Sync problem investigation and fix
  frame-sync-future-directions.md     # Timestamp approach evolution
  rk3588-gstreamer-pipeline.md        # GStreamer pipeline architecture
```
