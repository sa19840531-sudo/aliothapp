# Qingyun Evidence Identity and Exit-Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete E04/E05/E06/E15 so every formal evidence artifact has a verifiable byte identity, legacy rows can be safely backfilled, PostgreSQL enforces the identity contract, and Web exit-gate failures become auditable evidence-insufficient business outcomes without automatic recapture.

**Architecture:** Keep byte identity production in the Local/MinIO storage adapters, validate that identity at every formal repository write boundary, and backfill legacy rows through a standalone resumable application service rather than Alembic I/O. Extend the existing exit-gate snapshot/evaluation in place with versioned Manifest lineage and a typed business outcome; E15 consumes the Manifest result but never recomputes completeness.

**Tech Stack:** Python 3.12, FastAPI application/domain layers, asyncpg, PostgreSQL, Alembic, Local filesystem/MinIO, pytest/pytest-asyncio, Ruff.

## Global Constraints

- All new and modified comments must be Chinese UTF-8.
- Keep files single-purpose; do not add artifact backfill orchestration to an existing large repository.
- `qc_evidence_manifest.quality_status` remains the only evidence-completeness authority with exactly `pending/pass/fail`.
- E15 must not create or update Evidence Requests, Probe Work, or supplemental captures.
- Alembic migrations must not read Local or MinIO objects.
- `storage_path` must be nonblank, `checksum` must match `^sha256:[0-9a-f]{64}$`, and `size_bytes` must be an integer `>= 0`.
- `size_bytes=0` is valid for a real empty artifact and still requires the real SHA-256 digest.
- Existing v1 Manifest and Exit Gate payloads must remain readable.
- Migration revision must be selected from the actual worktree Alembic head; do not assume `0049` if another branch has claimed it.
- No new frontend menu or route is required.
- Every behavior change follows RED → observed expected failure → minimal GREEN → focused regression.
- Each commit message starts with `[AI编程]` and states module, migration impact, frontend route/menu impact, and uncovered test risk.

---

## File Structure

### New files

- `services/api/app/domain/evidence/artifact_identity.py`: canonical evidence byte-identity validation and reason codes.
- `services/api/app/application/evidence_artifact_backfill.py`: resumable backfill use case, row/result/report types, and fail-closed classification.
- `services/api/app/infrastructure/db/evidence_artifact_backfill_repository.py`: cursor scan and optimistic update of legacy rows.
- `scripts/backfill_evidence_artifact_identity.py`: CLI composition, dry-run/apply switch, JSON report, and process exit code.
- `services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py`: E06 constraints, `ui_tree`, and E15 evaluation columns.
- `services/api/tests/test_evidence_artifact_identity.py`: artifact identity behavior.
- `services/api/tests/test_evidence_artifact_backfill.py`: backfill application behavior.
- `services/api/tests/test_evidence_artifact_backfill_repository.py`: cursor and optimistic SQL contracts.
- `services/api/tests/test_evidence_identity_migration.py`: migration source/upgrade/downgrade contracts.

### Modified files

- `services/api/app/application/page_capture_persistence.py`: call the trusted identity validator before formal repository persistence.
- `services/api/app/application/app_capture_persistence.py`: validate App screenshot identities before repository persistence and Manifest publication.
- `services/api/app/infrastructure/storage/local_evidence_store.py`: expose streaming identity inspection for backfill.
- `services/api/app/infrastructure/storage/minio_evidence_store.py`: expose streaming identity inspection and close responses.
- `services/api/app/infrastructure/storage/factory.py`: construct the inspector-backed storage without introducing a second backend selector.
- `services/api/app/domain/evidence/contracts.py`: no stricter v1 parsing; only reuse canonical validation when building new Manifest references if compatible.
- `services/api/app/domain/exploration/evidence_exit_gate.py`: v2 Manifest lineage, typed business outcome, stable outcome identity, and v1 reader.
- `services/api/app/application/evidence_exit_gate.py`: typed blocked exception with stable code and retryability.
- `services/api/app/infrastructure/db/evidence_exit_gate_repository.py`: load Manifest lineage and persist the outcome atomically.
- `services/api/app/application/h5_exploration_turn.py`: map typed gate outcomes to BLOCKED/RETRY_WAIT/FAILED.
- `services/api/tests/test_page_capture_persistence.py`: reject invalid identities before DB writes.
- `services/api/tests/test_app_capture_persistence.py`: App identity boundary regression.
- `services/api/tests/test_evidence_exit_gate.py`: v1/v2 domain classification and identities.
- `services/api/tests/test_evidence_exit_gate_persistence.py`: SQL lineage and typed exception persistence.
- `services/api/tests/test_h5_exploration_turn.py`: business mapping and zero-action effect.
- `docs/专项能力与高频回归规则.md`: reusable identity/backfill/gate regression rules.

