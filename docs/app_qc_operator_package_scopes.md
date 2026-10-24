# App 质检：业务方可配置的包覆盖范围

> 对应报告「固定 11 页型当分母、没有的页也算未覆盖」。  
> 正式区分 not_applicable / not_explored 仍属平台能力；业务侧现阶段应通过  
> `POST /api/v2/tasks` → `target.exploration_target.required_page_types` **显式缩小分母**。  
> 代码入口：`services/api/app/domain/exploration/app_qc_package_scopes.py`（按 `package_name` 解析）。

## 操作原则

1. 只把本包**真实存在且本轮允许探索**的业务页型写入 `required_page_types`。
2. 本轮不测的能力（登录后、充值、发布）写进 `remark`，不要留在分母里冒充缺口。
3. 未传 `exploration_target` 时平台仍用目录全量；**不要指望平台按包名猜**（提交脚本应调用下方工厂函数）。
4. `minimum_business_pages` **必须等于** `required_page_types` 长度（硬覆盖闸门）。

## 快速取 payload

```python
from services.api.app.domain.exploration.app_qc_package_scopes import (
    exploration_target_for_package,
    task_remark_for_package,
)

target = {
    "platform": "app",
    "package_name": "top.qkjzg.zfiufm",  # 黑料不打烊当前包；或 app.p6228f.pa3 / vip.dibwo.txbazn
    "exploration_target": exploration_target_for_package("top.qkjzg.zfiufm"),
    ...
}
remark = task_remark_for_package(
    "top.qkjzg.zfiufm",
    profile="qa-b",
    apk_name="hlapp_1.8.9.apk",
)
```

---

## 黑料不打烊（hlapp / `top.qkjzg.zfiufm`）

代码：`hlapp_coverage_scope.py`。依据底栏：首页／精选／片库／直播／AI科技／圈子（脚本不点「我的」）。

| 页型 | 本包 | 本轮是否进分母 | 说明 |
|------|------|----------------|------|
| home | 有 | 是 | 首页 |
| content_list | 有 | 是 | 精选等列表 |
| content_detail | 有 | 是 | 内容详情 |
| video_list | 有 | 是 | 片库 |
| video_detail | 有 | 是 | 影片详情 |
| post_list | 有 | 是 | 圈子 |
| activity | 有 | 是 | 直播／活动／AI 入口类 |
| profile | 有（我的） | **否** | 脚本禁止点「我的」；未登录常为门禁 |
| settings | 深链／我的内 | **否** | 本轮不测 |
| payment | 充值类 | **否** | 高风险，本轮不测 |
| post_publish | 发布 | **否** | 高风险，本轮不测 |

```json
{
  "required_page_types": [
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "post_list",
    "activity"
  ],
  "minimum_business_pages": 7,
  "device_capability_template": "android_visual_readonly_v1"
}
```

---

## 海角乱伦（hjll / `app.p6228f.pa3`）

代码：`hjll_coverage_scope.py`。依据包内文案与目录别名：首页／视频／海角·社区；本轮不测「我的／设置」。

| 页型 | 本包 | 本轮是否进分母 | 说明 |
|------|------|----------------|------|
| home | 有 | 是 | 首页 |
| content_list | 有 | 是 | 列表／信息流 |
| content_detail | 有 | 是 | 内容详情 |
| video_list | 有 | 是 | 「视频」底栏 |
| video_detail | 有 | 是 | 影片详情 |
| post_list | 有 | 是 | 「海角／社区」 |
| profile | 有（我的） | **否** | 本轮无测号 |
| settings | 有 | **否** | 本轮不测 |
| payment | 可能 | **否** | 高风险 |
| post_publish | 可能 | **否** | 高风险 |
| activity | 未确认本轮必达 | **否** | 本轮不进分母 |

```json
{
  "required_page_types": [
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "post_list"
  ],
  "minimum_business_pages": 6,
  "device_capability_template": "android_visual_readonly_v1"
}
```

---

## 51品茶（51pc / `vip.dibwo.txbazn`）

代码：`pc51_coverage_scope.py`。依据包内文案：首页／茶馆／视频；本轮不测登录后个人中心与支付。

| 页型 | 本包 | 本轮是否进分母 | 说明 |
|------|------|----------------|------|
| home | 有 | 是 | 首页 |
| content_list | 有 | 是 | 「茶馆」列表 |
| content_detail | 有 | 是 | 茶馆／内容详情 |
| video_list | 有 | 是 | 「视频」 |
| video_detail | 有 | 是 | 影片详情 |
| profile | 可能 | **否** | 本轮无测号 |
| settings | 可能 | **否** | 本轮不测 |
| payment | 可能 | **否** | 高风险 |
| post_list / activity / post_publish | 未确认本轮必达 | **否** | 本轮不进分母 |

```json
{
  "required_page_types": [
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail"
  ],
  "minimum_business_pages": 5,
  "device_capability_template": "android_visual_readonly_v1"
}
```

---

## 硬覆盖验收口径

1. `required_page_types` 必须是上表「进分母」集合；`minimum_business_pages` 等于该集合长度。
2. 平台完成条件：`missing_page_types` 为空才算 `coverage_complete`；缺页只允许预算耗尽失败，不得提前当成功。
3. **若详情／社区被登录墙挡住**：代码无法凭空到 100%，必须给测号或再缩小分母。

## 业务侧仍须人工准备（代码替不了）

1. 测试账号或免短信／游客包（若要测登录后页）
2. 云手机健康、少抢机（降低截图 502）
3. 固定同一 APK 版本与任务口径
