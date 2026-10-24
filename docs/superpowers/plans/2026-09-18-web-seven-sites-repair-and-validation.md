# Web 七站修复、审核与探索验证

## 范围

当前工作树 `.worktrees/web-v2-dynamic-exploration-evidence`，分支 `codex/web-v2-home-menu-ad-dedup`。本轮仅修改 Web 专属模块、Web 测试和文档；未修改 App/H5 文件及共用 `infrastructure/browser/page_capture.py`。该共用文件已有前轮未提交修改，本轮保留不动。未提交、合并、推送代码，未修改主服务或主数据库。

测试目标：chigua.com、heiliao.com、hl365.com、www.91jav1.com、91porna.com、huangguoai.com、avhub360.com。每站普通菜单上限 5。测试使用当前工作树绑定的专用 API/Worker 和独立探索数据库，重启专用服务后执行，不使用主服务旧代码。

## 证据与修复

1. 上轮黄果详情 Capture `6bc98423-15ad-4585-8e23-54f2014066fe`：42 次稳定采样、609ms 连续窗口，采集前后结构摘要、URL、文档 epoch 一致，无加载遮挡、关键请求未决、媒体缺口；仅最终 DOM quiet=582ms 被 600ms 门槛拒绝。Web 合同现在独立复核贯穿采集的稳定结构窗口，保留原始 quiet 值，其他准入条件不变。不能用单个 `settle_complete` 标记或宽松计时替代结构证据。
2. 上轮 91porna Capture `c6da11f0-c45f-475b-b85d-b6e51b8a5286`：评论容器中的作者、分类、上下篇五个链接被计为主列表卡片，触发详情强冲突。新增通用完整类名词段的评论辅助链接特征。旧证据回放为主卡片 5→0、文章主体保持 1、推荐卡片保持 8；不新增页面类型，不按站点 URL 特判，不降低模型置信度或清除复核结论。
3. 广告调度从整 Run 目的 URL 去重改为同逻辑业务页幂等。不同业务页即使相同 href 也分别独立留证。应用调度和 PostgreSQL 预留层同时修改，仍每页最多一个，失败恢复不重复打开。
4. 详情候选实际落为列表时，详情账本仍记类型不匹配，但根据实际可信分类调度该页广告；不伪造详情成功，不递归增加菜单。
5. 模型返回闭集外详情候选 ID 时，成员校验移入原有有界只读重试，复用相同截图/DOM，不重发浏览器动作，不接受虚构 ID，不增加模型请求次数上限。

## 审核

- 发现并修正：评论容器中的推荐组件不可一并被剔除，保留其独立推荐统计，并增加嵌套推荐反例。
- 更新旧的跨页去重测试合同；覆盖同页重启幂等、跨页并发创建、列表广告关闭后进入详情、详情广告关闭后继续主路径。
- 保留 URL/epoch/结构漂移、加载层、关键网络请求、媒体缺失拒绝准入；保留广告域名格式、IP 字面量排除及非公网 DNS 地址安全保护。
- AVHub 上轮 Capture `74caebb5-05f5-461a-b1b7-3593f3cdbe16` 的边界 URL/epoch 相同，但结构摘要确实变化；旧审计显示轮播未暂停、CSS 动画已暂停。当前证据只支持动态结构漂移，不能宣称精确到某个轮播库。本轮不通过关闭一致性校验伪造修复；在七站测试中复核结果。

## 验证进度

第一批针对性测试 46 passed。扩大门禁初轮 472 passed、5 skipped：跳过原因是 5 个浏览器测试需要显式 `ALIOTH_WEB_TEST_BROWSER`，不是生产浏览器不可用。已定位现有 Playwright Chromium，最终门禁显式启用该路径重跑。最终回执为 `outputs/web-seven-sites-repairs-20260918/gate-final.xml`。