---

### Task 1: Canonical trusted artifact identity boundary

**Files:**
- Create: `services/api/app/domain/evidence/artifact_identity.py`
- Create: `services/api/tests/test_evidence_artifact_identity.py`
- Modify: `services/api/app/application/page_capture_persistence.py`
- Modify: `services/api/app/application/app_capture_persistence.py`
- Test: `services/api/tests/test_page_capture_persistence.py`
- Test: `services/api/tests/test_app_capture_persistence.py`

**Interfaces:**
- Produces: `EvidenceArtifactIdentityError(ValueError)` with `.reason_code: str`.
- Produces: `validate_evidence_artifact_identity(*, storage_path: str, checksum: str, size_bytes: int) -> None`.
- Consumes: the existing `PersistedEvidenceArtifact` returned by the storage adapter.

- [ ] **Step 1: Write failing domain tests**

```python
def test_identity_accepts_real_empty_sha256() -> None:
    validate_evidence_artifact_identity(
        storage_path="task/capture/empty.txt",
        checksum="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=0,
    )


@pytest.mark.parametrize(
    ("storage_path", "checksum", "size_bytes", "reason"),
    [
        (" ", "sha256:" + "a" * 64, 1, "storage_path_blank"),
        ("a", "", 1, "checksum_invalid"),
        ("a", "sha256:ABC", 1, "checksum_invalid"),
        ("a", "sha256:" + "a" * 64, -1, "size_bytes_invalid"),
        ("a", "sha256:" + "a" * 64, True, "size_bytes_invalid"),
    ],
)
def test_identity_rejects_untrusted_values(...):
    with pytest.raises(EvidenceArtifactIdentityError) as error:
        validate_evidence_artifact_identity(...)
    assert error.value.reason_code == reason
```

- [ ] **Step 2: Run the new tests and observe RED**

Run: `python -m pytest services/api/tests/test_evidence_artifact_identity.py -q`

Expected: import failure because `artifact_identity.py` does not exist.

- [ ] **Step 3: Implement the minimal canonical validator**

```python
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceArtifactIdentityError(ValueError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def validate_evidence_artifact_identity(*, storage_path, checksum, size_bytes) -> None:
    if not isinstance(storage_path, str) or not storage_path.strip() or storage_path != storage_path.strip():
        raise EvidenceArtifactIdentityError("证据存储路径不能为空或包含首尾空白", reason_code="storage_path_blank")
    if not isinstance(checksum, str) or _SHA256_PATTERN.fullmatch(checksum) is None:
        raise EvidenceArtifactIdentityError("证据 checksum 必须为规范 SHA-256", reason_code="checksum_invalid")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise EvidenceArtifactIdentityError("证据 size_bytes 必须为非负整数", reason_code="size_bytes_invalid")
```

- [ ] **Step 4: Add failing formal-write boundary tests**

Add one Web persistence test and one App persistence test that supply `checksum=""` or `size_bytes=-1`, assert the repository fake receives zero save calls, and assert a newly-created object is compensated while a `created=False` reused object is retained.

- [ ] **Step 5: Run the boundary tests and observe RED**

Run: `python -m pytest services/api/tests/test_page_capture_persistence.py services/api/tests/test_app_capture_persistence.py -q -k "identity or invalid_checksum"`

Expected: the repository is called because the services do not validate the returned storage identity.

- [ ] **Step 6: Validate every stored artifact before repository calls**

Add a private `_validate_stored_artifacts()` in `page_capture_persistence.py` that loops over formal artifacts and calls the domain validator. Reuse it from App persistence rather than duplicating the regex. Do not tighten `ArtifactReference.from_dict()` in this task.

