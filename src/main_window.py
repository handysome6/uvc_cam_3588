"""
MainWindow — dual-camera preview + capture UI.

Layout:
  ┌──────────────────┬──────────────────┐
  │   Left (A)       │   Right (D)      │
  │   (720p preview) │   (720p preview) │
  ├──────────────────┴──────────────────┤
  │  [Capture] [Auto] [Swap] [☑ PTS]   │
  │  Save path: [~/captures___________] │
  │  Status: ...                        │
  └─────────────────────────────────────┘

Capture files land in <save_path>/A_{YYYYMMDD}_{HHMMSS}_{mmm}.jpg (left)
                                  D_{YYYYMMDD}_{HHMMSS}_{mmm}.jpg (right)
"""

import os

from loguru import logger
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from camera_pipeline import CameraPipeline
from dual_camera_manager import DualCameraManager

DEFAULT_CAPTURE_DIR = os.path.expanduser("~/captures")
AUTO_CAPTURE_COUNT = 20
AUTO_CAPTURE_INTERVAL_MS = 2000


# ---------------------------------------------------------------------------
# PreviewWidget — native container for VideoOverlay rendering
# ---------------------------------------------------------------------------

class _PreviewWidget(QWidget):
    """
    Opaque native widget into which xvimagesink renders via VideoOverlay.

    WA_NativeWindow  — ensures the widget has its own OS window handle (winId)
    WA_PaintOnScreen — tells Qt not to manage painting (GStreamer owns the surface)
    paintEngine()    — returning None is required when WA_PaintOnScreen is set
    """

    def __init__(self, pipeline: CameraPipeline, parent=None):
        super().__init__(parent)
        self._pipeline = pipeline
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_PaintOnScreen)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: black;")

    def paintEngine(self):
        # Required when WA_PaintOnScreen is set; GStreamer manages rendering
        return None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._pipeline.expose()


# ---------------------------------------------------------------------------
# Placeholder for missing camera
# ---------------------------------------------------------------------------

