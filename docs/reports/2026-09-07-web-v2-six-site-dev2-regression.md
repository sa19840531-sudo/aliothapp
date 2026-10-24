# Web V2 六站实况探索报告

> 运行环境说明：`heiliao.com` 完成探索后，模型网关返回 HTTP 402 余额不足，后续五站因此在模型判定前失败。五站失败属于外部服务额度阻断，本批次仅证明失败报告与入口可见，不代表六站探索质检验收通过；供应商账户等原始异常字段均已脱敏。

- 结论：未通过
- 站点：6/6
- Catalog 菜单：166/166
- 单站菜单上限：10
- 同源菜单执行：10/51
- 限额跳过：106/106
- 首页基线：0/6
- 逻辑详情：0/51
- 跨域阻断：3/3
- Frontier 剩余：46

## 聚合门禁错误

- executed\_same\_origin\_count: expected=51, actual=10
- home\_baseline\_count: expected=6, actual=0
- detail\_count: expected=51, actual=0
- frontier\_remaining: expected=0, actual=46
- https://heiliao.com/: 最终报告未封存
- https://hl365.com/: 最终报告未封存
- https://www.91jav1.com/: 最终报告未封存
- http://91porna.com/: 最终报告未封存
- http://huangguoai.com/: 最终报告未封存
- https://avhub360.com/: 最终报告未封存
- https://heiliao.com/ preview: 首页证据计数必须为 1
- https://hl365.com/ preview: 任务不是可查看终态
- https://hl365.com/ preview: failure\_reason 非空
- https://hl365.com/ preview: evidence 仍含非终态菜单
- https://hl365.com/ preview: 首页证据计数必须为 1
- https://hl365.com/ preview: 已执行菜单计数与预算不一致
- https://www.91jav1.com/ preview: 任务不是可查看终态
- https://www.91jav1.com/ preview: failure\_reason 非空
- https://www.91jav1.com/ preview: evidence 仍含非终态菜单
- https://www.91jav1.com/ preview: 首页证据计数必须为 1
- https://www.91jav1.com/ preview: 已执行菜单计数与预算不一致
- http://91porna.com/ preview: 任务不是可查看终态
- http://91porna.com/ preview: failure\_reason 非空
- http://91porna.com/ preview: evidence 仍含非终态菜单
- http://91porna.com/ preview: 首页证据计数必须为 1
- http://91porna.com/ preview: 已执行菜单计数与预算不一致
- http://huangguoai.com/ preview: 任务不是可查看终态
- http://huangguoai.com/ preview: failure\_reason 非空
- http://huangguoai.com/ preview: evidence 仍含非终态菜单
- http://huangguoai.com/ preview: 首页证据计数必须为 1
- http://huangguoai.com/ preview: 已执行菜单计数与预算不一致
- https://avhub360.com/ preview: 任务不是可查看终态
- https://avhub360.com/ preview: failure\_reason 非空
- https://avhub360.com/ preview: evidence 仍含非终态菜单
- https://avhub360.com/ preview: 首页证据计数必须为 1
- https://avhub360.com/ preview: 已执行菜单计数与预算不一致

## 站点执行概览

| 顺序 | 站点 | Task 状态 | 报告 | Catalog | 首页 | 执行菜单 | 逻辑详情 | 限额跳过 | Frontier |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | https://heiliao.com/ | review | preview | 30 | 0 | 10 | 0 | 19 | 0 |
| 2 | https://hl365.com/ | failed | preview | 43 | 0 | 0 | 0 | 32 | 11 |
| 3 | https://www.91jav1.com/ | failed | preview | 8 | 0 | 0 | 0 | 1 | 6 |
| 4 | http://91porna.com/ | failed | preview | 65 | 0 | 0 | 0 | 52 | 11 |
| 5 | http://huangguoai.com/ | failed | preview | 9 | 0 | 0 | 0 | 1 | 8 |
| 6 | https://avhub360.com/ | failed | preview | 11 | 0 | 0 | 0 | 1 | 10 |

## 1. https://heiliao.com/