- [ ] **Step 7: Run focused and adjacent regressions**

Run:

```powershell
python -m pytest services/api/tests/test_evidence_artifact_identity.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_action_evidence.py -q
python -m ruff check services/api/app/domain/evidence/artifact_identity.py services/api/app/application/page_capture_persistence.py services/api/app/application/app_capture_persistence.py services/api/tests/test_evidence_artifact_identity.py
```

Expected: all selected tests pass; Ruff exits 0.

- [ ] **Step 8: Commit Task 1**

```powershell
git add services/api/app/domain/evidence/artifact_identity.py services/api/app/application/page_capture_persistence.py services/api/app/application/app_capture_persistence.py services/api/tests/test_evidence_artifact_identity.py services/api/tests/test_page_capture_persistence.py services/api/tests/test_app_capture_persistence.py
git commit -m "[AI编程] feat: 收紧证据附件可信身份；模块：Evidence/Capture；数据库迁移：否；前端菜单路由：否；测试风险：真实MinIO对象写入待集成验证"
```

### Task 2: Resumable legacy identity backfill

**Files:**
- Create: `services/api/app/application/evidence_artifact_backfill.py`
- Create: `services/api/app/infrastructure/db/evidence_artifact_backfill_repository.py`
- Create: `services/api/tests/test_evidence_artifact_backfill.py`
- Create: `services/api/tests/test_evidence_artifact_backfill_repository.py`
- Modify: `services/api/app/infrastructure/storage/local_evidence_store.py`
- Modify: `services/api/app/infrastructure/storage/minio_evidence_store.py`

**Interfaces:**
- Produces: `EvidenceArtifactBackfillRow`, `EvidenceArtifactComputedIdentity`, `EvidenceArtifactBackfillItemResult`, `EvidenceArtifactBackfillReport`.
- Produces: `EvidenceArtifactIdentityInspector.inspect_identity(storage_path: str) -> EvidenceArtifactComputedIdentity`.
- Produces: `EvidenceArtifactBackfillService.run(*, apply: bool, batch_size: int) -> EvidenceArtifactBackfillReport`.
- Repository methods: `scan_candidates(*, after_created_at, after_id, limit)`, `update_identity(*, row, computed) -> bool`.

- [ ] **Step 1: Write failing application tests for all result classes**

Use a real temporary Local store for byte inspection and an in-memory repository fake. Hand-check the SHA-256 literals. Cover:

- both fields missing → `updated` in apply mode;
- only size missing and checksum matches → `updated`;
- stored checksum conflicts → `conflict` and no update;
- file missing → `missing`;
- blank path → `invalid`;
- dry-run → `would_update` and no update;
- repeated run after update → `skipped`;
- any missing/conflict/invalid/error makes `report.ready_for_constraint` false.

- [ ] **Step 2: Run application tests and observe RED**

Run: `python -m pytest services/api/tests/test_evidence_artifact_backfill.py -q`

Expected: missing application module.

- [ ] **Step 3: Implement streaming inspectors**

Local inspector reads `1024 * 1024` byte chunks from `_resolve_path(storage_path)`. MinIO inspector calls `get_object`, iterates `response.stream(1024 * 1024)`, and always invokes `close()` and `release_conn()` in `finally`. Both return exactly:

```python
@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactComputedIdentity:
    checksum: str
    size_bytes: int
```

- [ ] **Step 4: Implement minimal fail-closed backfill service**

The service compares existing nonblank/non-NULL values to computed values before updating. It never invents paths, never overwrites a conflict, and advances the cursor using the last scanned row even when that row fails.

- [ ] **Step 5: Run application tests to GREEN**

Run: `python -m pytest services/api/tests/test_evidence_artifact_backfill.py -q`

The same file contains the Local temporary-file integration and a MinIO response fake that asserts `stream()`, `close()`, and `release_conn()` behavior.

- [ ] **Step 6: Write failing repository SQL tests**

Assert `scan_candidates` uses `(created_at, id)` cursor ordering and selects NULL/blank/malformed/negative candidates. Assert UPDATE matches id plus original checksum/size/storage_path using `IS NOT DISTINCT FROM`, returning false on a concurrent change.

- [ ] **Step 7: Implement the focused asyncpg repository**

