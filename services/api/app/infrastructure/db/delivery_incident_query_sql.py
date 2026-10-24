"""投递事故列表的分页与批量消息聚合 SQL。"""


def paged_delivery_incident_ctes_sql(
    *,
    publication_table: str,
    link_table: str,
    aggregate_type: str,
    publication_message_type: str,
    inspection_message_type: str,
) -> str:
    """先限定当前页，再按真实聚合键查询消息，Inbox 对本页消息统一计数。"""
    return f"""
        WITH scoped_publications AS MATERIALIZED (
            SELECT * FROM {publication_table}
            WHERE task_id = $1::uuid
            ORDER BY updated_at DESC, id
            LIMIT $2
        ), delivery_keys AS (
            SELECT id AS publication_id, task_id,
                   '{aggregate_type}'::varchar AS aggregate_type,
                   id::text AS aggregate_id, row_version AS aggregate_version
            FROM scoped_publications
            UNION
            -- 同一工作项可能有多条关联，沿用原 EXISTS 的消息去重语义。
            SELECT publication.id, publication.task_id, 'inspection_work_item',
                   work.id::text, work.row_version
            FROM scoped_publications AS publication
            JOIN {link_table} AS link ON link.publication_id = publication.id
            JOIN qc_inspection_work_item AS work ON work.id = link.work_item_id
        ), scoped_outbox AS MATERIALIZED (
            SELECT keys.publication_id, event.status, event.message_id
            FROM delivery_keys AS keys
            JOIN qc_outbox_event AS event
              ON event.task_id = keys.task_id
             AND event.aggregate_type = keys.aggregate_type
             AND event.aggregate_id = keys.aggregate_id
             AND event.aggregate_version = keys.aggregate_version
            WHERE event.message_type = ANY(ARRAY[
                '{publication_message_type}', '{inspection_message_type}'
            ]::varchar[])
        ), inbox_stats AS MATERIALIZED (
            SELECT message_id,
                   COUNT(*) FILTER (
                       WHERE status IN ('pending', 'processing', 'retry_wait')
                   ) AS pending_count,
                   COUNT(*) FILTER (WHERE status = 'dead_letter') AS dead_count
            FROM qc_event_inbox
            WHERE message_id IN (SELECT message_id FROM scoped_outbox)
            GROUP BY message_id
        ), delivery_stats AS (
            SELECT event.publication_id,
                   COUNT(*) FILTER (
                       WHERE event.status IN ('pending', 'publishing', 'retry_wait')
                   ) AS outbox_pending_count,
                   COUNT(*) FILTER (
                       WHERE event.status = 'dead_letter'
                   ) AS outbox_dead_letter_count,
                   COALESCE(SUM(inbox.pending_count), 0) AS inbox_pending_count,
                   COALESCE(SUM(inbox.dead_count), 0) AS inbox_dead_letter_count
            FROM scoped_outbox AS event
            LEFT JOIN inbox_stats AS inbox ON inbox.message_id = event.message_id
            GROUP BY event.publication_id
        )
    """
