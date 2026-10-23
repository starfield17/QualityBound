from __future__ import annotations

from qt_lifetime import dispose_window

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QComboBox

from core.models import CompressionMode, EncodeOptions, EncodePlan, VmafViewingContext
from gui.gui_mainwindow import MainWindow
from gui.queue.state import QueueItemStatus, create_queue_records

from _smart_quality_fixtures import _item


def _combo_item(combo: QComboBox, index: int) -> QStandardItem:
    """Return a combo box's item, which the panel builds from a standard model."""

    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    item = model.item(index)
    assert item is not None
    return item


class SmartGuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_smart_controls_and_queue_initial_state(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            panel = window.options_panel
            smart_index = panel.compression_mode_combo.findData(CompressionMode.SMART.value)
            panel.compression_mode_combo.setCurrentIndex(smart_index)
            self.assertTrue(panel.ratio_edit.isHidden())
            self.assertTrue(panel.min_vmaf_spin.isEnabled())
            self.assertEqual(panel.min_vmaf_spin.value(), 90.0)
            self.assertFalse(panel.viewing_context_combo.isHidden())
            self.assertEqual(
                panel.viewing_context_combo.currentData(),
                VmafViewingContext.HIGH_FIDELITY.value,
            )
            self.assertEqual(
                panel.viewing_context_combo.currentText(),
                "High-fidelity / Large display",
            )
            standard_index = panel.viewing_context_combo.findData(
                VmafViewingContext.STANDARD_DISPLAY.value
            )
            panel.viewing_context_combo.setCurrentIndex(standard_index)
            self.assertEqual(
                panel.read_options().viewing_context,
                VmafViewingContext.STANDARD_DISPLAY,
            )
            fixed_index = panel.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value)
            panel.compression_mode_combo.setCurrentIndex(fixed_index)
            self.assertTrue(panel.viewing_context_combo.isHidden())

            panel.apply_options(
                EncodeOptions(viewing_context=VmafViewingContext.STANDARD_DISPLAY)
            )
            self.assertEqual(
                panel.viewing_context_combo.currentData(),
                VmafViewingContext.STANDARD_DISPLAY.value,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.mov"
                source.write_bytes(b"source")
                item = _item(source, root / "output.mp4", EncodeOptions())
                plan = EncodePlan(
                    items=[item],
                    ffmpeg_path=Path("ffmpeg"),
                    ffprobe_path=Path("ffprobe"),
                    input_root=root,
                    output_root=root,
                )
                records = create_queue_records(plan, root)
                self.assertEqual(records[0].status, QueueItemStatus.WAITING_ANALYSIS)
        finally:
            dispose_window(window)

    def test_unavailable_vmaf_disables_smart_mode(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                {
                    "codecs": {"hevc": [], "av1": []},
                    "vmaf": {
                        "runnable": False,
                        "model": "vmaf_v1.0.16_3d0h",
                        "backend": "cpu",
                        "error_message": "missing libvmaf",
                    },
                }
            )
            panel = window.options_panel
            smart_index = panel.compression_mode_combo.findData(CompressionMode.SMART.value)
            smart_item = _combo_item(panel.compression_mode_combo, smart_index)
            self.assertFalse(smart_item.isEnabled())
            self.assertEqual(
                panel.compression_mode_combo.currentData(),
                CompressionMode.FIXED_BITRATE.value,
            )
        finally:
            dispose_window(window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