Use a separate file and the shared pool. Do not add this scan/update logic to `PostgresPageCaptureRepository`.

- [ ] **Step 8: Run repository and full Task 2 regressions**

Run:

```powershell
python -m pytest services/api/tests/test_evidence_artifact_backfill.py services/api/tests/test_evidence_artifact_backfill_repository.py -q
python -m ruff check services/api/app/application/evidence_artifact_backfill.py services/api/app/infrastructure/db/evidence_artifact_backfill_repository.py services/api/app/infrastructure/storage/local_evidence_store.py services/api/app/infrastructure/storage/minio_evidence_store.py services/api/tests/test_evidence_artifact_backfill.py services/api/tests/test_evidence_artifact_backfill_repository.py
```

- [ ] **Step 9: Commit Task 2**

```powershell
git add services/api/app/application/evidence_artifact_backfill.py services/api/app/infrastructure/db/evidence_artifact_backfill_repository.py services/api/app/infrastructure/storage/local_evidence_store.py services/api/app/infrastructure/storage/minio_evidence_store.py services/api/tests/test_evidence_artifact_backfill.py services/api/tests/test_evidence_artifact_backfill_repository.py
git commit -m "[AI编程] feat: 增加存量证据身份回填服务；模块：Evidence/Storage；数据库迁移：否；前端菜单路由：否；测试风险：大规模MinIO流式扫描待压测"
```

### Task 3: Backfill CLI and auditable report

**Files:**
- Create: `scripts/backfill_evidence_artifact_identity.py`
- Create: `services/api/tests/test_backfill_evidence_artifact_identity_script.py`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: `EvidenceArtifactBackfillService.run(apply, batch_size)` from Task 2.
- Produces CLI flags: `--apply`, `--batch-size <positive-int>`, `--report <workspace-path>`.
- Exit codes: `0` only when `ready_for_constraint=true`; `2` for completed scans with unresolved data; `1` for composition/runtime failure.

- [ ] **Step 1: Write failing CLI behavior tests**

