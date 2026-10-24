# H5 Business Page Popup Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 H5 完整探索任务以 3600 秒为唯一总执行时限；取消 Web 单动作 30 秒固定上限；对同一业务页面（规范化 URL + `page_type`）最多连续处理 20 层可信弹窗，并在第 21 层前确定性收口。

**Architecture:** 保留 `ActionIntent.deadline` 作为统一任务截止时间，Web Runtime 只把“剩余任务时间”下发给浏览器驱动。新增一个无副作用的 H5 业务页弹窗循环策略，使用已持久化 `H5TaskFrontierSnapshot` 统计同一页面身份下已经完成的可信弹窗动作；`H5CompletionAwareExplorer` 在委托 Agent 前执行该策略。页面捕获层继续负责单层弹窗的真实点击与前后验证，不在驱动内部隐藏多次点击。

**Tech Stack:** Python 3.12、FastAPI、pytest/pytest-asyncio、Playwright、PostgreSQL 持久化 H5 Frontier、Docker Compose。

## Global Constraints

- [ ] 不引入数据库迁移，不修改前端菜单或路由。
- [ ] Docker 只能 `--no-build` 重启；实施期间先运行本地测试，再重建已有镜像容器。
- [ ] `scan_mode=full` 的总时长必须保持 3600 秒；quick/custom 现有配置不被意外改写。
- [ ] 单动作不设置 30 秒等固定上限，但仍受任务剩余截止时间约束；截止时间已到时立即返回 `ACTION_DEADLINE_EXCEEDED`。
- [ ] `line_loading`、`startup_ad` 不进入业务页弹窗 20 层计数。
- [ ] 页面身份为“规范化 URL + 大小写归一的 `page_type`”；URL 保留 query 和 fragment。
- [ ] 每层弹窗都经过现有 before capture → 动作 → after capture → 页面检测链路。
- [ ] 所有新增注释和规则文档使用中文 UTF-8。

---

### Task 1: Remove the fixed Web action timeout cap

**Files:**

- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`

- [ ] **Step 1: Replace the old cap assertion with a remaining-deadline assertion**

  In `test_web_capture_runtime.py`, rename the existing regression test:

  ```python
  async def test_web_interaction_uses_task_deadline_without_fixed_action_cap() -> None:
  ```

  Build an intent whose deadline is approximately one hour in the future, execute it through `BrowserInteractionExecutor`, then assert:

  ```python
  timeout_ms = browser.requests[0].timeout_ms
  assert 3_500_000 <= timeout_ms <= 3_600_000
  ```

  This range makes scheduling drift harmless while proving that the old `30_000` cap is gone.

- [ ] **Step 2: Add an expired-deadline regression**

  Add a test with a deadline in the past and assert:

  ```python
  assert result.status is InteractionResultStatus.TIMED_OUT
  assert result.error_code == "ACTION_DEADLINE_EXCEEDED"
  assert browser.requests == []
  ```

- [ ] **Step 3: Run the focused tests and confirm RED**

  Run:

  ```powershell
  pytest -q services/api/tests/test_web_capture_runtime.py -k "task_deadline_without_fixed_action_cap or expired_deadline"
  ```

  Expected RED before the production edit: the future-deadline test observes `30_000` ms.

- [ ] **Step 4: Remove the fixed cap in production**

  Delete:

  ```python
  _WEB_INTERACTION_OPERATION_TIMEOUT_SECONDS = 30.0
  ```

  Replace the capped calculation with the already computed remaining duration:

  ```python
  timeout_seconds = _remaining_timeout_seconds(intent.deadline)
  if timeout_seconds <= 0:
      ...
  ```

  Do not add another action-local timeout constant.

- [ ] **Step 5: Run the focused tests and confirm GREEN**

  Run:

  ```powershell
  pytest -q services/api/tests/test_web_capture_runtime.py
  ```

- [ ] **Step 6: Commit the deadline change**

  ```powershell
  git add services/api/app/infrastructure/browser/capture_runtime.py services/api/tests/test_web_capture_runtime.py
  git commit -m "[AI编程] fix: H5 Web动作仅受任务截止时间约束（浏览器运行时；无迁移；无前端同步；真实长动作待E2E验证）"
  ```

---

### Task 2: Add a pure H5 business-page popup loop policy

**Files:**

- Create: `services/api/app/application/h5_popup_loop_policy.py`
- Create: `services/api/tests/test_h5_popup_loop_policy.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `docs/superpowers/specs/2026-08-15-h5-business-page-popup-loop-design.md`

- [ ] **Step 1: Write URL/page identity tests**

  Cover the public helper:

  ```python
  build_h5_business_page_key(location: str, page_type: str | None) -> str | None
  ```

  Required expectations:

  ```python
  assert build_h5_business_page_key(
      "HTTPS://Example.COM:443/path?tab=1#vip", "HOME"
  ) == "https://example.com/path?tab=1#vip|home"
  assert build_h5_business_page_key("https://example.com", "home") == (
      "https://example.com/|home"
  )
  assert build_h5_business_page_key("https://example.com/", "line_loading") is None
  assert build_h5_business_page_key("https://example.com/", "startup_ad") is None
  assert build_h5_business_page_key("not-a-url", "home") is None
  assert build_h5_business_page_key("https://example.com/", None) is None
  ```

  The implementation lowercases scheme/host, removes default `:80`/`:443`, ensures an empty path becomes `/`, and preserves query/fragment.

- [ ] **Step 2: Preserve source page type on trusted popup candidates**

  Extend the existing trusted-popup observation test to assert:

  ```python
  candidate.metadata["source_page_type"] == "home"
  ```

  In `_candidate_actions`, add `source_page_type` only to trusted popup-dismissal candidate metadata (when `popup_action_parameters is not None` and the source type is non-empty). Do not infer it from `expected_next_page_type`: that field describes the post-action expectation and may legitimately be `None`.