最终门禁：478 passed、0 failed、0 skipped（3 条测试环境警告）。Ruff 与 `git diff --check` 通过。专用 API/Worker 随后已重启加载新代码，在线源文件指纹 `7996a32b23d6dd4a681155e78eb380c3f5a1654a8eb3dcf31a1f459b0131e4c6`。

在线首轮回执目录：`outputs/web-seven-sites-repairs-20260918/live`。七站已全部结束，不能以离线回放或单测通过宣称在线探索成功。

## 已结束站点：chigua

- Run `669bbb53-a844-4bf6-8f02-0deb23b64a29`，2026-09-18 UTC 14:54:14—15:33:37，约 39 分 24 秒。
- 首页四次普通关闭点击后 OBSERVE 通过，模型判为 home（0.80），冻结菜单 href；5 个普通菜单上限生效。
- 菜单 1 `/48552.html` 跳到 `https://official.1fff8x-t1t1.cyou/`，记录跨域失败、恢复首页、继续菜单；未把外域当业务页。
- 菜单 2 `/archives.html` 为 content_list（0.82），GOTO `/archives/275796/` 为 content_detail（0.85）。
- 菜单 3 `/archives/144493/`、菜单 4 `/archives/187826/` 均为 content_detail（0.90）。
- 菜单 5 `/category/51djc/` 为 content_list（0.75），GOTO `/archives/274784/` 后 unknown（0.65），原因 `WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT`。未知页不伪造详情覆盖。
- 两个独立广告分支分别来自菜单 2 的详情与菜单 5 的列表，初始 href 均为 `https://ypqav341.com/`，最终均为 `https://www.qtvnaex341.cc:1280/`，两次均 ad_page 留证成功。证明跨业务页不再按相同 URL 去重；广告关闭后才推进主路径。
- 广告快照重放：其余已分类业务页无合格可见广告 href；未知页不属于五类业务广告入口，均有明确跳过原因。
- 最终 Run failed，但报告已 finalized、会话已 closed、所有业务阶段已到 session_close。失败原因保留，不等于任务卡住或未结束。

## 同版本在线测试新发现（首轮保留，追加修复另行验证）

- heiliao Run `10576679-c8c8-4672-ac70-1a53abc1c058`：年龄确认及 5 次普通关闭后 readiness 成功；模型提出 home，但置信度 0.60 并要求复核，原因是截图可见顶部导航与结构导航计数为零矛盾，最终 unknown。分类后仍调用仅接受 home 的菜单发现，抛出 `WEB_DISCOVERY_PAGE_ROLE_INVALID`。Run failed、会话 closed、数据库已有终稿但 technical_execution_complete=false；home checkpoint 停在 classification/running。这是新的未结算异常路径，不能宣称终态修复已覆盖所有情况。
- hl365 Run `628d2aff-e05a-49d8-abf0-450017b16502`：根导航动作报 `SURFACE_DRIVER_FAILED`，只有空白 before Capture。异常处理试图将空白证据作为 HTTP(S) 页面恢复引用，又抛出“URL 必须为无凭据 HTTP(S)”，遮蔽原错误并使 bootstrap_navigation checkpoint 悬挂。需要在 Web 首跳失败处理内保留动作诊断但不绑定空白 URL，不得用目标 URL 伪造已到达页面。
- 七站进行中不热改运行时代码，确保该轮源文件指纹一致。完成该轮后只对以上可证明的 Web 异常结算和导航特征做有界修复、审核、定向复测，保留首轮真实失败记录。
- heiliao 同 Capture 的 DOM/快照只读回放：`navigation_snapshot_from_html(..., require_runtime_hit_test=True)` 能证明 1 个顶部导航容器、6 个可见分类入口、1 个搜索入口；原始快照直接统计上述计数均为 0。Web Capture 策略的延迟发现分支未保存这份导航结构投影，分类直接读取旧通用节点，因此需要修复证据衔接而非新增页面类型或放宽分类门槛。

## 已结束：91jav1

