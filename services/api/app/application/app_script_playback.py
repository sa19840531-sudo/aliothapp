"""把錄製腳本的主流程拆成一步一步，跟海角同一條路。

每一步由系統執行。做完後系統再截一張動作後的圖，Agent 看的是那張圖。
不把整份腳本一次跑完，也不拿腳本自己存的 PNG 代替這張動作後截圖。
座標仍是腳本裡的點，不按畫面重選。
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from json import dumps
from pathlib import Path

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)

_SCREEN_W = 1440
_SCREEN_H = 3040
_VISUAL_CALLS = {
    "shows_gift_popup",
    "shows_save_button",
    "shows_recharge_button",
    "shows_bottom_line",
    "membership_paywall_visible",
    "next_chapter_visible",
    "back_button_visible",
    "_is_browser_focus",
}
# 這些字就在腳本要判斷的畫面上。系統截圖裡看得到才做該分支，看不到就走另一支。
_VISUAL_MARKERS = {
    "shows_gift_popup": "新人礼包",
    "shows_save_button": "立即保存",
    "shows_recharge_button": "立即充值",
    "shows_bottom_line": "我是有底线的",
    "membership_paywall_visible": "开通会员",
    "next_chapter_visible": "下一章",
}


class _StopLoop(Exception):
    """迴圈裡的 break。只在拆步時使用。"""


class _Skip(Exception):
    """這個值算不出來，呼叫端改走別的分支。"""


@dataclass(frozen=True)
class PlaybackStep:
    tag: str
    action: InteractionOperation
    semantic: str
    purpose: str
    settle_ms: int
    x: float | None = None
    y: float | None = None
    swipe_from: tuple[float, float] | None = None
    swipe_to: tuple[float, float] | None = None
    duration_ms: int = 350
    gate_marker: str | None = None
    gate_present: bool = True


_STEP_CACHE: dict[tuple[str, int, int], tuple[PlaybackStep, ...]] = {}


def parse_script_playback_steps(script_path: Path) -> tuple[PlaybackStep, ...]:
    """讀腳本主流程。同一份未改過的檔案不重複拆。檔案不在時回空。"""

    path = Path(script_path)
    try:
        if not path.is_file():
            return ()
        stat = path.stat()
    except OSError:
        # 路径存在但无读权限（如本机 AirtestIDE 锁文件）时按缺档处理。
        return ()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _STEP_CACHE.get(key)
    if cached is not None:
        return cached
    steps = _parse_script_playback_steps(path)
    if len(_STEP_CACHE) > 16:
        _STEP_CACHE.clear()
    _STEP_CACHE[key] = steps
    return steps


def _parse_script_playback_steps(path: Path) -> tuple[PlaybackStep, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    main = _main_statements(tree)
    if main is None:
        return ()
    parser = _Parser(script_name=path.name, functions=functions)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    parser.env[target.id] = _eval(node.value, parser.env)
                except _Skip:
                    pass
    parser.walk(main)
    return tuple(parser.steps)


def next_script_playback(
    work: ExplorationWork,
    *,
    steps: Sequence[PlaybackStep],
    prefix: str,
    index_key: str,
    done_key: str,
    reason_code: str,
    expected_package: str,
    script_name: str,
) -> ActionIntent | ExplorationTerminal:
    """下一步就是腳本的下一步。走完才收口，失敗留在同一步。"""

    metadata = work.frontier.observation.metadata
    meta = metadata if isinstance(metadata, Mapping) else {}
    if not steps:
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED,
            reason=f"{script_name} 沒有可執行的步驟。",
            outcome="exploration_blocked",
            reason_code=reason_code,
            coverage_summary={done_key: False},
        )
    index = _script_index(meta, index_key)
    index = _next_executable_index(steps, index, meta)
    if meta.get(done_key) is True or index >= len(steps):
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.COMPLETED,
            reason=(
                f"{script_name} 的步驟已走完。"
                "每一步做完都由系統截一張動作後的圖，Agent 直接看那張圖。"
            ),
            outcome="exploration_completed",
            reason_code=reason_code,
            coverage_summary={
                done_key: True,
                "coverage_complete": True,
                "visual_quality_satisfied": True,
            },
        )
    return _intent_for_step(
        work=work,
        step=steps[index],
        index=index,
        prefix=prefix,
        expected_package=expected_package,
    )


def apply_script_playback_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
    steps: Sequence[PlaybackStep],
    prefix: str,
    index_key: str,
    done_key: str,
) -> None:
    """這一步成功才進下一步。跟海角一樣，失敗不跳步、也不整份重跑。"""

    index = _script_index(prior, index_key)
    result[index_key] = index
    result[done_key] = prior.get(done_key) is True or index >= len(steps)
    if completed_action is None or action_succeeded is not True:
        return
    candidate_id = _action_candidate_id(completed_action)
    marker = f"{prefix}-script:"
    if not candidate_id.startswith(marker):
        return
    parts = candidate_id.split(":")
    if len(parts) < 3:
        return
    try:
        finished = int(parts[1])
    except ValueError:
        return
    tag = parts[2]
    if finished < 0:
        return
    if finished < len(steps) and steps[finished].tag != tag:
        result[index_key] = finished
        return
    nxt = finished + 1
    result[index_key] = nxt
    if nxt >= len(steps) and steps:
        result[done_key] = True
        result["coverage_complete"] = True
        result["visual_quality_satisfied"] = True


class _Parser:
    def __init__(
        self,
        *,
        script_name: str,
        functions: dict[str, ast.FunctionDef],
    ) -> None:
        self.script_name = script_name
        self.functions = functions
        self.env: dict[str, object] = {}
        self.steps: list[PlaybackStep] = []
        self.pending_ms = 0
        self._seq = 0
        self._fold_capture = False
        self._gate: tuple[str, bool] | None = None

    def walk(self, statements: list[ast.stmt]) -> None:
        for stmt in statements:
            self._statement(stmt)

    def _statement(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Break):
            raise _StopLoop
        if isinstance(stmt, ast.Pass):
            return
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            self._call(stmt.value, inline=True)
            return
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(stmt.value, ast.Call):
                self._call(stmt.value, inline=False)
                visual = _visual_call_name(stmt.value)
                if visual in _VISUAL_MARKERS and isinstance(target, ast.Name):
                    self.env[target.id] = ("__visual__", visual)
                    return
            try:
                self._bind(target, _eval(stmt.value, self.env))
            except _Skip:
                if isinstance(target, ast.Name):
                    self.env[target.id] = None
            return
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            try:
                current = self.env.get(stmt.target.id, 0)
                delta = _eval(stmt.value, self.env)
                if isinstance(current, (int, float)) and isinstance(delta, (int, float)):
                    self.env[stmt.target.id] = current + delta
            except _Skip:
                return
            return
        if isinstance(stmt, ast.For):
            self._for(stmt)
            return
        if isinstance(stmt, ast.While):
            self._while(stmt)
            return
        if isinstance(stmt, ast.If):
            self._if(stmt)
            return
        if isinstance(stmt, ast.Try):
            self.walk(stmt.body)
            return

    def _for(self, stmt: ast.For) -> None:
        try:
            items = _eval(stmt.iter, self.env)
        except _Skip:
            return
        if not isinstance(items, (list, tuple)):
            return
        for item in items:
            self._bind(stmt.target, item)
            try:
                self.walk(stmt.body)
            except _StopLoop:
                break

    def _while(self, stmt: ast.While) -> None:
        for _ in range(40):
            try:
                cond = _truth(_eval(stmt.test, self.env))
            except _Skip:
                cond = False
            if not cond:
                return
            try:
                self.walk(stmt.body)
            except _StopLoop:
                return

    def _if(self, stmt: ast.If) -> None:
        if _body_is_raise(stmt.body):
            return
        visual = self._visual_gate(stmt.test)
        if visual is not None:
            if _is_break_only(stmt.body):
                raise _StopLoop
            marker, body_when_present = visual
            if marker:
                self._walk_gated(stmt, marker, body_when_present)
            return
        branch = stmt.body
        try:
            value = _eval(stmt.test, self.env)
        except _Skip:
            return
        if value is None:
            branch = stmt.body
        elif _truth(value):
            branch = stmt.body
        else:
            branch = stmt.orelse
        if branch:
            self.walk(branch)

    def _visual_gate(self, test: ast.AST) -> tuple[str, bool] | None:
        negated = False
        node = test
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            negated = True
            node = node.operand
        name = _visual_call_name(node) if isinstance(node, ast.Call) else None
        if name is None and isinstance(node, ast.Name):
            stored = self.env.get(node.id)
            if (
                isinstance(stored, tuple)
                and len(stored) == 2
                and stored[0] == "__visual__"
                and isinstance(stored[1], str)
            ):
                name = stored[1]
        if name is None:
            return None
        return _VISUAL_MARKERS.get(name, ""), not negated

    def _walk_gated(self, stmt: ast.If, marker: str, body_when_present: bool) -> None:
        saved = self._gate
        self._gate = (marker, body_when_present)
        self.walk(stmt.body)
        if stmt.orelse:
            self._gate = (marker, not body_when_present)
            self.walk(stmt.orelse)
        self._gate = saved

    def _bind(self, target: ast.expr, value: object) -> None:
        if isinstance(target, ast.Name):
            self.env[target.id] = value
            return
        if isinstance(target, ast.Tuple) and isinstance(value, (list, tuple)):
            for name, item in zip(target.elts, value, strict=False):
                self._bind(name, item)

    def _call(self, call: ast.Call, *, inline: bool) -> None:
        name = _call_name(call)
        if name in {"print", "connect_device", "start_app"}:
            return
        if name == "sleep":
            try:
                seconds = float(_eval(call.args[0], self.env))
            except (IndexError, _Skip, TypeError, ValueError):
                return
            self.pending_ms += int(seconds * 1000)
            return
        if name == "capture_screen":
            label = "shot"
            if call.args:
                try:
                    label = str(_eval(call.args[0], self.env))
                except _Skip:
                    label = "shot"
            if self._fold_capture and self.steps:
                previous = self.steps[-1]
                self.steps[-1] = replace(
                    previous,
                    settle_ms=previous.settle_ms + self.pending_ms,
                    purpose=f"{previous.purpose} 動作後留證 {label}。",
                )
                self.pending_ms = 0
                self._fold_capture = False
                return
            self._emit(
                action=InteractionOperation.OBSERVE,
                semantic=f"留證截圖 {label}",
                purpose=f"按 {self.script_name} 留證截圖 {label}；本步不選點。",
                tag=f"shot-{label}",
            )
            return
        if name in {"tap_point", "tap_button"}:
            point = _tap_point(call, self.env)
            if point is None:
                return
            px, py = point
            x, y = _norm(px, py)
            self._emit(
                action=InteractionOperation.CLICK,
                semantic=f"點 ({px},{py})",
                purpose=f"按 {self.script_name} 點 ({px},{py})；本步不選點。",
                tag=f"tap-{px}-{py}",
                x=x,
                y=y,
            )
            return
        if name == "adb_shell":
            self._adb(call)
            return
        function = self.functions.get(name or "")
        if (
            inline
            and function is not None
            and name not in _VISUAL_CALLS
            and _straight_line(function)
        ):
            self.walk(function.body)

    def _adb(self, call: ast.Call) -> None:
        if not call.args:
            return
        try:
            command = str(_eval(call.args[0], self.env))
        except _Skip:
            return
        # mama.py cloud_back_twice：同一條 shell「tap ; tap」，中間禁止截圖。
        # 拆成一步 BACK（執行層 cloud_back_tap_twice），不拆成兩步 CLICK。
        segments = [part.strip() for part in command.split(";") if part.strip()]
        tap_points: list[tuple[int, int]] = []
        only_taps = True
        for segment in segments:
            parts = segment.split()
            if len(parts) >= 4 and parts[0] == "input" and parts[1] == "tap":
                tap_points.append((int(float(parts[2])), int(float(parts[3]))))
            else:
                only_taps = False
                break
        if only_taps and len(tap_points) >= 2 and len(set(tap_points)) == 1:
            px, py = tap_points[0]
            self._emit(
                action=InteractionOperation.BACK,
                semantic="云手机返回连点两下",
                purpose=(
                    f"按 {self.script_name} cloud_back_twice ({px},{py})；"
                    "底部导航栏最右侧「<」连点两下；中间不截图；本步不選點。"
                ),
                tag=f"cloud-back-twice-{px}-{py}",
            )
            return
        if only_taps and len(tap_points) == 1:
            px, py = tap_points[0]
            x, y = _norm(px, py)
            self._emit(
                action=InteractionOperation.CLICK,
                semantic=f"點 ({px},{py})",
                purpose=f"按 {self.script_name} 點 ({px},{py})；本步不選點。",
                tag=f"tap-{px}-{py}",
                x=x,
                y=y,
            )
            return
        if only_taps and len(tap_points) >= 2:
            for px, py in tap_points:
                x, y = _norm(px, py)
                self._emit(
                    action=InteractionOperation.CLICK,
                    semantic=f"點 ({px},{py})",
                    purpose=f"按 {self.script_name} 點 ({px},{py})；本步不選點。",
                    tag=f"tap-{px}-{py}",
                    x=x,
                    y=y,
                )
            return
        parts = command.split()
        if len(parts) >= 6 and parts[0] == "input" and parts[1] == "swipe":
            x1, y1, x2, y2 = (float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5]))
            duration = int(float(parts[6])) if len(parts) >= 7 else 350
            self._emit(
                action=InteractionOperation.SCROLL,
                semantic="下滑",
                purpose=f"按 {self.script_name} 下滑；本步不選點。",
                tag="swipe",
                swipe_from=_norm(x1, y1),
                swipe_to=_norm(x2, y2),
                duration_ms=duration,
            )
            return
        if len(parts) >= 3 and parts[0] == "input" and parts[1] == "keyevent" and parts[2] == "4":
            self._emit(
                action=InteractionOperation.BACK,
                semantic="返回",
                purpose=f"按 {self.script_name} 返回；本步不選點。",
                tag="back",
            )

    def _emit(self, **kwargs: object) -> None:
        self._seq += 1
        tag = str(kwargs.pop("tag"))
        settle = self.pending_ms
        self.pending_ms = 0
        self.steps.append(
            PlaybackStep(
                tag=f"{self._seq:03d}-{tag}",
                settle_ms=settle,
                action=kwargs["action"],  # type: ignore[arg-type]
                semantic=str(kwargs["semantic"]),
                purpose=str(kwargs["purpose"]),
                x=kwargs.get("x"),  # type: ignore[arg-type]
                y=kwargs.get("y"),  # type: ignore[arg-type]
                swipe_from=kwargs.get("swipe_from"),  # type: ignore[arg-type]
                swipe_to=kwargs.get("swipe_to"),  # type: ignore[arg-type]
                duration_ms=int(kwargs.get("duration_ms") or 350),
                gate_marker=None if self._gate is None else self._gate[0],
                gate_present=True if self._gate is None else self._gate[1],
            )
        )
        action = self.steps[-1].action
        self._fold_capture = action is not InteractionOperation.OBSERVE


def _intent_for_step(
    *,
    work: ExplorationWork,
    step: PlaybackStep,
    index: int,
    prefix: str,
    expected_package: str,
) -> ActionIntent:
    capture_id = str(work.source_capture_id or "unknown")
    candidate_id = f"{prefix}-script:{index}:{step.tag}"
    digest = sha256(
        dumps(
            [work.work_id, capture_id, candidate_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate_id,
        f"{prefix}_script": True,
        "product_script_orchestrated": True,
        "product_expected_package": expected_package,
        "product_settle_after_action_ms": step.settle_ms,
        "require_screenshot_evidence": True,
    }
    if step.action is InteractionOperation.SCROLL and step.swipe_from and step.swipe_to:
        parameters["from"] = {
            "x": step.swipe_from[0],
            "y": step.swipe_from[1],
            "space": "normalized_01",
        }
        parameters["to"] = {
            "x": step.swipe_to[0],
            "y": step.swipe_to[1],
            "space": "normalized_01",
        }
        parameters["duration_ms"] = step.duration_ms
    elif step.x is not None and step.y is not None:
        parameters["normalized_bounds"] = _bounds(step.x, step.y)
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=step.action,
        semantic_target=step.semantic,
        parameters=parameters,
        purpose=step.purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            step.action,
        ),
        deadline=work.deadline,
    )


def _script_index(metadata: Mapping[str, object], key: str) -> int:
    raw = metadata.get(key)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _action_candidate_id(action: object) -> str:
    raw = getattr(action, "candidate_id", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    parameters = getattr(action, "parameters", None)
    if isinstance(parameters, Mapping):
        nested = parameters.get("candidate_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    metadata = getattr(action, "metadata", None)
    if isinstance(metadata, Mapping):
        nested = metadata.get("candidate_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _bounds(x: float, y: float) -> dict[str, float]:
    half = 0.04
    return {
        "left": round(max(0.0, x - half), 4),
        "top": round(max(0.0, y - half), 4),
        "right": round(min(1.0, x + half), 4),
        "bottom": round(min(1.0, y + half), 4),
    }


def _norm(x: float, y: float) -> tuple[float, float]:
    xf, yf = float(x), float(y)
    if abs(xf) > 1 or abs(yf) > 1:
        xf, yf = xf / _SCREEN_W, yf / _SCREEN_H
    return xf, yf


def _main_statements(tree: ast.AST) -> list[ast.stmt] | None:
    """主流程在 start 之后。有 try 就走 try；杏吧这种直写流程就走后面的语句。"""

    main = _main_try(tree)
    if main is not None:
        return list(main.body)
    if not isinstance(tree, ast.Module):
        return None
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Expr) and _is_start_print(node):
            return list(tree.body[index + 1 :])
    return None


def _main_try(tree: ast.AST) -> ast.Try | None:
    if not isinstance(tree, ast.Module):
        return None
    body = tree.body
    for index, node in enumerate(body):
        if not isinstance(node, ast.Try):
            continue
        prev = body[index - 1] if index else None
        if isinstance(prev, ast.Expr) and _is_start_print(prev):
            return node
        if any(_is_start_print(stmt) for stmt in node.body[:3]):
            return node
    return None


def _is_start_print(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    call = stmt.value
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return False
    text = call.args[0].value
    return isinstance(text, str) and "===== start =====" in text


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and func.attr == "sleep":
        return "sleep"
    return None


def _tap_point(call: ast.Call, env: dict[str, object]) -> tuple[int, int] | None:
    try:
        if call.args and isinstance(call.args[0], ast.Starred):
            value = _eval(call.args[0].value, env)
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                return int(value[0]), int(value[1])
        if len(call.args) >= 2:
            return int(_eval(call.args[0], env)), int(_eval(call.args[1], env))
    except (TypeError, ValueError, _Skip):
        return None
    return None


def _eval(node: ast.AST, env: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise _Skip
    if isinstance(node, ast.Tuple):
        return tuple(_eval(item, env) for item in node.elts)
    if isinstance(node, ast.List):
        return [_eval(item, env) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _eval(node.operand, env)
        if isinstance(value, (int, float)):
            return -value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        operand = _eval(node.operand, env)
        if operand is None:
            raise _Skip
        return not _truth(operand)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _eval(node.left, env)
        right = _eval(node.comparators[0], env)
        op = node.ops[0]
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, env)
        right = _eval(node.right, env)
        if isinstance(node.op, ast.Add) and isinstance(left, (int, float)) and isinstance(
            right, (int, float)
        ):
            return left + right
        if isinstance(node.op, ast.Mod) and isinstance(left, str):
            return left % right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
        args = [_eval(arg, env) for arg in node.args]
        return tuple(range(*[int(arg) for arg in args]))
    # 支持 "input tap {} {}".format(*ENTRY_TAP) / .format(x, y)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        template = _eval(node.func.value, env)
        if not isinstance(template, str):
            raise _Skip
        args: list[object] = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                starred = _eval(arg.value, env)
                if not isinstance(starred, (list, tuple)):
                    raise _Skip
                args.extend(starred)
            else:
                args.append(_eval(arg, env))
        kwargs = {
            kw.arg: _eval(kw.value, env)
            for kw in node.keywords
            if isinstance(kw.arg, str)
        }
        try:
            return template.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            raise _Skip from None
    raise _Skip


def _truth(value: object) -> bool:
    return bool(value)


def _visual_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _VISUAL_CALLS:
            return node.func.id
    return None


def _screen_text(metadata: Mapping[str, object]) -> str:
    parts = [
        str(metadata.get("visual_summary") or ""),
        str(metadata.get("ocr_text") or ""),
        str(metadata.get("page_type") or ""),
        str(metadata.get("page_state") or ""),
    ]
    elements = metadata.get("key_elements")
    if isinstance(elements, (list, tuple)):
        parts.extend(str(item) for item in elements)
    return " ".join(parts)


def _next_executable_index(
    steps: Sequence[PlaybackStep],
    index: int,
    metadata: Mapping[str, object],
) -> int:
    """畫面分支只在系統截圖看得到對應文字時才做。看不到就跳過，不猜著點。"""

    text = _screen_text(metadata)
    while 0 <= index < len(steps):
        step = steps[index]
        if not step.gate_marker:
            return index
        if text and (step.gate_marker in text) is bool(step.gate_present):
            return index
        index += 1
    return index


def _body_is_raise(body: list[ast.stmt]) -> bool:
    return any(isinstance(stmt, ast.Raise) for stmt in body) and all(
        isinstance(stmt, (ast.Raise, ast.Expr)) for stmt in body
    )


def _is_break_only(body: list[ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], ast.Break)


def _straight_line(function: ast.FunctionDef) -> bool:
    return all(
        not isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try, ast.With))
        for stmt in function.body
    )

