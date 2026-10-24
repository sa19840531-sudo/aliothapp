"""以真实 PostgreSQL 验证命令范围的任务、会话和构建身份边界。

测试只在显式提供 ``ALIOTH_TEST_DATABASE_URL`` 时执行，并复用 App 集成测试
的隔离数据库迁移与任务清理。这里不使用 mock 行，所有断言都来自真实 SQL
查询结果。
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.command_scope_reader import (
    PostgresTaskCommandScopeReader,
)
from services.api.tests.test_app_postgres_integration import (
    _app_database,
    _upgrade_isolated_postgres,  # noqa: F401 - 注册显式测试库迁移 fixture
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@pytest.mark.asyncio
async def test_scope_reader_uses_real_task_session_and_verified_build_facts() -> None:
    """真实 SQL 必须支持 active/draining，并拒绝错任务、错会话和旧 epoch。"""
    async with _app_database() as (connection, pool, ids):
        other_task_id = str(uuid4())
        other_session_id = f"web-scope-{uuid4()}"
        reader = PostgresTaskCommandScopeReader(pool=pool)

        try:
            await connection.execute(
                """
                INSERT INTO qc_platform_build (
                    id, platform, original_filename, storage_path, sha256,
                    size_bytes, package_name, status, verified_at, created_by, tenant_id
                )
                VALUES (
                    $1::uuid, 'android', 'scope.apk', 'tests/scope.apk', $2,
                    1, 'com.example.scope', 'verified', now(), $3::uuid, $4::uuid
                )
                """,
                ids["build_ref"].removeprefix("app-build:"),
                "a" * 64,
                ids["user"],
                ids["tenant"],
            )
            await connection.execute(
                """
                INSERT INTO qc_surface_session (
                    session_id, task_id, surface_kind, session_owner, status,
                    finalization_epoch, platform_build_ref, activated_at
                )
                VALUES ($1, $2::uuid, 'android', 'scope-integration', 'active',
                        0, $3, now())
                """,
                ids["session"],
                ids["task"],
                ids["build_ref"],
            )

            app_scope = await reader.read_command_scope(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
            )
            assert app_scope is not None
            assert app_scope.finalization_epoch == 0
            assert app_scope.policy.allowed_packages == ("com.example.scope",)

            # 执行动作的读取器必须真实校验当前 Session Lease；仅能读到
            # task/session 不能证明底层设备调用仍属于本轮 Coordinator。
            await connection.execute(
                """
                UPDATE qc_surface_session
                SET lease_owner = 'scope-worker',
                    lease_token = $2::uuid,
                    command_sequence = 7,
                    lease_expires_at = now() + interval '5 minutes',
                    updated_at = now()
                WHERE session_id = $1
                """,
                ids["session"],
                ids["token_a"],
            )
            verified_execution_scope = await reader.verify_execution_context(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
                lease_owner="scope-worker",
                lease_token=ids["token_a"],
                command_sequence=7,
                finalization_epoch=0,
            )
            assert verified_execution_scope is not None
            assert verified_execution_scope.policy.allowed_packages == (
                "com.example.scope",
            )

            # 错 Lease、错序号和跨任务/跨 Session 组合均不能获得执行范围。
            assert (
                await reader.verify_execution_context(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                    lease_owner="scope-worker",
                    lease_token=ids["token_b"],
                    command_sequence=7,
                    finalization_epoch=0,
                )
                is None
            )
            assert (
                await reader.verify_execution_context(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                    lease_owner="scope-worker",
                    lease_token=ids["token_a"],
                    command_sequence=8,
                    finalization_epoch=0,
                )
                is None
            )

            # draining 仍是允许完成当前命令的有效会话状态。
            await connection.execute(
                """
                UPDATE qc_surface_session
                SET status = 'draining', draining_at = now(), updated_at = now()
                WHERE session_id = $1
                """,
                ids["session"],
            )
            draining_scope = await reader.read_command_scope(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
            )
            assert draining_scope is not None
            assert draining_scope.finalization_epoch == 0
            draining_execution_scope = await reader.verify_execution_context(
                task_id=ids["task"],
                session_id=ids["session"],
                surface_kind=SurfaceKind.ANDROID,
                lease_owner="scope-worker",
                lease_token=ids["token_a"],
                command_sequence=7,
                finalization_epoch=0,
            )
            assert draining_execution_scope is not None

            # 任务 epoch 前进后，旧 Coordinator 上下文必须失效，即使 Lease
            # 本身尚未过期；之后把 Session 追到新 epoch 才允许新上下文。
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_epoch = 1, updated_at = now()
                WHERE id = $1::uuid
                """,
                ids["task"],
            )
            assert (
                await reader.verify_execution_context(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                    lease_owner="scope-worker",
                    lease_token=ids["token_a"],
                    command_sequence=7,
                    finalization_epoch=0,
                )
                is None
            )
            await connection.execute(
                """
                UPDATE qc_surface_session
                SET finalization_epoch = 1, updated_at = now()
                WHERE session_id = $1
                """,
                ids["session"],
            )
            assert (
                await reader.verify_execution_context(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                    lease_owner="scope-worker",
                    lease_token=ids["token_a"],
                    command_sequence=7,
                    finalization_epoch=1,
                )
                is not None
            )

            # 已 verified 构建被撤销后，不能退化成空包名的任意 App 范围。
            await connection.execute(
                """
                UPDATE qc_platform_build
                SET status = 'deleted', storage_path = NULL, verified_at = NULL
                WHERE id = $1::uuid
                """,
                ids["build_ref"].removeprefix("app-build:"),
            )
            assert (
                await reader.read_command_scope(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                )
                is None
            )

            # 恢复同一已核验构建，继续验证跨任务/跨会话必须显式不匹配。
            await connection.execute(
                """
                UPDATE qc_platform_build
                SET status = 'verified', storage_path = 'tests/scope.apk', verified_at = now()
                WHERE id = $1::uuid
                """,
                ids["build_ref"].removeprefix("app-build:"),
            )
            await connection.execute(
                """
                INSERT INTO qc_task (
                    id, target_url, ruleset_id, status, target_platform,
                    submitter_id, tenant_id
                )
                VALUES (
                    $1::uuid, 'https://scope.example.test/home', $2::uuid,
                    'running', 'h5', $3::uuid, $4::uuid
                )
                """,
                other_task_id,
                ids["ruleset"],
                ids["user"],
                ids["tenant"],
            )
            await connection.execute(
                """
                INSERT INTO qc_surface_session (
                    session_id, task_id, surface_kind, session_owner, status,
                    finalization_epoch, activated_at
                )
                VALUES ($1, $2::uuid, 'web', 'scope-integration', 'active', 0, now())
                """,
                other_session_id,
                other_task_id,
            )

            web_scope = await reader.read_command_scope(
                task_id=other_task_id,
                session_id=other_session_id,
                surface_kind=SurfaceKind.WEB,
            )
            assert web_scope is not None
            assert web_scope.policy.allowed_hosts == ("scope.example.test",)

            # 真实存在但不属于同一 task/session 对的组合必须失败关闭。
            assert (
                await reader.read_command_scope(
                    task_id=ids["task"],
                    session_id=other_session_id,
                    surface_kind=SurfaceKind.WEB,
                )
                is None
            )
            assert (
                await reader.read_command_scope(
                    task_id=other_task_id,
                    session_id=ids["session"],
                    surface_kind=SurfaceKind.ANDROID,
                )
                is None
            )

            # 任务 epoch 前进而会话仍是旧 epoch 时，读侧不能返回旧范围。
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_epoch = 1, updated_at = now()
                WHERE id = $1::uuid
                """,
                other_task_id,
            )
            assert (
                await reader.read_command_scope(
                    task_id=other_task_id,
                    session_id=other_session_id,
                    surface_kind=SurfaceKind.WEB,
                )
                is None
            )

            # 会话切换到新 epoch 后，读侧只返回同一轮的新范围。
            await connection.execute(
                """
                UPDATE qc_surface_session
                SET finalization_epoch = 1, updated_at = now()
                WHERE session_id = $1
                """,
                other_session_id,
            )
            current_scope = await reader.read_command_scope(
                task_id=other_task_id,
                session_id=other_session_id,
                surface_kind=SurfaceKind.WEB,
            )
            assert current_scope is not None
            assert current_scope.finalization_epoch == 1
        finally:
            await connection.execute(
                "DELETE FROM qc_platform_build WHERE id = $1::uuid",
                ids["build_ref"].removeprefix("app-build:"),
            )
            await connection.execute(
                "DELETE FROM qc_task WHERE id = $1::uuid",
                other_task_id,
            )
