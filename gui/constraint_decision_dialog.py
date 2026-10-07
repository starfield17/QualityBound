from __future__ import annotations

from enum import Enum

from PySide6.QtWidgets import QAbstractButton, QMessageBox, QWidget

from core.i18n import Translator
from gui.qt_optionals import maybe_none
from core.models import DecisionActionCode, DecisionOption
from gui.queue_state import QueueItemRecord


class SizeMissDecision(str, Enum):
    ACCEPT = "accept"
    RETRY = "retry"
    DISCARD = "discard"


def _quality_action_text(tr: Translator, option: DecisionOption) -> str:
    value = option.suggested_value
    if option.action_code == DecisionActionCode.RELAX_SIZE and isinstance(value, (int, float)):
        return tr.t("gui.decision.relax_size", value=f"{float(value) * 100:.1f}%")
    if option.action_code == DecisionActionCode.RELAX_QUALITY and isinstance(value, (int, float)):
        return tr.t("gui.decision.relax_quality", value=f"{float(value):.1f}")
    if option.action_code == DecisionActionCode.CHANGE_MEDIA_BUDGET:
        return tr.t("gui.decision.change_media_budget")
    if option.action_code == DecisionActionCode.REANALYZE:
        return tr.t("gui.decision.reanalyze")
    return tr.t("gui.decision.skip")


def choose_quality_decision(
    parent: QWidget,
    tr: Translator,
    record: QueueItemRecord,
    options: list[DecisionOption],
) -> DecisionOption | None:
    quality = record.plan_item.quality_search_result
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr.t("gui.decision.title"))
    box.setText(tr.t("gui.decision.quality_intro", file=record.source_path.name))
    box.setInformativeText(
        (quality.reason if quality is not None and quality.reason else record.error_summary)
        or tr.t("gui.decision.no_detail")
    )
    buttons: dict[QAbstractButton, DecisionOption] = {
        box.addButton(_quality_action_text(tr, option), QMessageBox.ButtonRole.ActionRole): option
        for option in options
    }
    cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    # QMessageBox.clickedButton() is declared as always returning a button, but Qt
    # returns None when the box is closed without choosing one.
    clicked = maybe_none(box.clickedButton())
    if clicked is None or clicked is cancel_button:
        return None
    return buttons.get(clicked)


def choose_size_miss_decision(
    parent: QWidget,
    tr: Translator,
    record: QueueItemRecord,
) -> SizeMissDecision | None:
    result = record.result
    if result is None or result.rejected_output_path is None:
        return None
    actual = result.actual_output_bytes or 0
    allowed = result.allowed_output_bytes or 0
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr.t("gui.decision.size_miss_title"))
    box.setText(tr.t("gui.decision.size_miss_intro", file=record.source_path.name))
    box.setInformativeText(
        tr.t(
            "gui.decision.size_miss_detail",
            actual=f"{actual / (1024 * 1024):.1f} MiB",
            allowed=f"{allowed / (1024 * 1024):.1f} MiB",
            path=str(result.rejected_output_path),
        )
    )
    buttons: dict[QAbstractButton, SizeMissDecision] = {
        box.addButton(tr.t("gui.decision.accept_size_miss"), QMessageBox.ButtonRole.AcceptRole): SizeMissDecision.ACCEPT,
        box.addButton(tr.t("gui.decision.retry_size_miss"), QMessageBox.ButtonRole.ActionRole): SizeMissDecision.RETRY,
        box.addButton(tr.t("gui.decision.discard_size_miss"), QMessageBox.ButtonRole.DestructiveRole): SizeMissDecision.DISCARD,
    }
    cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    # QMessageBox.clickedButton() is declared as always returning a button, but Qt
    # returns None when the box is closed without choosing one.
    clicked = maybe_none(box.clickedButton())
    if clicked is None or clicked is cancel_button:
        return None
    return buttons.get(clicked)
