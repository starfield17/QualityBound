from pathlib import Path

from PySide6.QtCore import QAbstractItemModel, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

from gui.queue_model import FLEX_COLUMN_SPECS, FIXED_COLUMN_WIDTHS, QueueColumn, QueueTableModel
from gui.qt_optionals import maybe_none


class ResponsiveQueueTableView(QTableView):
    filesDropped = Signal(list)

    _RELEVANT_EVENT_TYPES = {
        QEvent.Type.Show,
        QEvent.Type.Hide,
        QEvent.Type.Resize,
        QEvent.Type.LayoutRequest,
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reflow_scheduled = False
        self._applying_reflow = False
        self._watched_model: QObject | None = None
        self._manual_flex_widths: dict[int, int] = {}

        self.viewport().installEventFilter(self)
        self.verticalScrollBar().installEventFilter(self)
        self.horizontalScrollBar().installEventFilter(self)

        header = self.horizontalHeader()
        header.sectionMoved.connect(self.schedule_reflow)
        header.sectionResized.connect(self._on_header_section_resized)
        header.sectionClicked.connect(self._on_header_clicked)

    def setModel(self, model: QAbstractItemModel | None) -> None:
        # QAbstractItemView.model() is declared as non-null but returns None before
        # the first setModel call, so keep the guard.
        previous_model = maybe_none(self.model())
        if previous_model is not None:
            self._disconnect_model_signals(previous_model)
        super().setModel(model)
        configure_header_resize_modes(self.horizontalHeader())
        if model is not None:
            self._connect_model_signals(model)
        self.schedule_reflow()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.schedule_reflow()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.schedule_reflow()

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() in {QEvent.Type.LayoutRequest, QEvent.Type.Polish}:
            self.schedule_reflow()
        return result

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.acceptDrops():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not self.acceptDrops():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if not self.acceptDrops():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                self.filesDropped.emit(urls)
                return
        super().dropEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        result = super().eventFilter(watched, event)
        if watched in {self.viewport(), self.verticalScrollBar(), self.horizontalScrollBar()}:
            if event.type() in self._RELEVANT_EVENT_TYPES:
                self.schedule_reflow()
        return result

    def setColumnHidden(self, column: int, hide: bool) -> None:
        super().setColumnHidden(column, hide)
        self.schedule_reflow()

    def schedule_reflow(self) -> None:
        if self._reflow_scheduled:
            return
        self._reflow_scheduled = True
        QTimer.singleShot(0, self._apply_reflow)

    def reflow_columns(self) -> None:
        self.schedule_reflow()

    def _connect_model_signals(self, model: QAbstractItemModel) -> None:
        model.modelReset.connect(self.schedule_reflow)
        model.layoutChanged.connect(self.schedule_reflow)
        model.rowsInserted.connect(self._on_rows_changed)
        model.rowsRemoved.connect(self._on_rows_changed)
        self._watched_model = model

    def _disconnect_model_signals(self, model: QAbstractItemModel) -> None:
        for signal, slot in [
            (model.modelReset, self.schedule_reflow),
            (model.layoutChanged, self.schedule_reflow),
            (model.rowsInserted, self._on_rows_changed),
            (model.rowsRemoved, self._on_rows_changed),
        ]:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if self._watched_model is model:
            self._watched_model = None

    def _on_rows_changed(self, *_args: object) -> None:
        self.schedule_reflow()

    def _on_header_section_resized(self, _logical_index: int, _old_size: int, _new_size: int) -> None:
        if self._applying_reflow:
            return
        if _logical_index in {int(column) for column in FLEX_COLUMN_SPECS}:
            self._manual_flex_widths[_logical_index] = max(_new_size, flex_minimum_width(_logical_index))
        self.schedule_reflow()

    def _on_header_clicked(self, section: int) -> None:
        # Sorting is opt-in: the first click fixes an order, the second reverses
        # it, and the model refuses to reorder while a run is active.
        model = maybe_none(self.model())
        if not isinstance(model, QueueTableModel) or not model.can_sort():
            return
        header = maybe_none(self.horizontalHeader())
        if header is None:
            return
        order = Qt.SortOrder.AscendingOrder
        if (
            header.sortIndicatorSection() == section
            and header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        ):
            order = Qt.SortOrder.DescendingOrder
        model.sort(section, order)
        header.setSortIndicator(section, order)
        header.setSortIndicatorShown(True)

    def _apply_reflow(self) -> None:
        self._reflow_scheduled = False
        if self._applying_reflow:
            return
        header = maybe_none(self.horizontalHeader())
        if header is None:
            return

        viewport_width = self.viewport().width()
        if viewport_width <= 0:
            return

        visible_fixed = [
            (column, width)
            for column, width in FIXED_COLUMN_WIDTHS.items()
            if not self.isColumnHidden(int(column))
        ]
        visible_flex = [
            (column, weight, min_width)
            for column, (weight, min_width) in FLEX_COLUMN_SPECS.items()
            if not self.isColumnHidden(int(column))
        ]

        if not visible_fixed and not visible_flex:
            return

        self._applying_reflow = True
        try:
            visual_flex = sorted(visible_flex, key=lambda item: header.visualIndex(int(item[0])))
            base_widths: dict[QueueColumn, int] = {}
            locked_columns: set[QueueColumn] = set()
            for column, _weight, min_width in visual_flex:
                manual_width = self._manual_flex_widths.get(int(column))
                if manual_width is not None and manual_width > min_width:
                    base_widths[column] = manual_width
                    locked_columns.add(column)
                else:
                    base_widths[column] = min_width
            flex_base_total = sum(base_widths.values())
            fixed_total = sum(width for _, width in visible_fixed)
            available_for_flex = max(0, viewport_width - fixed_total)
            target_flex_total = max(flex_base_total, available_for_flex)
            extra_flex = max(0, target_flex_total - flex_base_total)

            for column, width in visible_fixed:
                header.resizeSection(int(column), width)

            if not visual_flex:
                return

            distributable = [spec for spec in visual_flex if spec[0] not in locked_columns]
            total_weight = sum(weight for _, weight, _ in distributable)
            remaining_extra = extra_flex
            remaining_weight = total_weight
            flex_widths: dict[QueueColumn, int] = {}
            for column, weight, _min_width in visual_flex:
                base_width = base_widths[column]
                if column in locked_columns:
                    width = base_width
                elif remaining_weight <= 0 or column == distributable[-1][0]:
                    width = base_width + remaining_extra
                else:
                    share = int(round(remaining_extra * weight / remaining_weight))
                    share = min(share, remaining_extra)
                    width = base_width + share
                    remaining_extra -= share
                    remaining_weight -= weight
                flex_widths[column] = width

            for column, _, _ in visual_flex:
                header.resizeSection(int(column), flex_widths[column])

            actual_total = sum(
                header.sectionSize(column)
                for column in range(header.count())
                if not self.isColumnHidden(column)
            )
            slack = viewport_width - actual_total
            if slack != 0:
                slack_targets = distributable or visual_flex
                last_flex_column, _, last_min_width = slack_targets[-1]
                current_width = header.sectionSize(int(last_flex_column))
                corrected_width = current_width + slack
                if corrected_width < last_min_width:
                    corrected_width = last_min_width
                if corrected_width != current_width:
                    header.resizeSection(int(last_flex_column), corrected_width)
        finally:
            self._applying_reflow = False

    def clear_manual_width_overrides(self) -> None:
        self._manual_flex_widths.clear()
        self.schedule_reflow()


def flex_minimum_width(logical_index: int) -> int:
    for column, (_weight, min_width) in FLEX_COLUMN_SPECS.items():
        if int(column) == logical_index:
            return min_width
    return 48


def configure_header_resize_modes(header: QHeaderView) -> None:
    for column in QueueColumn:
        if column in FIXED_COLUMN_WIDTHS:
            header.setSectionResizeMode(int(column), QHeaderView.ResizeMode.Fixed)
        else:
            header.setSectionResizeMode(int(column), QHeaderView.ResizeMode.Interactive)


def create_queue_view(parent: QWidget | None = None) -> ResponsiveQueueTableView:
    view = ResponsiveQueueTableView(parent)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setAlternatingRowColors(True)
    view.setSortingEnabled(False)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    view.setDragEnabled(True)
    view.setAcceptDrops(True)
    view.setDropIndicatorShown(True)
    view.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
    view.setDefaultDropAction(Qt.DropAction.MoveAction)

    header = view.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionsMovable(True)
    header.setSectionsClickable(True)
    header.setHighlightSections(False)
    header.setMinimumSectionSize(48)

    configure_header_resize_modes(header)

    view.verticalHeader().setVisible(False)
    view.schedule_reflow()
    return view
