# Page Detection Canonical Example Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `page-detection` 和 `page-exploration` 的结构化示例对齐到当前 14 个 canonical page_type code。

**Architecture:** 保持 `domain/page/page_type_catalog.json` 为事实依据，本次只修正文档型阶段 Skill 的示例与文字约束。Tool Schema、运行时目录注入和页面识别 Tool 留给后续方案处理。

**Tech Stack:** Markdown、JSON 示例、Python 3.12 契约断言。

## Global Constraints

- 结构化页面类型只能使用 `line_loading`、`home`、`content_list`、`content_detail`、`ranking`、`profile`、`settings`、`payment`、`post_list`、`post_publish`、`activity`、`startup_ad`、`video_list`、`video_detail`。
- 证据不足时使用 `page_type: null`，不得输出 `unknown` 伪类型。
- 不修改页面目录、Tool、Schema、数据库和前端。
- 不覆盖工作区其他未提交改动。

---

### Task 1: 对齐两份阶段 Skill 的 canonical 示例

**Files:**
- Modify: `services/api/app/agents/skills/phase/page-detection/SKILL.md`
- Modify: `services/api/app/agents/skills/phase/page-exploration/SKILL.md`
- Modify: `docs/专项能力与高频回归规则.md`
- Verify: `services/api/app/domain/page/page_type_catalog.json`

**Interfaces:**
- Consumes: `page_type_catalog.json` 中 14 个 `page_types[].code`。
- Produces: 只包含 canonical code 或 `null` 的 `page_type`、`expected_page_type`、`expected_next_page_type` 示例。

- [ ] **Step 1: 运行修改前契约断言**

使用 Python 读取目录 code，并扫描两份 Skill JSON 示例中的 `page_type`、`expected_page_type`、`expected_next_page_type`；发现 `默认首页`、`广告推荐页`、`视频列表页`、`社区列表页`、`detail` 时必须失败。

- [ ] **Step 2: 修改 page-detection 示例与页面类型建议**

将结构化字段映射为 `home`、`video_list`、`post_list`，删除缺少观测依据的旧“广告推荐页”低置信候选；证据不足分支改为 `page_type: null`，页面类型建议改为完整 14 值列表。

- [ ] **Step 3: 修改 page-exploration 示例**

将两个 `detail` 示例值改为 `content_detail`，并在示例说明中明确 canonical code 约束。

- [ ] **Step 4: 重新运行契约断言**

预期所有结构化字段都属于 14 个 canonical code，页面类型建议集合与目录集合完全一致。

- [ ] **Step 5: 固化专项回归规则**

记录页面目录调整后必须同步审计阶段 Skill 结构化示例，并以目录 code 集合校验 `page_type`、`expected_page_type`、`expected_next_page_type`。

- [ ] **Step 6: 审核差异和工作区范围**

运行 `git diff --check`、目标文件差异和 `git status --short`；确认没有修改目录、Tool、Schema、数据库或前端文件，也没有覆盖其他未提交改动。

