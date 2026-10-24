"""分类夹具复用实际 Run 冻结合同，不以当前默认目录补历史身份。"""

from services.api.app.application.web_page_type_assessment import WebPageTypeCatalogIdentity
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


async def frozen_assessment_options(connection, ids):
    snapshot = await read_web_policy_on_connection(connection, ids["task"], ids["run"])
    assert snapshot is not None, "分类夹具必须先建立实际 Run 冻结策略"
    policy = materialize_web_policy(snapshot)
    return {
        "assessment_schema_version": policy.assessment_schema_version,
        "catalog_identity": WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
            snapshot.to_payload()["page_catalog"],
        ),
    }
