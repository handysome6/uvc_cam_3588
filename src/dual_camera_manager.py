"""
DualCameraManager — coordinates two CameraPipeline instances.

Provides unified start/stop lifecycle and simultaneous capture with
a shared timestamp across both cameras.
"""

import os
from datetime import datetime

from loguru import logger
from PySide6.QtCore import QObject, Signal

from camera_pipeline import CameraPipeline, StampedSample


class DualCameraManager(QObject):
    """
    Manages two CameraPipeline instances (cam0, cam1).

    Signals:
        camera_error(int, str) — camera index + error message
        camera_eos(int)        — camera index that reached EOS
        cameras_swapped()      — emitted when camera-to-canvas mapping is swapped
    """

    camera_error = Signal(int, str)
    camera_eos = Signal(int)
    cameras_swapped = Signal()

    def __init__(
        self,
        devices: list[str],
        use_overlay: bool = True,
        framerate: str = "55/2",
        parent: QObject = None,
    ):
        super().__init__(parent)
        self._devices = devices[:2]
        self._use_overlay = use_overlay
        self._framerate = framerate
        self._pipelines: list[CameraPipeline | None] = [None, None]
        self._camera_mapping = [0, 1]  # maps canvas position to pipeline index

        self._create_pipelines()

        active = sum(1 for p in self._pipelines if p is not None)
        logger.info("DualCameraManager: {}/2 cameras configured", active)

    def _create_pipelines(self):
        """Create CameraPipeline instances for each device."""
        self._pipelines = [None, None]
        for i, dev in enumerate(self._devices):
            pipe = CameraPipeline(
                device=dev, use_overlay=self._use_overlay,
                framerate=self._framerate, parent=self,
            )
            cam_index = i
            pipe.pipeline_error.connect(lambda msg, idx=cam_index: self.camera_error.emit(idx, msg))
            pipe.pipeline_eos.connect(lambda idx=cam_index: self.camera_eos.emit(idx))
            self._pipelines[i] = pipe
            logger.info("DualCameraManager: cam{} → {} @ {}", i, dev, self._framerate)

    def start(self, window_handles: list[int | None]) -> list[bool]:
        """
        Start both pipelines.

        Args:
            window_handles: [handle_left, handle_right] — native window IDs
                            for VideoOverlay. Pass None for absent cameras.
        Returns:
            [ok_left, ok_right] — True if pipeline reached PLAYING.
        """
        results = [False, False]
        for canvas_pos in range(2):
            pipe_idx = self._camera_mapping[canvas_pos]
            pipe = self._pipelines[pipe_idx] if pipe_idx < len(self._pipelines) else None
            if pipe is None:
                continue
            handle = window_handles[canvas_pos] if canvas_pos < len(window_handles) else None
            results[canvas_pos] = pipe.start(window_handle=handle)
        return results

    def stop(self):
        """Stop both pipelines and release resources."""
        for i, pipe in enumerate(self._pipelines):
            if pipe is not None:
                pipe.stop()
        logger.info("DualCameraManager: all pipelines stopped")

    def capture(self, directory: str, pts_in_filename: bool = False) -> list[str | None]:
        """
        Capture a timestamp-matched frame pair from both cameras.

        Instead of grabbing the single latest frame from each camera
        (which may come from different trigger pulses due to USB delivery
        skew), this method reads each camera's ring buffer of recent
        wall-clock-stamped samples and selects the pair whose arrival
        timestamps are closest — i.e. the pair from the same trigger pulse.

        Files are named based on canvas position: A_ (left) / D_ (right).

        Args:
            directory: Output directory for captured files.
            pts_in_filename: If True, append the absolute v4l2 timestamp
                             (nanoseconds) to each filename for sync debugging.

        Returns:
            [path_left, path_right] — saved file path, or None if capture failed.
        """
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now()
        ms = ts.microsecond // 1000
        ts_str = ts.strftime("%Y%m%d_%H%M%S_") + f"{ms:03d}"

        # Phase 1: snapshot ring buffers from both cameras
        pipes: list[CameraPipeline | None] = [None, None]
        rings: list[list[StampedSample]] = [[], []]
        for canvas_pos in range(2):
            pipe_idx = self._camera_mapping[canvas_pos]
            pipe = self._pipelines[pipe_idx] if pipe_idx < len(self._pipelines) else None
            pipes[canvas_pos] = pipe
            if pipe is not None:
                rings[canvas_pos] = pipe.snapshot_ring()

        # Phase 2: match frames by closest pad-probe timestamp
        #
        # Each ring entry carries a CLOCK_MONOTONIC timestamp stamped by a
        # pad probe on v4l2src's streaming thread (before the tee fans out
        # to preview/capture branches).  No preview decode contention at
        # this point, so same-trigger frames have timestamps within ~1 ms.
        matched: list[StampedSample | None] = [None, None]
        match_delta_ms: float | None = None

        if rings[0] and rings[1]:
            best_delta = float('inf')
            best_a: StampedSample | None = None
            best_d: StampedSample | None = None
            # O(N²) with N=5 — trivial cost
            for a_entry in rings[0]:
                for d_entry in rings[1]:
                    delta = abs(a_entry.timestamp_ns - d_entry.timestamp_ns)
                    if delta < best_delta:
                        best_delta = delta
                        best_a = a_entry
                        best_d = d_entry

            matched[0] = best_a
            matched[1] = best_d
            match_delta_ms = best_delta / 1_000_000
            logger.info(
                "Frame match: Δ={:.3f}ms (ring sizes: {} / {})",
                match_delta_ms, len(rings[0]), len(rings[1]),
            )
        else:
            # Fallback: only one camera has frames
            for canvas_pos in range(2):
                if rings[canvas_pos]:
                    matched[canvas_pos] = rings[canvas_pos][-1]

        # Phase 3: schedule file writes
        results: list[str | None] = [None, None]
        prefixes = ["A", "D"]
        for canvas_pos in range(2):
            pipe_idx = self._camera_mapping[canvas_pos]
            if matched[canvas_pos] is None:
                if pipes[canvas_pos] is not None:
                    logger.warning("Capture {} (cam{}): no frame cached", prefixes[canvas_pos], pipe_idx)
                continue
            ts_suffix = f"_ts{matched[canvas_pos].timestamp_ns}" if pts_in_filename else ""
            filename = f"{prefixes[canvas_pos]}_{ts_str}{ts_suffix}.jpg"
            path = os.path.join(directory, filename)
            pipes[canvas_pos].write_sample_to_file(matched[canvas_pos].sample, path)
            results[canvas_pos] = path
            logger.info("Capture {} (cam{}) → {}", prefixes[canvas_pos], pipe_idx, path)

        return results

    def pipeline(self, index: int) -> CameraPipeline | None:
        """Return the CameraPipeline for the given camera index (0 or 1)."""
        if 0 <= index < 2:
            return self._pipelines[index]
        return None

    def pipeline_for_canvas(self, canvas_pos: int) -> CameraPipeline | None:
        """Return the CameraPipeline currently mapped to the given canvas position (0=left, 1=right)."""
        if 0 <= canvas_pos < 2:
            pipe_idx = self._camera_mapping[canvas_pos]
            return self._pipelines[pipe_idx] if pipe_idx < len(self._pipelines) else None
        return None

    def swap_cameras(self):
        """Swap the camera-to-canvas mapping (left <-> right)."""
        self._camera_mapping.reverse()
        logger.info("DualCameraManager: cameras swapped, new mapping: {}", self._camera_mapping)
        self.cameras_swapped.emit()

    def set_framerate(self, framerate: str):
        """Change the capture framerate. Pipelines must be stopped and restarted by the caller."""
        self._framerate = framerate
        logger.info("DualCameraManager: framerate set to {}", framerate)
        self._create_pipelines()

    @property
    def framerate(self) -> str:
        return self._framerate

    @property
    def use_overlay(self) -> bool:
        return self._use_overlay

    @property
    def camera_count(self) -> int:
        """Number of cameras actually configured (0, 1, or 2)."""
        return sum(1 for p in self._pipelines if p is not None)
