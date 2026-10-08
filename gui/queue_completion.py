"""Presentation and user actions after one queue run has completed."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from core.i18n import Translator
from core.media import (
    PostEncodeAction,
    SpaceSavingsItem,
    SpaceSavingsOutcome,
    calculate_space_savings,
    execute_power_action,
    group_skipped_output_pairs,
    is_eligible_skipped_item,
    parse_post_encode_action,
    post_encode_action_key,
    publish_skipped_source,
)
from core.models import EncodePlanItem, EncodeResult, SkippedOutputPolicy
from gui.power_action_dialog import PowerActionCountdownDialog
from gui.queue_model import format_size
from gui.queue_state import QueueItemRecord, QueueItemStatus


class QueueCompletionHandler:
    def __init__(
        self,
        parent: QWidget | None,
        append_log: Callable[[str], None],
        notify: Callable[[str, str], None],
        close: Callable[[], object],
    ) -> None:
        self._parent = parent
        self._append_log = append_log
        self._notify = notify
        self._close = close

    def handle(
        self,
        records: list[QueueItemRecord],
        translator: Translator,
        config: Mapping[str, object],
    ) -> None:
        tr = translator
        self._append_log(tr.t("gui.log.encode_done"))
        self._maybe_publish_skipped_sources(records, tr)
        self._handle_post_queue_finished(records, tr, config)

    def _eligible_skipped_pairs(self, records: list[QueueItemRecord]) -> list[tuple[EncodePlanItem, EncodeResult]]:
        eligible: list[tuple[EncodePlanItem, EncodeResult]] = []
        for record in records:
            if record.result is None:
                continue
            if is_eligible_skipped_item(record.plan_item, record.result):
                eligible.append((record.plan_item, record.result))
        return eligible

    def _publish_skipped_pairs(self, pairs: list[tuple[EncodePlanItem, EncodeResult]], tr: Translator) -> None:
        for item, _result in pairs:
            published = publish_skipped_source(item)
            if published.copied:
                self._append_log(
                    tr.t(
                        "gui.log.skipped_source_copied",
                        source=item.source_path.name,
                        output=str(published.output_path),
                    )
                )
            else:
                self._append_log(
                    tr.t(
                        "gui.log.skipped_source_not_copied",
                        source=item.source_path.name,
                        reason=published.reason or "",
                    )
                )

    def _maybe_publish_skipped_sources(self, records: list[QueueItemRecord], tr: Translator) -> None:
        grouped = group_skipped_output_pairs(self._eligible_skipped_pairs(records))
        if not any(grouped.values()):
            return
        copy_pairs = grouped[SkippedOutputPolicy.COPY]
        ask_pairs = grouped[SkippedOutputPolicy.ASK]
        if copy_pairs:
            self._publish_skipped_pairs(copy_pairs, tr)
        if ask_pairs:
            listing = "\n".join(f"{item.source_path.name} → {item.output_path.name}" for item, _result in ask_pairs)
            answer = QMessageBox.question(
                self._parent,
                tr.t("gui.dialog.copy_skipped_title"),
                tr.t("gui.dialog.copy_skipped_text", files=listing),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._publish_skipped_pairs(ask_pairs, tr)

    def _space_savings_item(self, record: QueueItemRecord) -> SpaceSavingsItem:
        outcomes = {
            QueueItemStatus.DONE: SpaceSavingsOutcome.SUCCESS,
            QueueItemStatus.FAILED: SpaceSavingsOutcome.FAILED,
            QueueItemStatus.SKIPPED: SpaceSavingsOutcome.SKIPPED,
            QueueItemStatus.NEEDS_DECISION: SpaceSavingsOutcome.NEEDS_DECISION,
            QueueItemStatus.CANCELLED: SpaceSavingsOutcome.CANCELLED,
        }
        outcome = outcomes.get(record.status, SpaceSavingsOutcome.FAILED)
        return SpaceSavingsItem(
            outcome=outcome,
            source_path=record.source_path,
            output_path=record.output_path,
            actual_output_bytes=(record.result.actual_output_bytes if record.result is not None else None),
        )

    def _handle_post_queue_finished(
        self,
        records: list[QueueItemRecord],
        tr: Translator,
        config: Mapping[str, object],
    ) -> None:
        elapsed_sec = sum(float(record.elapsed_sec or 0.0) for record in records)
        savings = calculate_space_savings(
            [self._space_savings_item(record) for record in records],
            total_elapsed_sec=elapsed_sec,
        )
        if savings.successful_files > 0:
            report_lines = [
                "==================================================",
                tr.t("gui.report.batch_complete"),
                f"- {tr.t('gui.report.successful_files')}: {savings.successful_files}/{savings.total_files}",
                f"- {tr.t('gui.report.original_size')}: {format_size(savings.original_total_bytes)}",
                f"- {tr.t('gui.report.compressed_size')}: {format_size(savings.compressed_total_bytes)}",
                f"- {tr.t('gui.report.saved_space')}: {format_size(savings.saved_bytes)} ({savings.saved_ratio * 100:.1f}%)",
                "==================================================",
            ]
            self._append_log("\n".join(report_lines))
            if config.get("desktop_notifications", True):
                self._notify(
                    tr.t("app.title"),
                    tr.t(
                        "gui.notification.batch_done",
                        count=savings.successful_files,
                        saved=format_size(savings.saved_bytes),
                        ratio=f"{savings.saved_ratio * 100:.1f}%",
                    ),
                )

        if savings.failed_files > 0 or savings.cancelled_files > 0:
            failure_lines = [
                "==================================================",
                tr.t("gui.report.batch_incomplete"),
                f"- {tr.t('gui.report.failed_files')}: {savings.failed_files}",
                f"- {tr.t('gui.report.cancelled_files')}: {savings.cancelled_files}",
                "==================================================",
            ]
            self._append_log("\n".join(failure_lines))
            if config.get("desktop_notifications", True):
                self._notify(
                    tr.t("app.title"),
                    tr.t(
                        "gui.notification.batch_incomplete",
                        failed=savings.failed_files,
                        cancelled=savings.cancelled_files,
                    ),
                )

        post_action = parse_post_encode_action(config.get("post_encode_action", PostEncodeAction.DO_NOTHING.value))
        if post_action != PostEncodeAction.DO_NOTHING and savings.successful_files > 0:
            dialog = PowerActionCountdownDialog(tr, post_action, timeout_sec=30, parent=self._parent)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if post_action == PostEncodeAction.QUIT:
                    self._close()
                elif post_action in {PostEncodeAction.SLEEP, PostEncodeAction.SHUTDOWN}:
                    result = execute_power_action(post_action)
                    if not result.success:
                        message = tr.t(
                            "gui.power.action_failed",
                            action=tr.t(post_encode_action_key(post_action)),
                            error=result.error or "unknown error",
                        )
                        self._append_log(message)
                        QMessageBox.critical(self._parent, tr.t("gui.message.error"), message)
