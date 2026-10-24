---
name: app-product-orchestration
description: （遗留）产品编排说明。App Explorer 路由已改挂 app-product-capability；本文件保留供历史对照，不得作为坐标执行授权。
---

# app-product-orchestration（遗留）

> **F12**：App 路由不再挂载整个 `phase/`。请改读 [app-product-capability](../../app-product-capability/SKILL.md)。  
> **F04**：`load_app_product_profile_tool` 与观察字段 `app_product_capability_facts` 只返回能力事实，不含可执行 taps／绝对坐标。

## 迁移要点

1. 产品差异以只读语义时序／关窗策略表达，不得授予坐标。
2. 设备副作用必须经任务绑定、租约与 Coordinator；禁止自由回合直接 ADB。
3. 无 Profile 时允许通用候选探索，新产品不必填点位档案。