- task_id：8109a775-953b-4688-a9f8-f778eb6a4cd8
- run_id：d97f4c18-bde3-4052-bff6-12c3961712f2
- 任务状态：review
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：-
- 页面类型：-
- 状态计数：{&quot;menu\_limit\_skipped&quot;: 19, &quot;navigation\_failed&quot;: 9, &quot;page\_type\_unclassified&quot;: 1, &quot;popup\_blocked&quot;: 1}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | heiliao.home | page\_type\_unclassified | - | - | - |
| menu\_not\_executed | heiliao.hlcg | popup\_blocked | - | - | - |
| menu\_not\_executed | heiliao.jrrs | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.jqrm | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.lsdg | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.sjb | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.xycg | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.whhl | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.fczq | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.ycsq | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.mxcw | navigation\_failed | - | - | - |
| menu\_not\_executed | heiliao.mrds | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.qqqw | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.ttsq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.ysdj | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.whhj | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.shxw | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.thzq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.cpcd | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.yqby | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.syzy | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.djbl | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.jqxs | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.gchl | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.hlkt | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.hlbg | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.ttzz | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.mrrb | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.zbjx | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | heiliao.ybrg | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | review | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 2. https://hl365.com/

- task_id：0bf808f9-1fad-425b-81dc-06190da17909
- run_id：b552a4e6-4bab-4884-870a-c0d44135184b
- 任务状态：failed
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：\[redacted-sensitive\]
- 页面类型：-
- 状态计数：{&quot;home\_pending&quot;: 1, &quot;menu\_limit\_skipped&quot;: 32, &quot;pending&quot;: 10}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | hl365.home | home\_pending | - | - | - |
| menu\_not\_executed | hl365.24hcg | pending | - | - | - |
| menu\_not\_executed | hl365.sjb\_hl | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.sjb\_ttt | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.sjb\_rs | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.sjb\_bc | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.sjb\_qm | pending | - | - | - |
| menu\_not\_executed | hl365.mrds | pending | - | - | - |
| menu\_not\_executed | hl365.swdj | pending | - | - | - |
| menu\_not\_executed | hl365.rgtj | pending | - | - | - |
| menu\_not\_executed | hl365.mrrg | pending | - | - | - |
| menu\_not\_executed | hl365.hlda | pending | - | - | - |
| menu\_not\_executed | hl365.fcns | pending | - | - | - |
| menu\_not\_executed | hl365.xyrg | pending | - | - | - |
| menu\_not\_executed | hl365.whhl | pending | - | - | - |
| menu\_not\_executed | hl365.hlzt | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.mxbg | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.gcmw | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.mrst | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.lydt | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.llsq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.avjs | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.thdq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.whzj | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.qgzq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.wyxs | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.pmv | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.chjlb | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.back\_home | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.contribute | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.archives | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.tags | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.faq | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.weixin | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.tg\_1 | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.twitter | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.business | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.contact | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.app | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.navigation | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.yscloud | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.metaverse | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | hl365.short\_drama | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | failed | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 3. https://www.91jav1.com/

- task_id：cbcedfd7-b640-4617-a338-8ae15486e5a0
- run_id：11516c31-51b3-4867-b5d3-301a529cf996
- 任务状态：failed
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：\[redacted-sensitive\]
- 页面类型：-
- 状态计数：{&quot;cross\_origin\_blocked&quot;: 1, &quot;home\_pending&quot;: 1, &quot;menu\_limit\_skipped&quot;: 1, &quot;pending&quot;: 5}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | 91jav.home | home\_pending | - | - | - |
| menu\_not\_executed | 91jav.actress\_hot | pending | - | - | - |
| menu\_not\_executed | 91jav.subtitle | pending | - | - | - |
| menu\_not\_executed | 91jav.uncensored | pending | - | - | - |
| menu\_not\_executed | 91jav.theme | pending | - | - | - |
| menu\_not\_executed | 91jav.movie | pending | - | - | - |
| menu\_not\_executed | 91jav.official\_app | cross\_origin\_blocked | - | - | - |
| menu\_not\_executed | 91jav.home\_route | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | failed | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 4. http://91porna.com/

