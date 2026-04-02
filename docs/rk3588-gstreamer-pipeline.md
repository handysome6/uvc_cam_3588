# RK3588 GStreamer UVC Camera Pipeline

## Pipeline Overview

```
 /dev/video*
     |
     v
+--------------------------+
|  v4l2src                 |  io-mode=mmap
|  device=/dev/video*      |
+--------------------------+
     |  image/jpeg
     |  width=5120, height=3840
     |  framerate=55/2 (27.5fps)
     |
     v
+--------------------------+
|  jpegparse (name=parser) |<-- JPEG validation probe (sink pad)
+--------------------------+    drops corrupt frames before decoder
     |  image/jpeg, parsed=true
     |
     v
+--------------------------+
|  mppjpegdec              |  width=1280 height=720 format=NV12
|                          |  HW decode + resize in one element
+--------------------------+
     |  video/x-raw
     |  1280x720, NV12
     |
     v
+--------------------------+
|  xvimagesink             |  sync=false
+--------------------------+
```

## Element Mapping: Jetson vs RK3588

| Stage       | Jetson (NVIDIA)                 | RK3588 (Rockchip)               |
|-------------|--------------------------------|----------------------------------|
| JPEG decode | `nvv4l2decoder mjpeg=1`        | `mppjpegdec`                     |
| Resize      | `nvvidconv` + caps filter       | `mppjpegdec width=W height=H`   |
| Color fmt   | `video/x-raw(memory:NVMM),NV12`| `video/x-raw,NV12`              |
| Display     | `nveglglessink`                | `xvimagesink`                    |

Key difference: on RK3588, `mppjpegdec` has built-in `width` and `height` properties that perform HW-accelerated downscale inside the decoder itself, eliminating the need for a separate resize element.

## Hardware Decoder Details

`mppjpegdec` (Rockchip MPP JPEG decoder):

- **Sink**: `image/jpeg, parsed=true` -- requires `jpegparse` upstream
- **Src**: `video/x-raw` or `video/x-raw(memory:DMABuf)`
- **Built-in resize**: `width` and `height` properties (0 = original size)
- **Format selection**: `format` property -- NV12, NV16, I420, RGB, BGRA, etc.
- **Fast mode**: `fast-mode=true` (default) -- prioritizes throughput over quality
- **Rotation**: 0/90/180/270 degree rotation in HW

## Display Sink Notes

### Why not `rkximagesink`?

`rkximagesink` is the Rockchip-specific X/DRM video sink and would seem like the natural choice, but it has a bug in gst-rockchip 1.14.4: it **hardcodes `/dev/dri/card0`** for DRM device access. On boards where the RKNPU registers as `card0` and the actual display controller (`rockchip-drm`) is on `card1`, the sink fails with:

```
DRM v0.9.8 [rknpu -- RKNPU driver -- 20240828]
WARN:  could not get dumb buffer capability
ERROR: driver cannot handle dumb buffers
```

The `driver-name` and `bus-id` properties are ignored in the DRM open path. The NPU's DRM device doesn't support dumb buffers (it's a compute accelerator, not a display device), so the sink fails immediately.

**Workaround**: Use `xvimagesink` instead. It also implements `GstVideoOverlay` for Qt widget embedding.

### Available sinks (tested)

| Sink             | Status  | Notes                                  |
|-----------------|---------|----------------------------------------|
| `xvimagesink`   | Works   | Requires X11 display; supports VideoOverlay |
| `autovideosink` | Works   | Auto-selects; adds `videoconvert` overhead |
| `rkximagesink`  | Broken  | Hardcoded `/dev/dri/card0` -> opens NPU |
| `kmssink`       | Failed  | Needs correct plane/connector config   |
| `waylandsink`   | Failed  | Needs Wayland compositor running       |
| `fbdevsink`     | Failed  | Needs `videoconvert`; limited format support |
| `fakesink`      | Works   | No display, useful for decode benchmarks |

## Quick Test Commands

```bash
# Full resolution capture, HW decode + HW downscale to 720p preview
gst-launch-1.0 v4l2src device=/dev/video0 io-mode=mmap \
  ! image/jpeg,width=5120,height=3840,framerate=55/2 \
  ! jpegparse \
  ! mppjpegdec width=1280 height=720 format=NV12 \
  ! xvimagesink sync=false

# Lower resolution for quick testing
gst-launch-1.0 v4l2src device=/dev/video0 io-mode=mmap \
  ! image/jpeg,width=1280,height=720,framerate=30/1 \
  ! jpegparse \
  ! mppjpegdec \
  ! xvimagesink sync=false

# Decode benchmark (no display)
gst-launch-1.0 v4l2src device=/dev/video0 io-mode=mmap \
  ! image/jpeg,width=5120,height=3840,framerate=55/2 \
  ! jpegparse \
  ! mppjpegdec width=1280 height=720 format=NV12 \
  ! fakesink sync=false

# Use the Python script
python3 scirpts/gst_uvc_single_cam.py               # interactive device selection
python3 scirpts/gst_uvc_single_cam.py /dev/video0    # direct device path
python3 scirpts/gst_uvc_single_cam.py --sink fakesink  # decode-only benchmark
```
