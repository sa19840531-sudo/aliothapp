# Web V2 六站实况探索报告

> 注意：本文件由 preview 适配修复前的 runner 生成。下方逐节点 Task/Run/Capture 为真实实测数据，但头部 `menu_total=0` 等聚合值是旧版 final 字段误读，不代表未执行菜单。正确汇总见 `docs/reports/2026-09-04-web-v2-six-site-live-retest-summary.md`。

- 结论：未通过
- 站点：3/6
- Catalog 菜单：0/166
- 单站菜单上限：5
- 同源菜单执行：0/15
- 限额跳过：0/65
- 首页基线：0/6
- 逻辑详情：11/15
- 跨域阻断：0/3
- Frontier 剩余：0

## 聚合门禁错误

- menu\_total: expected=85, actual=0
- executed\_same\_origin\_count: expected=15, actual=0
- menu\_limit\_skipped\_count: expected=65, actual=0
- home\_baseline\_count: expected=3, actual=0
- detail\_count: expected=15, actual=11
- blocked\_cross\_origin\_count: expected=2, actual=0
- http://91porna.com/: 最终报告未封存
- http://huangguoai.com/: 最终报告未封存
- https://avhub360.com/: 最终报告未封存
- http://91porna.com/: site\_id expected=91porna\_com, actual=None
- http://91porna.com/: Catalog entries 或 menu\_ledger 不是数组
- http://91porna.com/ evidence: status 必须为 evidence\_ready
- http://91porna.com/ evidence: reason\_codes 必须为空
- http://91porna.com/ evidence \[91porna.home\]: 首页证据形态无效
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- http://91porna.com/ report/evidence: 缺少可对账的 entries
- http://huangguoai.com/: site\_id expected=huangguoai\_com, actual=None
- http://huangguoai.com/: Catalog entries 或 menu\_ledger 不是数组
- http://huangguoai.com/ evidence: status 必须为 evidence\_ready
- http://huangguoai.com/ evidence: reason\_codes 必须为空
- http://huangguoai.com/ evidence: detail\_count expected=5, actual=4
- http://huangguoai.com/ evidence: 详情证据条目 expected=5, actual=4
- http://huangguoai.com/ evidence \[huangguo.home\]: 首页证据形态无效
- http://huangguoai.com/ evidence \[huangguo.topics\]: 非成功终态 navigation\_failed
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- http://huangguoai.com/ evidence: 2e055343-8440-4b60-b5c6-08c60e0b4124 页面类型与账本不一致
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- http://huangguoai.com/ report/evidence: 缺少可对账的 entries
- https://avhub360.com/: site\_id expected=avhub360\_com, actual=None
- https://avhub360.com/: Catalog entries 或 menu\_ledger 不是数组
- https://avhub360.com/ evidence: status 必须为 evidence\_ready
- https://avhub360.com/ evidence: reason\_codes 必须为空
- https://avhub360.com/ evidence: detail\_count expected=5, actual=2
- https://avhub360.com/ evidence: 详情证据条目 expected=5, actual=2
- https://avhub360.com/ evidence \[avhub.home\]: 首页证据形态无效
- https://avhub360.com/ evidence \[avhub.new\]: 非成功终态 detail\_selection\_untrusted
- https://avhub360.com/ evidence \[avhub.theme\]: 非成功终态 detail\_page\_type\_mismatch
- https://avhub360.com/ evidence \[avhub.actress\]: 非成功终态 detail\_page\_type\_mismatch
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- \[redacted-sensitive\]
- https://avhub360.com/ evidence: e4d94a51-db66-4904-8d7e-d6da18db9e96 页面类型与账本不一致
- https://avhub360.com/ evidence: f6caa306-5062-413a-ac6a-1da5f53bc361 页面类型与账本不一致
- \[redacted-sensitive\]
- https://avhub360.com/ report/evidence: 缺少可对账的 entries

## 站点执行概览

| 顺序 | 站点 | Task 状态 | 报告 | Catalog | 首页 | 执行菜单 | 逻辑详情 | 限额跳过 | Frontier |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | http://91porna.com/ | review | preview | 0 | 1 | 0 | 5 | 0 | 0 |
| 2 | http://huangguoai.com/ | review | preview | 0 | 1 | 0 | 4 | 0 | 0 |
| 3 | https://avhub360.com/ | review | preview | 0 | 1 | 0 | 2 | 0 | 0 |

## 1. http://91porna.com/