- task_id：5fb9b4d9-cd5d-4b61-8c13-4b571877d260
- run_id：7aa9490e-271e-4ca2-afec-99570e0cfd93
- 任务状态：failed
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：\[redacted-sensitive\]
- 页面类型：-
- 状态计数：{&quot;cross\_origin\_blocked&quot;: 2, &quot;home\_pending&quot;: 1, &quot;menu\_limit\_skipped&quot;: 52, &quot;pending&quot;: 10}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | 91porna.home | home\_pending | - | - | - |
| menu\_not\_executed | 91porna.video | pending | - | - | - |
| menu\_not\_executed | 91porna.video.hot | pending | - | - | - |
| menu\_not\_executed | 91porna.video.original | pending | - | - | - |
| menu\_not\_executed | 91porna.video.gossip | pending | - | - | - |
| menu\_not\_executed | 91porna.video.mature | pending | - | - | - |
| menu\_not\_executed | 91porna.video.young\_style | pending | - | - | - |
| menu\_not\_executed | 91porna.video.animation | pending | - | - | - |
| menu\_not\_executed | 91porna.video.black | pending | - | - | - |
| menu\_not\_executed | 91porna.video.busty | pending | - | - | - |
| menu\_not\_executed | 91porna.video.swap | pending | - | - | - |
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
| task\_terminal | - | failed | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 5. http://huangguoai.com/

- task_id：332b23c1-37a0-46f9-87e9-9a3b29ac1b62
- run_id：23f25ec4-7521-4d42-8a7b-ddc06e678f10
- 任务状态：failed
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：\[redacted-sensitive\]
- 页面类型：-
- 状态计数：{&quot;home\_pending&quot;: 1, &quot;menu\_limit\_skipped&quot;: 1, &quot;pending&quot;: 7}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | huangguo.home | home\_pending | - | - | - |
| menu\_not\_executed | huangguo.ai\_short | pending | - | - | - |
| menu\_not\_executed | huangguo.ai\_comic | pending | - | - | - |
| menu\_not\_executed | huangguo.ai\_face\_swap | pending | - | - | - |
| menu\_not\_executed | huangguo.ai\_remix | pending | - | - | - |
| menu\_not\_executed | huangguo.topics | pending | - | - | - |
| menu\_not\_executed | huangguo.rank | pending | - | - | - |
| menu\_not\_executed | huangguo.gossip | pending | - | - | - |
| menu\_not\_executed | huangguo.home\_route | menu\_limit\_skipped | - | - | - |
| task\_terminal | - | failed | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |

## 6. https://avhub360.com/

- task_id：d4beaafa-d433-4777-9c45-bcfcc44bf309
- run_id：d23594f8-e14c-4615-9363-a03c393229ce
- 任务状态：failed
- 报告状态：ready / preview
- 报告结论：incomplete
- 报告错误码：-
- 失败原因：\[redacted-sensitive\]
- 页面类型：-
- 状态计数：{&quot;home\_pending&quot;: 1, &quot;menu\_limit\_skipped&quot;: 1, &quot;pending&quot;: 9}
- 证据包状态：needs\_review

| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |
|---|---|---|---|---|---|---|---|

### 探索过程节点

| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |
|---|---|---|---|---|---|
| task\_submitted | - | queued | - | - | - |
| menu\_not\_executed | avhub.home | home\_pending | - | - | - |
| menu\_not\_executed | avhub.new | pending | - | - | - |
| menu\_not\_executed | avhub.subtitle | pending | - | - | - |
| menu\_not\_executed | avhub.uncensored | pending | - | - | - |
| menu\_not\_executed | avhub.theme | pending | - | - | - |
| menu\_not\_executed | avhub.actress | pending | - | - | - |
| menu\_not\_executed | avhub.popular | pending | - | - | - |
| menu\_not\_executed | avhub.movie | pending | - | - | - |
| menu\_not\_executed | avhub.female | pending | - | - | - |
| menu\_not\_executed | avhub.home\_route | menu\_limit\_skipped | - | - | - |
| menu\_not\_executed | avhub.tags | pending | - | - | - |
| task\_terminal | - | failed | - | - | - |
| report\_sealed | - | preview | - | - | - |
| evidence\_sealed | - | needs\_review | - | - | - |