- [ ] **Step 3: Write trusted-popup classification tests**

  Introduce an internal predicate whose observable behavior is tested through the public evaluator. A candidate counts only when all of these are true:

  - `action_type is InteractionOperation.CLICK`
  - `risk_level is InteractionRiskLevel.LOW`
  - `navigation_depth == 0`
  - `metadata["target_keywords"]` includes `popup_dismissal` and `close_control`
  - `metadata["action_parameters"]["target_mode"] == "popup_dismissal"`
  - `popup_dismissal_capability` is non-empty and is also present in `target_keywords`
  - `obstruction_signature` is non-empty in both candidate metadata and action parameters and the two values match

  Add negative cases for a business navigation click, an untrusted popup candidate, mismatched signatures, and a high-risk action.

- [ ] **Step 4: Write terminal-entry counting tests**

  Define:

  ```python
  H5_BUSINESS_PAGE_POPUP_LIMIT = 20

  @dataclass(frozen=True, slots=True)
  class H5BusinessPagePopupLoopState:
      page_key: str
      completed_count: int
      limit: int
      limit_reached: bool
  ```

  Public evaluator:

  ```python
  evaluate_h5_business_page_popup_loop(
      *,
      snapshot: H5TaskFrontierSnapshot,
      location: str,
      page_type: str | None,
      current_candidates: Sequence[CandidateAction],
  ) -> H5BusinessPagePopupLoopState | None
  ```

  Its tests must prove:

  - 19 completed trusted popup entries on the same page returns `completed_count == 19` and `limit_reached is False`.
  - 20 completed trusted popup entries returns `completed_count == 20` and `limit_reached is True` when the current frontier still presents a trusted popup dismissal.
  - Entries on another normalized URL or another `page_type` do not count.
  - A current `SELECTED` entry is not counted as completed.
  - `EXECUTED` counts when it has `selected_at` and `finished_at`.
  - `FAILED` counts only when it has both timestamps, because it was actually selected and finished.
  - `BLOCKED` counts only when it has both timestamps; policy-blocked or never-selected entries do not consume the loop budget.
  - If the current candidates contain no trusted popup action, the evaluator returns `None`, even if historical count is 20.
  - Transient page types return `None`.

- [ ] **Step 5: Run the new tests and confirm RED**

  Run:

  ```powershell
  pytest -q services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py -k "popup_loop or trusted_popup_dismissal"
  ```

  Expected RED: module does not exist.

- [ ] **Step 6: Implement the policy without persistence changes**

  Implement only pure functions over immutable domain objects. Use `urllib.parse.urlsplit/urlunsplit`; do not add a table, new Frontier field, or mutable global counter. Read historical source type only from `entry.candidate.metadata["source_page_type"]` and compare its casefolded value with the current page type. Missing source provenance is fail-closed and does not consume the limit. Compare each entry's normalized `source_location` with the current normalized location.

  Do not count the current `SELECTED` entry. Treat a terminal entry as attempted only when timestamp requirements prove it was actually selected and finished.

- [ ] **Step 7: Run policy tests and static checks**

  Run:

  ```powershell
  pytest -q services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py
  ruff check services/api/app/application/h5_popup_loop_policy.py services/api/app/application/h5_exploration_observation.py services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py
  ```

- [ ] **Step 8: Commit the policy**

  ```powershell
  git add services/api/app/application/h5_popup_loop_policy.py services/api/app/application/h5_exploration_observation.py services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py docs/superpowers/specs/2026-08-15-h5-business-page-popup-loop-design.md
  git commit -m "[AI编程] feat: 增加H5业务页弹窗20轮策略（探索应用层；无迁移；无前端同步；组合根集成待后续提交）"
  ```

---

### Task 3: Enforce the popup limit at the H5 completion boundary

**Files:**

- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [ ] **Step 1: Add completion-boundary tests**

  Add focused async tests around `H5CompletionAwareExplorer.decide_next` with a delegate that records calls.

  Build work whose observation has:

  ```python
  observation.location = "https://example.test/home"
  observation.page_type = "home"
  observation.page_state = "popup"
  observation.metadata[H5_TASK_FRONTIER_METADATA_KEY] = snapshot.to_dict()
  ```

  Required cases:

  1. 19 completed trusted popup entries plus a current trusted popup candidate: delegate is called exactly once and its decision is returned.
  2. 20 completed entries plus a current trusted popup candidate: delegate is not called; result is:

     ```python
     ExplorationTerminalStatus.BOUNDED
     "popup_layer_limit_exceeded:20/20"
     ```

  3. `startup_ad` with the same history: delegate is called.
  4. Same URL but different current `page_type`: delegate is called.
  5. Twenty historical popup entries but no current popup candidate: existing completion logic runs; the new policy does not invent a terminal result.

- [ ] **Step 2: Run the completion tests and confirm RED**

  Run:

  ```powershell
  pytest -q services/api/tests/test_h5_runtime_composition.py -k "popup_loop or popup_layer_limit"
  ```

- [ ] **Step 3: Integrate the pure policy before delegate selection**

  In `H5CompletionAwareExplorer.decide_next`:

  1. Recover `H5TaskFrontierSnapshot` from `H5_TASK_FRONTIER_METADATA_KEY` whenever the payload is a mapping, not only when the local candidate list is empty.
  2. Evaluate the business-page popup loop using current observation location, page type, and current frontier candidates.
  3. If `limit_reached` is true, return a bounded terminal with exact reason `popup_layer_limit_exceeded:20/20`.
  4. Otherwise preserve all existing auth, objective, completion, delegate and selected-candidate validation behavior.

  The check belongs before `await self._delegate.decide_next(work)` so the 21st popup action is never emitted.

