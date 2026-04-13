# Frame Sync: Future Directions

This document outlines the approaches considered to eliminate frame-matching fragility. **Direction 1 (pad probe) has been implemented** and achieves 100% sync at 27Hz hardware trigger without any dependency on GStreamer's PTS computation or `base_time`.

## Problem Recap (Historical)

The original matching algorithm used `buffer.pts + pipeline.base_time` to recover v4l2 kernel timestamps. This was fragile because:

- `base_time` changes on pipeline restart
- `v4l2src` generates synthetic PTS when actual FPS ≠ negotiated FPS
- The recovery formula is not part of the GStreamer API contract
- Making the preview queue leaky (needed for probe jitter elimination) causes v4l2src to switch to synthetic PTS even at matched framerates

Any robust solution must produce a CLOCK_MONOTONIC timestamp that is:
1. **Accurate**: reflects when the sensor actually exposed (or when the USB frame arrived at the kernel)
2. **Cross-camera comparable**: on the same time base without per-pipeline offsets
3. **Immune to GStreamer internals**: no dependency on `base_time`, PTS computation mode, or pipeline state transitions

## Direction 1: Pad Probe on v4l2src Output (IMPLEMENTED)

**Idea**: Attach a `Gst.PadProbeType.BUFFER` probe on the tee's sink pad. Inside the probe, stamp `time.clock_gettime_ns(CLOCK_MONOTONIC)` and store it in a side-channel dict keyed by `buffer.pts`.

**Why this is better than the appsink callback**:

Wall-clock stamping in the appsink callback has >18ms jitter because it runs on the capture branch's streaming thread, after the tee has pushed to the preview branch. A probe on the tee's sink pad runs on v4l2src's own streaming thread, **before** the tee fans out to preview/capture branches. There is no preview decode contention at this point.

```
v4l2src  -->  tee  --[PROBE HERE on sink pad]
                   -->  preview queue (leaky) --> mppjpegdec (heavy decode)
                   -->  capture queue (leaky) --> appsink (looks up probe ts)
```

**Implementation** (in `camera_pipeline.py`):

```python
# In CameraPipeline.start(), after parse_launch:
tee = self._pipeline.get_by_name("t")
tee_sink_pad = tee.get_static_pad("sink")
tee_sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._stamp_probe)

def _stamp_probe(self, pad, info):
    buf = info.get_buffer()
    if buf is not None:
        pts = buf.pts
        if pts != Gst.CLOCK_TIME_NONE:
            ts = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            with self._probe_lock:
                self._probe_timestamps[pts] = ts  # side-channel dict
    return Gst.PadProbeReturn.OK

def _on_new_capture_sample(self, appsink):
    sample = appsink.emit("pull-sample")
    buf = sample.get_buffer()
    # Look up probe timestamp by PTS
    with self._probe_lock:
        ts = self._probe_timestamps.pop(buf.pts, None)
    if ts is None:
        ts = time.clock_gettime_ns(time.CLOCK_MONOTONIC)  # fallback
    self._sample_ring.append(StampedSample(ts, sample))
```

**Critical requirement — leaky preview queue**:

The preview queue must be `leaky=downstream`. Without this, backpressure from mppjpegdec blocks v4l2src's thread, which delays the probe callback and reintroduces the same jitter. Testing confirmed: non-leaky queue → 11/20 sync; leaky queue → 20/20 sync.

**Side effect**: Making the preview queue leaky causes `v4l2src` to switch PTS to synthetic mode (even at matched framerates). This makes `buffer.pts + base_time` unreliable. Since the pad probe approach doesn't depend on PTS accuracy, this is acceptable — it actually validates the decision to move away from `pts + base_time`.

**Key design decision — `buf.pts` as dict key** (not `id(buf)`):

`id(buf)` does not survive the tee copy — tee may copy or ref the buffer, changing the Python object identity. `buf.pts` is a scalar value that is preserved through tee and queue elements, making it a reliable cross-element key.

**Test results**:

| Test | Sync Rate | Match Delta |
|------|-----------|-------------|
| Headless (probe on queue sink pad) | 5/5 (100%) | ~0.9ms mean |
| GUI (27Hz, probe + leaky queue) | 20/20 (100%) | ~0.9ms mean |
| GUI (27Hz, probe + leaky queue, run 2) | 20/20 (100%) | ~0.9ms mean |

## Direction 2: Read v4l2 Kernel Timestamps Directly

**Idea**: Access the raw `struct v4l2_buffer.timestamp` from the kernel, bypassing GStreamer's PTS computation entirely.

The v4l2 kernel driver stamps each buffer with `CLOCK_MONOTONIC` when the USB transfer completes (`uvcvideo` driver, controlled by `clock=CLOCK_MONOTONIC` module parameter). This timestamp exists regardless of what `v4l2src` does with it.

