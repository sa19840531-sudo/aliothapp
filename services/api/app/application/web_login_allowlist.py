"""Web 登录站点白名单与秘密资料的严格解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class WebLoginAllowlistError(ValueError):
    """配置无效；错误内容不得携带原始凭据。"""


@dataclass(frozen=True)
class WebLoginProfile:
    origin: str
    identifier_kind: str
    _identifier: str = field(repr=False)
    _password: str = field(repr=False)

    def reveal_for_runtime(self) -> tuple[str, str]:
        """只供浏览器秘密输入阶段调用。"""
        return self._identifier, self._password


@dataclass(frozen=True)
class WebLoginAllowlist:
    _profiles: dict[str, WebLoginProfile] = field(repr=False)

    def resolve(self, url: str) -> WebLoginProfile | None:
        try:
            origin = _origin(url, allow_http=False, allow_path=True)
        except WebLoginAllowlistError:
            return None
        return self._profiles.get(origin)

    def redirect_candidate(self, url: str) -> WebLoginProfile | None:
        """HTTP 入口仅提供待验证候选；秘密释放仍由真实 HTTPS 页面决定。"""
        if urlsplit(url).scheme != "http":
            return None
        try:
            origin = _origin(url, allow_http=True, allow_path=True)
        except WebLoginAllowlistError:
            return None
        return self._profiles.get(origin)

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(self._profiles)


_LABELS = ("网址", "账号", "密码")


def _origin(url: str, *, allow_http: bool, allow_path: bool) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise WebLoginAllowlistError("WEB_LOGIN_SITE_INVALID") from None
    if (
        parsed.scheme not in ({"http", "https"} if allow_http else {"https"})
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and (parsed.query or parsed.fragment))
        or (not allow_path and parsed.path not in {"", "/"})
        or "*" in hostname
        or (parsed.scheme == "http" and port not in {None, 80})
    ):
        raise WebLoginAllowlistError("WEB_LOGIN_SITE_INVALID")
    try:
        normalized = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError:
        raise WebLoginAllowlistError("WEB_LOGIN_SITE_INVALID") from None
    if not normalized or any(not label for label in normalized.split(".")):
        raise WebLoginAllowlistError("WEB_LOGIN_SITE_INVALID")
    suffix = f":{port}" if port not in {None, 80, 443} else ""
    return f"https://{normalized}{suffix}"


def parse_web_login_allowlist(text: str) -> WebLoginAllowlist:
    """解析每组三行的附件格式；HTTP 记录仅能在 HTTPS 页面命中。"""
    if not isinstance(text, str) or not text.strip():
        raise WebLoginAllowlistError("WEB_LOGIN_CONFIG_EMPTY")
    profiles: dict[str, WebLoginProfile] = {}
    for block in re.split(r"(?:\r?\n)\s*(?:\r?\n)", text.lstrip("\ufeff").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) != 3:
            raise WebLoginAllowlistError("WEB_LOGIN_CONFIG_BLOCK_INVALID")
        values = []
        for label, line in zip(_LABELS, lines, strict=True):
            match = re.fullmatch(rf"{label}\s*[：:]\s*(.+)", line)
            if match is None or not match.group(1).strip():
                raise WebLoginAllowlistError("WEB_LOGIN_CONFIG_BLOCK_INVALID")
            values.append(match.group(1).strip())
        site, identifier, password = values
        origin = _origin(site, allow_http=True, allow_path=False)
        if origin in profiles:
            raise WebLoginAllowlistError("WEB_LOGIN_SITE_DUPLICATE")
        identifier_kind = (
            "phone" if re.fullmatch(r"1[3-9]\d{9}", identifier)
            else "email" if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", identifier)
            else "username"
        )
        profiles[origin] = WebLoginProfile(
            origin=origin,
            identifier_kind=identifier_kind,
            _identifier=identifier,
            _password=password,
        )
    return WebLoginAllowlist(profiles)


def load_web_login_allowlist(path: str | Path) -> WebLoginAllowlist:
    """读取仓库外受控文件，失败时不回显文件正文。"""
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise WebLoginAllowlistError("WEB_LOGIN_CONFIG_READ_FAILED") from None
    return parse_web_login_allowlist(content)
