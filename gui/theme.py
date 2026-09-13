from __future__ import annotations

from PySide6.QtWidgets import QWidget


def apply_theme(widget: QWidget) -> None:
    widget.setStyleSheet(
        """
        /* QualityBoundTheme */
        QMainWindow, QDialog {
            background: #F6F8FB;
            color: #0F172A;
        }
        QGroupBox {
            background: #FFFFFF;
            border: 1px solid #D8E0EA;
            border-radius: 8px;
            margin-top: 14px;
            padding: 14px 12px 12px 12px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #1E3A5F;
        }
        QToolBar {
            background: #FFFFFF;
            border-bottom: 1px solid #D8E0EA;
            spacing: 4px;
            padding: 6px;
        }
        QToolButton {
            border: 1px solid transparent;
            border-radius: 6px;
            padding: 6px 9px;
            color: #0F172A;
        }
        QToolButton:hover {
            background: #EAF1FB;
            border-color: #C8D7EA;
        }
        QToolButton:pressed {
            background: #DDEAF8;
        }
        QToolButton:disabled {
            color: #94A3B8;
            background: transparent;
        }
        QLabel#summaryTitle {
            color: #64748B;
            font-size: 12px;
            font-weight: 500;
        }
        QLabel#summaryValue {
            color: #0F172A;
            font-size: 13px;
            font-weight: 700;
        }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
            background: #FFFFFF;
            border: 1px solid #CBD5E1;
            border-radius: 5px;
            min-height: 28px;
            padding: 3px 7px;
            selection-background-color: #2563EB;
            selection-color: #FFFFFF;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
            border: 1px solid #2563EB;
        }
        QPushButton {
            background: #F8FAFC;
            border: 1px solid #CBD5E1;
            border-radius: 5px;
            min-height: 28px;
            padding: 4px 10px;
        }
        QPushButton:hover {
            background: #EAF1FB;
            border-color: #9DB7D8;
        }
        QPushButton:pressed {
            background: #DDEAF8;
        }
        QPushButton:disabled {
            color: #94A3B8;
            background: #F1F5F9;
        }
        QTabWidget::pane {
            border: 1px solid #D8E0EA;
            border-radius: 8px;
            background: #FFFFFF;
            top: -1px;
        }
        QTabBar::tab {
            background: #EEF3F8;
            border: 1px solid #D8E0EA;
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 7px 14px;
            margin-right: 2px;
            color: #334155;
        }
        QTabBar::tab:selected {
            background: #FFFFFF;
            color: #0F172A;
            font-weight: 600;
        }
        QProgressBar {
            background: #E8EEF6;
            border: 1px solid #D8E0EA;
            border-radius: 5px;
            min-height: 14px;
            text-align: center;
            color: #0F172A;
        }
        QProgressBar::chunk {
            background: #2563EB;
            border-radius: 4px;
        }
        QTableView {
            background: #FFFFFF;
            alternate-background-color: #F8FAFC;
            border: 1px solid #D8E0EA;
            border-radius: 6px;
            gridline-color: #E2E8F0;
            selection-background-color: #DCEBFF;
            selection-color: #0F172A;
        }
        QHeaderView::section {
            background: #EEF3F8;
            color: #334155;
            border: none;
            border-right: 1px solid #D8E0EA;
            border-bottom: 1px solid #D8E0EA;
            padding: 6px 8px;
            font-weight: 600;
        }
        QStatusBar {
            background: #FFFFFF;
            border-top: 1px solid #D8E0EA;
            color: #334155;
        }
        """
    )
