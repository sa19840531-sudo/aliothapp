"""将跨域证据关联从旧页面 Capture 约束切换到公共 Capture。"""

from __future__ import annotations

from alembic import op

revision = "20260806_0039"
down_revision = "20260806_0038"
branch_labels = None
depends_on = None


# 0035 已经负责以下 App/Action/Evidence/Inspection/Journey 外键。0039 只
# 处理 0009 遗留的任务日志关系，避免重复重建 0035 的约束并破坏其可延迟属性。
_CAPTURE_REFERENCES = (
    ("qc_task_log", "page_capture_id", "SET NULL"),
)

# 以下关系仍然是页面探索专属事实：它们依赖页面 Capture 的页面字段和探索动作
# 语义，不是 App 证据、动作或检查链路的通用 Capture 引用，因此刻意保留旧外键。
_PAGE_CAPTURE_ONLY_REFERENCES = (
    ("qc_page_capture", "parent_page_capture_id"),
    ("qc_page_exploration_action", "source_page_capture_id"),
    ("qc_page_exploration_action", "result_page_capture_id"),
    ("qc_task_page_node", "page_capture_id"),
    ("qc_pipeline_shadow_comparison", "page_capture_id"),
)


def _drop_reference(table_name: str, column_name: str, target_table: str) -> None:
    other_target_table = (
        "qc_capture" if target_table == "qc_page_capture" else "qc_page_capture"
    )
    op.execute(
        f"""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            FOR constraint_name IN
                SELECT constraint_row.conname
                FROM pg_constraint AS constraint_row
                JOIN pg_attribute AS attribute_row
                  ON attribute_row.attrelid = constraint_row.conrelid
                 AND attribute_row.attnum = ANY(constraint_row.conkey)
                WHERE constraint_row.contype = 'f'
                  AND constraint_row.conrelid = '{table_name}'::regclass
                  AND constraint_row.confrelid IN (
                      '{target_table}'::regclass,
                      '{other_target_table}'::regclass
                  )
                  AND attribute_row.attname = '{column_name}'
            LOOP
                EXECUTE format(
                    'ALTER TABLE {table_name} DROP CONSTRAINT %I',
                    constraint_name
                );
            END LOOP;
        END
        $$;
        """
    )


def _add_reference(
    table_name: str,
    column_name: str,
    target_table: str,
    on_delete: str,
) -> None:
    constraint_name = f"fk_{table_name}_{column_name}_{target_table}"
    op.execute(
        f"""
        ALTER TABLE {table_name}
        ADD CONSTRAINT {constraint_name}
            FOREIGN KEY ({column_name})
            REFERENCES {target_table}(id)
            ON DELETE {on_delete}
            DEFERRABLE INITIALLY DEFERRED
        """
    )


def upgrade() -> None:
    for table_name, column_name, on_delete in _CAPTURE_REFERENCES:
        _drop_reference(table_name, column_name, "qc_page_capture")
        _add_reference(table_name, column_name, "qc_capture", on_delete)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                LEFT JOIN qc_page_capture AS page_capture
                  ON page_capture.id = capture.id
                WHERE page_capture.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot restore qc_page_capture references while App Capture rows exist';
            END IF;
        END
        $$;
        """
    )
    for table_name, column_name, on_delete in _CAPTURE_REFERENCES:
        _drop_reference(table_name, column_name, "qc_capture")
        _add_reference(table_name, column_name, "qc_page_capture", on_delete)
