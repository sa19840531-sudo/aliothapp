"""二维码链接校验 Tool：解码二维码并校验 URL 的真实可达性与浏览器可访问性。"""

from __future__ import annotations

import base64
import binascii
import importlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from services.api.app.agents.tools.registry import PermissionLevel, tool

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
_BLOCKED_CONTENT_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "forbidden",
    "verify you are human",
    "robot",
    "unusual traffic",
    "enable javascript",
    "访问受限",
    "请先登录",
    "安全检查",
    "验证",
)


@tool(
    name="qr_link_validation_tool",
    description=(
        "对二维码截图或 URL 进行解码、可达性与第三方浏览器可访问性校验，"
        "识别失效、误导和跳转异常。"
    ),
    category="校验",
    permission_level=PermissionLevel.SAFE,
)
async def qr_link_validation_tool(
    url: str | None = None,
    image_base64: str | None = None,
    image_path: str | None = None,
    timeout_seconds: float = 8.0,
    max_redirects: int = 5,
) -> dict[str, Any]:
    """校验二维码截图或 URL。"""
    return await validate_qr_link(
        url=url,
        image_base64=image_base64,
        image_path=image_path,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )


async def validate_qr_link(
    *,
    url: str | None = None,
    image_base64: str | None = None,
    image_path: str | None = None,
    timeout_seconds: float = 8.0,
    max_redirects: int = 5,
) -> dict[str, Any]:
    """执行二维码解码与 URL 校验的核心逻辑。"""
    _ensure_single_input(url=url, image_base64=image_base64, image_path=image_path)

    if url is not None:
        normalized_url = _normalize_url(url)
        if normalized_url is None:
            return _build_result(
                input_type="url",
                decoded_texts=[url],
                decoded_urls=[],
                url_checks=[_invalid_url_check(url, "URL 不是有效的 http/https 地址")],
                issues=["URL 不是有效的 http/https 地址"],
            )
        url_check = await _inspect_url(
            normalized_url,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        )
        return _build_result(
            input_type="url",
            decoded_texts=[],
            decoded_urls=[normalized_url],
            url_checks=[url_check],
            issues=url_check["issues"],
        )

    image_bytes = _load_image_bytes(image_base64=image_base64, image_path=image_path)
    decoded_texts = _decode_qr_texts(image_bytes=image_bytes)
    decoded_urls = [normalized for text in decoded_texts if (normalized := _normalize_url(text))]

    if not decoded_urls:
        issue = "未从二维码中识别出可访问的 http/https URL"
        return _build_result(
            input_type="image",
            decoded_texts=decoded_texts,
            decoded_urls=[],
            url_checks=[],
            issues=[issue],
        )

    url_checks = [
        await _inspect_url(
            decoded_url,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        )
        for decoded_url in decoded_urls
    ]
    issues = _merge_issues(url_checks)
    return _build_result(
        input_type="image",
        decoded_texts=decoded_texts,
        decoded_urls=decoded_urls,
        url_checks=url_checks,
        issues=issues,
    )


def _ensure_single_input(
    *,
    url: str | None,
    image_base64: str | None,
    image_path: str | None,
) -> None:
    provided = [item is not None for item in (url, image_base64, image_path)]
    if sum(provided) != 1:
        raise ValueError("必须且只能提供 url、image_base64 或 image_path 其中一种输入")


def _load_image_bytes(*, image_base64: str | None, image_path: str | None) -> bytes:
    if image_path is not None:
        return Path(image_path).read_bytes()
    assert image_base64 is not None
    content = image_base64.strip()
    if content.startswith("data:"):
        _, _, payload = content.partition(",")
        content = payload
    try:
        return base64.b64decode(content)
    except binascii.Error as exc:
        raise ValueError("image_base64 不是合法的 base64 编码") from exc