Invoke `main(argv, service_factory=...)` directly. Assert default dry-run, explicit apply, invalid batch size rejection, JSON UTF-8 report creation, no raw payload field, and exit code 2 for a report containing conflict/missing.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest services/api/tests/test_backfill_evidence_artifact_identity_script.py -q`

Expected: script module missing.

- [ ] **Step 3: Implement CLI composition without arbitrary backend parameters**

Load the existing application Settings, construct the existing configured Local/MinIO store, repository, and service. Resolve the report path and reject paths outside the repository workspace or an explicit workspace-local output directory. Serialize only identities, counts, old/computed values, and reason codes.

- [ ] **Step 4: Run tests and dry-run help**

Run:

```powershell
python -m pytest services/api/tests/test_backfill_evidence_artifact_identity_script.py -q
python scripts/backfill_evidence_artifact_identity.py --help
python -m ruff check scripts/backfill_evidence_artifact_identity.py services/api/tests/test_backfill_evidence_artifact_identity_script.py
```

- [ ] **Step 5: Commit Task 3**

```powershell
git add scripts/backfill_evidence_artifact_identity.py scripts/README.md services/api/tests/test_backfill_evidence_artifact_identity_script.py
git commit -m "[AI编程] feat: 提供证据身份回填命令与报告；模块：Evidence/Ops；数据库迁移：否；前端菜单路由：否；测试风险：生产对象缺失需部署前dry-run核查"
```

### Task 4: PostgreSQL identity constraints and `ui_tree`

**Files:**
- Create: `services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py` (initial E06 portion; Task 6 extends the same migration before merge)
- Create: `services/api/tests/test_evidence_identity_migration.py`
- Modify: `services/api/tests/test_common_capture_migration.py` only if the shared migration-head contract requires it.

**Interfaces:**
- Produces PostgreSQL enforcement for nonblank path, canonical SHA-256, nonnegative non-NULL size, and `ui_tree` artifact type.
- Consumes: Task 3 report operationally, but migration itself performs only SQL preflight/constraints.

- [ ] **Step 1: Resolve the actual migration head**

Run:

```powershell
python -m alembic -c services/api/migrations/alembic.ini heads
Get-ChildItem services/api/migrations/versions -File | Sort-Object Name | Select-Object -Last 5 Name
```

Choose a revision/down_revision that creates one head in the worktree.

- [ ] **Step 2: Write failing migration behavior tests**

Import the migration module and execute `upgrade()`/`downgrade()` against a recording fake `op`. Assert ordered SQL behavior, not just arbitrary source text:

1. preflight detects unresolved rows and raises;
2. temporary checks are added `NOT VALID`;
3. checks are validated before `SET NOT NULL`;
4. checksum format is canonical;
5. artifact type values include `ui_tree`;
6. downgrade never emits DELETE/UPDATE against evidence rows.

- [ ] **Step 3: Run and observe RED**

Run: `python -m pytest services/api/tests/test_evidence_identity_migration.py -q`

Expected: migration import failure.

- [ ] **Step 4: Implement E06 migration SQL**

Use a `DO $$ BEGIN IF EXISTS (...) THEN RAISE EXCEPTION ... END IF; END $$` preflight. Add/validate temporary non-null checks, set column NOT NULL, retain path/format/nonnegative checks, and rebuild `ck_qc_evidence_artifact_type` with all currently frozen values plus `ui_tree`.

- [ ] **Step 5: Run migration-focused regressions**

Run:

```powershell
python -m pytest services/api/tests/test_evidence_identity_migration.py services/api/tests/test_common_capture_migration.py services/api/tests/test_h5_capture_role_alignment_migration.py -q
python -m ruff check services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py services/api/tests/test_evidence_identity_migration.py
```

- [ ] **Step 6: Commit Task 4**

```powershell
git add services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py services/api/tests/test_evidence_identity_migration.py services/api/tests/test_common_capture_migration.py
git commit -m "[AI编程] feat: 强制证据附件数据库身份；模块：Evidence/DB；数据库迁移：是，新增附件身份约束并允许ui_tree；前端菜单路由：否；测试风险：真实PG大表VALIDATE锁等待待验收"
```

### Task 5: Exit Gate v2 domain and typed business outcome

**Files:**
- Modify: `services/api/app/domain/exploration/evidence_exit_gate.py`
- Modify: `services/api/app/domain/exploration/__init__.py`
- Modify: `services/api/app/application/evidence_exit_gate.py`
- Modify: `services/api/tests/test_evidence_exit_gate.py`

**Interfaces:**
- Produces enum `EvidenceExitBusinessOutcome`: `EVIDENCE_INSUFFICIENT`, `EVIDENCE_PENDING`, `CONTROL_PLANE_INCOMPLETE`, `EVIDENCE_INTEGRITY_FAILED`.
- Produces `EvidenceExitBlockedOutcome` with `outcome_id`, lineage, `error_code`, and `retryable`.
- `EvidenceExitGateSnapshot` v2 adds optional Manifest identity/status/reason fields while `from_dict` accepts v1.
- `EvidenceExitGateBlockedError` exposes `.decision`, `.outcome`, `.error_code`, `.retryable`.

- [ ] **Step 1: Write failing v1 compatibility and v2 classification tests**

Cover literal payloads rather than round-tripping the same builder:

- a historical v1 snapshot parses without new fields;
- fail Manifest + RequirementSet classifies `evidence_insufficient`, code `EVIDENCE_INSUFFICIENT`, retryable false;
- pending Manifest classifies `evidence_pending`, retryable true;
- absent Manifest or RequirementSet classifies `control_plane_incomplete`, retryable true;
- an explicitly detected lineage-integrity failure builds `evidence_integrity_failed`, retryable false;
- outcome/evaluation identity changes when Manifest version/hash changes but not when `evaluated_at` changes.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest services/api/tests/test_evidence_exit_gate.py -q`

Expected: missing enum/outcome and unsupported v2 fields.

- [ ] **Step 3: Implement versioned domain contracts**

Keep `EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1` accepted and set the new default to `evidence-exit-gate-snapshot.v2`. Make required fields depend on protocol version. Build the outcome only from the persisted snapshot; do not inspect artifacts or rules.

- [ ] **Step 4: Implement typed blocked exception**

Map outcomes to stable codes in one dictionary in `application/evidence_exit_gate.py`. The exception constructor requires both a blocked decision and an outcome whose evaluation identity matches.

