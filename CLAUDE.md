# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PySide6 GUI application for dual 20MP UVC camera preview and capture on **Rockchip RK3588**. Two USB UVC cameras stream MJPEG at 5120x3840 @ 27.5 FPS via shared hardware trigger. The goal is dual 720p live previews with single-click full-resolution synchronized capture (saving raw MJPEG frames as .jpg without re-encoding).

## Reference Implementation

The sibling project `/home/cat/workspace/uvc_cam_jetson/src/` is the NVIDIA Jetson Orin reference. This project replicates its architecture for RK3588 hardware.

## Running the Application

```bash
cd src
python main.py                                          # auto-detect cameras
python main.py --devices /dev/video0,/dev/video2        # explicit devices
python main.py --no-overlay                             # appsink fallback (no VideoOverlay)
python main.py --pts-filename                           # embed probe timestamps in filenames
```

## Running Tests

```bash
# Headless dual camera sync test (no display needed)
# Note: folder name has a typo ("scirpts" not "scripts")
python3 scirpts/headless_sync_test.py                                  # auto-detect, 20 captures
python3 scirpts/headless_sync_test.py --captures 5 --interval 2.0     # quick test
python3 scirpts/headless_sync_test.py --devices /dev/video0,/dev/video2

# Single camera GStreamer test
python3 scirpts/gst_uvc_single_cam.py               # interactive device selection
python3 scirpts/gst_uvc_single_cam.py /dev/video0    # direct device
python3 scirpts/gst_uvc_single_cam.py --sink fakesink # decode benchmark (no display)
```

## Target Platform

- Rockchip RK3588 SoC (aarch64, Linux 6.1.84)
- GStreamer 1.20.3 with Rockchip MPP hardware-accelerated plugins
- X11 display server (Wayland not supported for video sink)
- UVC driver: `uvcvideo`, `clock=CLOCK_MONOTONIC`, `hwtimestamps=0`
- Two cameras on separate USB host controllers (`xhci-hcd.3.auto`, `xhci-hcd.11.auto`)

## RK3588 GStreamer Element Mapping

| Stage       | Jetson (NVIDIA)                 | RK3588 (Rockchip)               |
|-------------|--------------------------------|----------------------------------|
| JPEG decode | `nvv4l2decoder mjpeg=1`        | `mppjpegdec`                     |
| Resize      | `nvvidconv` + caps filter       | `mppjpegdec width=W height=H` (built-in)   |
| Color fmt   | `video/x-raw(memory:NVMM),NV12`| `video/x-raw,NV12`              |
| Display     | `nveglglessink`                | `xvimagesink`                    |

Key difference: `mppjpegdec` has built-in `width`/`height` properties that do HW-accelerated downscale inside the decoder itself — no separate resize element needed.

## Architecture

Each camera runs an independent GStreamer pipeline with a `tee` splitting into two branches:

```
v4l2src (MJPEG 5120x3840, io-mode=mmap)
  -> tee  --[pad probe stamps CLOCK_MONOTONIC here]
     ├─ Preview: queue(leaky) -> jpegparse (JPEG probe) -> mppjpegdec(1280x720) -> xvimagesink
     └─ Capture: queue(leaky) -> appsink (latest raw MJPEG frame only)
```

**Key classes (`src/`):**
- `CameraPipeline` — one per camera; builds/starts/stops the GStreamer pipeline, pad probe on tee sink pad for frame timestamps, ring buffer of 5 recent stamped samples, JPEG validation probe
- `DualCameraManager` — manages two `CameraPipeline` instances, coordinates synchronized capture by matching frames across cameras using minimum timestamp delta from ring buffers
- `MainWindow` — two preview panels (VideoOverlay via `xvimagesink`), capture/auto-capture/swap buttons, save path editor, PTS filename toggle, framerate presets
- `main.py` — entry point; GStreamer init, device auto-detection, platform-aware config
- `image_compare.py` — standalone side-by-side A/D image pair viewer with zoom, pan, and thumbnail navigation

**Test scripts (`scirpts/`):**
- `headless_sync_test.py` — headless dual camera sync test (no display/Qt needed), builds minimal v4l2src→queue→appsink pipelines with probe timestamps
- `gst_uvc_single_cam.py` — single camera GStreamer pipeline test

