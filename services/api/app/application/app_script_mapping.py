"""App 脚本映射：APK／包名 → 外部 Airtest 脚本或 Agent Workflow。

【退役说明】正式 App V2 生产组合根走有限自由探索（dynamic_frontier），
不再经本模块选 Airtest 脚本。``heuristic_script_choice``／``resolve_airtest_script``
仅保留给历史 playbook 解析与 demo；新代码禁止在生产执行链 import。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.api.app.core.config import get_settings
from services.api.app.infrastructure.llm_gateway.client_factory import (
    LLMConfigurationError,
    _build_raw_async_client,
)

APP_SCRIPT_ROOT_ENV = "ALIOTH_APP_SCRIPT_ROOT"
_DEFAULT_APP_SCRIPT_ROOT = Path("/opt/alioth/mobile-scripts/untitled.air")
UNTITLED_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled.py"
UNTITLED1_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled1.air" / "untitled1.py"
UNTITLED2_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled2.air" / "untitled2.py"
UNTITLED3_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled3.air" / "untitled3.py"
UNTITLED4_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled4.air" / "untitled4.py"
UNTITLED5_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled5.air" / "untitled5.py"
UNTITLED6_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled6.air" / "untitled6.py"
UNTITLED7_SCRIPT = _DEFAULT_APP_SCRIPT_ROOT / "untitled7.air" / "untitled7.py"

_SCRIPT_CATALOG: list[dict[str, str]] = [
    {
        "id": "untitled",
        "file": "untitled.py",
        "description": (
            "91吃瓜／91cg 类 App：广告位点后回 App，"
            "再扫底栏「社区／片库／福利／AI科技」并截图"
        ),
        "hints": "文件名常含 91cg；应用名常含 91吃瓜",
    },
    {
        "id": "untitled1",
        "file": "untitled1.py",
        "description": "黑料不打烊／hlapp 类 App：另一套桌面入口与底栏模板",
        "hints": "文件名常含 hlapp；应用名常含黑料不打烊",
    },
    {
        "id": "untitled2",
        "file": "untitled2.py",
        "description": "hlwapp／黑料网 类 App：untitled2 专属模板与流程",
        "hints": "文件名常以 hlwapp 开头；应用名常含黑料网",
    },
    {
        "id": "untitled3",
        "file": "untitled3.py",
        "description": "sns_91sp 类 App：untitled3 专属模板与流程",
        "hints": "APK 文件名常以 sns_91sp 开头",
    },
    {
        "id": "untitled4",
        "file": "untitled4.py",
        "description": "sns_911bl 类 App：untitled4 专属模板与流程",
        "hints": "APK 文件名常以 sns_911bl 开头",
    },
    {
        "id": "untitled5",
        "file": "untitled5.py",
        "description": "sns_51aw 类 App：untitled5 专属模板与流程",
        "hints": "APK 文件名常以 sns_51aw 开头",
    },
    {
        "id": "untitled6",
        "file": "untitled6.py",
        "description": "sns-hjsq 类 App：untitled6 专属模板与流程",
        "hints": "APK 文件名常以 sns-hjsq 开头",
    },
    {
        "id": "untitled7",
        "file": "untitled7.py",
        "description": "dx093j 类 App：untitled7 专属模板与流程",
        "hints": "APK 文件名常以 dx093j 开头",
    },
]


@dataclass(frozen=True)
class ScriptChoice:
    """脚本／执行路径选择结果。"""

    script_id: str
    path: Path
    reason: str
    source: str  # env | ai | heuristic
    # airtest=跑对应 Python；agent_workflow=未匹配前缀，走 Agent+Workflow
    execution_mode: str = "airtest"


AGENT_AD_CAPTURE_SCRIPT_ID = "agent_ad_capture"


def is_airtest_route(choice: ScriptChoice) -> bool:
    return (choice.execution_mode or "airtest") == "airtest"


def is_agent_workflow_route(choice: ScriptChoice) -> bool:
    return (choice.execution_mode or "") == "agent_workflow"


def _fallback_to_agent_workflow_if_missing(choice: ScriptChoice) -> ScriptChoice:
    """仓库不再内置 Airtest 脚本，缺少外部脚本时安全切回 Agent Workflow。"""
    if not is_airtest_route(choice) or choice.path.exists():
        return choice
    return ScriptChoice(
        script_id=AGENT_AD_CAPTURE_SCRIPT_ID,
        path=choice.path,
        reason=(
            f"{choice.reason}；脚本文件不存在：{choice.path}，"
            f"请配置 {APP_SCRIPT_ROOT_ENV} 或 ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT，"
            "本次改走 Agent+Workflow"
        ),
        source=choice.source,
        execution_mode="agent_workflow",
    )


def ai_script_select_enabled() -> bool:
    """是否启用 AI 选脚本（默认开）。"""
    raw = (os.getenv("ALIOTH_MOBILE_DEMO_AI_SCRIPT_SELECT") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _script_root() -> Path:
    configured = (os.getenv(APP_SCRIPT_ROOT_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _DEFAULT_APP_SCRIPT_ROOT


def _path_for_id(script_id: str) -> Path:
    normalized = script_id.strip().lower()
    root = _script_root()
    if normalized == "untitled":
        return root / "untitled.py"
    return root / f"{normalized}.air" / f"{normalized}.py"


def heuristic_script_choice(
    apk_filename: str | None = None,
    *,
    package_name: str | None = None,
    app_label: str | None = None,
) -> ScriptChoice:
    """规则：dx093j*→untitled7；…；91cg*→untitled；已知包名亦可命中。"""
    name = Path((apk_filename or "").strip()).name.lower()
    pkg = (package_name or "").strip().lower()
    label = (app_label or "").strip().lower()
    blob = " ".join(part for part in (name, pkg, label) if part)

    if name.startswith("dx093j"):
        return ScriptChoice(
            script_id="untitled7",
            path=_path_for_id("untitled7"),
            reason=f"规则命中 dx093j（apk={name or '-'}）",
            source="heuristic",
        )
    if name.startswith("sns-hjsq") or name.startswith("sns_hjsq"):
        return ScriptChoice(
            script_id="untitled6",
            path=_path_for_id("untitled6"),
            reason=f"规则命中 sns-hjsq（apk={name or '-'}）",
            source="heuristic",
        )
    if name.startswith("sns_51aw"):
        return ScriptChoice(
            script_id="untitled5",
            path=_path_for_id("untitled5"),
            reason=f"规则命中 sns_51aw（apk={name or '-'}）",
            source="heuristic",
        )
    if name.startswith("sns_911bl"):
        return ScriptChoice(
            script_id="untitled4",
            path=_path_for_id("untitled4"),
            reason=f"规则命中 sns_911bl（apk={name or '-'}）",
            source="heuristic",
        )
    if name.startswith("sns_91sp"):
        return ScriptChoice(
            script_id="untitled3",
            path=_path_for_id("untitled3"),
            reason=f"规则命中 sns_91sp（apk={name or '-'}）",
            source="heuristic",
        )
    if name.startswith("hlwapp") or "hlwapp" in blob or "黑料网" in (app_label or ""):
        return ScriptChoice(
            script_id="untitled2",
            path=_path_for_id("untitled2"),
            reason=f"规则命中 hlwapp／黑料网（apk={name or '-'}）",
            source="heuristic",
        )
    if (
        name.startswith("hlapp")
        or "hlapp" in blob
        or "黑料不打烊" in (app_label or "")
        or pkg in {"top.qkjzg.zfiufm", "com.edxuf.euloop", "uk.frtju.nnxsaf"}
        or "qkjzg" in blob
        or "zfiufm" in blob
        or "edxuf" in blob
        or "euloop" in blob
        or "hlbdy" in blob
        or "frtju" in blob
        or "nnxsaf" in blob
    ):
        return ScriptChoice(
            script_id="untitled1",
            path=_path_for_id("untitled1"),
            reason=f"规则命中 hlapp／黑料不打烊（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
        )
    if (
        name.startswith("91cg")
        or "91cg" in name
        or "91吃瓜" in (app_label or "")
        or "吃瓜" in label
    ):
        return ScriptChoice(
            script_id="untitled",
            path=_path_for_id("untitled"),
            reason=f"规则命中 91cg／吃瓜（apk={name or '-'}）",
            source="heuristic",
            execution_mode="airtest",
        )
    if pkg in {"me.mfanv.npymci"}:
        return ScriptChoice(
            script_id="untitled",
            path=_path_for_id("untitled"),
            reason=f"规则命中 91cg 已知包名（package={pkg}）",
            source="heuristic",
            execution_mode="airtest",
        )
    if (
        name.startswith("hjll")
        or pkg
        in {
            "app.p6228f.pa3",
            "app.p12b04.p51",
            "dark.wwcskrpojs.ebjhyebjbl",
        }
        or "p6228f" in blob
        or "p12b04" in blob
        or "wwcskrpojs" in blob
        or "海角乱伦" in (app_label or "")
        or ("海角" in (app_label or "") and "乱伦" in (app_label or ""))
    ):
        return ScriptChoice(
            script_id="hjll",
            path=_path_for_id("untitled"),
            reason=f"规则命中 hjll／海角（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
            execution_mode="agent_workflow",
        )
    if (
        name.startswith("51pc")
        or name.startswith("51品茶")
        or pkg == "vip.dibwo.txbazn"
        or "dibwo" in blob
        or "txbazn" in blob
        or "品茶" in (app_label or "")
        or "茶馆" in (app_label or "")
    ):
        return ScriptChoice(
            script_id="pc51",
            path=_path_for_id("untitled"),
            reason=f"规则命中 51pc／品茶（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
            execution_mode="agent_workflow",
        )
    if (
        name.startswith("91porn")
        or "91porn" in blob
        or pkg == "app.paf4c0.pede1e12de8c2f952a9aa"
        or "paf4c0" in blob
        or "91porn" in (app_label or "").casefold()
    ):
        return ScriptChoice(
            script_id="porn91",
            path=_path_for_id("untitled"),
            reason=f"规则命中 91PORN（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
            execution_mode="agent_workflow",
        )
    if (
        name.startswith("erbulld")
        or "erbulld" in blob
        or pkg == "app.p8b938.p1084bf711f98"
        or "p8b938" in blob
        or "萝莉岛" in (app_label or "")
    ):
        return ScriptChoice(
            script_id="erbull",
            path=_path_for_id("untitled"),
            reason=f"规则命中 萝莉岛2／erbulld（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
            execution_mode="agent_workflow",
        )
    if (
        pkg == "uerma.zqeluk.cwsxx"
        or "zqeluk" in blob
        or "cwsxx" in blob
        or "杏吧" in (app_label or "")
        or name.startswith("null2097")
    ):
        return ScriptChoice(
            script_id="xingba",
            path=_path_for_id("untitled"),
            reason=f"规则命中 杏吧／uerma.zqeluk.cwsxx（apk={name or '-'} package={pkg or '-'}）",
            source="heuristic",
            execution_mode="agent_workflow",
        )
    return ScriptChoice(
        script_id=AGENT_AD_CAPTURE_SCRIPT_ID,
        path=_path_for_id("untitled"),
        reason=f"规则未命中已知 APK 前缀（apk={name or '-'}），改走 Agent+Workflow",
        source="heuristic",
        execution_mode="agent_workflow",
    )


def _parse_script_id(text: str) -> tuple[str, str]:
    """从模型输出解析 script_id 与 reason。"""
    text = (text or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"模型未回传 JSON：{text[:200]}")
    data: dict[str, Any] = json.loads(m.group(0))
    script_id = str(data.get("script_id") or data.get("script") or "").strip().lower()
    aliases = {
        "untitled.py": "untitled",
        "untitled": "untitled",
        "untitled1.py": "untitled1",
        "untitled1": "untitled1",
        "untitled2.py": "untitled2",
        "untitled2": "untitled2",
        "untitled3.py": "untitled3",
        "untitled3": "untitled3",
        "untitled4.py": "untitled4",
        "untitled4": "untitled4",
        "untitled5.py": "untitled5",
        "untitled5": "untitled5",
        "untitled6.py": "untitled6",
        "untitled6": "untitled6",
        "untitled7.py": "untitled7",
        "untitled7": "untitled7",
    }
    script_id = aliases.get(script_id, script_id)
    if script_id not in {
        "untitled",
        "untitled1",
        "untitled2",
        "untitled3",
        "untitled4",
        "untitled5",
        "untitled6",
        "untitled7",
    }:
        raise ValueError(f"不支持的 script_id={script_id!r}")
    reason = str(data.get("reason") or "").strip() or "ai"
    return script_id, reason


async def _ai_choose_script(
    *,
    apk_filename: str | None,
    package_name: str | None,
    app_label: str | None,
) -> ScriptChoice:
    """调用 LLM 依 APK 信息选择脚本。"""
    settings = get_settings().llm
    client = _build_raw_async_client(
        settings,
        usage_source="app_script_mapping",
        usage_operation="airtest_script_routing",
    )
    catalog_text = json.dumps(_SCRIPT_CATALOG, ensure_ascii=False, indent=2)
    system = (
        "你是 Alioth APP 自动化脚本路由助手。"
        "根据 APK 信息从候选脚本中选一个最适合的。"
        "只输出一个 JSON，不要 markdown。"
        '格式: {"script_id":"untitled"|"untitled1"|...|"untitled7","reason":"简短中文理由"}。'
    )
    user = (
        f"APK 文件名：{apk_filename or '（未知）'}\n"
        f"packageName：{package_name or '（未知）'}\n"
        f"应用显示名：{app_label or '（未知）'}\n\n"
        f"候选脚本：\n{catalog_text}\n\n"
        "请选择 script_id。"
    )
    resp = await client.chat.completions.create(
        model=settings.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=120,
    )
    content = (resp.choices[0].message.content or "").strip()
    script_id, reason = _parse_script_id(content)
    return ScriptChoice(
        script_id=script_id,
        path=_path_for_id(script_id),
        reason=reason,
        source="ai",
    )


def resolve_airtest_script(
    apk_filename: str | None = None,
    *,
    package_name: str | None = None,
    app_label: str | None = None,
    prefer_ai: bool | None = None,
) -> ScriptChoice:
    """解析应使用的执行路径。"""
    configured = (os.getenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT") or "").strip()
    if configured:
        path = Path(configured)
        low_name = path.name.lower()
        script_id = "untitled"
        for candidate in (
            "untitled7",
            "untitled6",
            "untitled5",
            "untitled4",
            "untitled3",
            "untitled2",
            "untitled1",
        ):
            if low_name.startswith(candidate):
                script_id = candidate
                break
        return _fallback_to_agent_workflow_if_missing(
            ScriptChoice(
                script_id=script_id,
                path=path,
                reason="ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT 覆写",
                source="env",
                execution_mode="airtest",
            )
        )

    heuristic = heuristic_script_choice(
        apk_filename, package_name=package_name, app_label=app_label
    )
    if is_agent_workflow_route(heuristic):
        return heuristic

    use_ai = ai_script_select_enabled() if prefer_ai is None else prefer_ai
    if use_ai:
        try:
            choice = asyncio.run(
                _ai_choose_script(
                    apk_filename=apk_filename,
                    package_name=package_name,
                    app_label=app_label,
                )
            )
            if choice.path.exists():
                return ScriptChoice(
                    script_id=choice.script_id,
                    path=choice.path,
                    reason=f"规则已命中，AI 覆写为 {choice.script_id}：{choice.reason}",
                    source="ai",
                    execution_mode="airtest",
                )
            return _fallback_to_agent_workflow_if_missing(
                ScriptChoice(
                    script_id=heuristic.script_id,
                    path=heuristic.path,
                    reason=f"AI 选 {choice.script_id} 但文件不存在，沿用规则：{heuristic.reason}",
                    source="heuristic",
                    execution_mode="airtest",
                )
            )
        except (
            LLMConfigurationError,
            ValueError,
            RuntimeError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            return _fallback_to_agent_workflow_if_missing(
                ScriptChoice(
                    script_id=heuristic.script_id,
                    path=heuristic.path,
                    reason=f"AI 失败（{exc}），沿用规则：{heuristic.reason}",
                    source="heuristic",
                    execution_mode="airtest",
                )
            )
        except Exception as exc:  # noqa: BLE001 — 选脚本不得中断主流程
            return _fallback_to_agent_workflow_if_missing(
                ScriptChoice(
                    script_id=heuristic.script_id,
                    path=heuristic.path,
                    reason=(
                        f"AI 例外（{type(exc).__name__}: {exc}），沿用规则：{heuristic.reason}"
                    ),
                    source="heuristic",
                    execution_mode="airtest",
                )
            )

    return _fallback_to_agent_workflow_if_missing(heuristic)