**Option A: v4l2src `extra-controls` / `GstV4l2BufferPool` metadata**

GStreamer's `v4l2src` internally reads `v4l2_buffer.timestamp` and converts it to PTS. There is no public API to retrieve the raw kernel timestamp. However, some GStreamer builds expose it via `GstV4l2Meta` or buffer metadata — this is version and build dependent.

**Option B: Custom GStreamer element wrapping v4l2**

Write a minimal C GStreamer source element (or Python element using `Gst.ElementFactory`) that:
1. Opens `/dev/videoX` via V4L2 API directly
2. Does `VIDIOC_DQBUF` to get frames with `v4l2_buffer.timestamp`
3. Pushes `GstBuffer` with the kernel timestamp attached as metadata
4. The timestamp is authoritative and independent of GStreamer's clock system

**Option C: ioctl probe from Python**

Use a pad probe that, for each buffer, queries the v4l2 driver for the most recent dequeued buffer's timestamp via `VIDIOC_QUERYBUF`. This is fragile (buffer index tracking) and likely not worth the complexity.

**Pros**:
- Kernel timestamps are the ground truth — stamped in driver interrupt context
- No dependency on GStreamer clock, base_time, or PTS computation
- Works at any framerate, matched or mismatched

**Cons**:
- Option A: not portable across GStreamer versions
- Option B: requires C development, build integration, maintenance
- Option C: fragile and complex
- All options increase complexity significantly vs. the pad probe approach

**Risk**: High complexity for Option B. Option A depends on GStreamer internals (different fragility).

## Direction 3: Custom v4l2src Using Camera SDK

**Idea**: Replace GStreamer's `v4l2src` with a custom source element built against the camera vendor's SDK (`.so` + headers). The SDK may expose:
- Hardware frame counters (monotonic per-camera, from the sensor's internal counter)
- Trigger pulse index (which trigger pulse produced this frame)
- Precise sensor exposure timestamps

A shared trigger counter would be the ideal matching key — no timestamp comparison needed, just `frame_counter_A == frame_counter_D`.

**Implementation**: Write a GStreamer source element in C/C++ that:
1. Opens the camera via the vendor SDK instead of V4L2
2. Receives frames with SDK metadata (frame counter, trigger index, sensor timestamp)
3. Pushes `GstBuffer` with metadata attached
4. The matching logic uses frame counter equality instead of timestamp proximity

**Pros**:
- Frame counter matching is exact — no threshold, no ambiguity
- Eliminates all timestamp-related fragility
- May unlock other camera features (exposure control, gain, ROI)
- Proper integration path for production systems

**Cons**:
- Requires vendor SDK documentation and support
- C/C++ development + GStreamer plugin boilerplate
- SDK compatibility with kernel UVC driver (may need to disable uvcvideo for that device)
- Longer development cycle

**Risk**: High upfront effort, but highest long-term robustness.

## Direction 4: Negotiate Caps to Match Trigger Rate

**Idea**: Ensure the GStreamer caps framerate always matches the actual trigger rate, so `v4l2src` always produces real (not synthetic) PTS. This makes `buffer.pts + base_time` reliable.

Currently, the GUI has framerate presets (27Hz, 10Hz). If the trigger rate is always known and the caps are set to match, the current `pts + base_time` approach works correctly.

**Implementation**:
- Enforce that the caps framerate selector matches the hardware trigger rate
- Add validation: if frames arrive at a rate significantly different from negotiated caps, warn the user
- Document that mismatched rates produce unreliable timestamps

**Pros**:
- Zero code change to the matching logic
- Already works in the current implementation (27Hz trigger + 55/2 caps)

**Cons**:
- Fragile: user must manually keep caps and trigger rate in sync
- Breaks silently if trigger rate drifts or is changed without updating caps
- Does not address pipeline restart or caps renegotiation fragility
- Not a solution — just a usage constraint

**Risk**: Low effort but does not actually fix the underlying fragility. Suitable as a documented constraint, not as a fix.

## Recommendation

**Done**: Direction 1 (pad probe) is implemented and validated. The `base_time` dependency is fully eliminated. The probe stamps wall-clock time on v4l2src's streaming thread before any decode contention, giving sub-millisecond accuracy. The key insight was that the preview queue must be leaky to prevent backpressure from reintroducing jitter.

**Medium term**: Direction 4 as a documented constraint — enforce matched caps framerate in the GUI and warn on mismatch. This is complementary to the probe approach (though the probe works regardless of PTS mode).

**Long term**: Direction 3 (camera SDK) if the project moves toward production. Frame counter matching is fundamentally more robust than any timestamp-based approach, and the SDK may unlock features needed for industrial deployment.

Direction 2 is not recommended — it has similar complexity to Direction 3 but without the additional benefits of SDK access.
