"""Web 已有材料的持久复用；分类、发布和判定生命周期保持分离。"""

from dataclasses import asdict, replace

from services.api.app.application.web_page_classification import WebPageAssessment


class WebLogicalTargetClassifier:
    """包裹完整分类及通知链，使再次发现的页面只复用原有材料。"""

    def __init__(self, *, classifier, repository, task_id, agent_run_id, session_id):
        self._classifier = classifier
        self._repository = repository
        self._scope = dict(task_id=task_id, agent_run_id=agent_run_id, session_id=session_id)

    async def classify(self, capture):
        target = await self._repository.claim(
            **self._scope, source_capture_id=capture.capture_id, url=capture.url
        )
        bind_aliases = getattr(self._repository, "record_capture_redirect_aliases", None)
        if callable(bind_aliases):
            await bind_aliases(**self._scope, source_capture_id=capture.capture_id, url=capture.url)
            capture = await self._with_verified_root_fact(capture, target)
        if (
            not target["created"]
            and target.get("assessment")
            and target["assessment"].get("page_type") is None
            and getattr(capture, "capture_purpose", None) == "web_primary_content_evidence"
            and target.get("primary_capture_id") == capture.capture_id
        ):
            target["created"] = await self._repository.claim_refinement(
                **self._scope, source_capture_id=capture.capture_id, url=capture.url
            )
        if not target["created"]:
            if target["state"] != "materialized" or not target.get("assessment"):
                raise ValueError("WEB_LOGICAL_TARGET_INCOMPLETE")
            payload = dict(target["assessment"])
            for name in ("evidence", "reason_codes", "conflicts"):
                payload[name] = tuple(payload.get(name, ()))
            return replace(
                WebPageAssessment(**payload),
                reused_capture_id=target["assessment_capture_id"],
                reused_logical_page_id=target["logical_page_id"],
            )
        result = await self._classifier.classify(capture)
        await self._repository.materialize(
            **self._scope, capture_id=capture.capture_id, url=capture.url, assessment=asdict(result)
        )
        return result

    async def _with_verified_root_fact(self, capture, target):
        """别名登记后、首次签发前，用同 Capture 的持久目标补足可信根事实。"""
        from urllib.parse import urlsplit, urlunsplit

        from services.api.app.application.web_page_classification import WebReadyPageCapture

        find = getattr(self._repository, "find", None)
        if not isinstance(capture, WebReadyPageCapture) or not callable(find) or not capture.url:
            return capture
        if capture.structure_facts.is_canonical_root_url:
            return capture
        parsed = urlsplit(capture.url)
        root = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        root_target = await find(**self._scope, source_capture_id=capture.capture_id, url=root)
        if not root_target or root_target.get("logical_page_id") != target.get("logical_page_id"):
            return capture
        return replace(
            capture,
            page_role="home",
            structure_facts=replace(capture.structure_facts, is_canonical_root_url=True),
        )


class WebLogicalTargetPrimaryCapture:
    """主体采证尝试按持久目标占有，恢复时不重放旧动作。"""

    def __init__(self, *, service, repository):
        self._service = service
        self._repository = repository

    async def capture(self, **kwargs):
        from services.api.app.application.web_primary_content_capture import (
            WebPrimaryContentCaptureResult,
        )

        scope = {key: kwargs[key] for key in ("task_id", "agent_run_id", "session_id")}
        scope.update(source_capture_id=kwargs["capture_id"], url=kwargs["page_url"])
        target = await self._repository.claim_primary(**scope)
        if not target["created"]:
            if not target.get("primary_result"):
                raise ValueError("WEB_LOGICAL_PRIMARY_INCOMPLETE")
            from services.api.app.application.web_interaction_precision import (
                WebInteractionEvidenceMode,
                WebInteractionOutcome,
            )

            saved = dict(target["primary_result"])
            saved["interaction_outcomes"] = tuple(
                WebInteractionOutcome(
                    **dict(
                        item,
                        evidence_mode=WebInteractionEvidenceMode(
                            item.get("evidence_mode", "natural")
                        ),
                    )
                )
                for item in saved.get("interaction_outcomes", ())
            )
            saved["attempts"] = tuple(saved.get("attempts", ()))
            return replace(
                WebPrimaryContentCaptureResult(**saved),
                scroll_count=0,
                original_scroll_count=saved.get("original_scroll_count") or saved["scroll_count"],
                reused=True,
            )
        result = await self._service.capture(**kwargs)
        saved = asdict(result)
        try:
            await self._repository.finish_primary(**scope, result=saved)
        except Exception as exc:  # noqa: BLE001 - 写入围栏失败不能丢掉已经发生的动作审计
            return replace(
                result,
                status="failed",
                evidence_capture_id=None,
                reason_code="WEB_PRIMARY_CONTENT_PERSISTENCE_FAILED",
                diagnostic_type=type(exc).__name__,
            )
        return result