- [ ] **Step 5: Run focused domain regressions**

Run:

```powershell
python -m pytest services/api/tests/test_evidence_exit_gate.py services/api/tests/test_v2_protocol_contracts.py -q
python -m ruff check services/api/app/domain/exploration/evidence_exit_gate.py services/api/app/application/evidence_exit_gate.py services/api/tests/test_evidence_exit_gate.py
```

- [ ] **Step 6: Commit Task 5**

```powershell
git add services/api/app/domain/exploration/evidence_exit_gate.py services/api/app/domain/exploration/__init__.py services/api/app/application/evidence_exit_gate.py services/api/tests/test_evidence_exit_gate.py
git commit -m "[AI编程] feat: 定义门禁证据不足业务裁决；模块：Evidence/Gate；数据库迁移：否；前端菜单路由：否；测试风险：生产历史v1载荷抽样待验证"
```

### Task 6: Persist Manifest lineage and E15 outcome atomically

**Files:**
- Modify: `services/api/app/infrastructure/db/evidence_exit_gate_repository.py`
- Modify: `services/api/tests/test_evidence_exit_gate_persistence.py`
- Modify: `services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py`
- Modify: `services/api/tests/test_evidence_identity_migration.py`

**Interfaces:**
- Consumes Task 5 `build_evidence_exit_blocked_outcome(snapshot, evaluation_id)`.
- Produces evaluation columns: `manifest_id`, `manifest_version`, `manifest_hash`, `manifest_quality_status`, `business_outcome`, `outcome_payload`.
- `PostgresEvidenceExitGate.reserve()` raises the typed Task 5 exception only after the evaluation/outcome row has been written.

- [ ] **Step 1: Write failing repository tests**

Extend the fake gate row with a fail Manifest. Assert the evaluation INSERT receives the exact Manifest identity, `evidence_insufficient`, and outcome payload before raising. Add pending/no-Manifest cases and assert no Evidence Request INSERT/UPDATE appears in recorded SQL.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest services/api/tests/test_evidence_exit_gate_persistence.py -q`

Expected: snapshot lacks Manifest fields and INSERT lacks outcome columns.

- [ ] **Step 3: Extend gate query and parser**

`latest_manifest` must select `manifest_id`, `manifest_version`, `manifest_hash`, `quality_status`, and `quality_reason` from the newest version. `_parse_gate_snapshot` copies those values without reading `manifest_payload` or recalculating completeness.

- [ ] **Step 4: Persist decision and outcome in one transaction**

For blocked decisions build the Task 5 outcome, serialize it, and insert all lineage/outcome columns in the existing evaluation INSERT. On idempotent conflict, reload both `decision_payload` and `outcome_payload`, verify their linked identities, then raise the same typed error.

- [ ] **Step 5: Extend the existing migration and migration tests**

Add nullable lineage/outcome columns for old rows plus CHECK constraints:

- allow=true → business_outcome/outcome_payload NULL;
- allow=false → both present;
- evidence_insufficient → fail Manifest identity and requirement_set_id present;
- outcome vocabulary is closed.

Do not rewrite historical v1 rows.

- [ ] **Step 6: Run persistence and migration regressions**

Run:

```powershell
python -m pytest services/api/tests/test_evidence_exit_gate_persistence.py services/api/tests/test_v2_control_plane_repositories.py services/api/tests/test_evidence_identity_migration.py -q
python -m ruff check services/api/app/infrastructure/db/evidence_exit_gate_repository.py services/api/tests/test_evidence_exit_gate_persistence.py services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py
```

- [ ] **Step 7: Commit Task 6**

```powershell
git add services/api/app/infrastructure/db/evidence_exit_gate_repository.py services/api/tests/test_evidence_exit_gate_persistence.py services/api/migrations/versions/20260813_0049_evidence_artifact_identity_and_exit_outcome.py services/api/tests/test_evidence_identity_migration.py
git commit -m "[AI编程] feat: 原子持久化门禁Manifest血缘；模块：Evidence/Gate/DB；数据库迁移：是，扩展门禁裁决审计；前端菜单路由：否；测试风险：真实PG并发幂等待验证"
```

### Task 7: H5 production mapping and no-recapture proof

**Files:**
- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/tests/test_h5_exploration_turn.py`
- Modify: `services/api/tests/test_capture_coordinator.py`

