"""流程基建：观察序列封存／迟到补齐（无真机、内存可测）。"""

from __future__ import annotations

from services.api.app.domain.exploration.flow_foundation import FlowFoundationContractError
from services.api.app.domain.exploration.flow_foundation_evidence import (
    EvidenceSequenceSeal,
    ObservationSequenceDraft,
    assert_late_fill_creates_new_revision,
)
from services.api.app.domain.protocol import require_text


def seal_observation_sequence(
    draft: ObservationSequenceDraft,
    *,
    input_set_id: str | None = None,
    task_terminal: bool = False,
) -> EvidenceSequenceSeal:
    """将草稿封存为 EvidenceSequenceSeal；已封存则幂等返回当前 revision。"""

    if not isinstance(draft, ObservationSequenceDraft):
        raise TypeError("draft 必须为 ObservationSequenceDraft")
    require_text(draft.series_id, "series_id")
    if not draft.frames:
        raise FlowFoundationContractError("空观察序列不得封存")
    if draft.sealed_revision is not None:
        return EvidenceSequenceSeal(
            series_id=draft.series_id,
            revision=int(draft.sealed_revision),
            previous_revision=draft.previous_revision,
            input_set_id=str(
                input_set_id or f"input:{draft.series_id}:r{int(draft.sealed_revision)}"
            ),
            task_terminal=task_terminal,
        )
    revision = 1 if draft.previous_revision is None else int(draft.previous_revision) + 1
    sealed = EvidenceSequenceSeal(
        series_id=draft.series_id,
        revision=revision,
        previous_revision=draft.previous_revision,
        input_set_id=str(input_set_id or f"input:{draft.series_id}:r{revision}"),
        task_terminal=task_terminal,
    )
    draft.sealed_revision = sealed.revision
    return sealed


def late_fill_observation_sequence(
    *,
    sealed: EvidenceSequenceSeal,
    late_fill: bool = True,
    mutate_old_input_set: bool = False,
    reopen_terminal_task: bool = False,
) -> EvidenceSequenceSeal:
    """迟到补齐：只能开新 revision，不得改旧 InputSet／复活终态任务。"""

    return assert_late_fill_creates_new_revision(
        sealed=sealed,
        late_fill=late_fill,
        mutate_old_input_set=mutate_old_input_set,
        reopen_terminal_task=reopen_terminal_task,
    )
