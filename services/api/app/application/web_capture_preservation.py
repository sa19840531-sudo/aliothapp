"""仅 Web V2 注入的当前载荷门禁与分类后严格收口。"""

from services.api.app.application.web_evidence_exit_gate import build_web_evidence_exit_gate
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    PostgresEvidenceExitGate,
)
from services.api.app.infrastructure.db.web_payload_readiness_repository import (
    PostgresWebPayloadReadinessRepository,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store


class WebPayloadExitGate:
    def __init__(self, *, repository, gate):
        self._repository = repository
        self._gate = gate

    async def reserve(self, *, intent, permit):
        return await self._repository.preserve_and_reserve(intent=intent, permit=permit)

    def __getattr__(self, name):
        # 预留终结/过期由原门禁处理；Web 不启用 App 恢复补采路径。
        return getattr(self._gate, name)


def build_web_payload_exit_gate(*, settings, pool=None, artifact_store=None):
    def factory(config):
        store = artifact_store or build_evidence_artifact_store(settings.evidence_storage)
        return WebPayloadExitGate(
            repository=PostgresWebPayloadReadinessRepository(
                artifact_store=store, pool=pool, reservation_seconds=config.reservation_seconds
            ),
            gate=PostgresEvidenceExitGate(
                pool=pool, reservation_seconds=config.reservation_seconds
            ),
        )

    return build_web_evidence_exit_gate(
        settings=settings.web_evidence_exit_gate, postgres_gate_factory=factory
    )


class WebPayloadClassifying:
    """分类事实先提交，真实业务需求重裁后才进入原发布通知；不重写分类结论。"""

    def __init__(self, *, classifier, repository, task_id, agent_run_id, session_id):
        self._classifier = classifier
        self._repository = repository
        self._task_id = task_id
        self._agent_run_id = agent_run_id
        self._session_id = session_id

    async def classify(self, capture):
        assessment = await self._classifier.classify(capture)
        await self._repository.finalize_classified_capture(
            task_id=self._task_id,
            agent_run_id=self._agent_run_id,
            session_id=self._session_id,
            capture_id=capture.capture_id,
        )
        return assessment
