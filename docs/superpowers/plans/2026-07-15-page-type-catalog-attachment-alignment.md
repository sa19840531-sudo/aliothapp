# page_type Catalog Attachment Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将页面类型目录严格调整为附件定义的 17 个 page_type，并证明 JSON 与现有加载器可按英文 page_type 正常读取。

**Architecture:** 保持 `page_type_catalog.json` 现有数据结构不变，仅替换目录元数据和页面类型条目。附件第四节是 code 与中文含义的唯一权威；不扩展到加载器、Agent Tool、数据库、规则种子或前端。

**Tech Stack:** JSON、Python 3.12、项目现有 `services.api.app.domain.page.catalog` 加载器、PowerShell。

## Global Constraints

- 最终 page_type 必须且只能是：`global`、`general`、`toast`、`content_list`、`content_detail`、`ranking`、`profile`、`settings`、`payment`、`comment`、`post_list`、`post_publish`、`activity`、`ad_popup`、`startup_ad`、`pre_roll_ad`、`player`。
- `global` 必须保留；`line_loading` 与 `unknown` 必须移除。
- 只修改 `services/api/app/domain/page/page_type_catalog.json`，另保留本实施计划文件作为过程文档。
- 不覆盖工作区已有的未提交测试、Tool、Docker 或规则文档改动。
- 不引入数据库迁移；不需要同步前端菜单或路由。
- 所有文本使用 UTF-8；JSON 不允许注释。

---

## File Structure

- Modify: `services/api/app/domain/page/page_type_catalog.json` — 页面类型目录的唯一运行时数据文件。
- Create: `docs/superpowers/plans/2026-07-15-page-type-catalog-attachment-alignment.md` — 本次单文件配置调整的实施与验证计划。

### Task 1: 将目录收口为附件 17 值

**Files:**
- Modify: `services/api/app/domain/page/page_type_catalog.json`
- Verify only: `services/api/app/domain/page/catalog.py`

**Interfaces:**
- Consumes: `load_default_page_type_catalog() -> PageTypeCatalog`；`PageTypeCatalog.require(name: str) -> PageTypeDefinition`。
- Produces: 17 个 `PageTypeDefinition` 条目；每个英文 `code` 同时出现在该条目的 `aliases` 中，使现有 `require()` 可按英文值查询。

- [ ] **Step 1: 运行附件词表断言，确认当前目录先失败**

Run:

~~~powershell
@'
import json
from pathlib import Path

expected = {
    "global", "general", "toast", "content_list", "content_detail",
    "ranking", "profile", "settings", "payment", "comment",
    "post_list", "post_publish", "activity", "ad_popup", "startup_ad",
    "pre_roll_ad", "player",
}
payload = json.loads(Path("services/api/app/domain/page/page_type_catalog.json").read_text(encoding="utf-8"))
actual = [item["code"] for item in payload["page_types"]]
assert len(actual) == 17, (len(actual), actual)
assert len(actual) == len(set(actual)), actual
assert set(actual) == expected, {"missing": sorted(expected - set(actual)), "extra": sorted(set(actual) - expected)}
'@ | .\.venv\Scripts\python.exe -
~~~

Expected: FAIL；输出当前目录数量以及相对附件的 `missing` / `extra` 差异。

- [ ] **Step 2: 按附件替换目录元数据与 17 个条目**

顶层元数据必须设为：

~~~json
{
  "version": "2026-07-15.v2",
  "source": {
    "workbook_name": "质检平台_综合方案表 (3).html",
    "sheets": ["四、page_type 词表（17 值 · 贯穿全表的 tag 键）"],
    "extraction_method": "html-page-type-vocabulary-v1"
  }
}
~~~

每个条目保持当前完整字段结构：`code`、`name`、`status`、`aliases`、`signals`、`entry_hints`、`exit_hints`、`next_page_candidates`、`risk_actions`、`related_rules`、`source`、`remark`。`status` 一律为 `supported`，英文 `code` 必须同时写入 `aliases`；附件未定义的关系字段使用空数组。

实际条目必须逐行按以下矩阵填写，不得增加第 18 个条目：