**Interfaces:**
- Consumes typed `EvidenceExitGateBlockedError` from Task 5/6.
- Produces Work mapping: insufficient → `BLOCKED`; pending/control-plane incomplete → `RETRY_WAIT`; integrity failure → `FAILED`.

- [ ] **Step 1: Write failing H5 mapping tests**

Build real typed exceptions with literal snapshots. Through `H5ExplorationTurnRunner.run_one`, assert target transition and error code:

| outcome | work status | code |
|---|---|---|
| evidence_insufficient | blocked | EVIDENCE_INSUFFICIENT |
| evidence_pending | retry_wait | EVIDENCE_COMMIT_PENDING |
| control_plane_incomplete | retry_wait | EVIDENCE_CONTROL_PLANE_INCOMPLETE |
| evidence_integrity_failed | failed | EVIDENCE_INTEGRITY_FAILED |

In `test_capture_coordinator.py`, use a blocking real gate fake and an executor that raises if called; assert the executor and CaptureWriter each receive zero calls after the gate blocks.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest services/api/tests/test_h5_exploration_turn.py services/api/tests/test_capture_coordinator.py -q -k "exit_gate or evidence_insufficient"`

Expected: insufficient maps to FAILED and/or loses the stable error code.

- [ ] **Step 3: Implement explicit mapping**

Add `_failure_target(exc)` before generic retryability logic. Do not inspect exception strings. Preserve the existing lease transition path so audit/work status changes remain atomic at repository level.

- [ ] **Step 4: Prove no automatic recapture or request publication**

The coordinator test must assert no action/capture call. Repository tests from Task 6 must assert no SQL mutation of `qc_evidence_request`; do not add new mocks whose own calls are the only behavior under test.

- [ ] **Step 5: Run H5 and Web gate regressions**

Run:

```powershell
python -m pytest services/api/tests/test_h5_exploration_turn.py services/api/tests/test_capture_coordinator.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_evidence_exit_gate_persistence.py -q
python -m ruff check services/api/app/application/h5_exploration_turn.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_capture_coordinator.py
```

- [ ] **Step 6: Commit Task 7**

```powershell
git add services/api/app/application/h5_exploration_turn.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_capture_coordinator.py
git commit -m "[AI编程] fix: 将Web门禁阻断映射为业务终态；模块：H5/Evidence；数据库迁移：否；前端菜单路由：否；测试风险：真实Playwright离场动作待验收"
```

### Task 8: Regression rules, full verification, and merge readiness

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-08-13-qingyun-evidence-identity-and-gate.md` only to check completed steps and record exact command substitutions.

**Interfaces:**
- Consumes all prior tasks.
- Produces final reusable rules and fresh verification evidence.

- [x] **Step 1: Add reusable regression rules**

Document in Chinese:

- formal Artifact identity is path + canonical SHA-256 + nonnegative size;
- historical recovery always reads actual bytes and fails closed on missing/conflict;
- migration never performs object-store I/O or invents default identities;
- Exit Gate only reads Manifest quality and retains Manifest/RequirementSet lineage;
- deterministic evidence insufficiency is business blocked/needs_review and never triggers automatic Evidence Request/Probe.

- [x] **Step 2: Run the focused backend suite**

```powershell
python -m pytest services/api/tests/test_evidence_artifact_identity.py services/api/tests/test_evidence_artifact_backfill.py services/api/tests/test_evidence_artifact_backfill_repository.py services/api/tests/test_backfill_evidence_artifact_identity_script.py services/api/tests/test_evidence_identity_migration.py services/api/tests/test_evidence_completeness.py services/api/tests/test_evidence_exit_gate.py services/api/tests/test_evidence_exit_gate_persistence.py services/api/tests/test_evidence_manifest_repository.py services/api/tests/test_capture_coordinator.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py -q
```

- [x] **Step 3: Run the full backend suite and Ruff**

```powershell
python -m pytest services/api/tests -q
python -m ruff check services/api scripts
```

Expected: zero failures and zero Ruff errors. If the full historical suite contains unrelated pre-existing failures, reproduce them on the clean base commit before classifying them as baseline; do not silently waive failures.