def _make_placeholder(text: str = "Camera not connected") -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumSize(640, 360)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    label.setStyleSheet("background: black; color: #888; font-size: 16px;")
    return label


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(
        self,
        manager: DualCameraManager,
        pts_filename: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("UVC Camera — Dual Camera (RK3588)")
        self._manager = manager
        self._manager.camera_error.connect(self._on_camera_error)
        self._manager.camera_eos.connect(self._on_camera_eos)
        self._manager.cameras_swapped.connect(self._on_cameras_swapped)

        self._pts_default = pts_filename

        # Auto-capture state
        self._auto_timer: QTimer | None = None
        self._auto_count: int = 0

        # Preview widgets: one per canvas position (may be _PreviewWidget or QLabel)
        self._previews: list[QWidget] = [None, None]
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Preview area — two panels side by side with labels
        preview_container = QWidget()
        preview_root = QVBoxLayout(preview_container)
        preview_root.setContentsMargins(0, 0, 0, 0)
        preview_root.setSpacing(4)

        # Labels row
        labels_row = QWidget()
        labels_layout = QHBoxLayout(labels_row)
        labels_layout.setContentsMargins(0, 0, 0, 0)
        labels_layout.setSpacing(6)

        left_label = QLabel("Left (A)")
        left_label.setAlignment(Qt.AlignCenter)
        left_label.setStyleSheet("font-weight: bold; font-size: 14px;")

        right_label = QLabel("Right (D)")
        right_label.setAlignment(Qt.AlignCenter)
        right_label.setStyleSheet("font-weight: bold; font-size: 14px;")

        labels_layout.addWidget(left_label, stretch=1)
        labels_layout.addWidget(right_label, stretch=1)
        preview_root.addWidget(labels_row)

        # Preview panels row
        preview_row = QWidget()
        preview_layout = QHBoxLayout(preview_row)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)

        for canvas_pos in range(2):
            pipe = self._manager.pipeline_for_canvas(canvas_pos)
            if pipe is not None:
                if self._manager.use_overlay:
                    widget = _PreviewWidget(pipe)
                else:
                    widget = QLabel(f"Canvas {canvas_pos}: waiting…")
                    widget.setAlignment(Qt.AlignCenter)
                    widget.setMinimumSize(640, 360)
                    widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                    widget.setStyleSheet("background: black; color: #888;")
                    pipe.preview_frame.connect(
                        lambda img, lbl=widget: self._on_preview_frame(lbl, img)
                    )
            else:
                widget = _make_placeholder()
            self._previews[canvas_pos] = widget
            preview_layout.addWidget(widget, stretch=1)

        preview_root.addWidget(preview_row, stretch=1)
        root.addWidget(preview_container, stretch=1)

        # Controls row 1 — buttons and options
        row1 = QWidget()
        row1_layout = QHBoxLayout(row1)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.setSpacing(8)

        self._capture_btn = QPushButton("Capture")
        self._capture_btn.setMinimumHeight(36)
        self._capture_btn.setMinimumWidth(100)
        self._capture_btn.clicked.connect(self._on_capture)

        self._auto_btn = QPushButton(f"Auto Capture ({AUTO_CAPTURE_COUNT})")
        self._auto_btn.setMinimumHeight(36)
        self._auto_btn.setMinimumWidth(140)
        self._auto_btn.clicked.connect(self._on_auto_capture)

        self._swap_btn = QPushButton("Swap Cameras")
        self._swap_btn.setMinimumHeight(36)
        self._swap_btn.setMinimumWidth(120)
        self._swap_btn.clicked.connect(self._on_swap)

        self._pts_cb = QCheckBox("PTS in filename")
        self._pts_cb.setChecked(self._pts_default)

        self._status = QLabel("Starting pipelines…")
        self._status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        row1_layout.addWidget(self._capture_btn)
        row1_layout.addWidget(self._auto_btn)
        row1_layout.addWidget(self._swap_btn)
        row1_layout.addWidget(self._pts_cb)
        row1_layout.addWidget(self._status, stretch=1)
        root.addWidget(row1)

        # Controls row 2 — save path
        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.setSpacing(8)

        path_label = QLabel("Save path:")
        self._path_edit = QLineEdit(DEFAULT_CAPTURE_DIR)
        self._path_edit.setMinimumHeight(30)

        row2_layout.addWidget(path_label)
        row2_layout.addWidget(self._path_edit, stretch=1)
        root.addWidget(row2)

        self.resize(1310, 810)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_capture_dir(self) -> str:
        text = self._path_edit.text().strip()
        return os.path.expanduser(text) if text else DEFAULT_CAPTURE_DIR

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        # Defer pipeline start: native X11 windows are not yet realized during
        # showEvent.  A single-shot timer lets the event loop finish mapping
        # all widgets so winId() returns a valid X11 window ID.
        if not hasattr(self, "_pipelines_started"):
            self._pipelines_started = True
            QTimer.singleShot(100, self._start_pipelines)

    @Slot()
    def _start_pipelines(self):
        handles: list[int | None] = [None, None]
        for i in range(2):
            pipe = self._manager.pipeline(i)
            if pipe is not None and self._manager.use_overlay:
                wid = int(self._previews[i].winId())
                logger.debug("Preview widget {} winId = 0x{:x}", i, wid)
                handles[i] = wid

        logger.info("Starting pipelines (overlay={})", self._manager.use_overlay)
        results = self._manager.start(handles)

        started = sum(1 for r in results if r)
        if started > 0:
            capture_dir = self._get_capture_dir()
            self._status.setText(
                f"{started} camera(s) running — captures → {capture_dir}"
            )
            logger.success("{} pipeline(s) running | captures → {}", started, capture_dir)
        else:
            logger.error("No pipelines started")
            self._status.setText("Error: no cameras started")

    def closeEvent(self, event):
        logger.info("Window closing — stopping pipelines")
        self._stop_auto_capture()
        self._manager.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Capture button
    # ------------------------------------------------------------------

    @Slot()
    def _on_capture(self):
        logger.info("Capture triggered")
        capture_dir = self._get_capture_dir()
        pts = self._pts_cb.isChecked()
        paths = self._manager.capture(capture_dir, pts_in_filename=pts)

        saved = [p for p in paths if p is not None]
        if saved:
            names = [os.path.basename(p) for p in saved]
            self._status.setText(f"Saved: {', '.join(names)}")
            self._capture_btn.setEnabled(False)
            self._capture_btn.setText("Saved!")
            QTimer.singleShot(800, self._reset_capture_btn)
        else:
            logger.warning("Capture failed: no frames cached yet")
            self._status.setText("No frames cached yet — try again in a moment")

    @Slot()
    def _reset_capture_btn(self):
        self._capture_btn.setText("Capture")
        self._capture_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Auto-capture
    # ------------------------------------------------------------------

    @Slot()
    def _on_auto_capture(self):
        if self._auto_timer is not None:
            self._stop_auto_capture()
            return

        # Start auto-capture sequence
        self._auto_count = 0
        self._capture_btn.setEnabled(False)
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_CAPTURE_INTERVAL_MS)
        self._auto_timer.timeout.connect(self._auto_capture_tick)
        self._auto_timer.start()

        # Capture first frame immediately
        self._auto_capture_tick()

    @Slot()
    def _auto_capture_tick(self):
        self._auto_count += 1
        capture_dir = self._get_capture_dir()
        pts = self._pts_cb.isChecked()
        paths = self._manager.capture(capture_dir, pts_in_filename=pts)

        saved = [p for p in paths if p is not None]
        if saved:
            names = [os.path.basename(p) for p in saved]
            self._status.setText(
                f"Auto {self._auto_count}/{AUTO_CAPTURE_COUNT}: {', '.join(names)}"
            )
        else:
            self._status.setText(
                f"Auto {self._auto_count}/{AUTO_CAPTURE_COUNT}: no frame cached"
            )

        self._auto_btn.setText(
            f"Stop Auto ({self._auto_count}/{AUTO_CAPTURE_COUNT})"
        )

        if self._auto_count >= AUTO_CAPTURE_COUNT:
            self._stop_auto_capture()
            self._status.setText(
                f"Auto-capture complete — {AUTO_CAPTURE_COUNT} pairs saved to {capture_dir}"
            )

    def _stop_auto_capture(self):
        if self._auto_timer is not None:
            self._auto_timer.stop()
            self._auto_timer.deleteLater()
            self._auto_timer = None
        self._auto_count = 0
        self._auto_btn.setText(f"Auto Capture ({AUTO_CAPTURE_COUNT})")
        self._capture_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Swap button
    # ------------------------------------------------------------------

    @Slot()
    def _on_swap(self):
        logger.info("Swap cameras triggered")
        self._manager.stop()
        self._manager.swap_cameras()

    @Slot()
    def _on_cameras_swapped(self):
        logger.info("Cameras swapped — restarting pipelines with new mapping")
        handles: list[int | None] = [None, None]
        for canvas_pos in range(2):
            widget = self._previews[canvas_pos]
            if isinstance(widget, _PreviewWidget):
                handles[canvas_pos] = int(widget.winId())
        results = self._manager.start(handles)
        started = sum(1 for r in results if r)
        self._status.setText(f"Cameras swapped — {started} camera(s) running")

    # ------------------------------------------------------------------
    # Preview frame handler — appsink fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _on_preview_frame(label: QLabel, image: QImage):
        pixmap = QPixmap.fromImage(image)
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    # ------------------------------------------------------------------
    # Error / EOS feedback
    # ------------------------------------------------------------------

    @Slot(int, str)
    def _on_camera_error(self, cam_idx: int, msg: str):
        logger.error("Camera {} error: {}", cam_idx, msg)
        self._status.setText(f"Cam {cam_idx} error: {msg}")
        widget = self._previews[cam_idx]
        if isinstance(widget, QLabel):
            widget.setText(f"Camera {cam_idx} error:\n{msg}")

    @Slot(int)
    def _on_camera_eos(self, cam_idx: int):
        logger.warning("Camera {} EOS", cam_idx)
        self._status.setText(f"Cam {cam_idx}: stream ended (EOS)")
