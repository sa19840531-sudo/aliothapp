# sensitive_word_detection_tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 `sensitive_word_detection_tool`，对采集文本执行禁止内容库多分类穷举扫描。

**Architecture:** 禁止内容库与扫描算法放在 `domain/rule`；Tool 层只负责注册、调用和结构化返回。V1 使用本地 JSON 词库，后续可替换为数据库仓储。

**Tech Stack:** Python 3.12、Microsoft Agent Framework Tool Registry、pytest。

## Global Constraints

- 后端固定使用 Python FastAPI。
- AI 框架固定使用 Microsoft Agent Framework。
- Tool 层不得直接写数据库。
- 本轮不新增数据库迁移。
- 禁止内容分类固定为政治 / 恐怖 / 人身攻击 / 涉华 / 成人 / 赌博 / 私人广告 / 钓鱼诱导。
- 所有新增代码注释必须使用中文 UTF-8。

---

### Task 1: 禁止内容库与扫描算法

**Files:**
- Create: `services/api/app/domain/rule/__init__.py`
- Create: `services/api/app/domain/rule/forbidden_content_catalog.json`
- Create: `services/api/app/domain/rule/forbidden_content.py`
- Test: `services/api/tests/test_sensitive_word_tools.py`

**Interfaces:**
- Produces: `scan_sensitive_words(text: str) -> dict[str, Any]`

- [ ] **Step 1: Write failing tests**

覆盖多分类命中、白名单命中和空文本无命中。

- [ ] **Step 2: Run tests and confirm red**

Run: `python -m pytest services/api/tests/test_sensitive_word_tools.py -q`
Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement minimal scanner**

加载 JSON 词库，按 `contains` 穷举匹配，返回位置与白名单状态。

- [ ] **Step 4: Verify green**

Run: `python -m pytest services/api/tests/test_sensitive_word_tools.py -q`
Expected: PASS when dependencies are installed.

---

### Task 2: Tool 注册与文档同步

**Files:**
- Create: `services/api/app/agents/tools/sensitive_word_tools.py`
- Modify: `services/api/app/agents/bootstrap.py`
- Modify: `services/api/app/agents/tools/README.md`
- Modify: `docs/专项能力与高频回归规则.md`
- Test: `services/api/tests/test_sensitive_word_tools.py`

**Interfaces:**
- Produces: `sensitive_word_detection_tool(text: str) -> dict[str, Any]`

- [ ] **Step 1: Write registration test**

确认 `sensitive_word_detection_tool` 注册为 `PermissionLevel.SAFE`，category 为 `text`。

- [ ] **Step 2: Run tests and confirm red**

Run: `python -m pytest services/api/tests/test_sensitive_word_tools.py -q`
Expected: FAIL because Tool is not registered.

- [ ] **Step 3: Implement Tool and bootstrap import**

新增 Tool 文件，并在 `bootstrap.py` 导入触发注册。

- [ ] **Step 4: Verify**

Run: `python -m pytest services/api/tests/test_sensitive_word_tools.py -q`
Expected: PASS when dependencies are installed.
