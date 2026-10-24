# Web V2 六站探索质检实况复测汇总

## 结论

- 六个正式 `/api/v2/tasks` 任务均已进入 `review + sealed`，报告可用性均为 `preview`。
- 冻结 Catalog 共 166 个入口，全部形成菜单账本终态；实际进入菜单 26/30，菜单上限跳过 127，跨域阻断 3。
- 共形成 53 个规范 URL 对应的逻辑页面，其中 43 个已完成四类页型判定，10 个保留待复核；六站逻辑页面内重复 URL 均为 0。
- 已识别页型：`video_list=21`、`video_detail=13`、`content_list=7`、`content_detail=2`。
- `review`/`preview` 表示存在探索或质检缺口，因此不允许正式导出；不代表报告页面不可查看。

## 六站结果

| 站点 | Task | 逻辑页 | 已分类 | 待复核 | 菜单进入 | 详情完成 | 主要终态 |
|---|---|---:|---:|---:|---:|---:|---|
| heiliao.com | `11618bb7-2901-4cf3-b04b-e79b9e2068f2` | 9 | 4 | 5 | 5 | 0 | 3 个 `navigation_failed`，2 个 `popup_blocked` |
| hl365.com | `6e76f3fb-47fa-4431-aeab-1b2f6a0033e7` | 3 | 3 | 0 | 1 | 1 | 4 个 `allowlist_drifted` |
| 91jav1.com | `f392b7d3-cd38-4414-96bf-e60e090e1c26` | 10 | 8 | 2 | 5 | 3 | 1 个 `navigation_failed`，1 个 `detail_page_type_mismatch` |
| 91porna.com | `18ba39dc-3431-4e8c-9aef-15779b359210` | 11 | 11 | 0 | 5 | 5 | 五条菜单及详情链全部完成 |
| huangguoai.com | `b5e2dfd7-2246-4276-84ab-ca9c41da5c91` | 10 | 9 | 1 | 5 | 4 | `/topics/` 为 `navigation_failed`/待复核 |
| avhub360.com | `971f155d-0b0a-47fe-bd1d-9f0aea0d5d1e` | 10 | 8 | 2 | 5 | 2 | 2 个 `detail_page_type_mismatch`，1 个 `detail_selection_untrusted` |

## 待复核 URL

### heiliao.com

- `https://heiliao.com/archives/114291/`
- `https://heiliao.com/archives/114454/`
- `https://heiliao.com/jqrm/`
- `https://heiliao.com/archives/113994/`
- `https://heiliao.com/sjb/`

### 91jav1.com

- `https://www.91jav1.com/actress/hot/`
- `https://www.91jav1.com/theme/detail/17/update`

### huangguoai.com

- `https://huangguoai.com/topics/`

### avhub360.com

- `https://avhub360.com/theme/detail/57`
- `https://avhub360.com/actress/detail/2080`

## 本轮验证到的流程行为

1. 裸域名及 HTTP→HTTPS 跳转均归并为一个 `home` 角色逻辑页；首页业务类型仍来自四类页面目录。
2. 广告/年龄门禁的动作前后 Capture 保留为同一逻辑页的证据版本，工作台时间线不重复展示 URL。
3. 菜单 URL 已被首页或前序菜单规范化占用时，在导航和截图前按逻辑别名跳过；菜单上限和跨域入口只记录账本审计，不采集页面证据。
4. URL 白名单只辅助视频/内容方向；HTML 重复卡片、详情链接、播放器与正文结构不足或冲突时保留待复核。
5. `91porna.com` 的业务查询参数页面保持独立逻辑页；追踪参数、fragment、默认端口和同站 HTTP→HTTPS 才归并。
6. `avhub360.com/theme/detail/...` 可按实际 HTML 判为列表页，未按路径中的 `detail` 字样误判。
7. 六个任务均可通过 `/reports?task=<Task ID>` 读取 preview；正式导出仍由 Completion Gate 控制。

## Runner 说明

首次续跑报告生成时，runner 已允许 `preview`，但仍从旧版正式报告的 `page_exploration.menu_ledger` 读取菜单统计，导致把真实菜单误报为 0。现已改为：preview 使用 `/web-evidence` 与冻结 Catalog 补齐站点统计，并采用 preview 专属证据身份校验；final 继续执行原严格门禁。

第 4–6 站逐动作原始过程见 `docs/reports/2026-09-04-web-v2-sites-4-6-live-retest.md`；其中头部旧统计是修复前 runner 输出，逐节点表和 Task/Capture 身份仍为真实采集结果，应以上述汇总为准。
