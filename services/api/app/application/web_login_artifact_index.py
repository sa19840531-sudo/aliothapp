"""隔离登录账户证据的对象与数据库索引交接。"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)


class WebLoginArtifactIndexError(RuntimeError):
    """对象已写入，但其索引尚未成为可审计证据。"""

    error_code = "WEB_LOGIN_ACCOUNT_ARTIFACT_INDEX_FAILED"

    def __init__(self) -> None:
        super().__init__(self.error_code)


class WebLoginIndexedArtifactStore:
    """只有 Web 专用索引成功后，才向账户观察返回附件引用。"""

    def __init__(self, *, object_store, index, agent_run_id: str, main_session_id: str):
        self._object_store = object_store
        self._index = index
        self._agent_run_id = agent_run_id
        self._main_session_id = main_session_id

    async def store(
        self, *, task_id: str, page_capture_id: str, artifact_id: str,
        artifact_type: str, content, step: str, isolated_session_id: str,
    ):
        try:
            artifact = await self._object_store.store(
                task_id=task_id,
                page_capture_id=page_capture_id,
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                content=content,
            )
        except asyncio.CancelledError:
            _LOGGER.warning(
                "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING task=%s artifact=%s",
                task_id, artifact_id,
            )
            raise
        try:
            await self._index.register(
                task_id=task_id,
                agent_run_id=self._agent_run_id,
                main_session_id=self._main_session_id,
                isolated_session_id=isolated_session_id,
                source_capture_id=page_capture_id,
                step=step,
                artifact=artifact,
            )
        except asyncio.CancelledError:
            _LOGGER.warning(
                "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING task=%s artifact=%s",
                task_id, artifact_id,
            )
            raise
        except Exception:  # noqa: BLE001 - 数据库异常可能携带对象路径或账户文本
            try:
                confirmed = await self._index.confirm_registration(
                    task_id=task_id,
                    agent_run_id=self._agent_run_id,
                    main_session_id=self._main_session_id,
                    isolated_session_id=isolated_session_id,
                    source_capture_id=page_capture_id,
                    step=step,
                    artifact=artifact,
                )
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING task=%s artifact=%s",
                    task_id, artifact_id,
                )
                raise
            except Exception:  # noqa: BLE001 - 无法确认提交结果时不得误删对象
                confirmed = False
            if confirmed:
                return artifact
            _LOGGER.warning(
                "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING task=%s artifact=%s",
                task_id, artifact_id,
            )
            raise WebLoginArtifactIndexError() from None
        return artifact
