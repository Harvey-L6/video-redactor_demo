"""Desktop video editor for manual masks, keyframes, and trimming."""

import os
import sys
import tempfile
import traceback
from copy import deepcopy
from pathlib import Path

import cv2
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal, QItemSelectionModel
from PySide6.QtGui import QBrush, QColor, QImage, QPen, QPixmap, QPainter, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsScene,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QAbstractItemView,
    QMessageBox,
    QProgressBar,
    QTabWidget,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
    QSplitter,
    QScrollArea,
    QComboBox,
)

from models import Mask
from edit_engine import EditPlan
from video_processor import process_video


class EditableMaskItem(QGraphicsRectItem):
    """Scene-pixel rectangle. Move and resize are constrained to the video.

    on_geometry_changed writes geometry back to the normalized Mask,
    keeping preview and export coordinates consistent.
    """

    HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

    def __init__(self, mask, frame_rect, video_bounds, on_geometry_changed,
                 on_edit_start):
        super().__init__(QRectF(0, 0, frame_rect.width(), frame_rect.height()))
        # Avoid the default rect item's 1px pen altering its boundingRect,
        # which would otherwise introduce error on each drag / resize.
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.mask = mask
        self.video_bounds = QRectF(video_bounds)
        self.on_geometry_changed = on_geometry_changed
        self.on_edit_start = on_edit_start
        self.setPos(frame_rect.topLeft())
        self.setZValue(1)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self._resize_handle = None
        self._resize_start = None
        self._move_start = None
        self.active_now = True

    def _zoom(self):
        scene = self.scene()
        if scene is not None and scene.views():
            return max(0.001, abs(scene.views()[0].transform().m11()))
        return 1.0

    def _handle_size(self):
        rect = self.rect()
        return max(0.5, min(10.0 / self._zoom(), rect.width() / 3, rect.height() / 3))

    def _handle_rects(self):
        r = self.rect()
        s = self._handle_size()
        points = {
            "nw": (r.left(), r.top()),
            "n": (r.center().x(), r.top()),
            "ne": (r.right(), r.top()),
            "e": (r.right(), r.center().y()),
            "se": (r.right(), r.bottom()),
            "s": (r.center().x(), r.bottom()),
            "sw": (r.left(), r.bottom()),
            "w": (r.left(), r.center().y()),
        }
        # Handles stay entirely inside the bounding rect (no repaint clipping).
        result = {}
        for name, (x, y) in points.items():
            cx = min(max(x, r.left() + s / 2), r.right() - s / 2)
            cy = min(max(y, r.top() + s / 2), r.bottom() - s / 2)
            result[name] = QRectF(cx - s / 2, cy - s / 2, s, s)
        return result

    def _hit_handle(self, local_point):
        if not self.isSelected():
            return None
        # Prefer corners when handles touch on small rectangles.
        order = ("nw", "ne", "se", "sw", "n", "e", "s", "w")
        for name in order:
            if self._handle_rects()[name].contains(local_point):
                return name
        return None

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.active_now:
            pen = QPen(QColor("#F59E0B"), 0, Qt.PenStyle.DashLine)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self.isSelected():
            pen = QPen(QColor("#FACC15"), 0)
            painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
        else:
            pen = QPen(QColor("#93C5FD"), 0)
            painter.setBrush(QBrush(QColor(0, 0, 0, 150)))
        painter.setPen(pen)
        painter.drawRect(self.rect())

        if self.isSelected():
            painter.setPen(QPen(QColor("#1F2937"), 0))
            painter.setBrush(QBrush(QColor("#FACC15")))
            for handle in self._handle_rects().values():
                painter.drawRect(handle)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            r = self.rect()
            b = self.video_bounds
            return QPointF(
                max(b.left(), min(value.x(), b.right() - r.width())),
                max(b.top(), min(value.y(), b.bottom() - r.height())),
            )
        return super().itemChange(change, value)

    def hoverMoveEvent(self, event):
        cursors = {
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor,
            "w": Qt.CursorShape.SizeHorCursor,
        }
        h = self._hit_handle(event.pos())
        self.setCursor(cursors.get(h, Qt.CursorShape.SizeAllCursor))
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.on_edit_start()  # Pause playback before moving or resizing.
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.setSelected(not self.isSelected())
                event.accept()
                return
            if self.scene() is not None:
                for other in self.scene().selectedItems():
                    if other is not self:
                        other.setSelected(False)
            self.setSelected(True)
            handle = self._hit_handle(event.pos())
            if handle is not None:
                self._resize_handle = handle
                absolute_rect = QRectF(
                    self.pos().x(), self.pos().y(),
                    self.rect().width(), self.rect().height(),
                )
                self._resize_start = (absolute_rect, QPointF(event.scenePos()))
            else:
                self._move_start = (QPointF(self.pos()), QPointF(event.scenePos()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._move_start is not None:
            original, start_mouse = self._move_start
            self.setPos(original + event.scenePos() - start_mouse)
            event.accept()
            return
        if self._resize_handle is None:
            super().mouseMoveEvent(event)
            return

        original, start_mouse = self._resize_start
        dx = event.scenePos().x() - start_mouse.x()
        dy = event.scenePos().y() - start_mouse.y()
        b = self.video_bounds
        min_size = 5.0
        left, top, right, bottom = (
            original.left(), original.top(), original.right(), original.bottom()
        )
        h = self._resize_handle
        if "w" in h:
            left = max(b.left(), min(original.left() + dx, right - min_size))
        if "e" in h:
            right = min(b.right(), max(original.right() + dx, left + min_size))
        if "n" in h:
            top = max(b.top(), min(original.top() + dy, bottom - min_size))
        if "s" in h:
            bottom = min(b.bottom(), max(original.bottom() + dy, top + min_size))
        self.setRect(QRectF(0, 0, right - left, bottom - top))
        self.setPos(left, top)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._resize_handle is not None:
            self._resize_handle = None
            self._resize_start = None
            self.on_geometry_changed(self)
            event.accept()
            return
        if self._move_start is not None:
            self._move_start = None
            self.on_geometry_changed(self)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class VideoView(QGraphicsView):
    """Scalable scene. Coordinates stay in original video pixel space."""
    mask_drawn = Signal(float, float, float, float)

    def __init__(self):
        super().__init__()
        self.drawing_enabled = False
        self.start_point = None
        self.temp_rect = None
        self.zoom_mode = "fit"
        self._panning = False
        self._pan_from = None
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    def fit_video(self):
        if self.scene() is None or self.sceneRect().isEmpty():
            return
        self.zoom_mode = "fit"
        self.resetTransform()
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_zoom(self, factor):
        if self.sceneRect().isEmpty():
            return
        self.zoom_mode = "manual"
        self.resetTransform()
        self.scale(factor, factor)  # 1.0 means original video pixels at 100% OS scale

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.zoom_mode == "fit":
            self.fit_video()

    def wheelEvent(self, event):
        if self.sceneRect().isEmpty():
            return super().wheelEvent(event)
        steps = event.angleDelta().y() / 120
        if steps:
            current = self.transform().m11()
            target = max(0.15, min(8.0, current * (1.2 ** steps)))
            self.zoom_mode = "manual"
            self.scale(target / current, target / current)
            event.accept()

    def set_drawing_enabled(self, enabled):
        self.drawing_enabled = enabled
        self.setCursor(
            Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        if not enabled:
            self.cancel_drawing()

    def cancel_drawing(self):
        if self.temp_rect is not None and self.scene() is not None:
            self.scene().removeItem(self.temp_rect)
        self.temp_rect = None
        self.start_point = None

    def get_scene_point(self, event):
        point = self.mapToScene(event.position().toPoint())
        bounds = self.sceneRect()
        return QPointF(
            max(bounds.left(), min(point.x(), bounds.right())),
            max(bounds.top(), min(point.y(), bounds.bottom())),
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_from = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if (
            self.drawing_enabled
            and event.button() == Qt.MouseButton.LeftButton
            and not self.sceneRect().isEmpty()
        ):
            # Even during continuous drawing, existing masks remain editable.
            # Begin a new rectangle only when pressing outside existing masks.
            if isinstance(self.itemAt(event.position().toPoint()), EditableMaskItem):
                return super().mousePressEvent(event)
            raw_point = self.mapToScene(event.position().toPoint())
            if not self.sceneRect().contains(raw_point):
                return
            self.start_point = raw_point
            pen = QPen(QColor("yellow"))
            pen.setStyle(Qt.PenStyle.DashLine)
            self.temp_rect = self.scene().addRect(
                QRectF(raw_point, raw_point), pen,
                QBrush(QColor(0, 0, 0, 120)),
            )
            self.temp_rect.setZValue(2)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_from is not None:
            point = event.position().toPoint()
            delta = point - self._pan_from
            self._pan_from = point
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return
        if self.start_point is not None and self.temp_rect is not None:
            self.temp_rect.setRect(QRectF(
                self.start_point, self.get_scene_point(event)
            ).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self._pan_from = None
            self.setCursor(
                Qt.CursorShape.CrossCursor if self.drawing_enabled
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        if (
            self.start_point is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            rect = QRectF(self.start_point, self.get_scene_point(event)).normalized()
            self.cancel_drawing()
            if rect.width() >= 5 and rect.height() >= 5:
                self.mask_drawn.emit(rect.x(), rect.y(), rect.width(), rect.height())
            event.accept()
            return
        super().mouseReleaseEvent(event)


class EditRangeBar(QWidget):
    """Shows retained time sections below the original-video seek slider."""

    def __init__(self):
        super().__init__()
        self.total_seconds = 0.0
        self.keep_ranges = []
        self.setFixedHeight(14)
        self.setToolTip("藍色：輸出保留；灰色：不輸出（原始影片時間軸）")

    def set_ranges(self, duration, keep_ranges):
        self.total_seconds = duration
        self.keep_ranges = list(keep_ranges)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(5, 2, -5, -2)
        painter.fillRect(rect, QColor("#CBD5E1"))  # excluded
        if self.total_seconds > 0:
            for a, b in self.keep_ranges:
                left = rect.left() + round(rect.width() * a / self.total_seconds)
                right = rect.left() + round(rect.width() * b / self.total_seconds)
                if right > left:
                    painter.fillRect(
                        left, rect.top(), right - left, rect.height(),
                        QColor("#3B82F6")
                    )
        painter.end()


class ExportWorker(QThread):
    """Process video off the GUI thread; replace destination only on success."""

    def __init__(
        self, input_path: Path, output_path: Path, masks: list[Mask],
        edit_plan: EditPlan
    ):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.masks = deepcopy(masks)  # includes independent keyframe data
        self.edit_plan = EditPlan(
            keep_start=edit_plan.keep_start,
            keep_end=edit_plan.keep_end,
            deleted_ranges=list(edit_plan.deleted_ranges),
        )
        self.error = None

    def run(self):
        temporary_output = None
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=".redactor_",
                suffix=".mp4",
                dir=str(self.output_path.parent),
            )
            os.close(fd)
            temporary_output = Path(temp_name)

            process_video(
                input_path=self.input_path,
                output_path=temporary_output,
                masks=self.masks,
                edit_plan=self.edit_plan,
            )

            if not temporary_output.exists() or temporary_output.stat().st_size == 0:
                raise RuntimeError("輸出影片為空，請檢查編碼器設定")

            # 先確認真的能解出影片影格，避免覆蓋既有成品。
            check = cv2.VideoCapture(str(temporary_output))
            try:
                if not check.isOpened() or not check.read()[0]:
                    raise RuntimeError("輸出檔沒有可讀取的畫面，已保留原本的成品")
            finally:
                check.release()

            # A successful render atomically replaces a prior output, if any.
            os.replace(temporary_output, self.output_path)
        except Exception:
            self.error = traceback.format_exc()
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)


class VideoRedactorGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Redactor")
        self.resize(1480, 840)

        self.cap = None
        self.input_path = None
        self.fps = 0.0
        self.frame_count = 0
        self.current_frame = -1
        self.video_width = 0
        self.video_height = 0
        self.resume_after_drag = False

        self.masks = []
        self.mask_items = []
        self.next_mask_id = 1
        self._selection_syncing = False
        self.deleted_ranges = []
        self.export_worker = None
        self.exporting = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        self.open_btn = QPushButton("選擇影片")
        self.open_btn.clicked.connect(self.open_video)
        self.file_label = QLabel("尚未選擇影片")
        top.addWidget(self.open_btn)
        top.addWidget(self.file_label, 1)
        layout.addLayout(top)

        self.scene = QGraphicsScene(self)
        self.view = VideoView()
        self.view.setScene(self.scene)
        self.view.setBackgroundBrush(QBrush(Qt.GlobalColor.black))
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.frame_item = self.scene.addPixmap(QPixmap())
        self.frame_item.setZValue(0)
        self.view.mask_drawn.connect(self.add_mask)
        self.scene.selectionChanged.connect(self.on_scene_selection_changed)
        # Left: the video editor; right: independently scrollable settings.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 4, 0)
        preview_layout.addWidget(self.view, 1)
        self.splitter.addWidget(preview_panel)
        self.preview_layout = preview_layout

        timeline = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderMoved.connect(self.seek_frame)
        self.slider.sliderReleased.connect(self.on_slider_released)
        self.time_label = QLabel("00:00.0 / 00:00.0")
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.time_label)
        preview_layout.addLayout(timeline)

        self.range_bar = EditRangeBar()
        preview_layout.addWidget(self.range_bar)
        self.range_status = QLabel("藍色為保留區段，灰色為刪除區段（原始時間軸）")
        preview_layout.addWidget(self.range_status)

        controls = QHBoxLayout()
        self.prev_btn = QPushButton("上一幀")
        self.play_btn = QPushButton("播放")
        self.next_btn = QPushButton("下一幀")
        self.prev_btn.clicked.connect(lambda: self.step_frame(-1))
        self.play_btn.clicked.connect(self.toggle_play)
        self.next_btn.clicked.connect(lambda: self.step_frame(1))
        for btn in (self.prev_btn, self.play_btn, self.next_btn):
            btn.setEnabled(False)
            controls.addWidget(btn)
        preview_layout.addLayout(controls)

        zoom_row = QHBoxLayout()
        self.fit_btn = QPushButton("適應視窗")
        self.zoom_100_btn = QPushButton("100%")
        self.zoom_200_btn = QPushButton("200%")
        self.fit_btn.clicked.connect(self.view.fit_video)
        self.zoom_100_btn.clicked.connect(lambda: self.view.set_zoom(1.0))
        self.zoom_200_btn.clicked.connect(lambda: self.view.set_zoom(2.0))
        zoom_row.addWidget(self.fit_btn)
        zoom_row.addWidget(self.zoom_100_btn)
        zoom_row.addWidget(self.zoom_200_btn)
        zoom_row.addWidget(QLabel("滾輪縮放 · 中鍵拖曳平移"), 1)
        preview_layout.addLayout(zoom_row)

        self.edit_tabs = QTabWidget()
        mask_page = QWidget()
        mask_layout = QVBoxLayout(mask_page)
        crop_page = QWidget()
        crop_layout = QVBoxLayout(crop_page)
        self.edit_tabs.addTab(mask_page, "隱私遮罩")
        self.edit_tabs.addTab(crop_page, "影片裁剪")
        self.edit_tabs.setEnabled(False)
        settings_panel = QWidget()
        settings_layout = QVBoxLayout(settings_panel)
        settings_layout.setContentsMargins(4, 0, 0, 0)
        settings_layout.addWidget(self.edit_tabs)
        settings_scroller = QScrollArea()
        settings_scroller.setWidgetResizable(True)
        settings_scroller.setWidget(settings_panel)
        settings_scroller.setMinimumWidth(330)
        self.splitter.addWidget(settings_scroller)
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([990, 430])
        layout.addWidget(self.splitter, 1)

        mask_layout.addWidget(QLabel("遮罩清單（Ctrl／Shift 多選，可批次修改）"))
        self.mask_list = QListWidget()
        self.mask_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.mask_list.setMinimumHeight(155)
        self.mask_list.itemSelectionChanged.connect(self.on_list_selection_changed)
        mask_layout.addWidget(self.mask_list)

        selection_row = QHBoxLayout()
        self.select_all_masks_btn = QPushButton("全選遮罩")
        self.clear_selection_btn = QPushButton("取消選取")
        self.select_all_masks_btn.clicked.connect(self.mask_list.selectAll)
        self.clear_selection_btn.clicked.connect(self.mask_list.clearSelection)
        selection_row.addWidget(self.select_all_masks_btn)
        selection_row.addWidget(self.clear_selection_btn)
        mask_layout.addLayout(selection_row)
        self.selection_hint = QLabel("點擊黑框或清單選取；拖曳移動，拉控制點縮放。")
        self.selection_hint.setWordWrap(True)
        mask_layout.addWidget(self.selection_hint)

        mask_controls = QHBoxLayout()
        self.add_mask_btn = QPushButton("新增遮罩（框選）")
        self.add_mask_btn.setCheckable(True)
        self.add_mask_btn.setEnabled(False)
        self.add_mask_btn.toggled.connect(self.toggle_mask_drawing)
        self.edit_tabs.currentChanged.connect(self.on_edit_tab_changed)
        self.delete_mask_btn = QPushButton("刪除選取遮罩（Del）")
        self.delete_mask_btn.clicked.connect(self.delete_selected_mask)
        self.delete_mask_btn.setEnabled(False)
        self.add_mask_btn.setText("開始連續框選")
        mask_controls.addWidget(self.add_mask_btn)
        mask_controls.addWidget(self.delete_mask_btn)
        mask_layout.addLayout(mask_controls)

        time_controls = QVBoxLayout()
        self.start_spin = QDoubleSpinBox()
        self.end_spin = QDoubleSpinBox()
        for spin in (self.start_spin, self.end_spin):
            spin.setDecimals(2)
            spin.setRange(0, 0)
            spin.setSingleStep(0.1)
            spin.setSuffix(" 秒")
            spin.setEnabled(False)

        self.start_now_btn = QPushButton("目前→起點")
        self.end_now_btn = QPushButton("目前→終點")
        self.start_now_btn.clicked.connect(
            lambda: self.start_spin.setValue(self.current_time())
        )
        self.end_now_btn.clicked.connect(
            lambda: self.end_spin.setValue(self.current_time())
        )
        self.apply_time_btn = QPushButton("套用至所選遮罩")
        self.apply_time_btn.clicked.connect(self.apply_mask_times)
        for btn in (self.start_now_btn, self.end_now_btn, self.apply_time_btn):
            btn.setEnabled(False)

        for label, spin, btn in (
            ("開始", self.start_spin, self.start_now_btn),
            ("結束", self.end_spin, self.end_now_btn),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, 1)
            row.addWidget(btn)
            time_controls.addLayout(row)
        time_controls.addWidget(self.apply_time_btn)
        mask_layout.addLayout(time_controls)
        time_hint = QLabel("時間以原始影片為準；多選後按「套用至所選遮罩」批次修改，未指定時預設全片有效。")
        time_hint.setWordWrap(True)
        mask_layout.addWidget(time_hint)

        # 選取單一遮罩後，記錄原始時間軸上的位置點。
        mask_layout.addWidget(QLabel("移動遮罩（關鍵影格）"))
        self.keyframe_hint = QLabel(
            "先選取一個遮罩，按「啟用／新增位置點」。之後在其他時間拖曳或縮放，會自動記錄新位置。"
        )
        self.keyframe_hint.setWordWrap(True)
        mask_layout.addWidget(self.keyframe_hint)
        self.keyframe_list = QListWidget()
        self.keyframe_list.setMinimumHeight(100)
        self.keyframe_list.itemDoubleClicked.connect(self.jump_to_keyframe)
        self.keyframe_list.itemSelectionChanged.connect(self.update_keyframe_delete_enabled)
        mask_layout.addWidget(self.keyframe_list)
        position_row = QHBoxLayout()
        self.add_keyframe_btn = QPushButton("啟用／新增位置點")
        self.add_keyframe_btn.clicked.connect(self.add_current_keyframe)
        self.remove_keyframe_btn = QPushButton("刪除所選位置點")
        self.remove_keyframe_btn.clicked.connect(self.remove_selected_keyframe)
        position_row.addWidget(self.add_keyframe_btn)
        position_row.addWidget(self.remove_keyframe_btn)
        mask_layout.addLayout(position_row)
        interpolation_row = QHBoxLayout()
        interpolation_row.addWidget(QLabel("位置變化"))
        self.interpolation_combo = QComboBox()
        self.interpolation_combo.addItem("等速移動（線性）", "linear")
        self.interpolation_combo.addItem("停留後瞬間跳轉", "hold")
        self.interpolation_combo.currentIndexChanged.connect(self.on_interpolation_changed)
        interpolation_row.addWidget(self.interpolation_combo, 1)
        mask_layout.addLayout(interpolation_row)
        self.clear_keyframes_btn = QPushButton("轉回固定遮罩（清除所有位置點）")
        self.clear_keyframes_btn.clicked.connect(self.clear_selected_keyframes)
        mask_layout.addWidget(self.clear_keyframes_btn)

        # 裁剪分頁：每個時間都以原始影片為準，改動立即生效。
        crop_layout.addWidget(QLabel("保留起訖時間（其餘前後片段自動裁掉）"))
        trim_row = QVBoxLayout()
        self.trim_start = self.make_time_spin()
        self.trim_end = self.make_time_spin()
        self.trim_start.valueChanged.connect(self.update_edit_preview)
        self.trim_end.valueChanged.connect(self.update_edit_preview)
        self.trim_now_start = QPushButton("目前→起點")
        self.trim_now_end = QPushButton("目前→終點")
        self.trim_now_start.clicked.connect(
            lambda: self.trim_start.setValue(self.current_time())
        )
        self.trim_now_end.clicked.connect(
            lambda: self.trim_end.setValue(self.current_time())
        )
        for label, spin, btn in (
            ("從", self.trim_start, self.trim_now_start),
            ("到", self.trim_end, self.trim_now_end),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, 1)
            row.addWidget(btn)
            trim_row.addLayout(row)
        crop_layout.addLayout(trim_row)

        crop_layout.addWidget(QLabel("中間刪除區段（可新增多段）"))
        delete_row = QVBoxLayout()
        self.delete_start = self.make_time_spin()
        self.delete_end = self.make_time_spin()
        self.delete_now_start = QPushButton("目前→起點")
        self.delete_now_end = QPushButton("目前→終點")
        self.delete_now_start.clicked.connect(
            lambda: self.delete_start.setValue(self.current_time())
        )
        self.delete_now_end.clicked.connect(
            lambda: self.delete_end.setValue(self.current_time())
        )
        self.add_delete_btn = QPushButton("新增刪除區段")
        self.add_delete_btn.clicked.connect(self.add_delete_range)
        for label, spin, btn in (
            ("從", self.delete_start, self.delete_now_start),
            ("到", self.delete_end, self.delete_now_end),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, 1)
            row.addWidget(btn)
            delete_row.addLayout(row)
        delete_row.addWidget(self.add_delete_btn)
        crop_layout.addLayout(delete_row)

        self.deleted_list = QListWidget()
        self.deleted_list.setMinimumHeight(135)
        crop_layout.addWidget(self.deleted_list)
        self.remove_delete_btn = QPushButton("移除選取的刪除區段")
        self.remove_delete_btn.clicked.connect(self.remove_delete_range)
        crop_layout.addWidget(self.remove_delete_btn)
        crop_layout.addWidget(QLabel("提示：預覽仍是原始影片；灰色時間段不會匯出。"))

        export_row = QHBoxLayout()
        self.export_status = QLabel("目前尚未匯出")
        self.export_btn = QPushButton("匯出無聲 MP4")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_video)
        export_row.addWidget(self.export_status, 1)
        export_row.addWidget(self.export_btn)
        settings_layout.addLayout(export_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Busy indicator (no percent yet)
        self.progress.setVisible(False)
        settings_layout.addWidget(self.progress)
        self.esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.esc_shortcut.activated.connect(
            lambda: self.add_mask_btn.setChecked(False)
        )
        # Limit Del to canvas and mask list. Delete inside time inputs should
        # continue to edit the numeric field instead of removing a mask.
        for widget in (self.view, self.mask_list):
            shortcut = QShortcut(QKeySequence("Delete"), widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.delete_selected_mask)
            if not hasattr(self, "delete_shortcuts"):
                self.delete_shortcuts = []
            self.delete_shortcuts.append(shortcut)

        self.refresh_selection_ui()

    @staticmethod
    def make_time_spin():
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(0.0, 0.0)
        spin.setSingleStep(0.1)
        spin.setSuffix(" 秒")
        return spin

    def current_time(self):
        if self.fps <= 0 or self.current_frame < 0:
            return 0.0
        return self.current_frame / self.fps

    def duration(self):
        if self.fps <= 0:
            return 0.0
        return self.frame_count / self.fps

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇影片", str(Path.home()), "MP4 影片 (*.mp4)"
        )
        if not path:
            return

        new_cap = cv2.VideoCapture(path)
        if not new_cap.isOpened():
            QMessageBox.warning(self, "錯誤", "無法開啟影片")
            return

        fps = float(new_cap.get(cv2.CAP_PROP_FPS))
        count = int(new_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if fps <= 0 or count <= 0 or width <= 0 or height <= 0:
            new_cap.release()
            QMessageBox.warning(self, "錯誤", "影片資訊無效")
            return

        if not new_cap.read()[0]:
            new_cap.release()
            QMessageBox.warning(self, "錯誤", "無法讀取影片第一幀")
            return

        self.set_playing(False)
        self.add_mask_btn.setChecked(False)
        self.view.cancel_drawing()
        if self.cap is not None:
            self.cap.release()

        self._selection_syncing = True
        try:
            for item in self.mask_items:
                self.scene.removeItem(item)
            self.masks.clear()
            self.mask_items.clear()
            self.mask_list.clear()
            self.next_mask_id = 1
        finally:
            self._selection_syncing = False
        self.refresh_selection_ui()
        self.deleted_ranges.clear()
        self.deleted_list.clear()

        self.cap = new_cap
        self.input_path = Path(path)
        self.fps = fps
        self.frame_count = count
        self.current_frame = -1
        self.video_width = width
        self.video_height = height
        self.timer.setInterval(max(1, round(1000 / fps)))

        self.file_label.setText(self.input_path.name)
        self.file_label.setToolTip(str(self.input_path))
        self.slider.setRange(0, count - 1)
        self.slider.setEnabled(True)
        self.add_mask_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.edit_tabs.setEnabled(True)
        self.export_status.setText("尚未匯出")
        for btn in (self.prev_btn, self.play_btn, self.next_btn):
            btn.setEnabled(True)
        for spin in (
            self.start_spin, self.end_spin,
            self.trim_start, self.trim_end,
            self.delete_start, self.delete_end,
        ):
            spin.blockSignals(True)
            spin.setRange(0.0, self.duration())
            spin.setValue(0.0)
            spin.setEnabled(True)
            spin.blockSignals(False)
        self.trim_end.setValue(self.duration())
        self.delete_end.setValue(self.duration())
        self.update_edit_preview()

        if not self.seek_frame(0):
            QMessageBox.warning(self, "錯誤", "無法定位到影片第一幀")

    def display_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        image = QImage(
            rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888
        ).copy()
        pixmap = QPixmap.fromImage(image)
        old_rect = self.scene.sceneRect()
        self.frame_item.setPixmap(pixmap)
        self.scene.setSceneRect(0, 0, w, h)
        if (old_rect.width() != w or old_rect.height() != h):
            self.view.fit_video()
        self.slider.setValue(self.current_frame)
        self.update_time_label()
        self.update_mask_visibility()
        self.update_edit_preview()

    def on_edit_tab_changed(self, index):
        # 裁剪分頁不應維持框選模式，避免誤加遮罩。
        if index != 0:
            self.add_mask_btn.setChecked(False)

    def toggle_mask_drawing(self, enabled):
        if enabled:
            self.set_playing(False)
        self.view.set_drawing_enabled(enabled)
        self.add_mask_btn.setText(
            "結束框選（Esc）" if enabled else "開始連續框選"
        )

    def add_mask(self, x, y, width, height):
        if self.cap is None or self.exporting:
            return
        mask = Mask(
            name=f"遮罩 {self.next_mask_id}",
            start_time=0.0,
            end_time=self.duration(),
            x=x / self.video_width,
            y=y / self.video_height,
            width=width / self.video_width,
            height=height / self.video_height,
        )
        self.next_mask_id += 1
        self.masks.append(mask)
        item = EditableMaskItem(
            mask,
            QRectF(x, y, width, height),
            QRectF(0, 0, self.video_width, self.video_height),
            self.on_mask_geometry_changed,
            lambda: self.set_playing(False),
        )
        self.scene.addItem(item)
        self.mask_items.append(item)
        self.mask_list.addItem(self.mask_label(mask))
        self._selection_syncing = True
        try:
            self.mask_list.clearSelection()
            item.setSelected(True)
            self.mask_list.item(len(self.masks) - 1).setSelected(True)
            self.mask_list.setCurrentRow(
                len(self.masks) - 1,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        finally:
            self._selection_syncing = False
        self.refresh_selection_ui()
        self.update_mask_visibility()
        # Remain in drawing mode until user clicks End or presses Esc.

    @staticmethod
    def mask_label(mask):
        mode = f"移動 {len(mask.keyframes)} 點" if mask.keyframes else "固定"
        return f"{mask.name}｜{mask.start_time:.2f}～{mask.end_time:.2f} 秒｜{mode}"

    def selected_mask_rows(self):
        return sorted(
            self.mask_list.row(item) for item in self.mask_list.selectedItems()
        )

    def on_list_selection_changed(self):
        if self._selection_syncing:
            return
        self._selection_syncing = True
        try:
            selected = set(self.selected_mask_rows())
            for i, item in enumerate(self.mask_items):
                # Inactive masks are normally hidden; reveal before selecting.
                if i in selected:
                    item.setVisible(True)
                item.setSelected(i in selected)
        finally:
            self._selection_syncing = False
        self.refresh_selection_ui()
        self.update_mask_visibility()
        if len(selected) == 1:
            self.view.ensureVisible(self.mask_items[next(iter(selected))], 12, 12)

    def on_scene_selection_changed(self):
        if self._selection_syncing:
            return
        self._selection_syncing = True
        try:
            selected = set(self.scene.selectedItems())
            selected_rows = []
            for i, item in enumerate(self.mask_items):
                self.mask_list.item(i).setSelected(item in selected)
                if item in selected:
                    selected_rows.append(i)
            if selected_rows:
                self.mask_list.setCurrentRow(
                    selected_rows[-1],
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._selection_syncing = False
        self.refresh_selection_ui()
        self.update_mask_visibility()

    def refresh_selection_ui(self):
        rows = self.selected_mask_rows()
        selected = bool(rows)
        for control in (
            self.start_spin,
            self.end_spin,
            self.start_now_btn,
            self.end_now_btn,
            self.apply_time_btn,
            self.delete_mask_btn,
        ):
            control.setEnabled(selected and not self.exporting)
        self.select_all_masks_btn.setEnabled(bool(self.masks) and not self.exporting)
        self.clear_selection_btn.setEnabled(selected and not self.exporting)
        self.mask_list.setEnabled(not self.exporting)
        single = len(rows) == 1
        for control in (
            self.keyframe_list, self.add_keyframe_btn, self.interpolation_combo,
            self.clear_keyframes_btn,
        ):
            control.setEnabled(single and not self.exporting)
        self.remove_keyframe_btn.setEnabled(
            single and self.keyframe_list.currentRow() >= 0 and not self.exporting
        )
        self.refresh_keyframe_ui()
        if rows:
            mask = self.masks[rows[0]]
            self.start_spin.setValue(mask.start_time)
            self.end_spin.setValue(mask.end_time)
            if len(rows) > 1:
                self.selection_hint.setText(
                    f"已選取 {len(rows)} 個遮罩（黃色控制點）。下方時間會套用至全部所選；"
                    "目前顯示第一個遮罩的時間，請確認後再套用。"
                )
            else:
                self.selection_hint.setText(
                    f"目前選取：{mask.name}。拖曳可移動，拉黃色控制點可調整大小；Del 刪除。"
                )
        else:
            self.selection_hint.setText("點擊黑框或右側清單選取；Ctrl／Shift 可多選。")

    def on_mask_geometry_changed(self, item):
        if item not in self.mask_items or self.video_width <= 0 or self.video_height <= 0:
            return
        mask = item.mask
        rect = QRectF(item.pos().x(), item.pos().y(), item.rect().width(), item.rect().height())
        x = max(0.0, min(1.0, rect.left() / self.video_width))
        y = max(0.0, min(1.0, rect.top() / self.video_height))
        w = max(0.0, min(1.0 - x, rect.width() / self.video_width))
        h = max(0.0, min(1.0 - y, rect.height() / self.video_height))
        if mask.keyframes:
            # 移動遮罩：只更改目前時間點，不影響其他影格的位置。
            now = self.current_time()
            if not mask.is_active(now):
                QMessageBox.information(
                    self, "時間範圍外", "目前時間不在遮罩作用區段內，無法建立位置點。"
                )
                self.update_mask_visibility()  # restore geometry
                return
            mask.set_keyframe(now, x, y, w, h)
            self.refresh_keyframe_ui()
        else:
            # 固定遮罩直接更新整段作用時間內使用的位置與大小。
            mask.x, mask.y, mask.width, mask.height = x, y, w, h
        self.update_mask_visibility()

    def selected_single_mask(self):
        rows = self.selected_mask_rows()
        return (rows[0], self.masks[rows[0]]) if len(rows) == 1 else (None, None)

    def refresh_keyframe_ui(self):
        row, mask = self.selected_single_mask()
        previous_time = None
        selected = self.keyframe_list.currentRow()
        if mask is not None and 0 <= selected < len(mask.keyframes):
            previous_time = mask.keyframes[selected].time
        self.keyframe_list.blockSignals(True)
        self.keyframe_list.clear()
        if mask is not None:
            for keyframe in sorted(mask.keyframes, key=lambda k: k.time):
                self.keyframe_list.addItem(f"{keyframe.time:.2f} 秒 · 位置／大小")
            if previous_time is not None:
                for i, frame in enumerate(mask.keyframes):
                    if abs(frame.time - previous_time) < 1e-6:
                        self.keyframe_list.setCurrentRow(i)
                        break
            index = self.interpolation_combo.findData(mask.interpolation)
            self.interpolation_combo.blockSignals(True)
            self.interpolation_combo.setCurrentIndex(max(index, 0))
            self.interpolation_combo.blockSignals(False)
            self.clear_keyframes_btn.setEnabled(bool(mask.keyframes) and not self.exporting)
            self.keyframe_hint.setText(
                "移動遮罩：跳到其他時間，直接拖曳／縮放便新增或更新位置點。"
                if mask.keyframes else
                "固定遮罩：按「啟用／新增位置點」，才會開始記錄各時間的位置。"
            )
        else:
            self.keyframe_hint.setText("請在畫面或清單選取一個遮罩，才能編輯移動位置。")
            self.clear_keyframes_btn.setEnabled(False)
        self.keyframe_list.blockSignals(False)
        self.remove_keyframe_btn.setEnabled(
            mask is not None and self.keyframe_list.currentRow() >= 0 and not self.exporting
        )

    def add_current_keyframe(self):
        row, mask = self.selected_single_mask()
        if mask is None or self.exporting:
            return
        self.set_playing(False)
        now = self.current_time()
        if not mask.is_active(now):
            QMessageBox.warning(self, "無法新增", "請先跳到遮罩的起訖時間範圍內。")
            return
        geometry = mask.geometry_at(now)
        if not mask.keyframes and now > mask.start_time:
            # Seed the start, so adding a later point never changes the past.
            mask.set_keyframe(mask.start_time, *geometry)
        mask.set_keyframe(now, *geometry)
        self.mask_list.item(row).setText(self.mask_label(mask))
        self.refresh_keyframe_ui()
        self.update_mask_visibility()

    def update_keyframe_delete_enabled(self):
        self.remove_keyframe_btn.setEnabled(
            self.selected_single_mask()[1] is not None
            and self.keyframe_list.currentRow() >= 0
            and not self.exporting
        )

    def remove_selected_keyframe(self):
        row, mask = self.selected_single_mask()
        if mask is None or self.exporting:
            return
        index = self.keyframe_list.currentRow()
        if not 0 <= index < len(mask.keyframes):
            return
        time = mask.keyframes[index].time
        visible_geometry = mask.geometry_at(self.current_time())
        mask.remove_keyframe(time)
        if not mask.keyframes:
            # The last keyframe was deleted: keep the currently visible shape.
            mask.x, mask.y, mask.width, mask.height = visible_geometry
        self.mask_list.item(row).setText(self.mask_label(mask))
        self.refresh_keyframe_ui()
        self.update_mask_visibility()

    def clear_selected_keyframes(self):
        row, mask = self.selected_single_mask()
        if mask is None or self.exporting or not mask.keyframes:
            return
        # Convert the geometry seen at this moment into a fixed rectangle.
        x, y, w, h = mask.geometry_at(self.current_time())
        mask.x, mask.y, mask.width, mask.height = x, y, w, h
        mask.keyframes.clear()
        self.mask_list.item(row).setText(self.mask_label(mask))
        self.refresh_keyframe_ui()
        self.update_mask_visibility()

    def jump_to_keyframe(self, item):
        _, mask = self.selected_single_mask()
        if mask is None:
            return
        index = self.keyframe_list.row(item)
        if 0 <= index < len(mask.keyframes):
            self.set_playing(False)
            self.seek_frame(round(mask.keyframes[index].time * self.fps))

    def on_interpolation_changed(self, _index):
        _, mask = self.selected_single_mask()
        if mask is not None and not self.exporting:
            mask.interpolation = self.interpolation_combo.currentData()
            self.update_mask_visibility()

    def apply_mask_times(self):
        rows = self.selected_mask_rows()
        if not rows:
            return False

        start = self.start_spin.value()
        end = self.end_spin.value()
        if start >= end:
            QMessageBox.warning(
                self, "時間設定錯誤", "遮罩結束時間必須晚於開始時間。"
            )
            return False

        # Do not silently discard or strand moving-mask keyframes.
        for index in rows:
            mask = self.masks[index]
            if any(not (start <= f.time <= end) for f in mask.keyframes):
                QMessageBox.warning(
                    self, "位置點超出範圍",
                    f"{mask.name} 的位置點位於新起訖時間之外。請先調整或刪除位置點。"
                )
                return False
        for index in rows:
            mask = self.masks[index]
            mask.start_time = start
            mask.end_time = end
            self.mask_list.item(index).setText(self.mask_label(mask))
        self.update_mask_visibility()
        return True

    def delete_selected_mask(self):
        if self.exporting:
            return
        rows = self.selected_mask_rows()
        if not rows:
            return
        self._selection_syncing = True
        try:
            for index in reversed(rows):
                self.masks.pop(index)
                item = self.mask_items.pop(index)
                self.scene.removeItem(item)
                self.mask_list.takeItem(index)
            if self.masks:
                index = min(rows[0], len(self.masks) - 1)
                self.mask_items[index].setSelected(True)
                self.mask_list.item(index).setSelected(True)
                self.mask_list.setCurrentRow(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._selection_syncing = False
        self.refresh_selection_ui()
        self.update_mask_visibility()

    def update_mask_visibility(self):
        if self.fps <= 0:
            return
        now = self.current_time()
        for mask, item in zip(self.masks, self.mask_items):
            # Evaluate the same geometry as the export engine, every frame.
            x, y, width, height = mask.get_pixel_rect_at(
                now, self.video_width, self.video_height
            )
            item.setRect(QRectF(0, 0, width, height))
            item.setPos(x, y)
            item.active_now = mask.is_active(now)
            # Inactive but selected masks are visible as edit-only outlines.
            item.setVisible(item.active_now or item.isSelected())
            item.update()

    # ========================================================
    # 原始時間軸裁剪：資料先記錄，最終匯出才真正刪除
    # ========================================================

    def build_edit_plan(self):
        plan = EditPlan(
            keep_start=self.trim_start.value(),
            keep_end=self.trim_end.value(),
            deleted_ranges=list(self.deleted_ranges),
        )
        return plan, plan.get_keep_ranges(self.duration())

    def update_edit_preview(self, *_args):
        if self.cap is None:
            return
        try:
            _, keep_ranges = self.build_edit_plan()
        except ValueError as exc:
            self.range_bar.set_ranges(self.duration(), [])
            self.range_status.setText(f"裁剪設定錯誤：{exc}")
            return

        retained = sum(end - start for start, end in keep_ranges)
        now = self.current_time()
        is_kept = any(start <= now < end for start, end in keep_ranges)
        message = "保留" if is_kept else "刪除（不匯出）"
        self.range_bar.set_ranges(self.duration(), keep_ranges)
        self.range_status.setText(
            f"目前畫面：{message} ｜ 預估輸出：約 {retained:.2f} 秒"
            " ｜ 藍色保留、灰色刪除（原始時間軸）"
        )

    def add_delete_range(self):
        if self.cap is None:
            return
        start = self.delete_start.value()
        end = self.delete_end.value()
        if start >= end:
            QMessageBox.warning(self, "時間設定錯誤", "刪除區段的終點必須晚於起點。")
            return
        self.deleted_ranges.append((start, end))
        self.deleted_list.addItem(f"刪除：{start:.2f}～{end:.2f} 秒")
        self.deleted_list.setCurrentRow(len(self.deleted_ranges) - 1)
        self.update_edit_preview()

    def remove_delete_range(self):
        index = self.deleted_list.currentRow()
        if not 0 <= index < len(self.deleted_ranges):
            return
        self.deleted_ranges.pop(index)
        self.deleted_list.takeItem(index)
        self.update_edit_preview()

    def seek_frame(self, index):
        if self.cap is None:
            return False
        index = max(0, min(int(index), self.frame_count - 1))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        success, frame = self.cap.read()
        if not success:
            return False
        self.current_frame = index
        self.display_frame(frame)
        return True

    def next_frame(self):
        if self.cap is None:
            return
        if self.current_frame >= self.frame_count - 1:
            self.set_playing(False)
            return
        success, frame = self.cap.read()
        if not success:
            self.set_playing(False)
            return
        self.current_frame += 1
        self.display_frame(frame)

    def set_playing(self, playing):
        if playing:
            if self.cap is None:
                return
            if self.current_frame >= self.frame_count - 1:
                self.seek_frame(0)
            self.timer.start()
            self.play_btn.setText("暫停")
        else:
            self.timer.stop()
            self.play_btn.setText("播放")

    def toggle_play(self):
        self.set_playing(not self.timer.isActive())

    def step_frame(self, offset):
        self.set_playing(False)
        if self.cap is not None:
            self.seek_frame(self.current_frame + offset)

    def on_slider_pressed(self):
        self.resume_after_drag = self.timer.isActive()
        self.set_playing(False)

    def on_slider_released(self):
        if self.resume_after_drag:
            self.set_playing(True)
        self.resume_after_drag = False

    def update_time_label(self):
        if self.fps <= 0:
            return

        def fmt(seconds):
            return f"{int(seconds // 60):02}:{seconds % 60:04.1f}"

        self.time_label.setText(
            f"{fmt(self.current_time())} / {fmt(self.duration())}"
        )

    def set_exporting(self, busy):
        self.exporting = busy
        self.progress.setVisible(busy)
        self.export_status.setText("匯出中，請勿關閉視窗…" if busy else "匯出已結束")
        self.open_btn.setEnabled(not busy)
        for control in (
            self.slider,
            self.prev_btn,
            self.play_btn,
            self.next_btn,
            self.add_mask_btn,
            self.edit_tabs,
            self.export_btn,
        ):
            control.setEnabled(not busy and self.cap is not None)
        self.view.setEnabled(not busy)
        self.refresh_selection_ui()

    def export_video(self):
        if self.input_path is None:
            return

        # Time changes are explicit; do not silently overwrite several masks
        # with the first selected mask's spinbox values while exporting.

        try:
            edit_plan, _ = self.build_edit_plan()
        except ValueError as exc:
            QMessageBox.warning(self, "裁剪設定錯誤", str(exc))
            self.edit_tabs.setCurrentIndex(1)
            return

        self.set_playing(False)
        self.add_mask_btn.setChecked(False)

        output_dir = self.input_path.parent
        suggested = output_dir / f"{self.input_path.stem}_redacted.mp4"
        name, _ = QFileDialog.getSaveFileName(
            self, "匯出已遮罩影片", str(suggested), "MP4 影片 (*.mp4)"
        )
        if not name:
            return
        output_path = Path(name)
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")
        if self.input_path.resolve() == output_path.resolve():
            QMessageBox.warning(self, "錯誤", "輸出路徑不能覆蓋原始影片！")
            return

        self.set_exporting(True)
        self.export_worker = ExportWorker(
            self.input_path, output_path, self.masks, edit_plan
        )
        self.export_worker.finished.connect(self.on_export_finished)
        self.export_worker.start()

    def on_export_finished(self):
        worker = self.export_worker
        error = worker.error
        output_path = worker.output_path
        worker.deleteLater()
        self.export_worker = None
        self.set_exporting(False)

        if error:
            self.export_status.setText("匯出失敗")
            dialog = QMessageBox(self)
            dialog.setWindowTitle("匯出失敗")
            dialog.setIcon(QMessageBox.Icon.Critical)
            dialog.setText("影片匯出失敗，請查看詳細錯誤。")
            dialog.setDetailedText(error)
            dialog.exec()
        else:
            self.export_status.setText(f"已輸出：{output_path.name}")
            QMessageBox.information(
                self,
                "匯出完成",
                f"已輸出無聲影片：\n{output_path}\n\n"
                "請完整播放並確認所有敏感資訊均已遮蔽後，再分享影片。"
            )

    def closeEvent(self, event):
        if self.export_worker is not None and self.export_worker.isRunning():
            QMessageBox.information(
                self, "匯出中", "影片仍在匯出，完成後才能關閉程式。"
            )
            event.ignore()
            return
        self.timer.stop()
        if self.cap is not None:
            self.cap.release()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoRedactorGUI()
    window.show()
    sys.exit(app.exec())