- [ ] **Step 4: Run focused and neighboring regressions**

  Run:

  ```powershell
  pytest -q services/api/tests/test_h5_runtime_composition.py
  pytest -q services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_task_frontier.py
  ruff check services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_runtime_composition.py
  ```

- [ ] **Step 5: Commit the boundary integration**

  ```powershell
  git add services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_runtime_composition.py
  git commit -m "[AI编程] feat: 在H5完成边界限制业务页弹窗层数（运行时组合；无迁移；无前端同步；依赖真实站点回归）"
  ```

---

### Task 4: Retain the off-screen dialog close fix and update regression rules

**Files:**

- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Delete: `docs/superpowers/plans/2026-08-15-h5-browser-action-timeout.md`

- [ ] **Step 1: Review the existing off-screen regression**

  Confirm `test_offscreen_popup_close_control_uses_verified_direct_dispatch` still checks this sequence:

  1. trusted dialog close control is found;
  2. Playwright cannot scroll it into the viewport;
  3. direct DOM `element.click()` is used only for that verified close element;
  4. the original obstruction signature disappears after the click.

  Keep the 5-second `scroll_into_view_if_needed` sub-attempt as a local fallback probe. It is not the ActionIntent's total execution timeout and must not be described as one.

- [ ] **Step 2: Update the durable regression document**

  Replace the old rule claiming a 30-second Web action cap with rules that state:

  - H5 full task total duration is 3600 seconds.
  - Web single actions have no independent fixed time cap and consume only remaining task-deadline time.
  - Every stable business page key (`normalized URL + page_type`) may process at most 20 trusted popup layers.
  - `line_loading` and `startup_ad` are excluded.
  - Each popup layer must retain before/action/after/detection evidence.
  - Oversized fixed dialogs may use verified direct dispatch only after trusted-control identification and obstruction disappearance verification.

- [ ] **Step 3: Remove the superseded conflicting plan**

  Delete `docs/superpowers/plans/2026-08-15-h5-browser-action-timeout.md`, which documents the now-rejected 30-second action cap.

- [ ] **Step 4: Run browser capture regressions**

  Run:

  ```powershell
  pytest -q services/api/tests/test_page_capture_tool.py services/api/tests/test_capture_coordinator.py
  ruff check services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_page_capture_tool.py
  ```

- [ ] **Step 5: Commit the capture and documentation changes**

  ```powershell
  git add services/api/app/infrastructure/browser/page_capture.py services/api/tests/test_page_capture_tool.py docs/专项能力与高频回归规则.md
  git add -u docs/superpowers/plans/2026-08-15-h5-browser-action-timeout.md
  git commit -m "[AI编程] fix: 修复H5超屏弹窗关闭并固化回归规则（浏览器捕获；无迁移；无前端同步；需目标站E2E确认）"
  ```

---

### Task 5: Verify total duration configuration and run the complete local regression set

**Files:**

- Verify: `services/api/app/application/h5_task_runtime_settings.py`
- Verify: `services/api/app/application/v2_task_entries.py`
- Verify: `services/api/tests/test_h5_task_runtime_settings.py`
- Verify: all files modified in Tasks 1–4

- [ ] **Step 1: Prove full-mode duration remains 3600 seconds**

  Run:

  ```powershell
  pytest -q services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_task_execution_policy.py
  ```

  Confirm the existing full-mode test asserts `max_duration_seconds == 3600`. Do not force quick/custom modes to 3600.

- [ ] **Step 2: Run the combined regression suite**

  Run:

  ```powershell
  pytest -q services/api/tests/test_web_capture_runtime.py services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_page_capture_tool.py services/api/tests/test_capture_coordinator.py services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_task_execution_policy.py
  ```

- [ ] **Step 3: Run Ruff on every changed Python file**

  Run:

  ```powershell
  ruff check services/api/app/infrastructure/browser/capture_runtime.py services/api/app/infrastructure/browser/page_capture.py services/api/app/application/h5_popup_loop_policy.py services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_capture_tool.py services/api/tests/test_h5_popup_loop_policy.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_runtime_composition.py
  ```

- [ ] **Step 4: Inspect the final diff for scope and secret safety**

  Run:

  ```powershell
  git status --short
  git diff --check
  git diff --stat
  ```

  Do not stage `.env.local`, the local LLM bridge, the Compose override, API tokens, or the user-provided LLM key.

---

### Task 6: Restart Docker without a build and submit a fresh full H5 task

**Files:**

- Runtime-only: `.alioth-h5-v2.override.yml`
- Runtime-only: `.alioth-llm-bridge.py`
- Read configuration: `infra/env/.env.local.example`

- [ ] **Step 1: Record image IDs before restart**

  Run:

  ```powershell
  docker inspect alioth-api alioth-worker --format '{{.Name}} {{.Image}}'
  ```

- [ ] **Step 2: Recreate API and worker without building**

  Run:

  ```powershell
  docker compose -f infra/docker-compose.yml -f .alioth-h5-v2.override.yml --env-file infra/env/.env.local.example up -d --no-build --no-deps --force-recreate alioth-api alioth-worker
  ```

  Confirm the post-restart image IDs exactly match Step 1.

- [ ] **Step 3: Check effective non-secret configuration and health**

  Verify provider=`custom`, API style=`chat_completion`, model=`grok-4.5`, base URL points to the local host bridge, timeout=`60`, retries=`5`, run state backend=`postgres`, and H5 V2 is enabled. Never print the API key.

  Check API and worker health/log startup before posting a task.

