# agents/skills 指导型 Skill（Agent Skills 规范）

自然语言的操作规程/判断标准，**本身不执行任何动作**，只影响 Agent 如何决策（Agent "读取"而非"调用"）。严格遵循 [Agent Skills 规范](https://agentskills.io)：每个子目录一份 `SKILL.md`（YAML frontmatter 至少含 `name`/`description`），Agent 按"发现摘要 → 触发加载全文 → 按需执行内嵌脚本/加载附属资源"的渐进式披露读取，不产生副作用。

## 子目录

- [phase/](phase/README.md)：阶段 Skill，指导"流程怎么走"。
- [rule/](rule/README.md)：规则 Skill，指导"每类规则怎么判"。
- [qc-workflow/](qc-workflow/SKILL.md)：总控 Skill，唯一一个串联 phase/ 与 rule/ 全部 Skill、定义任务级执行循环的入口，**Main Agent 必须最先读取**（详见 [../instructions.py](../instructions.py) 的 `MAIN_AGENT_INSTRUCTIONS`）。
- [navigation-boundary-skill/](navigation-boundary-skill/SKILL.md)：导航边界 Skill，定义探索阶段默认门禁与用户主动输入覆盖门禁的执行规则。

## 与 Tool 的区别

| | Skill（本目录） | Tool（[../tools/](../tools/README.md)） |
| --- | --- | --- |
| 依据规范 | [Agent Skills](https://agentskills.io)（`SKILL.md`、渐进式披露） | Agent Framework function calling（`FunctionTool`） |
| 本质 | 自然语言文档 | 可执行函数 |
| Agent 的用法 | 读取/检索，影响决策，不执行动作 | 调用（function calling），产生副作用或返回数据 |
| 存放位置 | 本目录 | [../tools/](../tools/README.md) |

详见[最终整合方案设计 4.2 节](../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)（历史文档中称为"指导型 Playbook"，2026-07-05 起统一为 **Skill**，术语沿革见 [docs/专项能力与高频回归规则.md](../../../../../docs/专项能力与高频回归规则.md)）。

## 落地形式（实现阶段）

每个 Skill 落地为一个独立目录 + `SKILL.md`（可选配 `scripts/`、`references/`、`assets/` 等附属资源），便于版本管理与人工审阅、修订。

## V1 实现（2026-07，2026-07-05 术语更正）

`loader.py` 提供 `build_skills_context_provider()`：基于 Agent Framework 1.10.0 的 `SkillsProvider`（Agent Skills 规范：`agentskills.io`，L1 摘要常驻系统提示词、L2 通过 `load_skill` 工具按需加载完整正文、L3 通过 `read_skill_resource` 加载附属资源）扫描 `phase/`、`rule/`、`qc-workflow/`、`navigation-boundary-skill/` 子目录，每个子目录下以独立文件夹存放 `SKILL.md`。返回值作为 `context_providers` 传给 `agent_factory.build_main_agent`。

- [phase/task-planning/SKILL.md](phase/task-planning/SKILL.md) 已补全为 URL 质检任务规划 Skill。
- [phase/page-exploration/SKILL.md](phase/page-exploration/SKILL.md)、[phase/page-detection/SKILL.md](phase/page-detection/SKILL.md)、[phase/evidence-report/SKILL.md](phase/evidence-report/SKILL.md) 已补全为页面探索、页面识别、证据报告阶段 Skill。
- [rule/rule-matching/SKILL.md](rule/rule-matching/SKILL.md) 已补全为候选规则匹配 Skill。
- [rule/text-rule/SKILL.md](rule/text-rule/SKILL.md) 已补全为文案类规则判断 Skill。
- [rule/ad-rule/SKILL.md](rule/ad-rule/SKILL.md) 已补全为广告类场景与 Tool 编排 Skill，覆盖弹窗、启动、遮罩、贴片等广告能力场景。
- [rule/player-rule/SKILL.md](rule/player-rule/SKILL.md) 已补全为播放器类规则判断 Skill。
- [qc-workflow/SKILL.md](qc-workflow/SKILL.md) 已补全真实内容，是 Main Agent 系统指令强制最先读取的总控 Skill。
- [navigation-boundary-skill/SKILL.md](navigation-boundary-skill/SKILL.md) 已新增导航边界 Skill，支持用户主动输入覆盖默认门禁标准，并保留覆盖来源与拒绝原因用于审计。
