"""使用生产 default runner、effects、frontier 验证中断和失败后的完整收口。"""

from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


async def test_production_runner_full_page_gap_still_classifies_and_closes(monkeypatch):
    world = RecoveryWorld(monkeypatch, full_page_failure=True)
    result = await world.run()
    assert world.actions.count("full_page") == 2
    assert world.classifications == ["https://example.test/"]
    assert world.closed
    assert "WEB_FULL_PAGE_SCREENSHOT_FAILED" in world.rows["home"].gap_codes
    assert "WEB_FULL_PAGE_SCREENSHOT_FAILED" in result.gap_codes


async def test_production_runner_tls_exhaustion_continues_next_candidate(monkeypatch):
    world = RecoveryWorld(monkeypatch, with_candidates=True)
    await world.run()
    assert world.actions.count("https://example.test/broken") == 3
    assert world.actions.count("https://example.test/healthy") == 1
    assert "https://example.test/healthy" in world.classifications
    assert world.closed
    assert any(
        "WEB_NAVIGATION_RETRY_EXHAUSTED" in cursor.gap_codes for cursor in world.rows.values()
    )


async def test_production_runner_after_report_resume_only_closes_session(monkeypatch):
    world = RecoveryWorld(monkeypatch)
    await world.run(stop_at="session_close")
    before = list(world.actions)
    await world.run()
    assert world.actions == [*before, "session_close"]
    assert world.actions.count("bootstrap") == 1
    assert world.rows["home"].is_terminal
