# Dual Camera Frame Sync Investigation

## Problem Statement

Two identical 20MP UVC cameras (DECXIN Camera) are connected to an RK3588 board and hardware-triggered by a shared external trigger signal (TRG + GND). Both cameras receive the same trigger pulse simultaneously, so their sensor exposures are physically synchronized. However, when capturing image pairs through the GUI application, a significant percentage of pairs are **out of sync** -- the two saved frames come from different trigger pulses.

### Paradox

When the TRG pin is manually disconnected (cutting the trigger signal), the last frames displayed on both camera previews are always perfectly synchronized (verified by a millisecond timer placed in front of both cameras). This proves the cameras ARE physically synced. Yet during live streaming, captured pairs are frequently mismatched.

## Test Setup

- **Hardware**: Rockchip RK3588 SoC (aarch64, Linux 6.1.84)
- **Cameras**: 2x DECXIN Camera (20MP, USB UVC, 5120x3840 MJPEG)
- **Trigger**: External hardware trigger, shared TRG/GND pins
- **USB topology**: Camera A on `usb-xhci-hcd.3.auto-1`, Camera D on `usb-xhci-hcd.11.auto-1` (separate USB host controllers)
- **Sync reference**: Millisecond timer display placed in front of both cameras
- **GStreamer**: 1.20.3, with Rockchip MPP HW-accelerated plugins
- **UVC driver**: `uvcvideo`, kernel 6.1.84, `clock=CLOCK_MONOTONIC`, `hwtimestamps=0`

### GStreamer Pipeline Architecture

Each camera runs an independent pipeline with a `tee` split:

```
v4l2src (MJPEG 5120x3840, io-mode=mmap, framerate=55/2)
  -> tee
     +-- Preview: queue -> jpegparse -> mppjpegdec(1280x720) -> xvimagesink
     +-- Capture: queue(leaky=downstream, max-size-buffers=1)
                  -> appsink(drop=true, max-buffers=1, emit-signals=true)
```

The capture branch continuously caches the latest MJPEG frame via `_on_new_capture_sample()` callback into `_latest_sample` (protected by a per-camera `threading.Lock`). On button press, both cameras' `_latest_sample` are read back-to-back and written to disk.

## Experiments and Findings

### Experiment 1: Initial Observation (27Hz trigger)

**Setup**: 27Hz external trigger (matching negotiated `framerate=55/2` = 27.5fps).

**Result**: ~50% of captured pairs are visually desynced (10/20 pairs show different millisecond readings).

**Key observation**: Cutting the TRG pin always yields synced last frames. This proves physical sync exists; the desync is introduced by the software capture path.

### Experiment 2: Code Fix -- Back-to-Back Sample Snapshot

**Hypothesis**: The original `DualCameraManager.capture()` method interleaved "read sample" and "schedule write + log" for each camera in a serial loop. The code between reading camera A's sample and camera D's sample (filename construction, thread pool submission, `logger.info()`) took several milliseconds, creating a race window.

**Fix applied**: Split `capture()` into two phases:
1. **Phase 1 (snapshot)**: Read both `_latest_sample` references back-to-back (~microseconds apart)
2. **Phase 2 (write)**: Construct filenames and schedule file writes

**Result**: No improvement. Still ~50% desync rate. The race window was not the bottleneck.

### Experiment 3: 10Hz Trigger Rate

**Setup**: Reduced trigger frequency from 27Hz to 10Hz (100ms frame period instead of 36ms).

**Result**: Still ~50% desync. If the cause were a timing-window race, the desync rate should scale with frame period. The unchanged rate at 10Hz suggests a **deterministic off-by-one frame** issue, not a probabilistic timing race.

### Experiment 4: PTS Timestamp Diagnostic (10Hz)

**Goal**: Embed GStreamer buffer PTS into filenames to compare timestamps across cameras.

