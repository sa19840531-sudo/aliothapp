"""平台证据局部脱敏的不可变指令，不承载存储路径或访问授权。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_VERSION = "evidence-local-mask.v1"
MAX_REDACTION_ITEMS = 128
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]


class RedactionError(ValueError):
    """对外只暴露固定原因，不携带原文、对象路径或底层异常。"""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(f"{message}（{code}）")


class ImageRegion(BaseModel):
    """正常显示方向下原图像素坐标，右边和下边均为开区间。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: NonnegativeInt
    y: NonnegativeInt
    width: PositiveInt
    height: PositiveInt


class TextRange(BaseModel):
    """UTF-8 解码后的 Unicode 代码点范围，不使用字节或 UTF-16 偏移。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: NonnegativeInt
    end: PositiveInt

    @model_validator(mode="after")
    def require_nonempty(self):
        if self.end <= self.start:
            raise ValueError("脱敏文本范围必须满足 start < end")
        return self


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    """仅接受 RFC 6901 精确路径；根、通配符和无效转义不属于局部选择。"""
    if (
        not isinstance(pointer, str)
        or not pointer.startswith("/")
        or len(pointer) > 512
        or "*" in pointer
        or re.search(r"~(?![01])", pointer)
    ):
        raise RedactionError("REDACTION_POINTER_INVALID", "JSON 脱敏路径无效")
    tokens = tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))
    if len(tokens) > 32:
        raise RedactionError("REDACTION_POINTER_INVALID", "JSON 脱敏路径层级超限")
    return tokens


class RedactionSpec(BaseModel):
    """受控注册策略的参数；无操作必须显式选择 reviewed_unchanged。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["image_regions", "text_ranges", "json_pointers", "reviewed_unchanged"]
    image_width: PositiveInt | None = None
    image_height: PositiveInt | None = None
    regions: tuple[ImageRegion, ...] = Field(default=(), max_length=MAX_REDACTION_ITEMS)
    ranges: tuple[TextRange, ...] = Field(default=(), max_length=MAX_REDACTION_ITEMS)
    pointers: tuple[str, ...] = Field(default=(), max_length=MAX_REDACTION_ITEMS)

    @model_validator(mode="after")
    def require_matching_fields(self):
        image_fields = self.image_width is not None or self.image_height is not None or self.regions
        if self.method != "image_regions" and image_fields:
            raise ValueError("当前脱敏方法不能携带图片范围")
        if self.method != "text_ranges" and self.ranges:
            raise ValueError("当前脱敏方法不能携带文本范围")
        if self.method != "json_pointers" and self.pointers:
            raise ValueError("当前脱敏方法不能携带 JSON 路径")
        if self.method == "image_regions":
            if self.image_width is None or self.image_height is None or not self.regions:
                raise ValueError("图片局部脱敏必须绑定原图尺寸和非空范围")
            self._validate_regions()
        elif self.method == "text_ranges":
            if not self.ranges:
                raise ValueError("文本局部脱敏范围不能为空")
            ordered = sorted(self.ranges, key=lambda item: (item.start, item.end))
            if any(
                left.end > right.start for left, right in zip(ordered, ordered[1:], strict=False)
            ):
                raise ValueError("文本局部脱敏范围不能重叠或重复")
        elif self.method == "json_pointers":
            if not self.pointers:
                raise ValueError("JSON 局部脱敏路径不能为空")
            tokens = [pointer_tokens(pointer) for pointer in self.pointers]
            for index, left in enumerate(tokens):
                for right in tokens[index + 1 :]:
                    common = min(len(left), len(right))
                    if left[:common] == right[:common]:
                        raise ValueError("JSON 脱敏路径不能重复或包含父子冲突")
        return self

    def _validate_regions(self):
        for index, left in enumerate(self.regions):
            if left.x + left.width > self.image_width or left.y + left.height > self.image_height:
                raise ValueError("图片脱敏范围超出绑定尺寸")
            for right in self.regions[index + 1 :]:
                if (
                    left.x < right.x + right.width
                    and right.x < left.x + left.width
                    and left.y < right.y + right.height
                    and right.y < left.y + left.height
                ):
                    raise ValueError("图片脱敏范围不能重叠或重复")
        if sum(item.width * item.height for item in self.regions) == (
            self.image_width * self.image_height
        ):
            raise ValueError("全幅遮盖不能作为可供业务复核的局部脱敏")