- Run `196d0d8c-18e1-4553-b220-70d98777a8ef`。首页六次关闭成功，第七次因 `WEB_SEALED_CANDIDATE_STALE` 未执行；重新 OBSERVE 后判为 home（0.85），未绕过密封候选校验。
- 首页冻结 11 个导航 href；普通菜单上限 5，支付类入口安全排除。辅助入口 `/theme/` 为 unknown（0.65），独立结算，不占普通菜单额度。
- 菜单 1 `/actress/hot` 为 unknown（0.65）；菜单 2 `/article/58281` 为 unknown（0.60）。均保留未分类结果并继续，不引用旧轮次结论。
- 菜单 3 `/heiliao` 进入 `/heiliao/post/16`，判为 content_detail（0.85），截图、DOM、滚动补充证据保存。可选正文目标缺失单独记录，不作废正式详情分类。
- 菜单 4 `/movie` 为 content_list（0.75）；广告分支已调度，因 `WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS` 安全拦截并结算，随后主路径 GOTO `/article/58689` 成功。不得将此情况报告成未走广告分支。
- `/article/58689` 最终 content_detail（0.80）；详情单独广告分支同样安全拒绝并收口。菜单 5 `/my` 最终 unknown（0.80）；置信度高也不能代替目录页型和证据合同。
- 5 个普通菜单全部结算，正式详情 2 个，unknown 普通菜单 3 个；辅助入口另计。最终 failed、report_finalized=true、session_closed=true，所有业务 checkpoint 到 session_close；2 个广告 checkpoint 均到 ad_closeout/gapped。没有无限重试或悬挂菜单。

## 广告网络诊断

只读 DNS 复核（测试 API 容器，与在线工作容器同 Docker 网络）：91jav1 选中的 `lianai18.ps51b.com`、91porna 首页选中的 `dqd4sqh.cc` 当前均返回 `0.0.0.0` 与 `::`，`is_global=false`。这是“正常域名字符串解析到非公网地址”，不是 IP 字面量过滤遗漏。当前证据不能确定是上游域名配置还是本地/上游 DNS 过滤；需网络侧检查，不能关闭 SSRF 保护或伪造广告采集成功。此为事后 DNS 复核，不冒充动作发生瞬间的 DNS 记录。

进一步在实际专用 Worker 中复核上述两个广告域名，返回同样的 `0.0.0.0`、`::`。广告 `AdSafeTransport` 每跳通过系统 DNS 解析并固定公网连接；主浏览器导航是另一条路径。黄果根域的系统 DNS 检查也返回空地址，但本轮主浏览器实际成功到达年龄页，说明不能将系统 DNS 复核结果直接当作主浏览器不可达结论，也不能据此关闭广告地址保护。

## 已结束：91porna

Run `58c11fc4-a9b6-472b-9a23-7f3ecdd027ee`。HTTP 根导航升级 HTTPS 成功；一次关闭成功、下一候选失效被拒绝后 OBSERVE 恢复，首页 home（0.80）。首页广告分支安全拒绝并结算。辅助 `/comic/index/search` 实际 video_list（0.75），不能算 search_page 覆盖，无合格可见广告 href。第 1 个普通菜单为 content_list（0.82），其广告分支结算后进入 `/heiliao-chigua/18917711137`。

5 个菜单全部 detail_captured，详情分别为 `/heiliao-chigua/18917711137`（0.85）、`/heiliao-chigua/1891778113`（0.85）、`/heiliao-chigua/1891778274`（0.85）、`/heiliao-chigua/18917711142`（0.82）、`/heiliao-chigua/18917711134`（0.85）。首页、5 个列表及5个详情均已正式分类，无 unknown。广告独立失败未中断主路径，最后 Run failed、report_finalized=true、session_closed=true；完整广告计数与逐页跳过原因以最终广告审计为准。失败清单另保留被拒绝的旧候选关闭动作，不能把正常安全拒绝当作实际误点击。

## 已结束：黄果

