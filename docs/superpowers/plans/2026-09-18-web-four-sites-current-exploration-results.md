# 当前 Web 版本四站探索结果

## 本轮结论

用户指定四站的新探索已全部结束，四站 Run 均为 failed，成功详情采集为 0。91jav1、huangguoai、avhub360 均实际执行五个菜单；91porna 落地为 Cloudflare 504 页面，未进入菜单。此结论来自本轮全新 Task/Run，不复用上一轮网站结果。

当前活动 Run 为 0，四个主会话均 closed，未终结 checkpoint 为 0。正常结束探索不等于页面覆盖通过：未分类、加载未稳定、详情类型不匹配和广告安全限制仍为真实缺口。

第四站使用用户此前明确纠正的 `https://avhub360.com/`；`https://avhub360.comaaaa` 没有作为目标提交。

## 运行配置及证据校验

- 代码提交 `9fb0be835b07ea498dcb39437d993141cbcbdf57`，功能分支 `codex/web-v2-home-menu-ad-dedup`。本轮没有修改生产代码、数据库迁移、前端、App 或 H5。
- 专用 API/Worker 启动时间为 `2026-09-18T05:42:53Z`、`2026-09-18T05:42:54Z`；本轮开始前核验没有活动任务，Worker 修复文件 SHA256 与已提交文件一致。模型判定文件摘要为 `f33ac83f72d15539b852ca590d9a75ccf30ee0a4f73cf10a85f54a889fefe600`。
- 四站分别通过启动前 schema 0123、单 Web Worker、并发 1、checkpoint、代码契约和真实双源检查。请求、冻结策略及执行菜单上限均为 5；数据库 loop_guard 时长均为 10800 秒。
- 所有 845 个数据库引用文件均存在且 SHA256 相符。其中 DOM 135、screenshot 类型记录 149、滚动补图 page_coverage 15。screenshot 类型包括截图序列索引，不能将其数量当成独立页面数。
- 证据根目录沿用 `outputs/web-home-menu-ad-dedup-20260917-round1/evidence`，实际内容按本轮新 Task/Capture 隔离。启动空白页没有截图；菜单 href 来自各自清障后首页一次采集。
- 两个成功广告 Capture 均有落库截图、DOM、URL 等证据；所有广告截图帧均为 1920×1080，广告 page_coverage 为 0；主 Session 当前帧均不是广告帧。没有重复的规范化广告 href。

| 站点 | 实际菜单槽位 | 详情成功 | 独立广告分支 | 文件数 | Run 时段（UTC） |
| --- | ---: | ---: | --- | ---: | --- |
| www.91jav1.com | 5 | 0 | 1，安全检查拒绝 | 274 | 07:18:47–07:50:47 |
| 91porna.com | 0 | 0 | 0 | 21 | 07:51:36–07:52:49 |
| huangguoai.com | 5 | 0 | 1，成功 | 229 | 07:53:27–08:08:09 |
| avhub360.com | 5 | 0 | 1，成功 | 321 | 08:08:55–08:25:51 |

时段来自 Run created_at/updated_at，不将 180 分钟预算当成实际执行时间。

## www.91jav1.com

Task `6504c895-c0b6-41f7-b242-78bc3c98d92a`；Run `a708b835-98f4-4cce-8682-678c16fa8eba`。

根导航到 `https://www.91jav1.com/` 成功。首页经过多次清障与观察，最终为 home / 0.85。首页 Capture `5a95887c-3029-4f7a-b45d-41a095ca8633`，滚动补图 Artifact `aa5f638f-22ce-4dd0-86c9-001d1fa03c1f`。

清障动作第 8 步 CLICK 被 WEB_SEALED_CANDIDATE_STALE 拦截，未执行失效候选点击；第 9 步 OBSERVE 后首页恢复，继续菜单探索。完整动作事件已保留，不将这次拦截当成成功点击。

