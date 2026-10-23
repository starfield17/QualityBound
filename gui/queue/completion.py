"""Presentation and user actions after one queue run has completed."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, QThread, Signal, Slot

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from core.i18n import Text, Translator
from core.media import (
    PostEncodeAction,
    SpaceSavingsItem,
    SpaceSavingsOutcome,
    calculate_space_savings,
    execute_power_action,
    group_skipped_output_pairs,
    is_eligible_skipped_item,
    parse_post_encode_action,
    publish_skipped_source,
)
from core.models import EncodePlanItem, EncodeResult, SkippedOutputPolicy, SkippedOutputOutcome, OperationCancelledError
from gui.power_action_dialog import post_encode_action_text, PowerActionCountdownDialog
from gui.queue.model import format_size
from gui.queue.state import QueueItemRecord, QueueItemStatus


TITLE = Text(
    en='QualityBound',
    zh_cn='QualityBound',
    ja='QualityBound',
)

DIALOG_COPY_SKIPPED_TEXT = Text(
    en='Copy these sources skipped by Smart analysis to their planned output paths?\n\n{files}',
    zh_cn='要把这些被 Smart 分析跳过的片源复制到预定输出路径吗？\n\n{files}',
    ja='Smart分析によってスキップされたこれらのソースを計画された出力パスにコピーしますか？\n\n{files}',
)

DIALOG_COPY_SKIPPED_TITLE = Text(
    en='Copy files skipped by Smart analysis?',
    zh_cn='复制被 Smart 分析跳过的文件？',
    ja='Smart分析でスキップされたファイルをコピーしますか？',
)


class SkippedSourceCopyWorker(QThread):
    copied = Signal(str, object)

    def __init__(self, pairs: list[tuple[str, EncodePlanItem, EncodeResult]], parent: QObject, translator: Translator) -> None:
        super().__init__(parent)
        self.pairs = pairs
        self.translator = translator
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        for item_id, item, result in self.pairs:
            try:
                if self.cancelled.is_set():
                    return
                publish_skipped_source(item, result, cancel_check=self.cancelled.is_set)
            except OperationCancelledError:
                self.cancelled.set()
                return
            except (OSError, ValueError, RuntimeError) as exc:
                result.skipped_output_outcome = SkippedOutputOutcome.FAILED
                result.success = result.skipped = result.needs_decision = False
                result.error_message = self.translator.t(Text(en='Source copy failed: {error}', zh_cn='源文件复制失败：{error}', ja='ソースのコピーに失敗しました: {error}'), error=str(exc))
            self.copied.emit(item_id, result)


class QueueCompletionHandler(QObject):
    def __init__(
        self,
        parent: QWidget | None,
        append_log: Callable[[str], None],
        notify: Callable[[str, str], None],
        close: Callable[[], object],
    ) -> None:
        super().__init__(parent)
        self._copy_worker: SkippedSourceCopyWorker | None = None
        self._cancel_requested = False
        self._copy_records: dict[str, QueueItemRecord] = {}
        self._copy_translator: Translator | None = None
        self._copy_done: Callable[[bool], None] | None = None
        self._apply_copy_result: Callable[[str, EncodeResult], None] | None = None
        self._parent = parent
        self._append_log = append_log
        self._notify = notify
        self._close = close

    def is_busy(self) -> bool:
        return self._copy_worker is not None

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._copy_worker is not None:
            self._copy_worker.cancel()

    def process_stopped(
        self, records: list[QueueItemRecord], tr: Translator,
        apply_result: Callable[[str, EncodeResult], None], finished: Callable[[bool], None],
    ) -> None:
        """Resolve source-copy policy independently of unresolved analysis choices."""
        if self.is_busy():
            raise RuntimeError("Skipped-source publication is already running.")
        self._cancel_requested = False
        pairs = self._eligible_skipped_pairs(records)
        grouped = group_skipped_output_pairs(pairs)
        selected = list(grouped[SkippedOutputPolicy.COPY])
        for _item, result in grouped[SkippedOutputPolicy.IGNORE]:
            result.skipped_output_outcome = SkippedOutputOutcome.IGNORED
        asked = grouped[SkippedOutputPolicy.ASK]
        if asked:
            listing = "\n".join(f"{item.source_path.name} → {item.output_path.name}" for item, _ in asked)
            answer = QMessageBox.question(self._parent, tr.t(DIALOG_COPY_SKIPPED_TITLE),
                                          tr.t(DIALOG_COPY_SKIPPED_TEXT, files=listing),
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Yes:
                selected.extend(asked)
            else:
                for _item, result in asked:
                    result.skipped_output_outcome = SkippedOutputOutcome.IGNORED
        if self._cancel_requested:
            finished(True)
            return
        if not selected:
            finished(False)
            return
        by_result = {id(record.result): record for record in records if record.result is not None}
        self._copy_records = {by_result[id(result)].item_id: by_result[id(result)] for _, result in selected}
        self._copy_translator, self._copy_done, self._apply_copy_result = tr, finished, apply_result
        snapshots = [(by_result[id(result)].item_id, copy.deepcopy(item), copy.deepcopy(result))
                     for item, result in selected]
        worker = SkippedSourceCopyWorker(snapshots, self, tr)
        self._copy_worker = worker
        worker.copied.connect(self._on_source_copied)
        worker.finished.connect(self._on_copy_finished)
        worker.start()

    @Slot(str, object)
    def _on_source_copied(self, item_id: str, result: EncodeResult) -> None:
        record = self._copy_records[item_id]
        if self._apply_copy_result is not None:
            self._apply_copy_result(item_id, result)
        if self._copy_translator is not None:
            self._log_publication(record.bound_plan_item, result, self._copy_translator)

    @Slot()
    def _on_copy_finished(self) -> None:
        worker = self._copy_worker
        if worker is None:
            return
        cancelled = worker.cancelled.is_set()
        done = self._copy_done
        self._copy_worker = None
        self._copy_records = {}
        self._copy_translator = self._copy_done = self._apply_copy_result = None
        worker.deleteLater()
        if done is not None:
            done(cancelled)

    def _log_publication(self, item: EncodePlanItem, result: EncodeResult, tr: Translator) -> None:
        if result.skipped_output_outcome == SkippedOutputOutcome.COPIED:
            self._append_log(tr.t(Text(en='Copied Smart-skipped source {source} -> {output}', zh_cn='已复制被 Smart 分析跳过的片源 {source} -> {output}', ja='Smartスキップのソースをコピーしました: {source} -> {output}'), source=item.source_path.name,
                                  output=str(item.output_path)))
        else:
            self._append_log(tr.t(Text(en='Did not copy Smart-skipped source {source}: {reason}', zh_cn='未复制被 Smart 分析跳过的片源 {source}：{reason}', ja='Smartスキップのソースはコピーされませんでした: {source}: {reason}'), source=item.source_path.name,
                                  reason=result.error_message or ""))
        for warning in result.external_subtitle_warnings:
            self._append_log(warning)

    def handle(
        self,
        records: list[QueueItemRecord],
        translator: Translator,
        config: Mapping[str, object],
    ) -> None:
        tr = translator
        self._append_log(tr.t(Text(en='Encode jobs completed.', zh_cn='编码任务已完成。', ja='エンコードタスクが完了しました。')))
        self._maybe_publish_skipped_sources(records, tr)
        self._handle_post_queue_finished(records, tr, config)

    def _eligible_skipped_pairs(self, records: list[QueueItemRecord]) -> list[tuple[EncodePlanItem, EncodeResult]]:
        eligible: list[tuple[EncodePlanItem, EncodeResult]] = []
        for record in records:
            if record.result is None:
                continue
            if is_eligible_skipped_item(record.bound_plan_item, record.result):
                eligible.append((record.bound_plan_item, record.result))
        return eligible

    def _publish_skipped_pairs(self, pairs: list[tuple[EncodePlanItem, EncodeResult]], tr: Translator) -> None:
        for item, result in pairs:
            if result.skipped_output_outcome is not None:
                continue
            publish_skipped_source(item, result)
            self._log_publication(item, result, tr)

    def _maybe_publish_skipped_sources(self, records: list[QueueItemRecord], tr: Translator) -> None:
        grouped = group_skipped_output_pairs(self._eligible_skipped_pairs(records))
        if not any(grouped.values()):
            return
        for _item, result in grouped[SkippedOutputPolicy.IGNORE]:
            result.skipped_output_outcome = SkippedOutputOutcome.IGNORED
        copy_pairs = grouped[SkippedOutputPolicy.COPY]
        ask_pairs = grouped[SkippedOutputPolicy.ASK]
        if copy_pairs:
            self._publish_skipped_pairs(copy_pairs, tr)
        if ask_pairs:
            listing = "\n".join(f"{item.source_path.name} → {item.output_path.name}" for item, _result in ask_pairs)
            answer = QMessageBox.question(
                self._parent,
                tr.t(DIALOG_COPY_SKIPPED_TITLE),
                tr.t(DIALOG_COPY_SKIPPED_TEXT, files=listing),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                for _item, result in ask_pairs:
                    result.skipped_output_outcome = SkippedOutputOutcome.IGNORED
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
                tr.t(Text(en='Batch Encoding Complete', zh_cn='批处理转码完成', ja='バッチエンコード完了')),
                f"- {tr.t(Text(en='Successful', zh_cn='成功文件', ja='成功'))}: {savings.successful_files}/{savings.total_files}",
                f"- {tr.t(Text(en='Original total size', zh_cn='原始总体积', ja='元の合計サイズ'))}: {format_size(savings.original_total_bytes)}",
                f"- {tr.t(Text(en='Compressed total size', zh_cn='压缩后总体积', ja='圧縮後の合計サイズ'))}: {format_size(savings.compressed_total_bytes)}",
                f"- {tr.t(Text(en='Saved space', zh_cn='节省空间', ja='削減サイズ'))}: {format_size(savings.saved_bytes)} ({savings.saved_ratio * 100:.1f}%)",
                "==================================================",
            ]
            self._append_log("\n".join(report_lines))
            if config.get("desktop_notifications", True):
                self._notify(
                    tr.t(TITLE),
                    tr.t(
                        Text(en='Batch finished: {count} videos compressed, saved {saved} ({ratio}) space.', zh_cn='批处理转码完成：共完成 {count} 个视频，节省 {saved} ({ratio}) 空间。', ja='バッチエンコード完了: {count} 本の動画を圧縮し、{saved} ({ratio}) の容量を節約しました。'),
                        count=savings.successful_files,
                        saved=format_size(savings.saved_bytes),
                        ratio=f"{savings.saved_ratio * 100:.1f}%",
                    ),
                )

        if savings.failed_files > 0 or savings.cancelled_files > 0:
            failure_lines = [
                "==================================================",
                tr.t(Text(en='Batch incomplete', zh_cn='批处理未全部完成', ja='バッチ未完了')),
                f"- {tr.t(Text(en='Failed files', zh_cn='失败文件', ja='失敗したファイル'))}: {savings.failed_files}",
                f"- {tr.t(Text(en='Cancelled files', zh_cn='已取消文件', ja='キャンセルされたファイル'))}: {savings.cancelled_files}",
                "==================================================",
            ]
            self._append_log("\n".join(failure_lines))
            if config.get("desktop_notifications", True):
                self._notify(
                    tr.t(TITLE),
                    tr.t(
                        Text(en='Batch finished with {failed} failed and {cancelled} cancelled items.', zh_cn='批处理结束：{failed} 个失败，{cancelled} 个已取消。', ja='バッチ終了: {failed} 件失敗、{cancelled} 件キャンセル。'),
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
                            Text(en='Could not {action}: {error}', zh_cn='无法执行【{action}】：{error}', ja='{action} を実行できませんでした: {error}'),
                            action=tr.t(post_encode_action_text(post_action)),
                            error=result.error or "unknown error",
                        )
                        self._append_log(message)
                        QMessageBox.critical(self._parent, tr.t(Text(en='Error', zh_cn='错误', ja='エラー')), message)
