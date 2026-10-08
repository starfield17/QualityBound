from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from core.models import VmafBackend, VmafRuntimeSupport
from core.smart.v1.vmaf import VMAF_PRODUCTION_MODELS
from gui.gui_workers import EncoderCapabilityDetectWorker


class EncoderCapabilityDetectWorkerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_worker_requires_all_production_vmaf_models(self) -> None:
        completed: list[dict[str, object]] = []
        failed: list[str] = []

        def probe(
            _ffmpeg: Path, model: object, backend: VmafBackend
        ) -> VmafRuntimeSupport:
            name = str(getattr(model, "name"))
            runnable = name != VMAF_PRODUCTION_MODELS[-1].name
            return VmafRuntimeSupport(
                backend=backend,
                model=name,
                runnable=runnable,
                error_message=None if runnable else "missing model",
            )

        worker = EncoderCapabilityDetectWorker("ffmpeg")
        worker.completed.connect(completed.append)
        worker.failed.connect(failed.append)
        with (
            patch("gui.gui_workers.find_binary", return_value=Path("ffmpeg")),
            patch("gui.gui_workers.ensure_encoder_capabilities", return_value={}),
            patch("gui.gui_workers.probe_vmaf_runtime", side_effect=probe) as run_probe,
        ):
            worker.run()

        self.assertFalse(failed)
        self.assertEqual(run_probe.call_count, len(VMAF_PRODUCTION_MODELS))
        self.assertEqual(len(completed), 1)
        vmaf = completed[0]["vmaf"]
        self.assertIsInstance(vmaf, dict)
        assert isinstance(vmaf, dict)
        self.assertFalse(vmaf["runnable"])
        self.assertEqual(set(vmaf["models"]), {model.name for model in VMAF_PRODUCTION_MODELS})
        self.assertIn(VMAF_PRODUCTION_MODELS[-1].name, str(vmaf["error_message"]))


if __name__ == "__main__":
    unittest.main()