首页取得 11 个非空菜单 href，只有上述一个来源 Capture，包括 `/actress/hot`、`/article/58281`、`/heiliao`、`/movie`、`/my`、`/theme`、`/recommend` 等。随后没有重新采集菜单。辅助 `/theme/` 观察后为 video_list / 0.70，Capture `222eac30-6336-4217-9584-04de53ee9a40`；该页独立广告 href `https://lianai18.ps51b.com/` 被 `WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS` 拒绝并结束。

| 槽位 | 实际 GOTO 路径 | 页面分类/就绪结果 | 终态及具体原因 |
| --- | --- | --- | --- |
| 1 | `/actress/hot` | unknown / 0.65；模型候选 content_list | page_type_unclassified；WEB_PAGE_TYPE_LOW_CONFIDENCE |
| 2 | `/article/58281` | unknown / 0.65；模型候选 content_detail | page_type_unclassified；WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT |
| 3 | `/heiliao` | 两次观察均 loading，无正式分类 | navigation_failed；WEB_READINESS_LOADING |
| 4 | `/movie` | 两次观察均 loading，无正式分类 | navigation_failed；WEB_READINESS_LOADING |
| 5 | `/my` | 两次观察均 loading，无正式分类 | navigation_failed；WEB_READINESS_LOADING |

菜单 1 Capture `9718f2ed-b29b-456b-8b4f-c73ceece56a5`，补图 `c4480a4b-1e0e-4da4-b1a0-361c452a09ed`；菜单 2 Capture `e105ddf3-d6c8-49ac-8431-b69211586b0a`，补图 `2fedc692-3b4c-47f3-9ba2-0a6517a36235`。

菜单 3 的首次 loading 新帧为 `a991c395-c187-4f3c-8ac2-bb6145022d35`，重试观察帧为 `7bc2ade7-cac7-45ef-953c-9e88b3e81522`。重试耗尽后继续菜单 4、5；本轮未出现旧轮次的 WEB_CAPTURE_IDENTITY_MISMATCH。这是失败重试与耗尽后继续探索的真实见证，但 loading 页仍没有被提升为稳定页。

所有业务 checkpoint 为 session_close succeeded，Session closed，report_finalized=true，minimum_witness.complete=true。Run failed、error_message=null，表示页面与广告缺口，不能记为成功验收。

## 91porna.com

Task `dd7ffe68-cf93-4295-b077-e427f9f9ec0b`；Run `c43918e1-3bdc-4fd3-b175-6548ea971b27`。

请求 `http://91porna.com/`，落地 `https://91porna.com/`。根导航与后续观察均保存页面证据，标题为 `91porna.com | 504: Gateway time-out`。首屏图片人工核对显示 Cloudflare Gateway time-out，Browser Working、Cloudflare Working、Host Error。业务首页没有取得，菜单、详情、广告均未开始。

最终观察 Capture `f0317717-1307-41a1-b16b-ec13bb574b99`；DOM Artifact `fa6d15bc-14f9-47e2-80a8-31e25680b6bc`，首屏 Artifact `85e03061-9c94-49f6-ba88-c898fed45e87`。

系统阶段码为 WEB_READINESS_POPUP_BLOCKED，但实际留存画面是上游 504 错误页。将错误页笼统归入弹窗阻断的原因码表达不准确；本轮没有因此添加站点专用修复。

home checkpoint 已 session_close succeeded，Session closed，Run failed、error_message=null。Run final_report 中 is_final=true / report_stage=final，但验收 report_finalized=false，minimum_witness 缺少 candidate_filter_observed 和 report_finalized。不能仅凭生成了 final 载荷宣称完整验收。

## huangguoai.com

Task `fdc11de8-699e-473f-b797-d5d78b3e4c54`；Run `199fac9a-b34b-47fb-8856-04655846f7e9`。

HTTP 转 HTTPS 成功，入口 age_gate / 0.90。执行已授权年龄确认后，首页 home / 0.85，Capture `6201c7b0-ea33-4e6b-84b5-f514fc682078`；滚动补图 `ba012279-af3f-46af-8cb8-3d6748e78c71`。首页一次取得 8 个菜单 href：`/ai-duanju`、`/ai-huanlian`、`/ai-manju`、`/ai-mogai`、`/chigua`、`/topics`、`/go-home`、`/ranks/hot`。首页本次无广告分支。

