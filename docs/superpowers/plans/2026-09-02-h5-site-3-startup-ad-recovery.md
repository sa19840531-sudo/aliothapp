# H5 站3连续启动广告恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复未稳定 `startup_ad` 快捷识别分支丢失可信关闭能力的问题，并重新验证站3能否逐层关闭广告进入业务页。

**Architecture:** 保留现有“检测器签发结构能力、Observation 只投影可信能力、Browser Runtime 动作前重验”的三层边界。只让未稳定页面的确定性 `startup_ad` 分支复用稳定分支的 `_with_obstruction_dismissal_candidate`，不放宽文本或坐标点击；随后使用 fresh task 验证真实连续广告链。

**Tech Stack:** Python 3.12、FastAPI、pytest、Playwright/Chromium、PostgreSQL、Docker Compose、Alioth H5 V2 Explorer。

**Spec:** 用户本轮请求；历史证据任务 `5f280bcc-24d5-4436-82a3-9a6e16e7d961`；基线报告 `docs/reports/2026-09-01-h5-site-3-5-failure-diagnosis-rerun-report.html`。

## Global Constraints

- 保持 `Detection → Observation → SemanticTargetResolver → Browser Runtime` 的能力验签边界。
- 只允许由唯一、可见的已知 `startup_ad` DOM 结构签发 `startup_ad_dismissal`；不得增加通用文本、CSS 猜测或坐标点击。
- 正式复跑 URL 保持 `http://d24jdtq5qz8ff4.cloudfront.net`。
- 使用 `alioth_first_ruleset` v1、`scan_mode=full`、`channel=workbench` 和原产品信息。
- Docker 重启不得 build；登录令牌只在进程内使用，不写入文件或报告。
- 修复后更新 `docs/专项能力与高频回归规则.md`；所有新增文本为 UTF-8，注释使用中文。

---

### Task 1: 冻结历史证据与根因

**Files:**
- Read: `.alioth/evidence/5f280bcc-24d5-4436-82a3-9a6e16e7d961/`
- Read: `services/api/app/agents/tools/page_detection_tools.py:2074-2130`
- Read: `services/api/app/application/h5_exploration_observation.py:430-460`
- Read: `services/api/app/infrastructure/browser/page_capture.py:3780-4110`

**Interfaces:**
- Consumes: 历史 Capture、assessment、ActionIntent、action_log 和 DOM。
- Produces: 单一根因假设和修复边界。

- [x] **Step 1: 核对前两次跳过的 selector、dismissal_verified 与点击后状态差异**
- [x] **Step 2: 核对第三层 DOM 仍存在唯一 `0s(跳过)` 的 `.van-popup .skip-wrap`**
- [x] **Step 3: 对比成功稳定分支与失败未稳定快捷分支的能力字段**

### Task 2: 用回归测试复现能力丢失

**Files:**
- Modify: `services/api/tests/test_page_detection_tools.py`

**Interfaces:**
- Consumes: `detect_page_for_agent` 的真实检测管线和 `van-popup/skip-wrap/0s(跳过)` 证据。
- Produces: 未稳定页面仍必须签发结构能力的回归合同。

- [x] **Step 1: 新增真实站3结构回归测试**

```python
async def test_unsettled_popup_startup_ad_projects_trusted_dismissal_capability() -> None:
    # page_settle_complete=False，DOM 含唯一 van-popup/skip-wrap/0s(跳过)
    result = await detect_page_for_agent(...)
    action = result["candidate_actions"][0]
    assert action["obstruction_capability"] == "startup_ad_dismissal"
    assert action["target_keywords"] == [
        "startup_ad",
        "close_control",
        "startup_ad_dismissal",
    ]
    assert action["obstruction_signature"].startswith("h5-obstruction:")
```

- [x] **Step 2: 运行新测试并确认因 capability 为 `None` 失败，而不是环境或 fixture 错误**

Run: `pytest services/api/tests/test_page_detection_tools.py::test_unsettled_popup_startup_ad_projects_trusted_dismissal_capability -q`

Expected: FAIL，实际 `obstruction_capability` 为 `None`。

### Task 3: 最小修复未稳定 startup_ad 能力签发

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py:2074-2130`
- Test: `services/api/tests/test_page_detection_tools.py`

**Interfaces:**
- Consumes: 原始 `dom_chunks` 和 `H5ExplorationProfile.shell == "startup_ad"`。
- Produces: 与稳定分支相同的结构验签候选；无法验签时返回零关闭候选并继续有界观察。

- [x] **Step 1: 给 `_deterministic_unsettled_h5_result` 传入 `dom_chunks`**
- [x] **Step 2: 用 `_with_obstruction_dismissal_candidate(page_type="startup_ad", page_state="countdown", candidate_actions=[], dom_chunks=dom_chunks)` 生成候选，替换裸 `CandidateAction`**
- [x] **Step 3: 运行新测试确认 GREEN**
- [x] **Step 4: 运行 startup_ad 相关检测、Observation、Workflow 和 Browser Runtime 测试**

Run:

```powershell
pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_web_capture_runtime.py -q
```

Expected: 全部通过。

### Task 4: 更新高频回归规则并重启运行时

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: 本次根因和回归测试。
- Produces: 未稳定生命周期页不得绕过能力签发的长期规则。

- [x] **Step 1: 增加“未稳定 startup_ad 强结构仍须复用完整能力签发”规则和回归要求**
- [x] **Step 2: 执行 `git diff --check`**
- [x] **Step 3: 仅重启 `alioth-api`、`alioth-worker`，不得 build，并确认健康**

### Task 5: Fresh rerun 与报告

**Files:**
- Create: `docs/reports/assets/2026-09-02-h5-site-3-startup-ad-recovery/`
- Create: `docs/reports/2026-09-02-h5-site-3-startup-ad-recovery-report.html`

**Interfaces:**
- Consumes: 修复后的本地运行时和原任务参数。
- Produces: 新 Task/Run、逐层关闭过程、实际页型、截图和最终报告。

- [x] **Step 1: 登录并创建新的站3 full-scan 任务，令牌只保存在进程内**
- [x] **Step 2: 跟踪每次 startup_ad 点击、签名、关闭验证、后继页型与终态**
- [x] **Step 3: 下载截图归档并生成 HTML 报告**
- [x] **Step 4: 验证报告引用、图片、UTF-8、敏感信息、ZIP 完整性和 Docker/API 健康**
- [x] **Step 5: 运行完整 H5 测试集合并记录通过数**

## Execution Record

- RED：新增回归测试按预期因 `obstruction_capability=None` 失败。
- GREEN：相邻目标测试 `2 passed`；Detection / Observation / Workflow / Browser Runtime 聚焦回归 `1020 passed`。
- 完整后端：`7082 passed, 48 skipped, 29 warnings in 270.11s`。
- Fresh task：`bf6bfff9-7147-49fc-a5f0-6ca2dc7c73a0`；Run：`82695076-6036-4006-9eac-434f1d61ce6c`。
- 广告关闭结果：`startup_ad/popup → home/popup → home/stable`，动作日志 `dismissal_verified=true`；之后进入 `video_list/content_list/post_list/profile`。
- 终态：Task=`review`；Run=`failed/incomplete/h5_frontier_restore_exhausted`。后者发生在广告退场和多页业务探索之后，不代表开屏关闭失败。
- 报告：`docs/reports/2026-09-02-h5-site-3-startup-ad-recovery-report.html`。