## Frame Sync Mechanism

Two hardware-triggered cameras deliver frames with 1-2 frame USB delivery skew. The sync mechanism:

1. **Pad probe** on tee's sink pad stamps `clock_gettime_ns(CLOCK_MONOTONIC)` on v4l2src's streaming thread (before tee fans out to branches). Stored in a side-channel dict keyed by `buffer.pts`.
2. **Appsink callback** looks up the probe timestamp by PTS and appends `StampedSample(timestamp_ns, sample)` to a ring buffer (depth=5).
3. **On capture**, both ring buffers are snapshot and the pair with minimum timestamp delta is selected (O(N²) with N=5).

**Critical**: Both preview and capture queues must be `leaky=downstream`. The preview queue being leaky prevents mppjpegdec backpressure from blocking v4l2src's thread, which would add jitter to probe timestamps. See `docs/dual-camera-sync-investigation.md` for the full investigation and `docs/frame-sync-future-directions.md` for the evolution of the timestamp approach.

## Critical Design Constraints

- **No re-encoding on capture**: save raw MJPEG buffer bytes directly as .jpg — never use `cv::imwrite()` or PIL
- **Both queues must be leaky**: preview queue `leaky=downstream max-size-buffers=2` (prevents backpressure jitter on probe timestamps), capture queue `leaky=downstream max-size-buffers=1` (only keep latest)
- **Always-cached latest frame**: continuously cache via appsink callback into ring buffer, match and write on button press
- **File writes on worker thread**: never block the UI thread for disk I/O (use QThreadPool)
- **No GLib main loop**: Qt event loop only; poll GStreamer bus via QTimer
- **JPEG validation before jpegparse**: pad probe drops frames with corrupt SOI/EOI markers — prevents fatal decoder errors at high bitrate
- **No dependency on GStreamer base_time or PTS computation**: frame matching uses pad-probe wall-clock timestamps, not `buffer.pts + pipeline.base_time` (which is fragile under pipeline restart, caps renegotiation, and leaky queues causing synthetic PTS)

## Platform-Specific Issues

- **`rkximagesink` is broken**: hardcodes `/dev/dri/card0` which is RKNPU on this board, not the display controller. Use `xvimagesink` instead.
- **`xvimagesink` supports `GstVideoOverlay`**: can embed in Qt widgets via `set_window_handle()` — same pattern as Jetson's `nveglglessink`.
- **Platform detection**: check for `mppjpegdec` element factory via `Gst.ElementFactory.find("mppjpegdec")`.
- **v4l2 device stuck state**: GStreamer pipelines that error out without clean shutdown can leave the v4l2 driver in a stuck state (`EBUSY` on open). Requires USB reset (root) or physical camera replug.

## Quick GStreamer Test Commands

```bash
# Full resolution with HW downscale to 720p
gst-launch-1.0 v4l2src device=/dev/video0 io-mode=mmap \
  ! image/jpeg,width=5120,height=3840,framerate=55/2 \
  ! jpegparse ! mppjpegdec width=1280 height=720 format=NV12 \
  ! xvimagesink sync=false

# Decode benchmark (no display)
gst-launch-1.0 v4l2src device=/dev/video0 io-mode=mmap \
  ! image/jpeg,width=5120,height=3840,framerate=55/2 \
  ! jpegparse ! mppjpegdec width=1280 height=720 format=NV12 \
  ! fakesink sync=false
```

## Tech Stack

- **Python 3.10+** with **PySide6** (Qt 6)
- **GStreamer 1.20.3** via `gi.repository: Gst, GstVideo`
- **loguru** for structured logging
- GStreamer source: `v4l2src` (not platform camera src)
- Camera devices: `/dev/video0`, `/dev/video2` (auto-detected as USB UVC capture devices)

## Documentation

- `docs/dual-camera-sync-investigation.md` — full investigation of frame sync problem, experiments, root cause analysis, and implemented fix
- `docs/frame-sync-future-directions.md` — timestamp approaches evaluated (pad probe implemented, camera SDK for long-term)
- `docs/rk3588-gstreamer-pipeline.md` — GStreamer pipeline architecture notes
