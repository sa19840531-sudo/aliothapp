# aliothapp2.0：全部产品仅用通用 Agent

所有已知及未知包均采用限步、无状态辨识 Tool、约束输出。产品档案仅提供识别信息，不决定固定点击顺序。

- `execution_policy` 统一为 `agent_profile_only`。
- `ALIOTH_APP_EXECUTION_MODE=legacy_script|script|airtest` 不再启用脚本。
- Explorer 与 App Turn 不再调用产品脚本 override；后继截图不再推进脚本索引。
- 候选解析器与 Android 执行入口拒绝历史 `*-script:` 或产品脚本标记，错误码为 `LEGACY_PRODUCT_SCRIPT_DISABLED`。
- 旧模块与录制原件保留供历史证据查阅及单元对照，不再接入生产调度。原始实现可从基线提交恢复。

通用流程：清障 → 首页影片 → 上方分类抽样 → 全部实际底栏 → 条件漫画／钱包 → 支付宝 inspect_only → 返回首页。流程进度由主控制器保存，不使用产品固定点位脚本。