**Finding**: Raw PTS values showed a **constant +1631.108 ms offset** across all 20 pairs, with zero variation. This revealed that PTS is a **per-pipeline running time** (relative to each pipeline's `base_time`), not a shared clock. Comparing raw PTS across cameras is meaningless.

```
buffer.pts = v4l2_kernel_timestamp - pipeline.base_time
```

Since the two pipelines start sequentially (~1.6s apart in `DualCameraManager.start()`), their `base_time` values differ by exactly that gap.

### Experiment 5: Absolute v4l2 Timestamp Recovery (10Hz)

**Fix**: Recover the original CLOCK_MONOTONIC kernel timestamp by adding back `base_time`:

```python
absolute_ts = buffer.pts + pipeline.get_base_time()
```

**Finding**: The recovered timestamps were **synthetic, not real CLOCK_MONOTONIC values**. Evidence:

| Metric | Expected (real) | Observed (synthetic) |
|--------|----------------|---------------------|
| Inter-capture interval | ~2000 ms (auto-capture timer) | 727.273 ms |
| Interval ratio | 1.0 | 0.3636 = 10/27.5 = actual_fps / negotiated_fps |

GStreamer's `v4l2src` was generating timestamps from an internal frame counter multiplied by the negotiated frame duration (`1/27.5fps = 36.364ms`), rather than passing through the kernel buffer timestamps. This happens when the actual frame delivery rate (10Hz) mismatches the negotiated caps rate (27.5fps) -- `v4l2src`'s `GstBaseSrc` timestamp logic falls back to synthetic timestamps.

### Experiment 6: 27Hz Trigger with Absolute Timestamps

**Setup**: 27Hz trigger (matching negotiated 27.5fps), with `absolute_ts` in filenames.

**Result**: Timestamps are now **real CLOCK_MONOTONIC values**. Evidence:

| Metric | Value |
|--------|-------|
| Inter-capture interval | ~2000 ms (matches real wall-clock time) |
| Synced pair delta | -0.7 to -0.08 ms (real USB transfer delay) |
| Desynced pair delta | +35 to +75 ms (integer multiples of 36.4ms frame period) |

#### Timestamp Delta Distribution

```
Pair  1 [DESYNC (+2.1 frames)]: delta=   +75.293 ms
Pair  2 [  SYNC (same pulse) ]: delta=    +3.244 ms
Pair  3 [  SYNC (same pulse) ]: delta=    -0.784 ms
Pair  4 [DESYNC (+1.1 frames)]: delta=   +39.343 ms
Pair  5 [DESYNC (+2.0 frames)]: delta=   +71.306 ms
Pair  6 [DESYNC (+1.1 frames)]: delta=   +39.335 ms
Pair  7 [DESYNC (+2.0 frames)]: delta=   +71.288 ms
Pair  8 [DESYNC (+1.1 frames)]: delta=   +39.323 ms
Pair  9 [DESYNC (+2.0 frames)]: delta=   +71.289 ms
Pair 10 [DESYNC (+1.1 frames)]: delta=   +39.237 ms
Pair 11 [  SYNC (same pulse) ]: delta=    -0.746 ms
Pair 12 [DESYNC (+1.1 frames)]: delta=   +39.312 ms
Pair 13 [  SYNC (same pulse) ]: delta=    -0.720 ms
Pair 14 [DESYNC (+1.1 frames)]: delta=   +39.294 ms
Pair 15 [DESYNC (+2.1 frames)]: delta=   +75.323 ms
Pair 16 [DESYNC (+1.1 frames)]: delta=   +39.279 ms
Pair 17 [  SYNC (same pulse) ]: delta=    -0.728 ms
Pair 18 [DESYNC (+1.0 frames)]: delta=   +35.243 ms
Pair 19 [DESYNC (+2.1 frames)]: delta=   +75.314 ms
Pair 20 [  SYNC (same pulse) ]: delta=    -0.742 ms
```

Three distinct clusters:

| Cluster | Count | Mean Delta | Interpretation |
|---------|-------|-----------|---------------|
| Same pulse | 6/20 | -0.08 ms | Both caches hold frame from same trigger pulse |
| Off-by-1 | 8/20 | +38.8 ms | A has frame N+1, D still has frame N |
| Off-by-2 | 6/20 | +73.3 ms | A has frame N+2, D still has frame N |

**Critical observation**: Delta is **always positive** when desynced -- camera A's `_latest_sample` is always ahead of camera D's. This is a consistent USB delivery ordering, not a random race.

### Experiment 7: OCR Cross-Validation (27Hz)

**Goal**: Validate v4l2 timestamps against ground truth by OCR-reading the millisecond timer from captured images, then comparing with timestamp-based sync detection.

**OCR method**: Used a local OCR server to extract the displayed millisecond timer value from all 40 images (20 A + 20 D).

#### Cross-Reference Results

| Desync Type | v4l2 Delta | Timer Detects It? | Explanation |
|---|---|---|---|
| Off-by-1 frame | ~39 ms | Only ~50% of the time | 36ms < timer's ~75ms refresh interval |
| Off-by-2 frames | ~72 ms | ~100% of the time | 72ms >= timer refresh -- always visible |
| Same pulse | ~0.7 ms | Correctly shows sync | Same trigger pulse, same timer reading |

All 8 pairs where both methods had reliable readings showed **perfect agreement**. The 6 apparent "mismatches" were all off-by-1 frame pairs where the millisecond timer display hadn't refreshed between consecutive trigger pulses (36ms gap < 75ms display refresh). The v4l2 timestamps detected these invisible desyncs that human/OCR inspection missed.

**Actual desync rate**: **14/20 (70%)**, not the 10/20 (50%) visible to the eye.

## Root Cause

### Confirmed Mechanism

Camera A's `_latest_sample` appsink cache is consistently **1-2 trigger pulses ahead** of camera D's at the moment `snapshot_sample()` reads both caches. This is caused by USB delivery ordering: camera A's frames arrive and are processed by GStreamer's streaming thread before camera D's frames for the same trigger pulse.

The `_latest_sample` is a **single-slot cache with no frame identity**. It holds "the most recent frame" with no trigger-pulse number or matched timestamp. Even reading both caches within microseconds of each other cannot guarantee they hold frames from the same trigger pulse, because the underlying caches are already desynchronized.

### Why Cutting TRG Shows Perfect Sync

When the trigger signal is disconnected, no new frames arrive. Both `_latest_sample` caches freeze at their last value. Since the streaming threads have stopped updating, the sequential read always gets the last frame each camera processed -- which, after the pipeline has been running long enough, corresponds to the same trigger pulse (or close enough that the single-slot cache has converged). There is no "next frame" that can overwrite one cache while the other is being read.

## Key Technical Findings

### 1. GStreamer v4l2src Timestamp Behavior

`v4l2src` (GStreamer 1.20.3) does **not** reliably pass through kernel CLOCK_MONOTONIC buffer timestamps. Its behavior depends on whether the actual frame delivery rate matches the negotiated caps framerate:

| Condition | Timestamp Source | Usable for Cross-Camera Sync? |
|-----------|-----------------|-------------------------------|
| Actual FPS matches negotiated FPS | Real v4l2 kernel timestamps | Yes |
| Actual FPS differs from negotiated FPS | Synthetic (frame_count x negotiated_duration) | No |

When synthetic, timestamps advance at `(actual_fps / negotiated_fps)` of real time.

### 2. PTS is Per-Pipeline, Not Shared

GStreamer buffer PTS = `v4l2_kernel_timestamp - pipeline.base_time`. Since each pipeline has its own `base_time` (set at PLAYING transition), raw PTS values cannot be compared across cameras. Recovery requires `absolute_ts = pts + pipeline.get_base_time()`.

### 3. USB Delivery Ordering

Despite the two cameras being on **separate USB host controllers** (`xhci-hcd.3.auto` and `xhci-hcd.11.auto`), camera A's frames consistently arrive first. The delivery asymmetry is stable within a session and causes camera A's appsink cache to be 1-2 frames ahead of camera D's.

### 4. Visual Inspection Underestimates Desync

A millisecond timer with ~75ms display refresh cannot detect off-by-1 frame desyncs at 27Hz (36ms frame period). v4l2 timestamps revealed 70% desync rate vs. 50% visible to the eye.

## Timestamp Diagnostic Tool

The codebase includes a diagnostic mode for embedding pad-probe timestamps into captured filenames:

```bash
# Enable via CLI flag
python main.py --pts-filename

# Or toggle the "PTS in filename" checkbox in the GUI
```

Filename format with timestamps enabled:
```
A_20260413_112420_574_ts158485496717102.jpg
D_20260413_112420_574_ts158485480949882.jpg
                      ^^^^^^^^^^^^^^^^^^^
                      pad-probe CLOCK_MONOTONIC timestamp (nanoseconds)
```

These timestamps are the same values used by the frame matcher — stamped on v4l2src's streaming thread before the tee fans out. Same-trigger frames show deltas under ~1ms.

## Implemented Fix: Ring Buffer + Pad-Probe Timestamp Matching

### Approach

Replaced the single-slot `_latest_sample` cache with a ring buffer of 5 recent frames, each tagged with a CLOCK_MONOTONIC timestamp. On capture, both cameras' ring buffers are snapshot and the pair with minimum timestamp delta is selected.

### Implementation

**`camera_pipeline.py`:**
- `StampedSample(timestamp_ns, sample)` NamedTuple pairs a timestamp with each GStreamer sample
- `_sample_ring: deque[StampedSample]` (maxlen=5) replaces `_latest_sample`
- `_stamp_probe()` pad probe on the tee's sink pad stamps `clock_gettime_ns(CLOCK_MONOTONIC)` on v4l2src's streaming thread, stored in a side-channel dict keyed by `buffer.pts`
- `_on_new_capture_sample()` looks up the probe timestamp by PTS to tag each ring entry
- `snapshot_ring()` returns an atomic list copy for matching
- Preview queue is `leaky=downstream max-size-buffers=2` to prevent backpressure from blocking v4l2src's thread

**`dual_camera_manager.py`:**
- `capture()` Phase 1: snapshot both rings
- `capture()` Phase 2: O(N²) search for minimum `|a.timestamp_ns - d.timestamp_ns|` (N=5, trivial cost)
- Phases 3-4: write matched pair to disk

### Timestamp Source: Three Iterations

**Iteration 1 — Wall-clock in appsink callback (commit 5c0d353):**

Stamped frames with `time.clock_gettime_ns(CLOCK_MONOTONIC)` inside the appsink callback. This worked perfectly in headless mode (no preview decode, <1ms jitter), but **failed ~50% in the GUI** because mppjpegdec + videoconvert preview threads cause >18ms of CPU scheduling jitter on the capture callback. At 27Hz (36ms frame interval), jitter exceeding half the interval causes the matcher to select the wrong frame.

**Iteration 2 — v4l2 kernel timestamp (commit 05980e9):**

Changed to `buffer.pts + pipeline.get_base_time()`, which recovers the original v4l2 kernel CLOCK_MONOTONIC timestamp. Kernel timestamps are stamped in driver space, immune to userspace thread scheduling. Achieved 100% sync in GUI mode. However, this relies on undocumented `v4l2src` internals — `pts + base_time` only equals the real kernel timestamp when actual FPS matches negotiated FPS, and breaks under pipeline restart or caps renegotiation. See `docs/frame-sync-future-directions.md` for the full fragility analysis.

**Iteration 3 — Pad probe wall-clock (current):**

Attached a `BUFFER` pad probe on the tee's sink pad. The probe stamps `clock_gettime_ns(CLOCK_MONOTONIC)` and stores it in a dict keyed by `buffer.pts`. The appsink callback looks up the probe timestamp by PTS.

The probe runs on v4l2src's streaming thread, **before** the tee fans out to preview/capture branches — no preview decode contention at this point. A critical requirement is that the preview queue must be **leaky** (`leaky=downstream`); otherwise backpressure from mppjpegdec blocks v4l2src's thread and reintroduces the same jitter. Initial testing without the leaky queue showed 9/20 desync; adding `leaky=downstream` restored 20/20 sync.

### Test Results

| Test | Method | Sync Rate | Match Delta |
|------|--------|-----------|-------------|
| Headless (freerun ~10fps) | Wall-clock appsink | 20/20 (100%) | ~0.5ms mean |
| GUI (27Hz trigger) | Wall-clock appsink | 9/20 (45%) | wrong pairs ~35ms |
| GUI (27Hz trigger) | v4l2 kernel ts | 20/20 (100%) | ~1.9ms mean |
| GUI (27Hz trigger) | Pad probe (non-leaky queue) | 11/20 (55%) | wrong pairs ~35ms |
| GUI (27Hz trigger) | Pad probe + leaky queue | 20/20 (100%) | ~0.9ms mean |
| GUI (27Hz trigger, run 2) | Pad probe + leaky queue | 20/20 (100%) | ~0.9ms mean |

### Why Pad Probe Over v4l2 Kernel Timestamp

The v4l2 kernel timestamp approach (`buffer.pts + base_time`) achieved 100% sync but relied on undocumented `v4l2src` implementation behavior:

1. `pts + base_time = kernel_timestamp` is not a GStreamer API contract
2. Synthetic PTS when actual FPS mismatches negotiated caps FPS
3. `base_time` resets on pipeline restart, corrupting the first few frames
4. Caps renegotiation can break PTS continuity
5. Making the preview queue leaky (needed for the probe approach) also causes v4l2src to switch to synthetic PTS — confirmed by the probe_leaky test runs where `pts + base_time` deltas jumped to seconds

The pad probe approach eliminates all these dependencies. Wall-clock is stamped in Python on the correct thread (v4l2src's, before decode contention), with no dependency on GStreamer's internal PTS computation, base_time, or pipeline state.
