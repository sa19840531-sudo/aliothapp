"""APK 二進位 AndroidManifest 取包名：UTF-8 與 UTF-16 字串池都必須解析成功。

僅掃 UTF-16LE 的舊啟發式對 UTF-8 字串池的 APK 會回傳 None（上传被判
APK_PACKAGE_UNRESOLVED），或捞到无关字符串当成包名。
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from services.api.app.infrastructure.device.ldplayer import (
    try_extract_apk_version_from_manifest,
    try_extract_package_name_from_apk_manifest,
)


def _string_pool(values: list[str], *, utf8: bool) -> bytes:
    offsets: list[int] = []
    data = bytearray()
    for value in values:
        offsets.append(len(data))
        if utf8:
            encoded = value.encode("utf-8")
            data += bytes([len(value), len(encoded)]) + encoded + b"\x00"
        else:
            encoded = value.encode("utf-16le")
            data += struct.pack("<H", len(value)) + encoded + b"\x00\x00"
    while len(data) % 4:
        data += b"\x00"
    header_size = 28
    strings_start = header_size + len(offsets) * 4
    body = struct.pack(
        "<IIIII", len(values), 0, (1 << 8) if utf8 else 0, strings_start, 0
    ) + b"".join(struct.pack("<I", offset) for offset in offsets) + bytes(data)
    return struct.pack("<HHI", 0x0001, header_size, 8 + len(body)) + body


def _string_attr(name_index: int, value_index: int) -> bytes:
    return struct.pack(
        "<IIIIi", 0xFFFFFFFF, name_index, value_index, 0x03000008, value_index
    )


def _int_attr(name_index: int, value: int) -> bytes:
    """整數型屬性（如 versionCode）：值只在 data 欄，字串池裡沒有。"""
    return struct.pack("<IIIIi", 0xFFFFFFFF, name_index, 0xFFFFFFFF, 0x10000008, value)


def _start_element(*, name_index: int, attributes: list[bytes]) -> bytes:
    header_size = 16
    header_tail = struct.pack("<II", 0, 0)  # lineNumber, comment
    body = (
        struct.pack("<I", 0xFFFFFFFF)  # ns
        + struct.pack("<I", name_index)
        + struct.pack("<HHH", 20, 20, len(attributes))
        + struct.pack("<HHH", 0, 0, 0)  # id/class/style index
        + b"".join(attributes)
    )
    chunk_size = 8 + len(header_tail) + len(body)
    return struct.pack("<HHI", 0x0102, header_size, chunk_size) + header_tail + body


def _axml(
    package: str,
    *,
    utf8: bool,
    version_name: str | None = None,
    version_code: int | None = None,
) -> bytes:
    values = ["manifest", "package", package]
    attributes = [_string_attr(1, 2)]
    if version_name is not None:
        values += ["versionName", version_name]
        attributes.append(_string_attr(len(values) - 2, len(values) - 1))
    if version_code is not None:
        values.append("versionCode")
        attributes.append(_int_attr(len(values) - 1, version_code))
    pool = _string_pool(values, utf8=utf8)
    element = _start_element(name_index=0, attributes=attributes)
    body = pool + element
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def _apk(tmp_path: Path, manifest: bytes, name: str) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"dex\n035\x00")
    return path


def test_extracts_package_from_utf16_string_pool(tmp_path: Path) -> None:
    apk = _apk(tmp_path, _axml("com.example.utf16", utf8=False), "utf16.apk")

    assert try_extract_package_name_from_apk_manifest(apk) == "com.example.utf16"


def test_extracts_package_from_utf8_string_pool(tmp_path: Path) -> None:
    """UTF-8 字串池：舊的 UTF-16 掃描完全掃不到，必須由結構解析取回。"""
    apk = _apk(tmp_path, _axml("dark.bcusxygvjc.ffpdgkshpw", utf8=True), "utf8.apk")

    assert try_extract_package_name_from_apk_manifest(apk) == "dark.bcusxygvjc.ffpdgkshpw"


def test_returns_none_for_non_axml_manifest(tmp_path: Path) -> None:
    apk = _apk(tmp_path, b"not-a-binary-xml", "plain.apk")

    assert try_extract_package_name_from_apk_manifest(apk) is None


def test_extracts_version_from_manifest_without_aapt(tmp_path: Path) -> None:
    """版本恆為空会让安装侧「无法比较版本」按同包名跳过安装，旧版本永远覆盖不掉。"""
    manifest = _axml(
        "com.example.versioned", utf8=True, version_name="4.2.1", version_code=421
    )
    apk = _apk(tmp_path, manifest, "versioned.apk")

    version = try_extract_apk_version_from_manifest(apk)

    assert version.version_name == "4.2.1"
    assert version.version_code == 421


def test_version_is_empty_for_non_axml_manifest(tmp_path: Path) -> None:
    apk = _apk(tmp_path, b"not-a-binary-xml", "plain-version.apk")

    version = try_extract_apk_version_from_manifest(apk)

    assert version.version_name is None
    assert version.version_code is None