- [ ] **Step 4: Login and submit the task without exposing the token**

  Authenticate with username `admin` and the supplied password, retain the bearer token only in process memory, then POST to `/api/v2/tasks` using the exact payload from the approved request. The real target string must be plain:

  ```json
  "url": "https://35.194.167.63:85/"
  ```

  Record only the returned `task_id`/`run_id`, not credentials or token.

---

### Task 7: Observe vector retrieval, worker behavior, popup evidence, and page coverage

**Files:**

- No source edits unless a new reproducible blocker is found.

- [ ] **Step 1: Monitor until a terminal outcome or the 3600-second task deadline**

  Poll task/run status and action execution records. Tail only relevant worker logs. Verify no action is marked `ACTION_TIMED_OUT` merely because it reached 30 seconds. An action may still return `ACTION_DEADLINE_EXCEEDED` when the task's remaining deadline is exhausted.

- [ ] **Step 2: Audit vector retrieval evidence**

  For each available page-type assessment, report:

  - repository configured/degraded status;
  - exact retrieval attempts and matches;
  - vector retrieval attempts and matches;
  - fallback/static matches;
  - whether the retrieved evidence influenced page-type assessment or candidate generation.

  “Vector attempted with zero matches” must be reported separately from “vector repository not applied.”

- [ ] **Step 3: Audit popup-loop evidence per business page key**

  Group trusted popup actions by normalized URL + page type. For each group report completed/failed/blocked selected attempts, confirm each layer has before/action/after/detection evidence, and confirm the count never exceeds 20. If a 21st popup is encountered, expect bounded reason `popup_layer_limit_exceeded:20/20`.

- [ ] **Step 4: List explored pages and calculate type coverage**

  Deduplicate explored states while retaining URL, page type, page state, capture ID, action path and terminal result. Compare against:

  ```text
  line_loading, startup_ad, home, content_list, content_detail,
  video_list, video_detail, ranking, profile, settings, payment,
  post_list, activity
  ```

  Coverage percentage is `distinct covered requested types / 13 * 100`. The target is approximately 90%, which requires at least 12 of 13 types. Separate genuinely unreachable types from exploration defects.

- [ ] **Step 5: Use TDD for any new blocker and repeat the fresh-task check**

  For every blocker that prevents safe exploration:

  1. preserve logs/captures and identify the failing boundary;
  2. add the smallest regression test and observe RED;
  3. implement the scoped fix and observe GREEN;
  4. update `docs/专项能力与高频回归规则.md` when reusable;
  5. run the impacted suite and Ruff;
  6. recreate containers again with `--no-build` only if the existing image already contains the source through bind mounts; otherwise report that the no-build constraint prevents runtime uptake instead of silently building;
  7. submit a new task and re-audit from clean evidence.

- [ ] **Step 6: Final verification and handoff**

  Run `git diff --check`, the complete relevant pytest set, Ruff, and a final `git status --short`. Report:

  - changed modules and commits;
  - Docker image IDs proving no build;
  - final task/run IDs and terminal reason;
  - vector retrieval evidence;
  - explored page inventory and 13-type coverage numerator/percentage;
  - popup attempts per business page and whether the 20-layer bound was exercised;
  - any target-site-unreachable page types or residual risks;
  - database migration: none;
  - frontend menu/route synchronization: none.

---

### Task 8: Keep every H5 V2 browser path at 390×844

**Files:**

- Create: `services/api/app/application/h5_viewport.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_auth_navigation.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`
- Modify: `services/api/tests/test_h5_auth_bootstrap.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Write failing consumer-boundary tests**

  Assert literal `viewport_width=390` and `viewport_height=844` values on the bootstrap Surface Session and navigation intent, normal startup/popup/tab candidate parameters, authentication return navigation, Frontier source restoration, and browser-loss Session plus recovery navigation.

- [x] **Step 2: Run the focused tests and confirm RED**

  ```powershell
  docker exec -w /workspace alioth-api python -m pytest services/api/tests/test_h5_exploration_workflow.py::test_surface_bootstrapper_registers_session_and_uses_dual_capture_navigation services/api/tests/test_h5_runtime_composition.py::test_h5_candidate_parameters_preserve_trusted_startup_ad_capability services/api/tests/test_h5_runtime_composition.py::test_h5_candidate_parameters_preserve_trusted_popup_capability services/api/tests/test_h5_runtime_composition.py::test_h5_candidate_parameters_preserve_trusted_bottom_navigation_capability services/api/tests/test_h5_runtime_composition.py::test_coordinator_recovery_rebaselines_before_returning_execution_work services/api/tests/test_h5_runtime_composition.py::test_browser_recovery_keeps_source_lost_and_registers_strict_new_session services/api/tests/test_h5_auth_bootstrap.py::test_auth_navigation_keeps_h5_mobile_viewport -q
  ```

  Expected and observed before implementation: 7 failures caused by absent viewport keys.

- [x] **Step 3: Add one immutable H5 viewport policy**

  Implement `h5_viewport_parameters() -> dict[str, int]` with fixed `390×844` values. Inject a fresh dictionary into every H5 Session specification and ActionIntent boundary；H5 Completion 包装器还必须在精确候选验签前归一化通用 Agent 构造的 ActionIntent。固定值覆盖认证调用方或通用 Agent 的桌面默认参数。

- [x] **Step 4: Re-run the same focused tests and confirm GREEN**

  Expected and observed after the first implementation: 7 passed. The real task then proved the persisted ActionIntent was `390×844` while PNG remained `1280×720`, because the earlier before Capture had already created the lazy Playwright Context. Add a Web Runtime regression asserting before/action/after requests all receive `390×844`, propagate viewport through `CaptureWriteRequest`, and re-run Coordinator/Web/App adjacent suites plus a fresh no-build PNG check before claiming runtime acceptance.

---

### Task 9: Preserve fixed navigation in bounded UI Snapshot

**Files:**

- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `docs/专项能力与高频回归规则.md`

- [x] **Step 1: Preserve the real failure evidence**

  The fresh `390×844` task reached `video_list`, but its 300-element UI Snapshot omitted the bottom Tabbar even though the DOM retained it. The following structural navigation action therefore failed closed with `SEMANTIC_TARGET_NOT_FOUND`.

- [x] **Step 2: Write and observe a RED Chromium regression**

  Build a rendered page with 320 ordinary actions followed by a fixed Vant-style bottom navigation. Confirm the old collector reaches its 300-element limit and drops all navigation labels.

- [x] **Step 3: Prioritize navigation inside the existing bounded candidate set**

  Reorder only candidates already admitted by the UI Snapshot contract. The first real retry proved that promoting every descendant of `[class*='van-tabs']` also promotes the complete content pane and still starves the bottom Tabbar. Split bounded reserves into bottom navigation, Tab headers and general navigation; never promote the entire Tab content subtree. Preserve deduplication, visibility checks, the 300-element limit, the 512 KiB serialized limit and downstream unique semantic verification.

- [x] **Step 4: Run impacted verification and repeat the no-build task**

  Run the full page-capture and semantic navigation suites plus Ruff, update the reusable rule, commit, restart Docker without build, submit a fresh task and confirm the long `video_list` page can leave through its bottom navigation.

---

### Task 10: Keep global bottom navigation out of stale local-source recovery

**Files:**

- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/tests/test_h5_exploration_turn.py`
- Modify: `docs/专项能力与高频回归规则.md`