辅助 `/topics/` 导航后未通过就绪，记 WEB_AUXILIARY_NOT_READY，结束后继续主菜单。

| 槽位 | 实际 GOTO 路径 | 分类（最终 / 模型候选） | 终态及具体原因 |
| --- | --- | --- | --- |
| 1 | `/ai-duanju/` | unknown / 0.65；模型候选 video_list | page_type_unclassified；WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT |
| 2 | `/ai-huanlian/` | unknown / 0.65；模型候选 video_list | page_type_unclassified；WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW |
| 3 | `/ai-manju/` | content_list / 0.75 | detail_selection_untrusted；WEB_DETAIL_SELECTION_FAILED |
| 4 | `/ai-mogai/` | unknown / 0.60；模型候选 video_list | page_type_unclassified；WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW |
| 5 | `/chigua/` | unknown / 0.75；模型候选 content_list | page_type_unclassified；WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT |

菜单 3 Capture `4f79d945-7b85-4dac-bc09-80e69316847f`。在主路径继续前，独立广告请求 `https://ujt61cs6.jepomx.com/gn-mark/?channelCode=4s55tb`，落地至同一路径无查询参数地址并成功留存 ad_page / 1.00，Capture `73950499-40b7-4a3f-9ffd-c35410a44578`。

该广告的源/落地 DOM 分别为 `f6a72c7e-3de6-406d-9e31-d81b3a14946a`、`c0e57719-ca71-4cea-bd68-da0a020aa770`；截图记录为 `3f197598-d145-4254-a73c-9d87eb5f4e4e`、`93abccb5-72f1-4ae5-9ba6-f7f7d8bfebd4`、`a752cb97-5c50-42b9-b2ff-5eeb52e62af2`。广告已 ad_closeout succeeded，再继续详情选择。

详情选择具体诊断为 phase=reason_sanitize、reason_code=WEB_DETAIL_SELECTION_REASON_REJECTED、exception_type=ValueError、attempt_count=1、candidate_count=21。有候选集合，但选择说明未通过清理校验；本轮没有执行详情 GOTO，不能声称已到达详情。

所有业务 checkpoint 均 session_close succeeded；Session closed，report_finalized=true，minimum_witness.complete=true，Run failed、error_message=null。第五菜单本次正常通过 readiness，无旧版身份漂移。

## avhub360.com

Task `16b78d62-6a50-462f-ad52-6322fa2ecf60`；Run `e09c9a65-bbe2-40d0-a0d4-53f5662733a6`。

根导航成功。首页五次普通清障点击后，首次观察出现 WEB_READINESS_ARTIFACT_SKEW，Capture `ea618c1d-4087-4dc7-b687-50b3dd056cd9`；第二次观察恢复就绪，Capture `22594823-545b-4b41-8203-b706cd557e60`，最终 home / 0.85，补图 `6371b3e9-81ad-4321-99f5-0c8ae082b69c`。没有 WEB_CAPTURE_IDENTITY_MISMATCH，首次 skew 未进入最终缺口集合。

首页一次取得 14 个菜单 href，来源均为上述 ready 首页，包括 `/actress`、`/female`、`/gohome`、`/movie`、`/my/history`、`/tags`、`/new`、`/theme` 等；首页无广告分支。

| 槽位 | 实际 GOTO 菜单 | 页面分类 | 详情/终态 |
| --- | --- | --- | --- |
| 1 | `/actress` | content_list / 0.75 | `/actress/detail/3189` 为 video_list / 0.85，WEB_DETAIL_PAGE_TYPE_MISMATCH |
| 2 | `/female` | content_list / 0.70 | `/article/58351` 为 unknown / 0.65，WEB_DETAIL_PAGE_TYPE_MISMATCH；分类原因 WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT |
| 3 | `/gohome` | unknown / 0.60 | page_type_unclassified；WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT |
| 4 | `/movie` | 两次观察 loading，无正式分类 | navigation_failed；WEB_READINESS_LOADING |
| 5 | `/my/history` | 两次观察 loading，无正式分类 | navigation_failed；WEB_READINESS_LOADING |