def _decode_qr_texts(*, image_bytes: bytes) -> list[str]:
    try:
        cv2 = importlib.import_module("cv2")
        numpy = importlib.import_module("numpy")
    except ModuleNotFoundError as exc:
        raise RuntimeError("二维码截图解码需要安装 opencv-python-headless 和 numpy") from exc

    image_array = numpy.frombuffer(image_bytes, dtype=numpy.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        return []

    detector = cv2.QRCodeDetector()
    decoded_texts: list[str] = []

    if hasattr(detector, "detectAndDecodeMulti"):
        try:
            success, texts, _, _ = detector.detectAndDecodeMulti(image)
        except Exception:
            success = False
            texts = []
        if success and texts:
            decoded_texts.extend(_normalize_decoded_text(text) for text in texts)

    if not decoded_texts:
        try:
            text, _, _ = detector.detectAndDecode(image)
        except Exception:
            text = ""
        if text:
            decoded_texts.append(_normalize_decoded_text(text))

    return [text for text in _unique_preserve_order(decoded_texts) if text]


async def _inspect_url(
    url: str,
    *,
    timeout_seconds: float,
    max_redirects: int,
) -> dict[str, Any]:
    try:
        async with _create_http_client(
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        ) as client:
            response = await client.get(url, headers=_BROWSER_HEADERS)
    except httpx.TooManyRedirects:
        return _build_url_check(
            input_url=url,
            reachable=False,
            browser_accessible=False,
            status_code=None,
            final_url=url,
            redirect_count=max_redirects + 1,
            redirect_chain=[],
            redirect_anomaly=True,
            issues=["重定向次数过多，疑似跳转异常或循环跳转"],
        )
    except httpx.RequestError as exc:
        return _build_url_check(
            input_url=url,
            reachable=False,
            browser_accessible=False,
            status_code=None,
            final_url=url,
            redirect_count=0,
            redirect_chain=[],
            redirect_anomaly=False,
            issues=[f"请求失败：{exc.__class__.__name__}"],
        )

    redirect_chain = [
        {
            "status_code": item.status_code,
            "url": str(item.request.url),
            "location": item.headers.get("location"),
        }
        for item in response.history
    ]
    final_url = str(response.url)
    content_text = _safe_response_text(response)
    blocked_issue = _blocked_content_issue(content_text)
    reachable = response.status_code < 400
    browser_accessible = reachable and blocked_issue is None
    redirect_anomaly = _looks_like_cross_domain_redirect(url, final_url) or (
        len(response.history) > 0 and not reachable
    )
    issues: list[str] = []
    if not reachable:
        issues.append(f"HTTP 状态码 {response.status_code}，链接不可达")
    if blocked_issue is not None:
        issues.append(blocked_issue)
    if redirect_anomaly:
        issues.append("发生跨域或异常跳转")

    return _build_url_check(
        input_url=url,
        reachable=reachable,
        browser_accessible=browser_accessible,
        status_code=response.status_code,
        final_url=final_url,
        redirect_count=len(response.history),
        redirect_chain=redirect_chain,
        redirect_anomaly=redirect_anomaly,
        issues=issues,
    )


def _create_http_client(*, timeout_seconds: float, max_redirects: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        max_redirects=max_redirects,
    )


def _normalize_url(candidate: str) -> str | None:
    text = candidate.strip()
    if not text:
        return None
    if text.startswith("www."):
        text = f"https://{text}"
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_decoded_text(text: str) -> str:
    return text.strip()


def _safe_response_text(response: httpx.Response) -> str:
    try:
        return response.text[:4096]
    except UnicodeDecodeError:
        return response.content[:4096].decode(errors="ignore")


def _blocked_content_issue(text: str) -> str | None:
    lower_text = text.lower()
    for marker in _BLOCKED_CONTENT_MARKERS:
        if marker in lower_text:
            return f"页面内容包含可疑拦截标记：{marker}"
    return None


def _looks_like_cross_domain_redirect(original_url: str, final_url: str) -> bool:
    original_host = _canonical_host(original_url)
    final_host = _canonical_host(final_url)
    return original_host is not None and final_host is not None and original_host != final_host


def _canonical_host(url: str) -> str | None:
    parsed = urlsplit(url)
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _invalid_url_check(url: str, issue: str) -> dict[str, Any]:
    return _build_url_check(
        input_url=url,
        reachable=False,
        browser_accessible=False,
        status_code=None,
        final_url=url,
        redirect_count=0,
        redirect_chain=[],
        redirect_anomaly=False,
        issues=[issue],
    )


def _build_url_check(
    *,
    input_url: str,
    reachable: bool,
    browser_accessible: bool,
    status_code: int | None,
    final_url: str,
    redirect_count: int,
    redirect_chain: list[dict[str, Any]],
    redirect_anomaly: bool,
    issues: list[str],
) -> dict[str, Any]:
    is_valid = reachable and browser_accessible and not redirect_anomaly and not issues
    return {
        "input_url": input_url,
        "final_url": final_url,
        "status_code": status_code,
        "reachable": reachable,
        "browser_accessible": browser_accessible,
        "redirect_count": redirect_count,
        "redirect_chain": redirect_chain,
        "redirect_anomaly": redirect_anomaly,
        "issues": issues,
        "is_valid": is_valid,
    }


def _merge_issues(url_checks: list[dict[str, Any]]) -> list[str]:
    return [issue for check in url_checks for issue in check["issues"]]


def _build_result(
    *,
    input_type: str,
    decoded_texts: list[str],
    decoded_urls: list[str],
    url_checks: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "input_type": input_type,
        "decoded_texts": decoded_texts,
        "decoded_urls": decoded_urls,
        "url_checks": url_checks,
        "issues": issues,
        "is_valid": bool(url_checks) and all(check["is_valid"] for check in url_checks),
    }


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result