#!/usr/bin/env python3
"""Side-by-side A/D image pair comparison viewer with zoom, pan, and thumbnail navigation."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_PAIR_RE = re.compile(r"^([AD])_(.+?)(\.\w+)$")

THUMB_W, THUMB_H = 120, 90


@dataclass
class ImagePair:
    timestamp: str
    left_path: str
    right_path: str


# ---------------------------------------------------------------------------
# Pair scanning
# ---------------------------------------------------------------------------


class PairScanner:
    """Find A_/D_ image pairs in a directory."""

    @staticmethod
    def scan_folder(directory: str) -> list[ImagePair]:
        d = Path(directory)
        if not d.is_dir():
            logger.warning("Not a directory: {}", directory)
            return []

        # Collect all A_ and D_ files keyed by timestamp
        a_files: dict[str, Path] = {}
        d_files: dict[str, Path] = {}
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = _PAIR_RE.match(f.name)
            if not m:
                continue
            prefix, ts, _ext = m.groups()
            if prefix == "A":
                a_files[ts] = f
            else:
                d_files[ts] = f

        pairs: list[ImagePair] = []
        for ts in sorted(a_files.keys()):
            if ts in d_files:
                pairs.append(ImagePair(ts, str(a_files[ts]), str(d_files[ts])))
            else:
                logger.warning("Orphan A file (no D match): {}", a_files[ts].name)

        for ts in sorted(d_files.keys()):
            if ts not in a_files:
                logger.warning("Orphan D file (no A match): {}", d_files[ts].name)

        logger.info("Found {} image pairs in {}", len(pairs), directory)
        return pairs

    @staticmethod
    def infer_from_file(file_path: str) -> tuple[list[ImagePair], int]:
        """Scan the parent folder and return (all_pairs, index_of_selected)."""
        p = Path(file_path)
        m = _PAIR_RE.match(p.name)
        if not m:
            logger.warning("File does not match A_/D_ pattern: {}", p.name)
            return [], -1

        _prefix, ts, _ext = m.groups()
        pairs = PairScanner.scan_folder(str(p.parent))
        for i, pair in enumerate(pairs):
            if pair.timestamp == ts:
                return pairs, i
        return pairs, 0


# ---------------------------------------------------------------------------
# Zoom + Pan graphics view
# ---------------------------------------------------------------------------


class ZoomPanGraphicsView(QGraphicsView):
    """QGraphicsView with mouse-wheel zoom and click-drag pan."""

    _MIN_SCALE = 0.05
    _MAX_SCALE = 50.0
    _ZOOM_FACTOR = 1.25

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._current_scale = 1.0

        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(self.renderHints().SmoothPixmapTransform, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)

    def set_image(self, path: str) -> None:
        pix = QPixmap(path)
        if pix.isNull():
            logger.warning("Failed to load image: {}", path)
            return
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pix)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self.resetTransform()
        self._current_scale = 1.0
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        # Record the effective scale after fitInView
        self._current_scale = self.transform().m11()

    def clear_image(self) -> None:
        self._scene.clear()
        self._pixmap_item = None

    def wheelEvent(self, event):  # noqa: N802
        if self._pixmap_item is None:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta > 0:
            factor = self._ZOOM_FACTOR
        elif delta < 0:
            factor = 1.0 / self._ZOOM_FACTOR
        else:
            event.ignore()
            return

        new_scale = self._current_scale * factor
        if new_scale < self._MIN_SCALE or new_scale > self._MAX_SCALE:
            event.accept()
            return

        self.scale(factor, factor)
        self._current_scale = new_scale
        event.accept()


# ---------------------------------------------------------------------------
# Thumbnail loader (background)
# ---------------------------------------------------------------------------


class _ThumbnailSignals(QObject):
    loaded = Signal(int, QPixmap)  # (index, thumbnail)


class ThumbnailLoader(QRunnable):
    def __init__(self, index: int, path: str, size: QSize):
        super().__init__()
        self.signals = _ThumbnailSignals()
        self._index = index
        self._path = path
        self._size = size
        self.setAutoDelete(True)

    def run(self):
        pix = QPixmap(self._path)
        if pix.isNull():
            return
        thumb = pix.scaled(self._size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.signals.loaded.emit(self._index, thumb)


# ---------------------------------------------------------------------------
# Thumbnail navigation bar
# ---------------------------------------------------------------------------


class ThumbnailBar(QWidget):
    pair_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._labels: list[QLabel] = []
        self._current = -1

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(THUMB_H + 30)

        self._container = QWidget()
        self._layout = QHBoxLayout(self._container)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        self._scroll.setWidget(self._container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)
        self.setFixedHeight(THUMB_H + 34)

    def set_pairs(self, pairs: list[ImagePair]) -> None:
        # Clear existing
        for lbl in self._labels:
            self._layout.removeWidget(lbl)
            lbl.deleteLater()
        self._labels.clear()
        self._current = -1

        for i, pair in enumerate(pairs):
            lbl = QLabel()
            lbl.setFixedSize(THUMB_W, THUMB_H)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background: #555; border: 2px solid transparent;")
            lbl.setToolTip(pair.timestamp)
            # Click handler via mousePressEvent
            lbl.mousePressEvent = lambda _ev, idx=i: self.pair_selected.emit(idx)
            self._layout.addWidget(lbl)
            self._labels.append(lbl)

        # Kick off async thumbnail loading
        thumb_size = QSize(THUMB_W, THUMB_H)
        pool = QThreadPool.globalInstance()
        for i, pair in enumerate(pairs):
            loader = ThumbnailLoader(i, pair.left_path, thumb_size)
            loader.signals.loaded.connect(self._on_thumb_loaded)
            pool.start(loader)

    def _on_thumb_loaded(self, index: int, pixmap: QPixmap) -> None:
        if 0 <= index < len(self._labels):
            self._labels[index].setPixmap(pixmap)

    def set_current(self, index: int) -> None:
        if self._current >= 0 and self._current < len(self._labels):
            self._labels[self._current].setStyleSheet(
                "background: #555; border: 2px solid transparent;"
            )
        if 0 <= index < len(self._labels):
            self._labels[index].setStyleSheet(
                "background: #555; border: 2px solid #00bcd4;"
            )
            self._scroll.ensureWidgetVisible(self._labels[index])
        self._current = index


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class CompareWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Compare — A / D Pairs")
        self.resize(1400, 800)

        self._pairs: list[ImagePair] = []
        self._current_index = -1
        self._last_dir = os.path.expanduser("~/captures")

        # --- Central widget ---
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Labels
        labels_row = QHBoxLayout()
        lbl_left = QLabel("Left (A)")
        lbl_left.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_right = QLabel("Right (D)")
        lbl_right.setAlignment(Qt.AlignmentFlag.AlignCenter)
        labels_row.addWidget(lbl_left)
        labels_row.addWidget(lbl_right)
        root.addLayout(labels_row)

        # Graphics views
        views_row = QHBoxLayout()
        self._left_view = ZoomPanGraphicsView()
        self._right_view = ZoomPanGraphicsView()
        views_row.addWidget(self._left_view)
        views_row.addWidget(self._right_view)
        root.addLayout(views_row, stretch=1)

        # Thumbnail bar
        self._thumb_bar = ThumbnailBar()
        self._thumb_bar.pair_selected.connect(self._load_pair)
        root.addWidget(self._thumb_bar)

        # --- Status bar ---
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Open a folder or image to begin")

        # --- Menu ---
        menu = self.menuBar().addMenu("&File")
        act_folder = QAction("Open &Folder…", self)
        act_folder.setShortcut(QKeySequence("Ctrl+O"))
        act_folder.triggered.connect(self._open_folder)
        menu.addAction(act_folder)

        act_image = QAction("Open &Image…", self)
        act_image.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_image.triggered.connect(self._open_image)
        menu.addAction(act_image)

        menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

    # --- File open ---

    def _open_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open Capture Folder", self._last_dir)
        if not d:
            return
        self._last_dir = d
        pairs = PairScanner.scan_folder(d)
        self._set_pairs(pairs)

    def _open_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Image", self._last_dir, "Images (*.jpg *.jpeg *.png *.bmp)"
        )
        if not path:
            return
        self._last_dir = str(Path(path).parent)
        pairs, idx = PairScanner.infer_from_file(path)
        self._set_pairs(pairs, idx)

    def _set_pairs(self, pairs: list[ImagePair], start_index: int = 0) -> None:
        self._pairs = pairs
        self._current_index = -1
        self._thumb_bar.set_pairs(pairs)
        if pairs:
            self._load_pair(start_index)
        else:
            self._left_view.clear_image()
            self._right_view.clear_image()
            self._status.showMessage("No image pairs found")

    # --- Navigation ---

    def _load_pair(self, index: int) -> None:
        if not self._pairs or index < 0 or index >= len(self._pairs):
            return
        self._current_index = index
        pair = self._pairs[index]
        self._left_view.set_image(pair.left_path)
        self._right_view.set_image(pair.right_path)
        self._thumb_bar.set_current(index)
        self._status.showMessage(
            f"Pair {index + 1} of {len(self._pairs)}  |  {pair.timestamp}"
        )

    def _go_prev(self) -> None:
        if self._current_index > 0:
            self._load_pair(self._current_index - 1)

    def _go_next(self) -> None:
        if self._current_index < len(self._pairs) - 1:
            self._load_pair(self._current_index + 1)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    # --- Open path from CLI ---

    def open_path(self, path: str) -> None:
        p = Path(path)
        if p.is_dir():
            self._last_dir = str(p)
            pairs = PairScanner.scan_folder(str(p))
            self._set_pairs(pairs)
        elif p.is_file():
            self._last_dir = str(p.parent)
            pairs, idx = PairScanner.infer_from_file(str(p))
            self._set_pairs(pairs, idx)
        else:
            logger.error("Path does not exist: {}", path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="A/D image pair comparison viewer")
    parser.add_argument("path", nargs="?", default=None, help="Folder or image file to open")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = CompareWindow()
    if args.path:
        window.open_path(args.path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