- task_id：18ba39dc-3431-4e8c-9aef-15779b359210
- run_id：1c00bd53-8d16-40e4-adb5-90962cf33c33
- 任务状态：review
- 报告状态：failed / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：-
- 页面类型：-
- 状态计数：{}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| home\_probe | 91porna.home | home\_baseline | video\_list | - | 46dd7687-b39f-4049-af76-4999c576ed8c |
| home\_ready | 91porna.home | home\_baseline | video\_list | - | ba18faf3-d0cc-4b20-a98f-612e2cfba0d6 |
| menu\_action | 91porna.video | completed | - | action-execution:81d4fa9e60d271e49682ca6f2be04eeffe3b5852291e8646a3db7cf548927143 | - |
| menu\_probe | 91porna.video | detail\_captured | video\_list | - | b9162e49-f4ea-4b2c-9a21-0871e1d2dce2 |
| menu\_ready | 91porna.video | detail\_captured | video\_list | - | 29231b6b-d537-4a3e-9f0c-1b41ab8cf73c |
| menu\_classified | 91porna.video | detail\_captured | video\_list | - | 29231b6b-d537-4a3e-9f0c-1b41ab8cf73c |
| detail\_action | 91porna.video | completed | - | action-execution:786208ab5f8cad926e2cb3cd773bb518076893cad391a63fe12b13965d5e1526 | - |
| detail\_probe | 91porna.video | detail\_captured | video\_detail | - | 65d31815-4a0c-4b86-b021-aea8f0969c61 |
| detail\_ready | 91porna.video | detail\_captured | video\_detail | - | 7b76bb25-5d3c-4318-bb3c-c9d990a9f66d |
| detail\_classified | 91porna.video | detail\_captured | video\_detail | - | 7b76bb25-5d3c-4318-bb3c-c9d990a9f66d |
| menu\_action | 91porna.video.hot | completed | - | action-execution:70c5c3eeea56563bdab72f1ea27a0230785ebf402bb8e8de01f945863654fa2f | - |
| menu\_probe | 91porna.video.hot | detail\_captured | video\_list | - | e0fd6e09-135f-4ecb-9fae-45a4c18d0dff |
| menu\_ready | 91porna.video.hot | detail\_captured | video\_list | - | 6290c944-4aaf-4239-ac30-a571a94e5cee |
| menu\_classified | 91porna.video.hot | detail\_captured | video\_list | - | 6290c944-4aaf-4239-ac30-a571a94e5cee |
| detail\_action | 91porna.video.hot | completed | - | action-execution:1838d1efc42928f33f02f92b410878b63b7a77ae66f0e216360fc8e5aa90ea15 | - |
| detail\_probe | 91porna.video.hot | detail\_captured | video\_detail | - | 06fa1eb3-f8ce-406a-a6ad-3115650807a5 |
| detail\_ready | 91porna.video.hot | detail\_captured | video\_detail | - | 512535f6-a9fd-46af-b6e7-f47da402d190 |
| detail\_classified | 91porna.video.hot | detail\_captured | video\_detail | - | 512535f6-a9fd-46af-b6e7-f47da402d190 |
| menu\_action | 91porna.video.original | completed | - | action-execution:b2056fc0983d96ba22de053c09456d08a093974ed1bfff9cd9fcf241d1e0bdd7 | - |
| menu\_probe | 91porna.video.original | detail\_captured | video\_list | - | 89329660-00ad-41be-85b1-0d72d6926f0d |
| menu\_ready | 91porna.video.original | detail\_captured | video\_list | - | fb44b770-0e57-4518-aa2e-f97eabefc8cc |
| menu\_classified | 91porna.video.original | detail\_captured | video\_list | - | fb44b770-0e57-4518-aa2e-f97eabefc8cc |
| detail\_action | 91porna.video.original | completed | - | action-execution:d54799a5e7c83cba2032413712930e6085e8bfb09e5b8295cd83e923061f249e | - |
| detail\_probe | 91porna.video.original | detail\_captured | video\_detail | - | f74dbb6b-f6a8-4c53-896e-7e61479486cd |
| detail\_ready | 91porna.video.original | detail\_captured | video\_detail | - | 44fd968b-cf5d-41d4-8111-90f77b209324 |
| detail\_classified | 91porna.video.original | detail\_captured | video\_detail | - | 44fd968b-cf5d-41d4-8111-90f77b209324 |
| menu\_action | 91porna.video.gossip | completed | - | action-execution:012fda96c14932397e1bca5ec4ca306637c93526f089d616dfa7df05aef5ac35 | - |
| menu\_probe | 91porna.video.gossip | detail\_captured | video\_list | - | 14df380c-9160-4fa2-ac33-0e39f32aebfe |
| menu\_ready | 91porna.video.gossip | detail\_captured | video\_list | - | 08f93e6e-7b5f-4403-9c39-fe1ff78bc74c |
| menu\_classified | 91porna.video.gossip | detail\_captured | video\_list | - | 08f93e6e-7b5f-4403-9c39-fe1ff78bc74c |
| detail\_action | 91porna.video.gossip | completed | - | action-execution:2ce69523771a16feb5a496c37189a2944c6e2d0ddbda264764895cd6bbd8856a | - |
| detail\_probe | 91porna.video.gossip | detail\_captured | video\_detail | - | 3bc6e272-69b5-4edb-885c-6568ba698199 |
| detail\_ready | 91porna.video.gossip | detail\_captured | video\_detail | - | bfbad33b-6fed-4151-8222-6f084356de23 |
| detail\_classified | 91porna.video.gossip | detail\_captured | video\_detail | - | bfbad33b-6fed-4151-8222-6f084356de23 |
| menu\_action | 91porna.video.mature | completed | - | action-execution:dd0338e190576a502e4ff746b152d93634aa0cab401586fe53001889a4e0e748 | - |
| menu\_probe | 91porna.video.mature | detail\_captured | video\_list | - | f5c7158f-7fae-470e-84de-b0fe271a19da |
| menu\_ready | 91porna.video.mature | detail\_captured | video\_list | - | 5904d482-a588-40af-acfc-0bd2467d79fa |
| menu\_classified | 91porna.video.mature | detail\_captured | video\_list | - | 5904d482-a588-40af-acfc-0bd2467d79fa |
| detail\_action | 91porna.video.mature | completed | - | action-execution:56cf7a05169178885d11adcfa77053e300263ae2bd5765cfb19059a9e413da77 | - |
| detail\_probe | 91porna.video.mature | detail\_captured | video\_detail | - | 2735b432-6db7-4d83-93f8-140513ff7796 |
| detail\_ready | 91porna.video.mature | detail\_captured | video\_detail | - | 4ef4b2b9-e520-41c1-a62c-31eb779f6f79 |
| detail\_classified | 91porna.video.mature | detail\_captured | video\_detail | - | 4ef4b2b9-e520-41c1-a62c-31eb779f6f79 |
| menu\_not\_executed | 91porna.video.young\_style | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.animation | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.black | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.busty | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.swap | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.internal | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.massage | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.tanhua | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.incest | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.video.category\_three | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.amateur | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.edit | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.contrast | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.influencer | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.celebrity | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.short.original | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.today | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.campus | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.celebrity | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.influencer | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.contest | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.gossip.people | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai.short | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai.comic | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai.beauty | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai.face\_swap | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.ai.external\_short | cross\_origin\_blocked | - | - | - |
| menu\_not\_executed | 91porna.av | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.group | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.uncensored | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.subtitle | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.uniform | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.black | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.av.sm | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.adult | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.jp | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.cn | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.3d | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.fan | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.animation.set | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets.latest | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets.rank | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets.category | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets.people | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.sets.brand | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.novels | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.novels.city | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.novels.campus | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.novels.mature | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.novels.family | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.brand | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.brand.home | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | 91porna.brand.app | cross\_origin\_blocked | - | - | - |
| menu\_not\_executed | 91porna.brand.more | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | review | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 2. http://huangguoai.com/