- [x] **Step 4: Run migration topology and compile checks**

```powershell
python -m alembic -c services/api/migrations/alembic.ini heads
python -m compileall -q services/api/app scripts/backfill_evidence_artifact_identity.py
git diff --check dev_1.5...HEAD
```

Expected: one Alembic head, compile exit 0, no whitespace errors.

- [x] **Step 5: Run available frontend regression even though routes are unchanged**

```powershell
Set-Location app/workstation
npm test
npm run build
Set-Location ../..
```

Expected: Vitest and production build pass. This protects the existing `evidence_insufficient` display contract.

- [x] **Step 6: Review requirements line by line**

Compare the implementation with `docs/superpowers/specs/2026-08-13-qingyun-evidence-identity-and-gate-design.md` sections 5–16 and record any intentionally unexecuted environment acceptance item: real PostgreSQL large-table validation, real MinIO historical scan, Playwright action suppression, Worker restart/idempotency.

- [x] **Step 7: Commit documentation and plan tracking**

```powershell
git add docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-08-13-qingyun-evidence-identity-and-gate.md
git commit -m "[AI编程] doc: 固化证据身份与门禁回归规则；模块：Evidence/H5；数据库迁移：已包含；前端菜单路由：无需同步；测试风险：真实PG/MinIO/Playwright已按验收记录"
```

- [x] **Step 8: Request final code review**

Use `superpowers:requesting-code-review` with base SHA `770be7d` and current HEAD. Fix every Critical/Important finding with a failing regression test first, then rerun Steps 2–5.

- [x] **Step 9: Finish and merge**

Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. The user has already selected local merge to `dev_1.5`; after fresh branch verification, merge from the main checkout, rerun Steps 2–5 on the merged tree, remove the owned `.worktrees/...` directory, and delete the feature branch only after merged verification succeeds.

### 执行记录（2026-08-13）

- 聚焦后端回归：90 项通过。
- 功能分支首次全量后端回归：2886 项通过、21 项跳过；与最新 `dev_1.5` 合流并最终合并后的全量回归：2938 项通过、21 项跳过。30 条 warning 均为既有第三方弃用或测试密钥长度提示。
- Ruff、compileall、`git diff --check` 和 Alembic 单 head 检查通过，当前 head 为 `20260813_0049`。
- 工作台 Vitest：8 个文件、24 项测试通过；`vue-tsc + vite build` 通过。
- 全量回归在 Windows 暴露了历史 APK 暂存文件句柄未显式关闭问题；已用持有句柄引用的确定性回归复现并修复，避免 `os.replace` 依赖垃圾回收释放文件锁。
- 测试使用 worktree 内可执行 `.venv`，pytest 的 `--basetemp` 与 npm cache 均放在 worktree 内，未使用用户临时目录规避沙箱限制。
- 尚未执行的环境验收：真实 PostgreSQL 大表 `VALIDATE CONSTRAINT` 锁等待、真实 MinIO 历史对象扫描与故障注入、PostgreSQL + Playwright 的页面离场动作抑制、Worker 崩溃/重启后的幂等恢复。这些项目必须在部署验收中完成，不能由当前单元/合同测试替代。
- 设计第 12 节的低基数指标属于建议增强项，本批次已提供稳定原因码、结构化回填报告和 Gate Outcome 审计载荷；指标采集器接入未纳入 E04/E05/E06/E15 强制验收，后续可在统一 metrics 基础设施中补齐。
- 两轮独立代码审查共发现 2 个 Critical、5 个 Important、2 个 Minor。Critical 与全部 Important 均已按失败回归修复：H5 blocked 终态载荷、Bootstrap 路径身份、Outcome/数据库列一致性、生产血缘完整性分类及 pass Manifest 冲突禁止放行、E05 脱敏审计报告和有界读取重试；重复校验 Minor 同步清理，有限并发吞吐 Minor 保留为真实 MinIO 压测项。
- 已把最新 `dev_1.5` 合入隔离 worktree，解决专项规则文档追加冲突并保留 H5 Auth 与 E15 两组领域导出；合并提交落到 `dev_1.5` 后再次完成后端全量、Ruff、compileall、Alembic 单 head、工作台 Vitest 与生产构建验证。