| code | name | 中文别名 / 识别词 | dom_hints | layout_hints | risk_actions |
|---|---|---|---|---|---|
| `global` | 全页面通用（全局规则） | 全页面、全局规则 | global | 不代表独立页面，用于匹配所有页面的通用规则 | 空 |
| `general` | 通用内容页 | 通用页面、普通页面 | general、page、content | 未归入专项类型的普通内容页面 | 空 |
| `toast` | 轻提示 / 浮层文案 | 提示、轻提示、Toast | toast、message、notification | 页面上方或中部短暂出现的轻量提示层 | 空 |
| `content_list` | 内容列表页（视频板块等） | 内容列表、视频列表 | list、feed、card | 多个内容卡片、封面或条目的列表区域 | 空 |
| `content_detail` | 内容详情页 | 内容详情、视频详情 | detail、content | 单个内容主体及其标题、互动或推荐区域 | 空 |
| `ranking` | 榜单页 | 榜单、排行榜、排名 | ranking、rank、leaderboard | 按名次排列的内容或用户列表 | 空 |
| `profile` | 我的 / 个人主页（身份凭证卡） | 我的、个人主页、身份凭证卡 | profile、mine、account | 用户身份、资产、权益或个人入口聚合区域 | 空 |
| `settings` | 设置页 | 设置、系统设置 | settings、setting、preferences | 配置项、开关或账号设置列表 | 空 |
| `payment` | 支付 / 价格页 | 支付、价格、充值、购买 | payment、pay、price、recharge | 商品价格、支付方式、充值或购买确认区域 | 点击支付、确认购买、提交充值 |
| `comment` | 评论区 / 用户输入 | 评论、用户输入、输入框 | comment、input、textarea | 评论列表与用户输入区域 | 输入内容、发布评论 |
| `post_list` | 帖子列表页 | 帖子列表、社区列表 | post-list、post、community、feed | 帖子卡片或社区内容流 | 空 |
| `post_publish` | 发帖 / 发布页 | 发帖、发布、提交帖子 | publish、post-editor、composer | 标题、正文、图片等内容编辑与发布区域 | 输入内容、提交发布 |
| `activity` | 活动页 | 活动、运营活动 | activity、campaign、promotion | 活动主题、任务、奖励或运营配置页面 | 空 |
| `ad_popup` | 弹窗广告 | 广告弹窗、弹窗广告、关闭广告 | ad-popup、modal、dialog、advertisement | 覆盖当前页面的广告弹窗或广告浮层 | 点击广告主体、打开外部链接 |
| `startup_ad` | 启动 / 开屏广告 | 启动广告、开屏广告、跳过广告 | startup-ad、splash-ad、skip | 产品启动阶段全屏展示的广告 | 点击广告主体、打开外部链接 |
| `pre_roll_ad` | 播放前贴片广告 | 前贴片广告、播放前广告、跳过广告 | pre-roll、preroll、video-ad | 正片播放前出现的倒计时或可跳过广告 | 点击广告主体、打开外部链接 |
| `player` | 播放页 / 播放器 | 播放页、播放器、播放 | player、video、media | 视频或音频播放器及其控制区域 | 空 |

- [ ] **Step 3: 重新运行附件词表断言，确认通过**

Run: 重复 Step 1 命令。

Expected: PASS，退出码为 0，无输出。

- [ ] **Step 4: 验证现有加载器和英文查询契约**

Run:

~~~powershell
@'
from services.api.app.domain.page.catalog import load_default_page_type_catalog

expected = {
    "global", "general", "toast", "content_list", "content_detail",
    "ranking", "profile", "settings", "payment", "comment",
    "post_list", "post_publish", "activity", "ad_popup", "startup_ad",
    "pre_roll_ad", "player",
}
catalog = load_default_page_type_catalog()
assert catalog.version == "2026-07-15.v2"
assert len(catalog.page_types) == 17
assert {item.code for item in catalog.page_types} == expected
for page_type in expected:
    assert catalog.require(page_type).code == page_type
print("page_type catalog attachment alignment: PASS")
'@ | .\.venv\Scripts\python.exe -
~~~

Expected: 输出 `page_type catalog attachment alignment: PASS`，退出码为 0。

- [ ] **Step 5: 检查文件差异与格式**

Run:

~~~powershell
git diff --check -- services/api/app/domain/page/page_type_catalog.json
git diff -- services/api/app/domain/page/page_type_catalog.json
~~~

Expected: `git diff --check` 退出码为 0；差异中只有目录元数据和 17 个页面类型条目的替换。

- [ ] **Step 6: 提交实现**

Run:

~~~powershell
git add -- services/api/app/domain/page/page_type_catalog.json docs/superpowers/plans/2026-07-15-page-type-catalog-attachment-alignment.md
git commit -m "[AI编程] fix: 对齐附件page_type词表" -m "涉及模块：页面领域目录。" -m "数据库迁移：无。前端菜单/路由同步：无需。" -m "未覆盖测试风险：未完成的V2 Tool与测试契约不在本次单文件调整范围。"
~~~

Expected: 新提交只包含目录 JSON 与本实施计划；不得包含工作区其他未提交文件。