- HTTP 根导航到 HTTPS，age_gate（0.92），确认后首页 home（0.85）。8 个顶部导航 href 冻结，普通菜单上限 5。
- 辅助入口 `/topics/` 实际 content_list（0.70），不冒充 activity 覆盖；按业务页类型触发广告，`https://ujt61cs6.jepomx.com/gn-mark/` 留证并标记 ad_page，分支关闭后推进主路径。
- 菜单 1 `/ai-duanju/` video_list（0.80）→ `/detail/6083/` content_detail（0.90）。列表可选主体定位有 `WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED`，不作废已完成分类和滚动补充截图。
- 菜单 2 `/ai-huanlian/` video_list（0.75）→ `/detail/5876/` content_detail（0.75）。
- 菜单 3 `/ai-manju/` video_list（0.80）→ `/detail/2312/` content_detail（0.90）。
- 菜单 4 `/ai-mogai/` video_list（0.80）→ `/detail/5856/` content_detail（0.80）。
- 菜单 5 `/chigua/` content_list（0.82）→ `/archives/459/` content_detail（0.85）。
- 5 个菜单全部 detail_captured、所有业务阶段 session_close/succeeded，report_finalized=true、session_closed=true。没有复现旧 DOM quiet 临界误拒绝；这不等于所有页都触发了新增稳定窗口豁免，精确边界由单测与旧证据回放证明。
- 总体 failed，广告网络拒绝及辅助入口类型不匹配仍作为缺口保留；不将主路径 5/5 完成等同于全能力验收通过。

## 已结束：AVHub

- Run `528035a2-5a70-46fe-a331-26f0ca156223`：根 GOTO 成功，5 次普通关闭成功，末次关闭后的遮挡层列表为空。随后两次 OBSERVE 均为 `WEB_READINESS_TIMEOUT`，尚未进入正式分类、菜单或广告。
- 两次 Capture `98b06cff-5c09-48c4-bb66-fdfb9cf1a892`、`84067ffd-ec54-4174-82d5-f40e4fa98574` 的 URL、epoch、前后结构一致，最终媒体 ready、关键请求 0、DOM quiet 约 3 秒；但采集前 settle 采样窗口分别只有约 488ms、534ms，未达到稳定窗口要求，采样时仍记录 5 个关键请求未决、持续 DOM 变化，quorum=false。不同阶段指标不能拼成一份已稳定证明。
- 这次是稳定采样超时，不是模型类型判错，也不是上轮结构验签漂移。不能通过新增类型、单读最终 quiet 或关闭一致性校验放行。当前阶段结算已到 session_close，会话 closed；没有可信正式分类，因此 report_finalized=false，不能宣称完整采集成功。

## 首轮结束后的有界追加修复

七站同版本首轮均已结束。随后新增三个通用 Web 修复：空白首跳不绑定业务恢复 URL；未知首页在分类阶段转缺口结算并阻止菜单/辅助授权；延迟菜单授权时仍保存同 Capture 已证明的顶部导航结构事实。没有新增类型、站点特判、置信度豁免或 App/H5 改动。

修改前测试重现了 3 种空白引用的 URL 校验异常、未知首页首次及恢复时的角色异常，以及可见顶部导航事实丢失。修改后针对性 79 项通过。扩大门禁首次 602 passed / 1 failed，失败为旧测试仍期望分类失败后继续 href_discovery；更新为 frontier_drain 的新失败结算合同后，完整重跑 **603 passed、0 failed、0 skipped**（3 条警告）。包含真实 PostgreSQL、Playwright 和 App/H5 隔离回归，回执 `outputs/web-seven-sites-repairs-20260918/gate-followup.xml`。Ruff、diff --check 通过。

审核核对：原始错误诊断保留、没有伪造已到达 URL、未知首页恢复重入不授权导航、菜单失败继续后续菜单、广告独立关闭路径不变、同帧导航事实不提前签发动作。未修改 App/H5 及共用采集器；共用文件 SHA256 仍为 `7A47A91D98D5F4F5DDF69319FD3F2838DC86B048D99EA309A3ED6E95278CB6CE`。

