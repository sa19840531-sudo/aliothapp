"""冻结局部指令拒绝隐式无操作、跨方法字段和有冲突的选择。"""

import pytest
from pydantic import ValidationError

from services.api.app.domain.evidence.redaction import (
    MAX_REDACTION_ITEMS,
    RedactionSpec,
)


def image_spec(**overrides):
    return {
        "method": "image_regions",
        "image_width": 10,
        "image_height": 10,
        "regions": [{"x": 0, "y": 0, "width": 2, "height": 3}],
        **overrides,
    }


def test_spec_and_nested_members_are_immutable_and_roundtrip_json():
    spec = RedactionSpec.model_validate(image_spec())
    assert isinstance(spec.regions, tuple)
    assert RedactionSpec.model_validate_json(spec.model_dump_json()) == spec
    with pytest.raises(ValidationError):
        spec.method = "reviewed_unchanged"
    with pytest.raises(ValidationError):
        spec.regions[0].x = 5


@pytest.mark.parametrize("field", ["x", "y", "width", "height"])
@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_image_coordinates_require_strict_nonnegative_or_positive_integers(field, value):
    source = image_spec()
    source["regions"][0][field] = value
    with pytest.raises(ValidationError):
        RedactionSpec.model_validate(source)


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "image_regions"},
        image_spec(regions=[]),
        image_spec(image_width=None),
        image_spec(image_height=True),
        image_spec(image_width=10.0),
        image_spec(regions=[{"x": 9, "y": 0, "width": 2, "height": 1}]),
        image_spec(regions=[{"x": 0, "y": 9, "width": 1, "height": 2}]),
        image_spec(regions=[{"x": 0, "y": 0, "width": 0, "height": 1}]),
        image_spec(regions=[{"x": 0, "y": 0, "width": 10, "height": 10}]),
        image_spec(
            regions=[
                {"x": 0, "y": 0, "width": 5, "height": 10},
                {"x": 5, "y": 0, "width": 5, "height": 10},
            ]
        ),
        image_spec(
            regions=[
                {"x": 0, "y": 0, "width": 3, "height": 3},
                {"x": 2, "y": 2, "width": 3, "height": 3},
            ]
        ),
        {"method": "text_ranges", "ranges": []},
        {"method": "text_ranges", "ranges": [{"start": 3, "end": 3}]},
        {"method": "text_ranges", "ranges": [{"start": 4, "end": 3}]},
        {"method": "text_ranges", "ranges": [{"start": True, "end": 3}]},
        {"method": "text_ranges", "ranges": [{"start": 0, "end": 3.0}]},
        {"method": "text_ranges", "ranges": [{"start": 0, "end": 3}, {"start": 2, "end": 4}]},
        {"method": "json_pointers", "pointers": []},
        {"method": "json_pointers", "pointers": [""]},
        {"method": "json_pointers", "pointers": ["no-slash"]},
        {"method": "json_pointers", "pointers": ["/phone/~2"]},
        {"method": "json_pointers", "pointers": ["/people/*/phone"]},
        {"method": "json_pointers", "pointers": ["/a", "/a"]},
        {"method": "json_pointers", "pointers": ["/a", "/a/phone"]},
        {"method": "json_pointers", "pointers": ["/a~1b", "/a~1b/phone"]},
        {"method": "json_pointers", "pointers": ["/" + "x" * 512]},
        {"method": "json_pointers", "pointers": ["/a" * 33]},
        {"method": "reviewed_unchanged", "ranges": [{"start": 0, "end": 1}]},
        {"method": "reviewed_unchanged", "image_width": 10},
        {"method": "reviewed_unchanged", "pointers": ["/a"]},
        {"method": "text_ranges", "ranges": [{"start": 0, "end": 1}], "pointers": ["/a"]},
        {"method": "reviewed_unchanged", "unregistered": "ignored"},
    ],
)
def test_invalid_or_conflicting_specs_fail_at_contract_boundary(payload):
    with pytest.raises(ValidationError):
        RedactionSpec.model_validate(payload)


def test_touching_regions_and_ranges_are_distinct_valid_local_instructions():
    spec = RedactionSpec.model_validate(
        image_spec(
            regions=[
                {"x": 0, "y": 0, "width": 2, "height": 2},
                {"x": 2, "y": 0, "width": 2, "height": 2},
            ]
        )
    )
    assert len(spec.regions) == 2
    text = RedactionSpec(
        method="text_ranges",
        ranges=[
            {"start": 0, "end": 1},
            {"start": 1, "end": 2},
        ],
    )
    assert len(text.ranges) == 2


@pytest.mark.parametrize("field,method", [("ranges", "text_ranges"), ("pointers", "json_pointers")])
def test_selector_collection_has_a_fixed_upper_bound(field, method):
    items = (
        [{"start": 2 * index, "end": 2 * index + 1} for index in range(MAX_REDACTION_ITEMS + 1)]
        if field == "ranges"
        else [f"/field-{index}" for index in range(MAX_REDACTION_ITEMS + 1)]
    )
    with pytest.raises(ValidationError):
        RedactionSpec.model_validate({"method": method, field: items})
