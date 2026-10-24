"""H5 定向补采的精确 Manifest 读取适配。"""

from __future__ import annotations


class H5TargetedRecaptureObservationReader:
    """普通判定沿用既有读取器，仅补采判定读取绑定的精确 Manifest。"""

    def __init__(self, *, inner: object, exact_manifest_reader: object) -> None:
        self._inner = inner
        self._exact_manifest_reader = exact_manifest_reader

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def load_source_for_manifest(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        manifest_id: str,
    ) -> object:
        return await self._exact_manifest_reader.load_source_for_manifest(
            task_id=task_id,
            page_capture_id=page_capture_id,
            manifest_id=manifest_id,
        )