专用数据库 active_runs=0 后重启 API/Worker。追加版本指纹 `12e265c9ebc27110b393461c95c351c9d5304cad8f6149e44db629a961c88912`，只对黑料和 HL365 新建定向复测，目录 `outputs/web-seven-sites-repairs-20260918/followup`。首轮与追加复测分别留存，不把旧证据回放冒充新版在线七站验证。

## 首轮汇总与证据索引

| 站点 | 普通菜单 | 详情成功 | 正式 unknown | 广告成功/分支 | 原始 Run 状态 |
|---|---:|---:|---:|---:|---|
| chigua | 5 | 3 | 1 | 2/2 | failed |
| heiliao | 0 | 0 | 1 | 0/0 | failed |
| hl365 | 0 | 0 | 0（首跳未到达） | 0/0 | failed |
| 91jav1 | 5 | 2 | 4（含辅助页1） | 0/2 | failed |
| 91porna | 5 | 5 | 0 | 0/10 | failed |
| 黄果 | 5 | 5 | 0 | 1/10 | failed |
| AVHub | 0 | 0 | 0（未过 readiness） | 0/0 | failed |

24 个广告分支中 3 个成功、21 个因非公网地址被拒绝；不存在把同 URL 跨业务页去重后漏开的旧问题。真实图片共 294 张；7 站文件缺失 0、摘要不符 0，会话均已关闭。成功广告图片均为 1920×1080 首屏，广告没有追加滚动截图。截图类型还包括 JSON 回执，不应直接用 artifact 数量当作图片数量。

详细动作顺序、全部菜单候选及执行状态、逐 Capture 的 URL/页型/分数/原因、截图/DOM 文件链接、逐页广告跳过或失败原因、checkpoint 终态见 [逐步探索与证据索引](D:/project/Python/alioth/.worktrees/web-v2-dynamic-exploration-evidence/docs/superpowers/plans/2026-09-19-web-seven-sites-step-by-step-evidence.md)。

仍需区分的缺口：chigua 两个独立正文区域触发主体冲突；91jav1 两个低置信度页与两个证据不足页保留 unknown（`/my` 虽 0.80 仍为 EVIDENCE_INSUFFICIENT）；广告域名的非公网 DNS；HL365 根导航驱动失败的底层网络原因未确定；AVHub 稳定采样超时。当前证据不支持新增页面类型，也不能通过放宽安全或可信证据门槛宣称七站全通过。

## 追加版本在线复测最终结果

### 黑料：导航恢复、五菜单全部结算，内容判定仍有缺口

- Run `c7dcec62-c959-41f7-96fd-f28fb0867e90`，UTC 2026-09-18 17:29:58—17:50:26，约 20 分 28 秒。
- 根 GOTO 成功，age_gate（0.90），年龄确认及 5 次普通关闭完成，OBSERVE 就绪，滚动补证保存。正式首页由首轮 unknown（0.60）变为 home（0.80）；同帧顶部导航事实满足合同，34 条顶部 href 已取得。
- 首页独立广告到达 `https://d3o3n2nxky47xw.cloudfront.net/gcpa032`，首屏/DOM 留存并标记 ad_page，关闭后再推进主路径。
- 搜索辅助入口 `/index/search` 为 unknown（0.60）：模型提出 search_page，但缺搜索触发器和查询结果组事实；独立结算，不占普通菜单额度。
- 菜单 1 `/archives/39668/` 为 unknown（0.65）；模型 content_detail，主体计数不足、重复卡片强冲突。
- 菜单 2 `/archives/8147/` 为 content_list（0.70），该页独立广告到达 `https://d1z3ecmmrbmix9.cloudfront.net/gcpa032` 并成功关闭；详情候选 `/archives/115347/` 为 unknown（0.65），模型 video_detail，但缺已证明播放器及唯一主体，重复卡片强冲突。
- 菜单 3 `/archives/8148/` 为 unknown（0.65）；同样为 content_detail 提议与主体/卡片强冲突。
- 菜单 4 `/cpcd/` 为 video_list（0.75），广告 `https://vpe9prp78e.98319580.com/` 留证成功；详情候选 `/archives/115713/` 为 unknown（0.65），video_detail 提议与主体/播放器缺失冲突。可选正文目标缺失不作废正式页和滚动证据。
- 菜单 5 `/djbl/` 为 unknown（0.65）：模型提出 content_list，结构必要条件满足，但模型明确要求复核，未强制放行。
- 最终 5/5 菜单结算，3 个菜单未分类、2 个候选详情类型不匹配；无可信详情成功。3/3 广告成功，6 个正式 unknown（含辅助页及详情）。业务 checkpoint 全部 session_close/succeeded，广告全部 ad_closeout/succeeded，session_closed=true、report_finalized=true，Run failed。
- 内容页同 DOM 可见 detail-page、detail-blockquote 等混合容器及大量链接，但现有主体投影为 0。仅凭容器命名不能把促销/推荐链接整体认作正文，也不能凭模型看到视频就伪造已证明主播放器；后续应核对正文、推荐与媒体证据归属，而非增加站点类型。这个缺口本轮尚未消除。