- [x] **Step 1: Preserve the post-navigation restoration failure**

  The repaired `video_list` action completed and landed on stable `/product` home. The next global “广场” Tabbar candidate still referenced the earlier root-home state, so generic stale-source recovery navigated `/` twice, retriggered `startup_ad`, exhausted restoration and left the Work/Run inconsistent.

- [x] **Step 2: Write and observe a RED recovery regression**

  Construct a signed static `bottom_navigation` candidate from root home while the active observation is another stable home URL/state. Confirm the old runner invokes the recovery port instead of executing the globally visible bottom navigation.

- [x] **Step 3: Skip stale-source replay only for trusted global bottom navigation**

  Require the exact platform Tabbar selector, click/low/depth-1 contract, trusted navigation source and a stable, unobstructed, unauthenticated business observation. Continue to send page-local Tabs and every other stale selector through bounded source recovery; Interaction Runtime still revalidates the current UI Snapshot before any click.

- [x] **Step 4: Verify and repeat the no-build task**

  Run the H5 turn/runtime suites and Ruff, commit, restart without build, submit a fresh task and confirm the next global bottom-navigation siblings execute without navigating the root URL.

---

### Task 11: Capture custom `.btn` interaction containers

**Files:**

- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `docs/专项能力与高频回归规则.md`

- [x] **Step 1: Prove the profile action exists outside the UI Snapshot**

  On stable `/mine`, DOM, visible text and screenshot contained “去签到”, but the 48-element non-truncated UI Snapshot omitted its `.btn > div` event structure. The semantic resolver correctly failed closed with `SEMANTIC_TARGET_NOT_FOUND`.

- [x] **Step 2: Write and observe a RED Chromium regression**

  Render `.btn > div` with `cursor:pointer` and assert the parent event container is captured once with exact text and `interactive=true`. The old candidate selector returns no target.

- [x] **Step 3: Admit only the exact `btn` class token**

  Add `[class~='btn']` to the existing bounded candidate query. Keep visibility, hit testing, serialization limits and unique semantic resolution unchanged; do not use a broad substring selector.

- [x] **Step 4: Verify and repeat the no-build task**

  Run page-capture and H5 runtime suites plus Ruff, commit, restart without build, submit a fresh task and confirm “去签到” reaches `activity` or produces a post-action page fact instead of a pre-action semantic miss.

---

### Task 12: Keep custom `.btn` semantics executable

**Files:**

- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`
- Modify: `docs/专项能力与高频回归规则.md`

- [x] **Step 1: Preserve the execution-boundary failure**

  The repaired profile UI Snapshot contains the exact “去签到” `.btn` container and a passing center hit test, but records `interactive=false`; the execution resolver filters it before text matching and returns `SEMANTIC_TARGET_NOT_FOUND`.

- [x] **Step 2: Write and observe a RED resolver regression**

  Reproduce the real custom `div.btn` element with exact text, non-native interaction metadata and a successful hit test. Confirm the old resolver rejects it.

- [x] **Step 3: Align resolver trust with the bounded capture contract**

  Admit only an exact `btn` class token as a custom click surface. Preserve disabled checks, current-snapshot unique semantic matching and center hit-test rejection; do not trust arbitrary `div` text or broad class substrings.

- [x] **Step 4: Verify and repeat the no-build task**

  Run the Web Capture Runtime, page-capture and H5 suites plus Ruff, commit, restart without build, submit a fresh task and confirm “去签到” produces a completed action with post-action evidence.

---

### Task 13: Restore profile siblings through direct-parent back

**Files:**

- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/application/h5_exploration_recovery.py`
- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`
- Modify: `services/api/tests/test_h5_exploration_turn.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Preserve the real activity-to-profile recovery failure**

  The no-build task completed “去签到” and recognized `/signin` as `activity`, but the next settings sibling navigated `/mine` instead of using browser history. That retriggered `startup_ad`; the second restore then attempted an invalid BOUNDED transition without an `ExplorationTerminal`, masking the root cause as a row-version/lease conflict.

