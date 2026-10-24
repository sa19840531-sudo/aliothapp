"""集合页独立契约与历史冻结目录隔离。"""

import json
from pathlib import Path

import pytest

from services.api.app.domain.page import web_page_catalog as catalog_module


def payload():
    return json.loads(Path(catalog_module.__file__).with_name(
        'web_page_type_catalog.json').read_text(encoding='utf-8'))


def test_collection_is_optional_business_type_without_changing_list_types():
    from services.api.app.domain.exploration.web_exploration import WEB_LIST_PAGE_TYPES

    catalog = catalog_module.load_web_page_type_catalog()
    definition = catalog.require_code('collection_page')
    assert definition.category == 'business'
    assert definition.coverage_required is False
    assert 'collection_page' not in WEB_LIST_PAGE_TYPES
    assert catalog.codes == catalog_module.WEB_PAGE_TYPE_CODES
    assert {item.page_type for item in definition.next_page_candidates} == {
        'content_list', 'video_list'}


@pytest.mark.parametrize('version', ['2026-09-14.web.v9', '2026-09-15.web.v10'])
def test_previous_frozen_catalog_does_not_acquire_collection_type(version):
    codes = catalog_module.web_page_type_codes_for_catalog_version(version)
    assert 'collection_page' not in codes
    assert len(codes) == 10


def test_collection_cannot_require_coverage_on_every_site():
    data = payload()
    definition = next(row for row in data['page_types'] if row['code'] == 'collection_page')
    definition['coverage_required'] = True
    with pytest.raises(catalog_module.WebPageTypeCatalogError):
        catalog_module.parse_web_page_type_catalog(data)


@pytest.mark.parametrize('version', [
    '2026-09-19.web.v11', '2026-09-26.web.v12', '2026-09-28.web.v13',
    '2026-09-29.web.v14', '2026-09-29.web.v15', '2026-09-30.web.v16',
    '2026-09-30.web.v17', '2026-09-30.web.v18',
])
def test_coverage_uses_each_frozen_collection_catalog_version(version):
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot

    snapshot = WebCoverageSnapshot.create(
        task_id='task', agent_run_id='run', finalization_epoch=0,
        required_page_types=('home',), observed_page_types=('home',),
        optional_page_types=('collection_page',), unrecognized_page_types=(),
        catalog_version=version,
    )
    assert snapshot.coverage_status == 'complete'
    assert snapshot.required_page_types == ('home',)
    assert 'collection_page' in snapshot.optional_page_types
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        parse_web_coverage_snapshot_row,
    )
    from services.api.tests.test_web_coverage_snapshot_repository import _snapshot_row

    assert parse_web_coverage_snapshot_row(_snapshot_row(snapshot)) == snapshot


@pytest.mark.parametrize('version', [
    '2026-09-13.web.v8', '2026-09-14.web.v9', '2026-09-15.web.v10',
])
def test_old_snapshot_remains_old_and_rejects_new_collection(version):
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot

    arguments = dict(task_id='task', agent_run_id='run', finalization_epoch=0,
        required_page_types=('home',), observed_page_types=('home',),
        unrecognized_page_types=(), catalog_version=version)
    snapshot = WebCoverageSnapshot.create(**arguments)
    assert 'collection_page' not in snapshot.not_applicable_page_types
    assert snapshot.coverage_status == 'complete'
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        parse_web_coverage_snapshot_row,
    )
    from services.api.tests.test_web_coverage_snapshot_repository import _snapshot_row

    assert parse_web_coverage_snapshot_row(_snapshot_row(snapshot)) == snapshot
    with pytest.raises(ValueError, match='非 Web 标准页型'):
        WebCoverageSnapshot.create(**arguments, optional_page_types=('collection_page',))


def test_unknown_catalog_version_is_not_reinterpreted_as_old_seven_types():
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot

    with pytest.raises(ValueError, match='版本不受支持'):
        WebCoverageSnapshot.create(
            task_id='task', agent_run_id='run', finalization_epoch=0,
            required_page_types=('home',), observed_page_types=('home',),
            unrecognized_page_types=(), catalog_version='2026-10-16.web.v99',
        )


def test_collection_coverage_preserves_historical_denominator():
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot

    arguments = dict(task_id='task', agent_run_id='run', finalization_epoch=0,
        required_page_types=('home',), observed_page_types=('home', 'collection_page'),
        unrecognized_page_types=(), optional_page_types=('collection_page',))
    current = WebCoverageSnapshot.create(**arguments, catalog_version='2026-09-19.web.v11')
    assert current.coverage_status == 'complete'
    assert 'collection_page' not in current.required_page_types
    with pytest.raises(ValueError):
        WebCoverageSnapshot.create(**arguments, catalog_version='2026-09-15.web.v10')


@pytest.mark.asyncio
@pytest.mark.parametrize('model_type,confidence,review,expected', [
    ('collection_page', .85, False, 'collection_page'),
    ('collection_page', .65, False, 'unknown'),
    ('collection_page', .95, True, 'unknown'),
    ('unknown', .85, False, 'unknown'),
    ('video_list', .85, False, 'video_list'),
])
async def test_collection_never_overrides_model_semantics_or_review(
    model_type, confidence, review, expected,
):
    from services.api.app.application.web_page_classification import (
        WebPageAssessment,
        WebPageClassifier,
    )
    from services.api.tests.test_web_page_classification import Assessor, _capture

    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(page_type=model_type, confidence=confidence,
            evidence=('当前主体集合语义',), needs_review=review)),
        catalog=catalog_module.load_web_page_type_catalog(),
    ).classify(_capture())
    assert result.page_type == expected
