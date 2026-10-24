"""通过正式管理 API 幂等安装独立绿色第4版，默认预览且不启用任何规则。"""

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.api.app.application.green_rule_catalog import (  # noqa: E402
    GREEN_RULESET_CODE,
    GREEN_RULESET_VERSION,
    build_green_ruleset_manifest,
)


class GreenRulesetInstallationError(ValueError):
    """远端版本或规则被编辑，安装器拒绝覆盖、迁移或猜测恢复。"""


async def install_green_ruleset(
    client=None, *, version=GREEN_RULESET_VERSION, apply=False, publish=False
) -> dict:
    """先完整核对已有行，再只创建缺失行；冲突后重读，不调用更新或删除接口。"""
    manifest = build_green_ruleset_manifest(version=version)
    if not apply:
        if publish:
            raise GreenRulesetInstallationError("发布必须显式同时选择执行安装")
        return {"action": "dry_run", "manifest": manifest}
    if client is None:
        raise GreenRulesetInstallationError("执行安装需要已授权的正式管理 API 客户端")
    ruleset = await _find_ruleset(client, version)
    created_ruleset = False
    if ruleset is None:
        response = await client.post("/rulesets", json=manifest["ruleset"])
        if response.status_code == 409:
            ruleset = await _find_ruleset(client, version)
            if ruleset is None:
                raise GreenRulesetInstallationError("同版本创建冲突后未能读回精确规则集")
        else:
            ruleset = _response_object(response, expected=(201,))
            created_ruleset = True
    _check_ruleset(ruleset, manifest["ruleset"])
    ruleset_id = ruleset["id"]
    expected_rules = {rule["rule_code"]: rule for rule in manifest["rules"]}
    existing = await _rules(client, ruleset_id)
    _check_rules(existing, expected_rules, ruleset_id)
    created_codes = []
    for code, payload in expected_rules.items():
        if code in existing:
            continue
        response = await client.post("/rules", json={"ruleset_id": ruleset_id, **payload})
        if response.status_code == 409:
            current = await _rules(client, ruleset_id)
            _check_rules(current, expected_rules, ruleset_id)
            if code not in current:
                raise GreenRulesetInstallationError("规则创建冲突后未能读回同一冻结规则")
        else:
            actual = _response_object(response, expected=(201,))
            _check_rules({code: actual}, expected_rules, ruleset_id)
            created_codes.append(code)
    # 再次整组回读；缺项、人工启用、手工编辑或多出规则均阻断发布。
    current = await _rules(client, ruleset_id)
    _check_rules(current, expected_rules, ruleset_id)
    if set(current) != set(expected_rules):
        raise GreenRulesetInstallationError("绿色第4版规则没有完整读回，禁止发布")
    ruleset = _response_object(await client.get(f"/rulesets/{ruleset_id}"))
    _check_ruleset(ruleset, manifest["ruleset"])
    if publish and ruleset["status"] != "enabled":
        ruleset = _response_object(await client.post(f"/rulesets/{ruleset_id}/publish"))
        _check_ruleset(ruleset, manifest["ruleset"])
        if ruleset["status"] != "enabled" or not ruleset.get("snapshot_hash"):
            raise GreenRulesetInstallationError("规则集发布未返回正式快照，不继续后续动作")
    return {
        "action": "installed",
        "ruleset_id": ruleset_id,
        "version": version,
        "status": ruleset["status"],
        "snapshot_hash": ruleset.get("snapshot_hash"),
        "created_ruleset": created_ruleset,
        "created_rule_codes": created_codes,
        "rule_count": len(current),
        "rules_enabled": False,
    }


async def _find_ruleset(client, version):
    rows = await _pages(client, "/rulesets", {"keyword": GREEN_RULESET_CODE})
    matched = [
        row
        for row in rows
        if row.get("ruleset_code") == GREEN_RULESET_CODE and row.get("version") == version
    ]
    if len(matched) > 1:
        raise GreenRulesetInstallationError("同编码同版本有多条规则集，拒绝猜测目标")
    return matched[0] if matched else None


async def _rules(client, ruleset_id):
    rows = await _pages(client, "/rules", {"ruleset_id": ruleset_id})
    indexed = {}
    for row in rows:
        code = row.get("rule_code")
        if not isinstance(code, str) or code in indexed:
            raise GreenRulesetInstallationError("规则列表有重复或缺失身份")
        indexed[code] = row
    return indexed


async def _pages(client, path, params):
    rows, page = [], 1
    while True:
        response = _response_object(
            await client.get(path, params={**params, "page": page, "page_size": 100})
        )
        items, total = response.get("items"), response.get("total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            raise GreenRulesetInstallationError("管理接口分页返回格式不完整")
        rows.extend(items)
        if len(rows) >= total:
            return rows
        if not items:
            raise GreenRulesetInstallationError("管理接口分页尚未读全，拒绝按缺失行新建")
        page += 1


def _check_ruleset(actual, expected):
    if not isinstance(actual.get("id"), str) or actual.get("status") not in ("draft", "enabled"):
        raise GreenRulesetInstallationError("同版本规则集状态不可接续，拒绝改变已有状态")
    if any(actual.get(key) != value for key, value in expected.items() if key != "status"):
        raise GreenRulesetInstallationError("同版本规则集元数据与第4版目录不一致，拒绝覆盖")


def _check_rules(actual, expected, ruleset_id):
    if set(actual) - set(expected):
        raise GreenRulesetInstallationError("同版本含目录之外的规则，拒绝覆盖或删除")
    for code, row in actual.items():
        if row.get("ruleset_id") != ruleset_id:
            raise GreenRulesetInstallationError("管理接口返回跨规则集规则，拒绝继续")
        for key, value in expected[code].items():
            stored = row.get(key)
            if key in ("description", "scope", "page_type", "url_pattern") and stored is None:
                stored = ""
            if stored != value:
                raise GreenRulesetInstallationError(
                    f"已有第4版规则 {code} 的字段 {key} 不一致，拒绝覆盖"
                )


def _response_object(response, *, expected=(200,)):
    if response.status_code not in expected:
        raise GreenRulesetInstallationError(
            f"管理 API 返回 HTTP {response.status_code}，安装已停止"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GreenRulesetInstallationError("管理 API 未返回完整 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise GreenRulesetInstallationError("管理 API 返回类型不正确")
    return payload


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="显式执行正式API安装；默认仅预览")
    parser.add_argument(
        "--publish", action="store_true", help="安装读回后发布规则集；八条规则仍关闭"
    )
    parser.add_argument("--version", type=int, default=GREEN_RULESET_VERSION)
    parser.add_argument("--base-url", help="正式管理接口根地址，例如 http://127.0.0.1:8000/api/v1")
    parser.add_argument("--output", type=Path, help="保存UTF-8预览或安装回执")
    return parser.parse_args()


async def _main(args):
    if not args.apply:
        return await install_green_ruleset(version=args.version, publish=args.publish)
    token = os.environ.get("ALIOTH_RULE_INSTALL_TOKEN")
    parsed = urlsplit(args.base_url or "")
    if (
        not token
        or parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GreenRulesetInstallationError("执行安装须提供管理接口地址及ALIOTH_RULE_INSTALL_TOKEN")
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        follow_redirects=False,
    ) as client:
        return await install_green_ruleset(
            client, version=args.version, apply=True, publish=args.publish
        )


def main():
    args = _arguments()
    result = asyncio.run(_main(args))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    summary = deepcopy(result)
    if "manifest" in summary:
        summary["manifest"] = {
            "ruleset": result["manifest"]["ruleset"],
            "rule_count": len(result["manifest"]["rules"]),
        }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