- [x] **Step 2: Write RED lineage, fallback and terminal regressions**

  Require every successor observation to retain its direct parent state, require direct-parent recovery to choose back, require the second failed attempt to fall back to navigate, and require infrastructure-bounded transitions to include a matching terminal payload and the updated row version.

- [x] **Step 3: Implement exact direct-parent recovery**

  Sign `parent_state_version` into successor observation metadata. Use back only when the selected source state and URL match that exact parent and the current URL matches the active observation; use controlled navigate on attempt two. Emit a stable BOUNDED terminal for exhausted Frontier/browser recovery paths.

- [x] **Step 4: Verify before no-build replay**

  Run focused RED/GREEN tests, impacted H5 suites and Ruff before restarting the mounted API/Worker containers and submitting a fresh task.

---

### Task 14: Sign and execute list-to-detail first-card navigation

**Files:**

- Add: `services/api/app/application/h5_list_detail_navigation.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/infrastructure/browser/semantic_target_resolver.py`
- Modify: `services/api/tests/test_page_detection_tools.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`

- [x] **Step 1: Preserve the candidate-loss boundary**

  Real `video_list`, `post_list` and `content_list` DOMs contained cards, but the detector exposed their CSS as the candidate target. The H5 observation contract correctly rejected that platform locator, so no detail entry reached the task Frontier.

- [x] **Step 2: Cover real target structures**

  Add regressions for movie cards, `dynamicData-box` post cards and `ratio-box` content cards, plus the existing same-origin detail anchors. Require the detector result to expose a semantic target and a separate trusted selector capability.

- [x] **Step 3: Close the capability at every trust boundary**

  Maintain the source-list/expected-detail/selector allowlist in one application contract. Observation and Runtime revalidate action, risk, depth, stable page state, keywords and exact page-type mapping. The browser never executes the stored structural selector directly; it chooses the first current-viewport, center-hit card leaf from the trusted UI Snapshot.

- [ ] **Step 4: Verify sibling traversal in a fresh no-build task**

  Confirm `video_list → video_detail → back`, `post_list → content_detail(post detail) → back` and `content_list → content_detail → back`, then continue the remaining bottom-navigation and profile siblings without rebuilding the image.

---

### Task 15: Preserve browser-history parent across detail popups

**Files:**

- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Preserve the real detail-return failure**

  The first signed list-detail action reached and recognized `video_detail`. A popup appeared on that detail page; after one close Capture the global sibling still referenced `video_list`, but the immediate action parent was now another detail-popup state. Recovery could no longer prove the browser-history parent and navigated `/movie`, retriggering `startup_ad` before exhausting restoration.

- [x] **Step 2: Write RED history and repeated-popup regressions**

  Require a location-changing action to record its history parent, same-location popup actions to inherit it, and recovery to choose back when the selected source matches that preserved pair. Give a trusted popup occurrence a distinct candidate identity per Capture so an identical reappearing layer is not silently removed by the executed-candidate filter.

- [x] **Step 3: Implement bounded propagation**

  Persist `history_parent_state_version/location` separately from the immediate action parent. Accept it only when both selected source state and normalized source URL match. Preserve same-Capture idempotency while allowing subsequent Captures to close an identical layer; the existing business-page 20-round policy remains the hard bound.

- [ ] **Step 4: Repeat the no-build task**

  Confirm detail popups clear until stable, the following recovery action is `back` rather than `navigate`, the source list rebaselines successfully, and the next list/profile sibling executes.

---

### Task 16: Preserve repeated popup occurrences in the task Frontier and authorize back depth

**Files:**

- Modify: `services/api/app/domain/exploration/h5_frontier.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_h5_task_frontier.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Preserve the second real detail-popup failure**

  The no-build run reached `video_detail`; after the first close, its next Capture still contained a `z-index: 2007/2008` overlay/dialog. Observation created a new popup candidate, but the task Frontier deduplicated it by the unchanged obstruction signature. Recovery correctly selected `back`, yet omitted `navigation_depth`, so the browser safety gate rejected it as a same-page popup return.

- [x] **Step 2: Write RED Frontier-occurrence and back-capability regressions**

  Require a repeated trusted signature from a different Capture to remain pending while the same occurrence in one Capture stays idempotent. Require a history-parent `back` to carry the selected source entry's positive navigation depth.

- [x] **Step 3: Implement both bounded capabilities**

  Include the platform-issued popup candidate occurrence in task-Frontier semantic and entry fingerprints. Derive `back.navigation_depth` only from the unique selected entry matching the recovery source state.

- [ ] **Step 4: Repeat the no-build task again**

  Confirm both video-detail overlays are cleared before recovery, `back` passes the browser safety gate, `/movie` is rebaselined, and exploration proceeds to later list/profile siblings.

---

### Task 17: Require durable popup dismissal and filter stable inline close controls

**Files:**

- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `services/api/tests/test_page_detection_tools.py`

- [x] **Step 1: Preserve the third real-run blockers**

  On `video_detail`, the same trusted Dialog signature was clicked four times. Each old node disappeared long enough for immediate verification, then the identical ad was recreated before the next Capture, so the high-z hide fallback never ran. After the page finally became stable, a model-generated ordinary target `关闭` for the player inline ad entered Frontier, changed the detail to `loading`, and caused recovery to navigate the root URL and retrigger `startup_ad`.

- [x] **Step 2: Write RED durable-dismissal and stable-close regressions**

  Require the original Dialog signature to remain absent for 800ms before success. Require a stable business page without structural obstruction proof to discard generic close/dismiss candidates while retaining trusted popup candidates.

- [x] **Step 3: Implement conservative durability and filtering**

  Track signature absence inside the page predicate; a same-signature reappearance resets the stability clock, which makes the existing verified `z-index >= 500` Dialog/mask hide fallback run. Filter only close-like model actions when neither popup state nor trusted DOM obstruction exists.

- [ ] **Step 4: Repeat the no-build task with durable dismissal**

  Confirm the repeated video-detail ad triggers the high-z fallback, the stable detail exposes no ordinary `关闭` candidate, and the next recovery action is an authorized `back` to `/movie`.

---

### Task 18: Reject inline close controls when the model misclassifies popup state

**Files:**

- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/tests/test_page_detection_tools.py`

