"""旧冲突编号必须有对应H5结构，禁止把App旧编号当作H5已升级。"""

H5_REVISIONS = {
    "20261014_0133", "20261014_0134", "20261016_0135",
    "20261016_0136", "20261016_0137", "20261016_0138",
}


def validate_legacy_h5_identity(connection) -> None:
    if connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").scalar() is None:
        return
    revisions = {
        row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        .fetchall()
    }
    legacy = revisions & H5_REVISIONS
    if not legacy:
        return
    columns = {
        row[0] for row in connection.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='qc_h5_session_journey_guard'"
        ).fetchall()
    }
    required = {"token", "task_id", "session_id", "status", "drained_at"}
    if legacy != {"20261014_0133"}:
        required.add("physical_stop_proof")
    if not required <= columns:
        raise RuntimeError(
            "旧迁移编号与H5结构不符；可能属于旧App迁移链。"
            "停止升级，请先核对原迁移来源；禁止自动stamp、补写结构或覆盖版本。"
        )
