"""
DualCameraManager — coordinates two CameraPipeline instances.

Provides unified start/stop lifecycle and simultaneous capture with
a shared timestamp across both cameras.
"""

import os
from datetime import datetime

from loguru import logger
from PySide6.QtCore import QObject, Signal

from camera_pipeline import CameraPipeline


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
        parent: QObject = None,
    ):
        super().__init__(parent)
        self._use_overlay = use_overlay
        self._pipelines: list[CameraPipeline | None] = [None, None]
        self._camera_mapping = [0, 1]  # maps canvas position to pipeline index

        for i, dev in enumerate(devices[:2]):
            pipe = CameraPipeline(device=dev, use_overlay=use_overlay, parent=self)
            cam_index = i
            pipe.pipeline_error.connect(lambda msg, idx=cam_index: self.camera_error.emit(idx, msg))
            pipe.pipeline_eos.connect(lambda idx=cam_index: self.camera_eos.emit(idx))
            self._pipelines[i] = pipe
            logger.info("DualCameraManager: cam{} → {}", i, dev)

        active = sum(1 for p in self._pipelines if p is not None)
        logger.info("DualCameraManager: {}/2 cameras configured", active)

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

    def capture(self, directory: str) -> list[str | None]:
        """
        Capture the latest frame from both cameras with a shared timestamp.
        Files are named based on canvas position: A_ (left) / D_ (right).

        Args:
            directory: Output directory for captured files.

        Returns:
            [path_left, path_right] — saved file path, or None if capture failed.
        """
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now()
        ms = ts.microsecond // 1000
        ts_str = ts.strftime("%Y%m%d_%H%M%S_") + f"{ms:03d}"

        results: list[str | None] = [None, None]
        prefixes = ["A", "D"]
        for canvas_pos in range(2):
            pipe_idx = self._camera_mapping[canvas_pos]
            pipe = self._pipelines[pipe_idx] if pipe_idx < len(self._pipelines) else None
            if pipe is None:
                continue
            filename = f"{prefixes[canvas_pos]}_{ts_str}.jpg"
            path = os.path.join(directory, filename)
            if pipe.capture_to_file(path):
                results[canvas_pos] = path
                logger.info("Capture {} (cam{}) → {}", prefixes[canvas_pos], pipe_idx, path)
            else:
                logger.warning("Capture {} (cam{}): no frame cached", prefixes[canvas_pos], pipe_idx)

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

    @property
    def use_overlay(self) -> bool:
        return self._use_overlay

    @property
    def camera_count(self) -> int:
        """Number of cameras actually configured (0, 1, or 2)."""
        return sum(1 for p in self._pipelines if p is not None)