- [x] **Step 1: Preserve the fourth real-run blocker**

  The next no-build run cleared four trusted detail Dialog occurrences, but the model then labeled the remaining player page `video_detail|popup`. Because inline `<span>关闭</span>` was visible, the old text-only gate authorized it despite there being no real Dialog or structural obstruction proof.

- [x] **Step 2: Write a RED popup-misclassification regression**

  Require `page_state=popup` plus the real MoviePlayer inline-ad DOM to discard the model close action, while retaining the existing weak-structure acknowledgement regression.

- [x] **Step 3: Close the unscoped-text trust gap**

  Without a visible Dialog or structural proof, allow only explicit `我知道了/稍后` acknowledgement text. Generic `关闭/close/跳过/skip` can no longer obtain dismissal authority from page state plus arbitrary visible text.

- [ ] **Step 4: Repeat the no-build task after the stricter gate**

  Confirm the detail returns directly to its list, then continue the remaining list-detail and profile sibling traversal.

---

### Task 19: Prefer the preserved history parent when a child frontier becomes empty

**Files:**

- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/tests/test_h5_exploration_workflow.py`

- [x] **Step 1: Preserve the fifth real-run selection failure**

  The stricter close gate correctly removed the inline player action. The resulting `video_detail|popup` had no local candidates and still retained `/movie` as its exact history parent, but global selection chose an older root-page sibling first. Recovery therefore navigated `/`, retriggered startup flow and exhausted restoration.

- [x] **Step 2: Write a RED parent-before-root regression**

  Construct pending siblings from both root and video-list states while the active detail state is empty. Require selection of the video-list sibling identified by the preserved history-parent state and normalized location.

- [x] **Step 3: Add a narrow empty-child preference**

  Pass history-parent preference only when the newly observed page exposes no candidate and is not already at that parent location. Current popup layers and profile/list business candidates keep their original active-lineage priority.

- [ ] **Step 4: Repeat no-build traversal**

  Confirm the next recovery action is `back` with positive depth, `/movie` rebaselines, and exploration proceeds through later list-detail and profile siblings.

---

### Task 20: Ignore low-z video-detail containers without dismissal controls

**Files:**

- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/tests/test_page_detection_tools.py`

- [x] **Step 1: Add the explicit video-detail boundary**

  A video-detail Dialog/overlay pair with no trusted dismissal capability and a maximum explicit obstruction `z-index` of 500 must not enter the popup loop.

- [x] **Step 2: Scope z-index evidence to obstruction nodes**

  Parse only visible Dialog and `van-overlay` nodes. An unrelated page toolbar at `z-index: 9999` cannot promote a low-level player container into a popup.

- [x] **Step 3: Preserve real popup handling**

  Keep trusted close controls and obstruction layers above 500 on the existing bounded dismissal path; low-level ignored structures fall through to the empty-child history-parent return policy.

- [ ] **Step 4: Verify in the next no-build task**

  Confirm the final video-detail observation produces no popup action and the following action is a positive-depth `back` to `/movie`.

---

### Task 21: Accept a stable dynamic parent only after browser back

**Files:**

- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_exploration_recovery.py`
- Modify: `services/api/tests/test_h5_exploration_observation.py`
- Modify: `services/api/tests/test_h5_exploration_turn.py`

- [x] **Step 1: Preserve the sixth real-run recovery failure**

  The no-build run reached `profile → activity`, completed sign-in and returned with browser history to the exact `/mine` profile page. The business mutation changed its state version, so strict byte equality rejected the successful back, used controlled navigate and retriggered `startup_ad` on `/mine`.

- [x] **Step 2: Write RED platform-signing and dynamic-parent regressions**

  Require every candidate to carry the detector-derived source page type while ignoring any model-provided source type. Require a stable, recognized, unobstructed profile returned by the first back at the exact source URL to remain usable when only its dynamic state version changed.

- [x] **Step 3: Implement a back-only semantic equivalence boundary**

  Keep exact state-version matching as the default. On mismatch, accept only route `back`, exact normalized source URL, platform-signed source page type, recognized/stable state and no overlay or authentication requirement. Controlled navigate, missing signatures, different page types and blocked states remain strict failures.

- [ ] **Step 4: Repeat the no-build profile sibling traversal**

  Confirm `activity → back → profile` does not issue a follow-up navigate, then execute the pending `settings` and `payment` siblings before returning to remaining list-detail coverage.

---

### Task 22: Rebind the leased Work relation after source rebaseline

**Files:**

- Modify: `services/api/app/infrastructure/db/explorer_work_repository.py`
- Modify: `services/api/tests/test_v2_control_plane_repositories.py`

- [x] **Step 1: Preserve the seventh real-run persistence failure**

  Run 7 cleared the video-detail layers and returned to stable `/movie`, then executed the pending first bottom tab. Its after Capture reached `/product`, but successor commit failed because the leased Work relational `source_capture_id` still referenced the pre-back video-detail Capture while its updated JSON payload referenced the rebaseline video-list Capture.

- [x] **Step 2: Write a RED relational-rebind regression**

  Require `update_payload` to persist both the rebaseline Capture in the Work JSON and the same Capture in the indexed relational column before successor commit.

- [x] **Step 3: Rebind through an ownership-checked atomic update**

  In the existing running-lease and row-version guarded update, set `source_capture_id` from `qc_capture` only when it belongs to the same task and Surface Session and has status `captured`. Keep the payload hash, lease fencing and version increment in the same statement.

- [ ] **Step 4: Repeat the no-build traversal past the first restored sibling**

  Confirm the action after `video_detail → back → video_list` commits its successor instead of `H5_EXPLORATION_TURN_FAILED`, then continue profile and list-detail coverage.

---

### Task 23: Revalidate profile payment visual parameters at the selected-candidate boundary

**Files:**

- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Preserve the eighth real-run binding failure**

  Run 8 passed the restored video-list successor, covered later bottom tabs, entered `profile → activity`, and returned to stable `/mine`. The Frontier then correctly selected the screenshot-backed `立即开通 → payment` candidate, but the selected-candidate binding rejected its locally issued `visual_coordinate` parameters because the Runtime capability whitelist did not contain the profile-payment visual contract.

- [x] **Step 2: Write a RED visual-capability regression**

  Require the Runtime to preserve the complete platform-issued screenshot identity and bounded coordinate fields for a valid profile-payment candidate. The unmodified Runtime must fail by omitting `target_mode`; unrelated URLs, invalid checksums, confidence below `0.80`, and extra model parameters must remain rejected.

- [x] **Step 3: Add strict local revalidation**

  Accept only a stable `profile` observation at the same normalized URL, `click/medium/depth1`, an allowed membership entry label, expected `payment`, screenshot keywords, an exact parameter key set, valid SHA-256 identity, finite normalized bounds with area at most `35%`, and confidence at least `0.80`. The browser resolver still compares the source and current Capture snapshots before clicking, so a rebaseline visual drift remains retryable instead of executing stale coordinates.

- [ ] **Step 4: Repeat the no-build profile traversal**

  Confirm `activity → back → profile → payment → back → profile → settings` runs without the parameter-binding error, then continue any uncovered page types.

---

### Task 24: Enter post detail through the first visible title leaf

**Files:**

- Modify: `services/api/app/application/h5_list_detail_navigation.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_page_detection_tools.py`
- Test: `services/api/tests/test_page_capture_tool.py`
- Test: `services/api/tests/test_web_capture_runtime.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Preserve the ninth real-run navigation miss**

  Run 9 signed `.dynamicData-box:visible`; the click stayed on `/square` because the target site enters post detail only through the nested title.

- [x] **Step 2: Write title-only RED regressions**

  Require detection to sign `.dynamicData-box:visible .title:visible`, Snapshot to collect only the title nested below `dynamicData-content`, and Resolver to reject the parent, disabled/off-screen titles and later siblings.

- [x] **Step 3: Remove the whole-card fallback**

  Centralize the selector contract, use selector-specific `title` Snapshot proof, and return no `dynamicData` candidate when the title leaf is absent. Do not add a generic `.title` selector or visual-coordinate fallback.

- [ ] **Step 4: Verify the real post-detail round trip**

  Confirm `/square` clicks the first visible post title, reaches `content_detail`, applies the trusted popup/low-z policy, and returns by same-session back before the next sibling.

---

### Task 25: Skip uncatalogued pages without failing the H5 Run

**Files:**

- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/domain/exploration/h5_frontier.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/application/h5_page_type_assessment_adapter.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`

- [x] **Step 1: Drop uncatalogued optional targets as whole actions**

  An action whose explicit `expected_next_page_type` cannot resolve through the current Catalog is removed and audited; popup actions without an expected type and Catalog-backed actions remain eligible.

- [x] **Step 2: Represent an uncatalogued landing as a normal skip**

  When structure, route and navigation aliases cannot map the current page, return `unrecognized` with exact Catalog skip metadata, no candidates and no rule matching. Do not echo the model's invalid type value.

- [x] **Step 3: Persist skipped Frontier state and continue siblings**

  Add a terminal `skipped` entry status that consumes no coverage/failure counts. Assessment accepts only the exact skip pair; Workflow marks the selected entry skipped and uses the preserved history parent to select the next sibling.

- [ ] **Step 4: Verify the real uncatalogued route behavior**

  Report `ranking` as `uncatalogued/skipped`, confirm no technical task error, and verify a remaining Catalog-backed sibling executes after back recovery.

---

### Task 26: Recover the unique signed H5 candidate from a premature model terminal

**Files:**

- Modify: `services/api/app/agents/explorer/maf_agent.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Test: `services/api/tests/test_maf_explorer_agent.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`

- [x] **Step 1: Type the Run 9 early-terminal failure**

  Distinguish “eligible candidate exists but model returns bounded/no-progress” as `ExplorerPrematureTerminalError`; leave all other model/output failures unchanged.

- [x] **Step 2: Build a narrow H5-only fallback**

  Only a unique selected low/medium candidate with exact current Frontier identity can become an ActionIntent. Reuse platform-derived parameters, purpose, viewport and capture profile, prefix the identity with `h5-platform-fallback:`, then run the normal binding validator.

- [x] **Step 3: Keep every unsafe case closed**

  Multiple candidates, blocked/high-risk candidates, selected ID drift and generic Explorer errors never enter the fallback.

- [ ] **Step 4: Verify the content-list successor in a no-build task**

  Reproduce the prior `/ai` single-profile-candidate state and confirm Worker persists a `h5-platform-fallback:` action instead of failing the Run, then continue the remaining profile/detail branches.
