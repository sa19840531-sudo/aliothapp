"""持久化跨页面、任务级与环境矩阵证据输入集合。

Revision ID: 20260814_0060
Revises: 20260814_0059
Create Date: 2026-08-14

EvidenceInputSet 是规则执行前已经封存的输入闭包，不是规则判定结果：
- 主表绑定 task/run/rule/scope/finalization_epoch 与权威候选快照；
- 成员表保存有序 Capture 与 PASS Manifest 的精确版本、哈希及附件集合；
- task scope 不借用任意 page_capture_id 作为锚点；
- 表内容只允许幂等原值写入，不允许原地改写。

成员的 PASS 状态、Capture 角色与 Manifest 内 artifact/packet 集合由同一事务内的
PostgresEvidenceInputSetRepository 校验；普通 CHECK/FK 无法表达 JSON 集合闭包。
UPDATE 被数据库守卫拒绝；DELETE 沿用 Evidence Manifest 的生命周期边界，只由任务
级 ON DELETE CASCADE 清理，仓储不暴露单行删除入口，避免拦截级联导致任务无法清理。
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0060"
down_revision = "20260814_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_evidence_manifest_task_capture_identity
        ON qc_evidence_manifest (
            task_id, capture_id, manifest_id, manifest_version, manifest_hash
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_input_set (
            input_set_id varchar(96) PRIMARY KEY,
            input_set_hash varchar(128) NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            source_agent_run_id uuid NOT NULL,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            rule_contract_hash varchar(128) NOT NULL,
            ruleset_snapshot_hash varchar(128) NOT NULL,
            scope varchar(32) NOT NULL,
            scope_key varchar(128) NOT NULL,
            finalization_epoch integer NOT NULL,
            eligibility_snapshot_id varchar(160) NOT NULL,
            eligibility_snapshot_hash varchar(128) NOT NULL,
            selection_policy_hash varchar(128) NOT NULL,
            eligible_capture_ids jsonb NOT NULL,
            protocol_version varchar(64) NOT NULL,
            input_set_payload jsonb NOT NULL,
            sealed_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_evidence_input_set_agent_run
                FOREIGN KEY (task_id, source_agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_evidence_input_set_task_identity
                UNIQUE (task_id, input_set_id),
            CONSTRAINT uq_qc_evidence_input_set_selection UNIQUE (
                task_id, rule_id, scope, scope_key, finalization_epoch
            ),
            CONSTRAINT ck_qc_evidence_input_set_scope CHECK (
                scope IN ('page', 'journey', 'task', 'environment_matrix')
            ),
            CONSTRAINT ck_qc_evidence_input_set_epoch CHECK (
                finalization_epoch >= 0
            ),
            CONSTRAINT ck_qc_evidence_input_set_identity CHECK (
                input_set_hash ~ '^[0-9a-f]{64}$'
                AND input_set_id = 'evidence-input-set:' || input_set_hash
                AND scope_key ~ '^[a-z][a-z0-9_.:-]{0,127}$'
                AND protocol_version = 'evidence-input-set.v1'
            ),
            CONSTRAINT ck_qc_evidence_input_set_text CHECK (
                btrim(input_set_hash) <> ''
                AND btrim(rule_code) <> ''
                AND btrim(rule_contract_hash) <> ''
                AND btrim(ruleset_snapshot_hash) <> ''
                AND btrim(scope_key) <> ''
                AND btrim(eligibility_snapshot_id) <> ''
                AND btrim(eligibility_snapshot_hash) <> ''
                AND btrim(selection_policy_hash) <> ''
            ),
            CONSTRAINT ck_qc_evidence_input_set_json CHECK (
                jsonb_typeof(eligible_capture_ids) = 'array'
                AND jsonb_array_length(eligible_capture_ids) > 0
                AND jsonb_typeof(input_set_payload) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_input_set_member (
            input_set_id varchar(96) NOT NULL,
            task_id uuid NOT NULL,
            ordinal integer NOT NULL,
            capture_id uuid NOT NULL,
            capture_role varchar(32) NOT NULL,
            page_sequence integer NOT NULL,
            manifest_id varchar(96) NOT NULL,
            manifest_version integer NOT NULL,
            manifest_hash varchar(128) NOT NULL,
            artifact_ids jsonb NOT NULL,
            observation_packet_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            journey_execution_id varchar(96),
            environment_profile_id varchar(160),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (input_set_id, ordinal),
            CONSTRAINT fk_qc_evidence_input_set_member_set
                FOREIGN KEY (task_id, input_set_id)
                REFERENCES qc_evidence_input_set(task_id, input_set_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_qc_evidence_input_set_member_capture
                FOREIGN KEY (task_id, capture_id)
                REFERENCES qc_capture(task_id, id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_qc_evidence_input_set_member_manifest
                FOREIGN KEY (
                    task_id, capture_id, manifest_id,
                    manifest_version, manifest_hash
                )
                REFERENCES qc_evidence_manifest (
                    task_id, capture_id, manifest_id,
                    manifest_version, manifest_hash
                )
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_evidence_input_set_member_capture
                UNIQUE (input_set_id, capture_id),
            CONSTRAINT ck_qc_evidence_input_set_member_numbers CHECK (
                ordinal > 0 AND page_sequence >= 0 AND manifest_version > 0
            ),
            CONSTRAINT ck_qc_evidence_input_set_member_role CHECK (
                capture_role ~ '^[a-z][a-z0-9_]{0,31}$'
            ),
            CONSTRAINT ck_qc_evidence_input_set_member_json CHECK (
                jsonb_typeof(artifact_ids) = 'array'
                AND jsonb_array_length(artifact_ids) > 0
                AND jsonb_typeof(observation_packet_ids) = 'array'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_input_set_task_scope_epoch
        ON qc_evidence_input_set (
            task_id, scope, finalization_epoch, sealed_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_input_set_rule_scope
        ON qc_evidence_input_set (
            rule_id, scope, scope_key, sealed_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_input_set_member_capture
        ON qc_evidence_input_set_member (task_id, capture_id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_input_set_member_manifest
        ON qc_evidence_input_set_member (task_id, manifest_id)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_evidence_input_set_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                RAISE EXCEPTION
                    'evidence input set % is immutable', OLD.input_set_id
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_evidence_input_set_immutability_guard
        BEFORE UPDATE ON qc_evidence_input_set
        FOR EACH ROW EXECUTE FUNCTION qc_guard_evidence_input_set_immutability()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_evidence_input_set_member_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                RAISE EXCEPTION
                    'evidence input set member %/% is immutable',
                    OLD.input_set_id, OLD.ordinal
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_evidence_input_set_member_immutability_guard
        BEFORE UPDATE ON qc_evidence_input_set_member
        FOR EACH ROW EXECUTE FUNCTION qc_guard_evidence_input_set_member_immutability()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_evidence_input_set LIMIT 1)
               OR EXISTS (SELECT 1 FROM qc_evidence_input_set_member LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade evidence input set schema while sealed rows exist';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_evidence_input_set_member_immutability_guard "
        "ON qc_evidence_input_set_member"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_evidence_input_set_member_immutability()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_evidence_input_set_immutability_guard "
        "ON qc_evidence_input_set"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_evidence_input_set_immutability()")
    op.execute("DROP INDEX IF EXISTS idx_qc_evidence_input_set_member_manifest")
    op.execute("DROP INDEX IF EXISTS idx_qc_evidence_input_set_member_capture")
    op.execute("DROP TABLE IF EXISTS qc_evidence_input_set_member")
    op.execute("DROP TABLE IF EXISTS qc_evidence_input_set")
    op.execute("DROP INDEX IF EXISTS uq_qc_evidence_manifest_task_capture_identity")