### HL365：根导航可达，低置信度首页正常收口

- Run `5e1c1267-a688-41e3-af4c-493491abf75b`，UTC 2026-09-18 17:51:15—18:03:43，约 12 分 28 秒。
- 根 GOTO 成功，age_gate（0.92），年龄确认及 6 次普通关闭完成。OBSERVE 通过 readiness，滚动补充截图保存。首轮首跳驱动失败本次没有复现，不宣称网络问题已被代码修复。
- 首页正式 Capture `944714ae-e9c6-44fc-9411-5b20ea870d64`：模型提出 home（0.65），顶部导航、可见分类入口、重复主卡片等 home 必要条件全部满足；最终因 `WEB_PAGE_TYPE_LOW_CONFIDENCE` 保留 unknown。不是导航特征为零，也不是 readiness 错误。
- 追加修复在线触发：记录 `WEB_HOME_PAGE_TYPE_UNCLASSIFIED`，不调用 home-only 菜单授权，不派发辅助导航，checkpoint 到 session_close/succeeded、会话 closed。没有旧角色异常、URL 二次异常或悬挂 running 阶段。
- 0 菜单/广告是未知首页被正常挡住，不是广告分支漏调度。report_finalized=false、Run failed，不能计为采集成功。空白首跳异常路径由新增失败注入回归证明，此次在线只证明正常导航与未知首页收口。

### 复测审计与本轮结论

黑料 65 张、HL365 18 张真实图片，文件缺失 0、摘要不符 0，会话均 closed。首轮加复测共 377 张真实图片、27 个广告分支（6 成功、21 非公网地址拒绝），这是 9 次 Run 的合计，不冒充同一版本七站全通过。

[两站复测详细过程与证据](D:/project/Python/alioth/.worktrees/web-v2-dynamic-exploration-evidence/docs/superpowers/plans/2026-09-19-web-two-sites-followup-evidence.md) 含全部菜单 href、动作顺序、URL、页面类型、分数、分类原因、截图/DOM 和广告逐页审计。

本轮按“通用 Web 修复 → 审核与 478 项首轮门禁 → 七站首轮 → 有界追加修复与 603 项门禁 → 两站复测 → 证据审计”结束，不继续自动扩展修复循环。代码门禁通过不等于站点验收通过：正文/推荐/播放器归属、部分模型低置信度、AVHub 稳定采样、广告 DNS 安全拒绝仍是明确未解决项。未新增页面类型；未降低分类置信度、放宽 SSRF 或修改 App/H5；未提交或推送。

环境收尾：确认专用库 active_runs=0 后，专用 `alioth-web-five-api-20260917`、`alioth-web-five-worker-20260917` 已停止并恢复原状态；主服务未操作，全部报告与证据保留。
