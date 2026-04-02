# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PySide6 GUI application for dual 20MP UVC camera preview and capture on **Rockchip RK3588**. Two USB UVC cameras stream MJPEG at 5120x3840 @ 27.5 FPS. The goal is dual 720p live previews with single-click full-resolution capture (saving raw MJPEG frames as .jpg without re-encoding).

## Reference Implementation

The sibling project `/home/cat/workspace/uvc_cam_jetson/src/` is the NVIDIA Jetson Orin reference. This project replicates its architecture for RK3588 hardware.

## Running the Application

```bash
cd src
python main.py                                          # auto-detect cameras
python main.py --devices /dev/video0,/dev/video2        # explicit devices
python main.py --no-overlay                             # appsink fallback (no VideoOverlay)
```

## Target Platform

- Rockchip RK3588 SoC (aarch64, Linux)
- GStreamer with Rockchip MPP hardware-accelerated plugins
- X11 display server (Wayland not supported for video sink)

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
  -> tee
     ├─ Preview: jpegparse (JPEG probe) -> mppjpegdec(1280x720,NV12) -> xvimagesink
     └─ Capture: queue(leaky) -> appsink (latest raw MJPEG frame only)
```

**Key classes (`src/`):**
- `CameraPipeline` — one per camera; builds/starts/stops the GStreamer pipeline, caches latest MJPEG sample via appsink, provides `capture_to_file(path)`
- `DualCameraManager` — manages two `CameraPipeline` instances, coordinates simultaneous capture with shared timestamp, handles camera swap (stop/restart required)
- `MainWindow` — two preview panels (VideoOverlay via `xvimagesink`), capture button, swap button, status bar
- `main.py` — entry point; GStreamer init, device auto-detection, platform-aware config

## Critical Design Constraints

- **No re-encoding on capture**: save raw MJPEG buffer bytes directly as .jpg — never use `cv::imwrite()` or PIL
- **Capture branch must be leaky**: `queue leaky=downstream max-size-buffers=1` + `appsink drop=true max-buffers=1` — only keep the latest frame
- **Always-cached latest frame**: continuously cache via appsink callback, write on button press
- **File writes on worker thread**: never block the UI thread for disk I/O (use QThreadPool)
- **No GLib main loop**: Qt event loop only; poll GStreamer bus via QTimer
- **JPEG validation before jpegparse**: pad probe drops frames with corrupt SOI/EOI markers — prevents fatal decoder errors at high bitrate

## Platform-Specific Issues

- **`rkximagesink` is broken**: hardcodes `/dev/dri/card0` which is RKNPU on this board, not the display controller. Use `xvimagesink` instead.
- **`xvimagesink` supports `GstVideoOverlay`**: can embed in Qt widgets via `set_window_handle()` — same pattern as Jetson's `nveglglessink`.
- **Platform detection**: check for `mppjpegdec` element factory via `Gst.ElementFactory.find("mppjpegdec")`.

## Running the Testing Script

```bash
# Note: folder name has a typo ("scirpts" not "scripts")
python3 scirpts/gst_uvc_single_cam.py               # interactive device selection
python3 scirpts/gst_uvc_single_cam.py /dev/video0    # direct device
python3 scirpts/gst_uvc_single_cam.py --sink fakesink # decode benchmark (no display)
```

Requires: Python 3.6+, PyGObject, GStreamer 1.0 with MPP plugins, `v4l2-ctl`.

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
- **GStreamer** via `gi.repository: Gst, GstVideo`
- **loguru** for structured logging
- GStreamer source: `v4l2src` (not platform camera src)
- Camera devices: `/dev/video0`, `/dev/video1` (or as auto-detected)
