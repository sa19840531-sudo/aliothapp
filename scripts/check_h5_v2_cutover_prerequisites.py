#!/usr/bin/env python3
"""只读检查 H5 从 V1 切到 V2 的前置条件。

H5 旧入口的默认版本与 V2 API 的运行时路由由两个开关共同决定：

- ``ALIOTH_WORKFLOW_VERSION``：只决定未显式指定版本的旧入口默认工作流版本。
  ``shadow`` 依赖旧入口生成 ``workflow_version=v2`` 任务快照；``on`` 的 V2 API
  会显式锁定 ``v2``，运行时以持久化任务快照为权威。
- ``ALIOTH_H5_V2_MODE``：``off`` 走 V1；``shadow`` 由 V1 权威执行并旁路记录
  V2 只读投影；``on`` 由 V2 双 Agent 正式执行。

本脚本不连接数据库、Redis、对象存储与浏览器，只读配置与代码装配点。
``ready`` 只表示可以开始切换动作，**不代表切换已验证通过**。

用法::

    python -m scripts.check_h5_v2_cutover_prerequisites --target shadow
    python -m scripts.check_h5_v2_cutover_prerequisites --target on --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.api.app.core.config import get_settings  # noqa: E402

TARGETS = ("shadow", "on")


@dataclass(frozen=True, kw_only=True)
class Check:
    """单条前置检查结果。"""

    name: str
    passed: bool
    detail: str
    blocking: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
        }


def _workflow_routing_check(settings, *, target: str) -> Check:
    version = settings.workflow.version
    shadow_target = target == 'shadow'
    return Check(
        name='legacy_entry_default_locks_v2',
        passed=version == 'v2',
        detail=(
            f'ALIOTH_WORKFLOW_VERSION={version}；'
            + (
                'shadow 依赖旧入口生成 workflow_version=v2 任务快照'
                if shadow_target
                else '该值只控制旧入口默认快照；V2 API 会显式锁定 v2，运行时以任务快照路由'
            )
        ),
        blocking=shadow_target,
    )


def _mode_check(settings, *, target: str) -> Check:
    mode = settings.workflow.h5_v2_mode
    return Check(
        name=f"h5_v2_mode_is_{target}",
        passed=mode == target,
        detail=f"ALIOTH_H5_V2_MODE={mode}，目标 {target}",
        # 本项描述当前处于哪一档，不阻塞"能否切"，只提示当前位置
        blocking=False,
    )


def _shadow_recorder_check() -> Check:
    """shadow 档必须能构建只读投影记录器，否则影子期等于空转。"""
    try:
        from services.api.app.application.h5_workflow_shadow import (
            build_h5_workflow_shadow_recorder,
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="shadow_recorder_available",
            passed=False,
            detail=f"影子记录器不可导入：{exc}",
        )
    return Check(
        name="shadow_recorder_available",
        passed=callable(build_h5_workflow_shadow_recorder),
        detail="影子期由 V1 权威执行，V2 只读投影旁路记录且失败不污染 V1 事实",
    )


def _v2_handler_check() -> Check:
    """on 档必须能构建正式 V2 Handler。"""
    try:
        from services.api.app.application.h5_runtime_composition import (
            build_h5_workflow_handler,
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="v2_handler_available",
            passed=False,
            detail=f"V2 Handler 不可导入：{exc}",
        )
    return Check(
        name="v2_handler_available",
        passed=callable(build_h5_workflow_handler),
        detail="on 档由 RuntimeBackedH5WorkflowHandler 承担 H5 正式执行",
    )


def _evidence_chain_check(settings) -> Check:
    """V2 判定依赖清单链路；清单读取仍为 off 时只能读旧路径。"""
    mode = settings.evidence_manifest_read.mode
    return Check(
        name="evidence_manifest_read_enabled",
        passed=mode != "off",
        detail=(
            f"ALIOTH_EVIDENCE_MANIFEST_READ_MODE={mode}；"
            "为 off 时判定仍走旧读取，证据不经清单校验"
        ),
        # 不阻塞 H5 切换本身：两条灰度可独立推进
        blocking=False,
    )


def _exit_gate_check(settings) -> Check:
    mode = settings.web_evidence_exit_gate.mode
    return Check(
        name="web_evidence_exit_gate_enabled",
        passed=mode != "off",
        detail=(
            f"ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE={mode}；"
            "为 off 时证据不全不会拦截离场动作"
        ),
        blocking=False,
    )


def collect_checks(*, target: str) -> tuple[Check, ...]:
    settings = get_settings()
    checks = [
        _workflow_routing_check(settings, target=target),
        _mode_check(settings, target=target),
        _evidence_chain_check(settings),
        _exit_gate_check(settings),
    ]
    if target == "shadow":
        checks.append(_shadow_recorder_check())
    else:
        checks.append(_v2_handler_check())
        checks.append(
            Check(
                name="shadow_observed_before_on",
                passed=False,
                detail=(
                    "切 on 前必须先在 shadow 档跑过真实任务并核对 V2 投影结论；"
                    "本脚本无法代替该人工确认"
                ),
                blocking=False,
            )
        )
    return tuple(checks)


def build_report(*, target: str) -> dict[str, object]:
    checks = collect_checks(target=target)
    blocking_failures = [item for item in checks if item.blocking and not item.passed]
    return {
        "target": target,
        "ready": not blocking_failures,
        "blocking_failures": [item.name for item in blocking_failures],
        "checks": [item.to_dict() for item in checks],
        "note": "ready 只表示可以开始切换动作，不代表切换已验证通过",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=TARGETS,
        default="shadow",
        help="目标档位，默认 shadow",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出，便于流水线消费",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_report(target=args.target)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"目标档位：{report['target']}")
        for item in report["checks"]:
            mark = "OK  " if item["passed"] else ("FAIL" if item["blocking"] else "WARN")
            print(f"[{mark}] {item['name']}：{item['detail']}")
        print(f"\nready={report['ready']}  {report['note']}")
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