- task_id：b5e2dfd7-2246-4276-84ab-ca9c41da5c91
- run_id：1fdee8ab-4cd7-4a1d-8a89-170d388b36a6
- 任务状态：review
- 报告状态：failed / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：-
- 页面类型：-
- 状态计数：{}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| home\_probe | huangguo.home | home\_baseline | video\_list | - | 2c8652b0-6895-4638-abb2-a80af08bb3ef |
| home\_ready | huangguo.home | home\_baseline | video\_list | - | bc8aacd2-a333-4868-adc1-87938a1c4018 |
| menu\_action | huangguo.ai\_short | completed | - | action-execution:7c7ca289badc876142115efecab2b0049300566a381ba23b2ad2022faf3c3ac1 | - |
| menu\_probe | huangguo.ai\_short | detail\_captured | video\_list | - | c1f91111-9bea-400a-a1fb-469da2d726ba |
| menu\_ready | huangguo.ai\_short | detail\_captured | video\_list | - | 7577500a-405b-4c42-86a0-7aeda1d1a15a |
| menu\_classified | huangguo.ai\_short | detail\_captured | video\_list | - | 7577500a-405b-4c42-86a0-7aeda1d1a15a |
| detail\_action | huangguo.ai\_short | completed | - | action-execution:8c6632b2c3c1f757d501b1109a85378a506690be724d1bf5b629e01eaf5d7ff5 | - |
| detail\_probe | huangguo.ai\_short | detail\_captured | video\_detail | - | d4328f9d-db7b-44e7-b7f7-1756f4fbf931 |
| detail\_ready | huangguo.ai\_short | detail\_captured | video\_detail | - | 2e9a75a1-c17e-4b78-af14-8c06a952b5ac |
| detail\_classified | huangguo.ai\_short | detail\_captured | video\_detail | - | 2e9a75a1-c17e-4b78-af14-8c06a952b5ac |
| menu\_action | huangguo.ai\_comic | completed | - | action-execution:66071ef314e5354d1ed8f9e42ff02358e7b1f7f88b7715a3d06829b58111697f | - |
| menu\_probe | huangguo.ai\_comic | detail\_captured | video\_list | - | 1e06b66b-0d1d-45cd-9d33-a20ecd18527a |
| menu\_ready | huangguo.ai\_comic | detail\_captured | video\_list | - | d7237764-0d5e-4fd3-91b9-3d7465860115 |
| menu\_classified | huangguo.ai\_comic | detail\_captured | video\_list | - | d7237764-0d5e-4fd3-91b9-3d7465860115 |
| detail\_action | huangguo.ai\_comic | completed | - | action-execution:1c84e2ddca2bbf396ebc74a3fbbbae3622551a52e9669964b61ae7c110bb474a | - |
| detail\_probe | huangguo.ai\_comic | detail\_captured | video\_detail | - | b005ab87-0e97-4220-9bbe-bdc566463598 |
| detail\_ready | huangguo.ai\_comic | detail\_captured | video\_detail | - | 837ec854-953c-43d8-9574-0e20cb20c6d6 |
| detail\_classified | huangguo.ai\_comic | detail\_captured | video\_detail | - | 837ec854-953c-43d8-9574-0e20cb20c6d6 |
| menu\_action | huangguo.ai\_face\_swap | completed | - | action-execution:ea65d9c9e61083e4efd28c1e231154799e62b6414bf1cddd3960a0d792bc7891 | - |
| menu\_probe | huangguo.ai\_face\_swap | detail\_captured | video\_list | - | b3461922-6260-49ae-9e6d-da3edabda576 |
| menu\_ready | huangguo.ai\_face\_swap | detail\_captured | video\_list | - | 2731dc0a-ad02-4b02-a88a-c9a62595f548 |
| menu\_classified | huangguo.ai\_face\_swap | detail\_captured | video\_list | - | 2731dc0a-ad02-4b02-a88a-c9a62595f548 |
| detail\_action | huangguo.ai\_face\_swap | completed | - | action-execution:cd61c1679d2e8faee3253e943ccc3cb0b293f760153be4f042edcd9b10f20694 | - |
| detail\_probe | huangguo.ai\_face\_swap | detail\_captured | video\_detail | - | 0d33bf48-673c-46e7-9c1a-6a3185fe8aa3 |
| detail\_ready | huangguo.ai\_face\_swap | detail\_captured | video\_detail | - | 5756f056-b5b1-401c-8083-a0e34fa93908 |
| detail\_classified | huangguo.ai\_face\_swap | detail\_captured | video\_detail | - | 5756f056-b5b1-401c-8083-a0e34fa93908 |
| menu\_action | huangguo.ai\_remix | completed | - | action-execution:436ac4d05f4d76ca0005d0de4c7b97a3916161a519e192c1d84d4555c3b1458b | - |
| menu\_probe | huangguo.ai\_remix | detail\_captured | video\_list | - | 9d5e86e5-5174-40cc-9481-b669c0f865e0 |
| menu\_ready | huangguo.ai\_remix | detail\_captured | video\_list | - | e6fc728c-18cc-45bf-935f-eda9495c45df |
| menu\_classified | huangguo.ai\_remix | detail\_captured | video\_list | - | e6fc728c-18cc-45bf-935f-eda9495c45df |
| detail\_action | huangguo.ai\_remix | completed | - | action-execution:95d5845723c9f3fbf222475541c7c05f7bc96755caa19a32e3302f00acf96b5e | - |
| detail\_probe | huangguo.ai\_remix | detail\_captured | video\_detail | - | adb84c12-0c32-4dd8-a98c-8c943162511a |
| detail\_ready | huangguo.ai\_remix | detail\_captured | video\_detail | - | 786fcbc0-224a-4fab-b923-48f6cd8a08ba |
| detail\_classified | huangguo.ai\_remix | detail\_captured | video\_detail | - | 786fcbc0-224a-4fab-b923-48f6cd8a08ba |
| menu\_not\_executed | huangguo.topics | navigation\_failed | - | - | - |
| menu\_not\_executed | huangguo.rank | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | huangguo.gossip | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | huangguo.home\_route | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | review | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 3. https://avhub360.com/