菜单 1 Capture `6bed3ffb-60e0-4800-b518-d25b72524741`，候选落地 Capture `889974ac-8aa6-459b-8ce5-fb0931fefd0a`；菜单 2 Capture `783cba12-75a5-44bd-9f26-1865d3b36ea1`，候选落地 Capture `112fea93-a61b-447c-932a-3f5215430727`。两个详情候选均实际 GOTO；没有将路径含 detail 或 article 当成详情成立的依据。

菜单 2 广告请求 `https://57176238.com/`，允许公网跨域跳转并落地 `https://0pqkvj9.56612671.com/`，ad_page / 1.00，Capture `d2c4e3a5-7661-46e9-a399-3de57b20e4ca`。源/落地 DOM 为 `9ffa269d-acd8-48e2-a0b1-90b25a5bb59f`、`e42569cb-6c9b-4c47-8ca9-d988305ca32b`；截图记录为 `1e48799a-56d9-4388-9930-018c2790c154`、`822970e5-1cd6-4f89-8396-d236d72f7945`、`c4573084-14f7-456e-a7a0-a7ce5822d9e8`。广告结束后再执行详情 GOTO。

所有业务 checkpoint 为 session_close succeeded；Session closed，report_finalized=true，minimum_witness.complete=true，Run failed、error_message=null。loading 耗尽仍记缺口，没有作废已有首屏、DOM 或补图。

## 本轮问题收集及结果边界

1. 首页上游 504 错误被笼统表达为弹窗阻断，导致报告原因码和留存画面不一致；91porna 本轮没有可探索业务首页。
2. 多个菜单已有模型候选类型，但被低置信度、证据不足、模型复核要求或强负向冲突收口为 unknown；其具体分类审计已列出，不能直接认定全部属于同一代码异常。
3. 91jav1 与 avhub360 的多个路径实际导航后两次观察均 loading；新版本能正常重试、耗尽后继续并关闭会话，页面仍没有达到稳定准入。
4. huangguoai 有 21 个详情候选，但模型选择说明清理校验拒绝；avhub360 两个候选落地类型不满足详情契约。因此本轮成功详情为 0。
5. 四个 Task 数据库 finalization_state 均仍为 open，虽然四个 Run 已有 final 载荷。91porna 的验收 report_finalized=false；其余三个验收字段为 true 也不表示零缺口完成。
6. 广告沿用既有静态安全渲染策略；部分脚本懒加载内容可能不呈现完整广告视觉。该已知边界未在本轮放宽。

本轮按探索请求完成测试和报告，不在运行中调整分类门槛、重新提交失败站点或实施新生产修复。

## 详细动作与证据入口

每站 report.md 与 events.jsonl 包含完整动作序号、状态变化、GOTO URL、页面类型、Capture、截图/DOM/HAR/动作链 Artifact、存储路径及菜单执行槽位；下列报告均为本轮新目录。

- [91jav1 逐步报告](../../../outputs/web-four-sites-current-20260918/live/www.91jav1.com/1-www.91jav1.com/report.md)
- [91porna 逐步报告](../../../outputs/web-four-sites-current-20260918/live/91porna.com/1-91porna.com/report.md)
- [huangguoai 逐步报告](../../../outputs/web-four-sites-current-20260918/live/huangguoai.com/1-huangguoai.com/report.md)
- [avhub360 逐步报告](../../../outputs/web-four-sites-current-20260918/live/avhub360.com/1-avhub360.com/report.md)
- [845 个文件数据库与摘要核验](../../../outputs/web-four-sites-current-20260918/live/evidence-verification.json)
- [分类原因与结算状态核验](../../../outputs/web-four-sites-current-20260918/live/classification-audit.json)
