"""按冻结局部指令生成可复核副本，原始字节和证据台账始终不变。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from pydantic import ValidationError

from services.api.app.domain.evidence.redaction import (
    POLICY_VERSION as POLICY_VERSION,
)
from services.api.app.domain.evidence.redaction import (
    RedactionError,
    RedactionSpec,
    pointer_tokens,
)

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000
MASK_TEXT = "[已脱敏]"
_IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_TEXT_FORMATS = {"text/plain", "text/html"}


@dataclass(frozen=True)
class RenderedRedaction:
    """派生字节及其独立摘要；可复核不代表规则结论或隐私自动识别已通过。"""

    content: bytes
    content_type: str
    checksum: str
    size_bytes: int
    strategy: str
    business_reviewable: bool = True


def render_redaction(content: bytes, content_type: str, spec: RedactionSpec) -> RenderedRedaction:
    """仅处理受支持且有界的材料；全部指令成功才返回完整派生件。"""
    if not isinstance(content, bytes) or len(content) > MAX_SOURCE_BYTES:
        raise RedactionError("REDACTION_SOURCE_INVALID", "脱敏原件类型或大小无效")
    try:
        if not isinstance(spec, RedactionSpec):
            raise ValueError("脱敏指令未冻结")
        # 即使内部调用使用 model_construct/model_copy，也不能跳过消费侧合同校验。
        spec = RedactionSpec.model_validate(spec.model_dump())
    except (ValueError, ValidationError, TypeError):
        raise RedactionError("REDACTION_SPEC_INVALID", "脱敏指令不满足注册合同") from None
    if not isinstance(content_type, str):
        raise RedactionError("REDACTION_FORMAT_UNSUPPORTED", "材料格式没有已注册的局部策略")
    media = content_type.split(";", 1)[0].strip().lower()
    unchanged = spec.method == "reviewed_unchanged"
    if media in _IMAGE_FORMATS:
        if not unchanged and spec.method != "image_regions":
            _method_mismatch()
        derived = _image(content, media, spec)
        result_media = "image/png"
    elif media in _TEXT_FORMATS:
        if not unchanged and spec.method != "text_ranges":
            _method_mismatch()
        derived = _text(content, spec)
        result_media = "text/plain; charset=utf-8"
    elif media == "application/json":
        if not unchanged and spec.method != "json_pointers":
            _method_mismatch()
        derived = _json(content, spec)
        result_media = "application/json"
    else:
        raise RedactionError("REDACTION_FORMAT_UNSUPPORTED", "材料格式没有已注册的局部策略")
    if len(derived) > MAX_SOURCE_BYTES:
        raise RedactionError("REDACTION_BYTE_LIMIT", "脱敏派生件超过交付大小上限")
    return RenderedRedaction(
        content=derived,
        content_type=result_media,
        checksum="sha256:" + sha256(derived).hexdigest(),
        size_bytes=len(derived),
        strategy=spec.method,
    )


def _method_mismatch():
    raise RedactionError("REDACTION_METHOD_MISMATCH", "局部策略与原件格式不匹配")


def _image(content: bytes, media: str, spec: RedactionSpec) -> bytes:
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format != _IMAGE_FORMATS[media] or getattr(source, "n_frames", 1) != 1:
                raise ValueError("图片实际格式或帧数不符")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片像素数超限")
            source.load()
            # 浏览器按 EXIF 方向展示原图，局部坐标必须绑定同一正常显示方向。
            oriented = ImageOps.exif_transpose(source)
            if spec.method == "image_regions" and oriented.size != (
                spec.image_width,
                spec.image_height,
            ):
                raise RedactionError("REDACTION_IMAGE_SIZE_MISMATCH", "原图尺寸与冻结坐标不符")
            # 先铺白底再贴透明图，避免隐藏 RGB 或透明通道绕过烧录遮盖。
            rgba = oriented.convert("RGBA")
            pixels = Image.new("RGB", oriented.size, "white")
            pixels.paste(rgba, mask=rgba.getchannel("A"))
            if spec.method == "image_regions":
                drawing = ImageDraw.Draw(pixels)
                for region in spec.regions:
                    drawing.rectangle(
                        (
                            region.x,
                            region.y,
                            region.x + region.width - 1,
                            region.y + region.height - 1,
                        ),
                        fill=(32, 32, 32),
                    )
            output = BytesIO()
            # 全新像素对象重编码，不复制 EXIF、文本块、ICC 或源压缩流。
            pixels.save(output, format="PNG")
            return output.getvalue()
    except RedactionError:
        raise
    except (
        ValueError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
    ):
        raise RedactionError("REDACTION_SOURCE_INVALID", "图片原件无法按注册策略处理") from None


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeError:
        raise RedactionError("REDACTION_SOURCE_INVALID", "文本原件必须是有效 UTF-8") from None


def _text(content: bytes, spec: RedactionSpec) -> bytes:
    source = _decode(content)
    if spec.method == "reviewed_unchanged":
        return source.encode("utf-8")
    ranges = sorted(spec.ranges, key=lambda item: (item.start, item.end))
    if any(item.end > len(source) for item in ranges):
        raise RedactionError("REDACTION_RANGE_INVALID", "脱敏范围超出原文代码点长度")
    if sum(item.end - item.start for item in ranges) == len(source):
        raise RedactionError("REDACTION_FULL_COVERAGE", "全文遮盖不能用于局部业务复核")
    result, offset = [], 0
    for span in ranges:
        result.extend((source[offset : span.start], MASK_TEXT))
        offset = span.end
    result.append(source[offset:])
    return "".join(result).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 原件具有重复字段")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("JSON 原件包含非有限常量")


class _JsonNumber(str):
    """保留原 JSON 数字字面量，避免无关业务金额在浮点重编码时丢失精度。"""


def _check_json_shape(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise ValueError("JSON 原件结构超限")
        children = (
            item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        )
        pending.extend((child, depth + 1) for child in children)


def _locate(value: Any, tokens: tuple[str, ...]):
    parent = value
    for token in tokens[:-1]:
        parent = parent[_member(parent, token)]
    return parent, _member(parent, tokens[-1])


def _member(parent: Any, token: str):
    if isinstance(parent, dict) and token in parent:
        return token
    if isinstance(parent, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
        index = int(token)
        if index < len(parent):
            return index
    raise RedactionError("REDACTION_POINTER_INVALID", "JSON 脱敏路径不存在或不是精确成员")


def _json(content: bytes, spec: RedactionSpec) -> bytes:
    try:
        value = json.loads(
            _decode(content),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_int=_JsonNumber,
            parse_float=_JsonNumber,
        )
        _check_json_shape(value)
        # 全部路径先验证再替换，避免先修改父对象后影响后续指令的身份。
        targets = [_locate(value, pointer_tokens(pointer)) for pointer in spec.pointers]
        for parent, member in targets:
            parent[member] = MASK_TEXT
        return _encode_json(value).encode("utf-8")
    except RedactionError:
        raise
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise RedactionError("REDACTION_SOURCE_INVALID", "JSON 原件无法按注册策略处理") from None


def _encode_json(value: Any, depth: int = 0) -> str:
    """规范缩进和字段顺序，但不把精确金额或大整数转换成宿主浮点数。"""
    if isinstance(value, _JsonNumber):
        return str(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        members = [
            json.dumps(key, ensure_ascii=False) + ": " + _encode_json(value[key], depth + 1)
            for key in sorted(value)
        ]
        brackets = ("{", "}")
    elif isinstance(value, list):
        if not value:
            return "[]"
        members = [_encode_json(item, depth + 1) for item in value]
        brackets = ("[", "]")
    else:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    indent = "  " * (depth + 1)
    return (
        brackets[0]
        + "\n"
        + indent
        + (",\n" + indent).join(members)
        + "\n"
        + "  " * depth
        + brackets[1]
    )
