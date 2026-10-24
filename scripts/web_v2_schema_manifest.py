"""Web 迁移安全结构摘要；与独立新库完整升级后的固定清单比较。"""

import hashlib
import json

from sqlalchemy import text

_TABLE_SCOPE = "(c.relname LIKE 'qc_web_%' OR c.relname='qc_page_capture')"
_QUERIES = {
    "columns": f"""
        SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
               a.attnotnull,a.attidentity::text,a.attgenerated::text,pg_get_expr(d.adbin,d.adrelid)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE c.relnamespace=current_schema()::regnamespace AND {_TABLE_SCOPE}
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2
    """,
    "constraints": f"""
        SELECT c.relname,k.conname,k.contype::text,k.convalidated,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        WHERE c.relnamespace=current_schema()::regnamespace AND {_TABLE_SCOPE} ORDER BY 1,2
    """,
    "indexes": f"""
        SELECT c.relname,i.relname,x.indisvalid,pg_get_indexdef(i.oid)
        FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid
        JOIN pg_class i ON i.oid=x.indexrelid
        WHERE c.relnamespace=current_schema()::regnamespace AND {_TABLE_SCOPE} ORDER BY 1,2
    """,
    "triggers": f"""
        SELECT c.relname,t.tgname,t.tgenabled::text,pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        WHERE c.relnamespace=current_schema()::regnamespace AND {_TABLE_SCOPE}
          AND NOT t.tgisinternal ORDER BY 1,2
    """,
    "functions": """
        SELECT proname,pg_get_function_identity_arguments(oid),pg_get_functiondef(oid)
        FROM pg_proc WHERE pronamespace=current_schema()::regnamespace
          AND proname LIKE '%web%' ORDER BY 1,2
    """,
}


def schema_manifest(connection):
    # 固定 public schema；其他 schema/PG 输出差异一律人工审核，不能静默绕过验签。
    if connection.execute(text("SELECT current_schema()")).scalar_one() != "public":
        raise RuntimeError("仅支持 public schema 的已审查 Web 迁移结构")
    return {
        key: hashlib.sha256(
            json.dumps(
                [list(row) for row in connection.execute(text(query)).all()],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for key, query in _QUERIES.items()
    }


# 来源：独立 PostgreSQL 新库由全量 Alembic 链升级至 0121；旧链兼容回归必须一致。
EXPECTED_MANIFEST = {
    "columns": "4773e5e31bd4996301277c515e6ef720743c435d46936e64bf8fb5738e8ae8b6",
    "constraints": "fc41cb4aa9c08d08bae87505c72b0688e87d23532320b19a93ceb41712170c19",
    "indexes": "4acaaa92bc04d961221db359ea7cefd451e1df4fde5eb26aa30647a979f949db",
    "triggers": "846c491cebed95f2efd4eed5a7d29b7087551cb6c3088b19c285489ab26dc799",
    "functions": "c688a8e65d5f295f88c4a6d31fe2f65d8d9131c75e8aeeb0b818bf93975e88b0",
}
