"""导出多证据、容量护栏和正式冻结材料的真实读取边界。"""

import hashlib

from services.api.app.application.report_export_evidence import (
    collect_export_evidence,
    export_evidence_note,
    referenced_artifact_ids,
)


class Store:
    def __init__(self, entries):
        self.entries = entries
        self.reads = []

    async def read(self, path):
        self.reads.append(path)
        content = self.entries[path]
        if isinstance(content, Exception):
            raise content
        return content


async def test_multi_image_collection_preserves_order_and_all_omission_reasons():
    ids = referenced_artifact_ids([
        {"evidence_artifact_ids": [
            "a", "a", "missing", "json", "unreadable", "empty", "large", "b", "c",
        ]},
        {"evidence_artifact_ids": ["b"]},
    ])
    metadata = {key: {"storage_path": key, "content_type": "image/png"}
                for key in ids if key != "missing"}
    metadata["json"]["content_type"] = "application/json"
    store = Store({"a": b"a", "b": b"b", "c": b"c", "empty": b"",
                   "large": b"x" * 400_001, "unreadable": OSError("读取失败")})
    omissions = {}
    images = await collect_export_evidence(
        ids, metadata, store, omissions=omissions, max_bytes=400_000, max_count=2,
    )
    assert list(images) == ["a", "b"]
    assert omissions == {
        "missing": "未找到该材料", "json": "非图片材料，未内嵌", "unreadable": "材料读取失败",
        "empty": "材料内容为空", "large": "超过单张 400 KB 内嵌上限",
        "c": "超过全报告 2 张内嵌上限",
    }
    assert "c" not in store.reads
    note = export_evidence_note(ids, images, omissions)
    assert "已内嵌 2 张截图" in note
    assert "原材料标识：a、missing、json、unreadable、empty、large、b、c" in note
    assert "c（超过全报告 2 张内嵌上限）" in note


async def test_frozen_collection_keeps_all_verified_images_and_rejects_live_or_changed_materials():
    store = Store({"a": b"a", "b": b"b", "changed": b"changed", "no-checksum": b"n"})
    metadata = {
        key: {"storage_path": key, "content_type": "image/png",
              "checksum": "sha256:" + hashlib.sha256(key.encode()).hexdigest()}
        for key in ("a", "b", "changed")
    }
    metadata["changed"]["checksum"] = "sha256:" + "0" * 64
    metadata["no-checksum"] = {"storage_path": "no-checksum", "content_type": "image/png"}
    omissions = {}
    images = await collect_export_evidence(
        ["a", "changed", "live", "no-checksum", "b"], metadata, store,
        omissions=omissions, max_bytes=400_000, max_count=12, require_checksum=True,
    )
    assert list(images) == ["a", "b"]
    assert omissions == {
        "changed": "冻结材料校验失败", "live": "冻结报告未提供该材料",
        "no-checksum": "材料存储信息不完整",
    }
    assert "no-checksum" not in store.reads


def test_no_evidence_is_explicit_and_duplicated_references_are_not_repeated():
    assert export_evidence_note([], {}, {}) == "判定材料：本条结果未关联截图证据。"
    assert export_evidence_note(["a", "a"], {"a": "data:image/png;base64,YQ=="}, {}) == (
        "判定材料：已内嵌 1 张截图；原材料标识：a。"
    )