- task_id：971f155d-0b0a-47fe-bd1d-9f0aea0d5d1e
- run_id：1cd30b24-13dc-48df-8da9-0254cc2a3c32
- 任务状态：review
- 报告状态：failed / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：-
- 页面类型：-
- 状态计数：{}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| home\_probe | avhub.home | home\_baseline | video\_list | - | 5c716646-737e-438a-bfa9-94b753dfcf19 |
| home\_ready | avhub.home | home\_baseline | video\_list | - | b273f878-7c56-4499-9fae-0a196e63e418 |
| menu\_not\_executed | avhub.new | detail\_selection\_untrusted | - | - | - |
| menu\_action | avhub.subtitle | completed | - | action-execution:0db2ad458e97871eea888c683ca68f0ac50992148b2d77dfd9f354948324ec77 | - |
| menu\_probe | avhub.subtitle | detail\_captured | video\_list | - | e4d94a51-db66-4904-8d7e-d6da18db9e96 |
| menu\_ready | avhub.subtitle | detail\_captured | video\_list | - | aeaf030c-9cad-449a-8a6d-290c787af5fd |
| menu\_classified | avhub.subtitle | detail\_captured | video\_list | - | aeaf030c-9cad-449a-8a6d-290c787af5fd |
| detail\_action | avhub.subtitle | completed | - | action-execution:8f3a993cbe202050f125262b5a515f6753b6f530233bba3abe8c5a1283c2063a | - |
| detail\_probe | avhub.subtitle | detail\_captured | video\_detail | - | 86a6f7c7-d602-403d-9db4-6b2d3ff95229 |
| detail\_ready | avhub.subtitle | detail\_captured | video\_detail | - | a92174de-74f3-4d53-96a8-1963dd0cbbcd |
| detail\_classified | avhub.subtitle | detail\_captured | video\_detail | - | a92174de-74f3-4d53-96a8-1963dd0cbbcd |
| menu\_action | avhub.uncensored | completed | - | action-execution:046c0c1569ae53a649e082f7882244ec0abdcc5789e54652d3a0d8d73d122bea | - |
| menu\_probe | avhub.uncensored | detail\_captured | video\_list | - | 35dd486b-821b-49e0-af0a-044ddc9e02f5 |
| menu\_ready | avhub.uncensored | detail\_captured | video\_list | - | 0184a2f4-9e0a-413f-85d5-5580c77e6310 |
| menu\_classified | avhub.uncensored | detail\_captured | video\_list | - | 0184a2f4-9e0a-413f-85d5-5580c77e6310 |
| detail\_action | avhub.uncensored | completed | - | action-execution:f41745970039dd27bbc4c4e29c519b6e856d339eed15802a9ec41c856e0ac76d | - |
| detail\_probe | avhub.uncensored | detail\_captured | video\_detail | - | bbbf052a-690a-4d7c-af70-5c028f6c985a |
| detail\_ready | avhub.uncensored | detail\_captured | video\_detail | - | 54d6b372-d557-4c8d-b6a4-6f7403d1b286 |
| detail\_classified | avhub.uncensored | detail\_captured | video\_detail | - | 54d6b372-d557-4c8d-b6a4-6f7403d1b286 |
| menu\_not\_executed | avhub.theme | detail\_page\_type\_mismatch | - | - | - |
| menu\_not\_executed | avhub.actress | detail\_page\_type\_mismatch | - | - | - |
| menu\_not\_executed | avhub.popular | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | avhub.movie | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | avhub.female | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | avhub.home\_route | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | avhub.tags | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | review | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |
