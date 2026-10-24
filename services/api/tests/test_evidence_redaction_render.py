"""验证遮盖烧录、原件不可变和非敏感业务事实的逐项保留。"""

import json
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from services.api.app.application.evidence_redaction_render import (
    MASK_TEXT,
    MAX_IMAGE_PIXELS,
    POLICY_VERSION,
    render_redaction,
)
from services.api.app.domain.evidence.redaction import RedactionError, RedactionSpec


def image_source(fmt="PNG", *, mode="RGB", size=(20, 10), metadata=False):
    image = Image.new(mode, size, "red")
    image.putpixel((19, 9), (12, 34, 56) if mode == "RGB" else (12, 34, 56, 255))
    output = BytesIO()
    kwargs = {}
    if metadata:
        exif = Image.Exif()
        exif[270] = "private-exif-phone-13800138000"
        kwargs["exif"] = exif
        if fmt == "PNG":
            text = PngImagePlugin.PngInfo()
            text.add_text("comment", "private-png-phone-13800138000")
            kwargs["pnginfo"] = text
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def image_spec(**changes):
    return RedactionSpec.model_validate(
        {
            "method": "image_regions",
            "image_width": 20,
            "image_height": 10,
            "regions": [{"x": 1, "y": 2, "width": 3, "height": 2}],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "fmt,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_image_masks_only_selected_pixels_and_removes_all_metadata(fmt, media):
    original = image_source(fmt, metadata=True)
    original_hash = sha256(original).hexdigest()
    result = render_redaction(original, media, image_spec())
    assert result.business_reviewable and result.strategy == "image_regions"
    assert result.content_type == "image/png"
    assert result.size_bytes == len(result.content)
    assert result.checksum == "sha256:" + sha256(result.content).hexdigest()
    assert POLICY_VERSION == "evidence-local-mask.v1"
    assert b"private-" not in result.content and b"13800138000" not in result.content
    assert sha256(original).hexdigest() == original_hash
    with Image.open(BytesIO(original)) as before, Image.open(BytesIO(result.content)) as after:
        assert after.mode == "RGB" and after.size == (20, 10)
        assert after.info == {} and not after.getexif()
        before_pixels = before.convert("RGB")
        for y in range(10):
            for x in range(20):
                assert after.getpixel((x, y)) == (
                    (32, 32, 32) if 1 <= x < 4 and 2 <= y < 4 else before_pixels.getpixel((x, y))
                )


def test_last_pixel_region_is_inclusive_without_spilling_into_business_context():
    spec = image_spec(regions=[{"x": 19, "y": 9, "width": 1, "height": 1}])
    result = render_redaction(image_source(), "image/png", spec)
    with Image.open(BytesIO(result.content)) as after:
        assert after.getpixel((19, 9)) == (32, 32, 32)
        assert after.getpixel((18, 9)) == (255, 0, 0)
        assert after.getpixel((19, 8)) == (255, 0, 0)


def test_transparency_cannot_retain_hidden_pixels_in_reencoded_output():
    image = Image.new("RGBA", (20, 10), (255, 0, 0, 0))
    image.putpixel((19, 9), (12, 34, 56, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    result = render_redaction(output.getvalue(), "image/png", image_spec())
    with Image.open(BytesIO(result.content)) as after:
        assert after.mode == "RGB"
        assert after.getpixel((0, 0)) == (255, 255, 255)
        assert after.getpixel((1, 2)) == (32, 32, 32)
        assert after.getpixel((19, 9)) == (12, 34, 56)


def test_exif_display_orientation_defines_size_and_local_coordinate_basis():
    image = Image.new("RGB", (10, 20), "red")
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    image.save(output, format="PNG", exif=exif)
    result = render_redaction(output.getvalue(), "image/png", image_spec())
    with Image.open(BytesIO(result.content)) as after:
        assert after.size == (20, 10) and not after.getexif()
        assert after.getpixel((1, 2)) == (32, 32, 32)


def test_exact_source_dimensions_are_required_even_if_region_still_fits():
    with pytest.raises(RedactionError) as error:
        render_redaction(image_source(), "image/png", image_spec(image_width=21))
    assert error.value.code == "REDACTION_IMAGE_SIZE_MISMATCH"


@pytest.mark.parametrize("method", ["image_regions", "reviewed_unchanged"])
def test_animated_sources_are_rejected_even_for_explicit_unchanged(method):
    output = BytesIO()
    Image.new("RGB", (20, 10), "red").save(
        output,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (20, 10), "blue")],
        duration=100,
    )
    spec = image_spec() if method == "image_regions" else RedactionSpec(method=method)
    with pytest.raises(RedactionError) as error:
        render_redaction(output.getvalue(), "image/png", spec)
    assert error.value.code == "REDACTION_SOURCE_INVALID"


def test_image_pixel_limit_is_checked_before_decoding(monkeypatch):
    import services.api.app.application.evidence_redaction_render as renderer

    monkeypatch.setattr(renderer, "MAX_IMAGE_PIXELS", 199)
    with pytest.raises(RedactionError) as error:
        render_redaction(image_source(), "image/png", image_spec())
    assert error.value.code == "REDACTION_SOURCE_INVALID"
    assert MAX_IMAGE_PIXELS == 40_000_000


def test_unicode_codepoint_ranges_keep_emoji_and_unselected_business_text():
    source = "✅订单😀 手机13800138000 金额 42.50 元"
    start = source.index("138")
    spec = RedactionSpec(method="text_ranges", ranges=[{"start": start, "end": start + 11}])
    result = render_redaction(source.encode(), "text/plain; charset=UTF-8", spec)
    assert result.content.decode() == "✅订单😀 手机[已脱敏] 金额 42.50 元"
    assert b"13800138000" not in result.content and result.business_reviewable
    assert source == "✅订单😀 手机13800138000 金额 42.50 元"


def test_multiple_text_ranges_are_applied_against_original_offsets():
    spec = RedactionSpec(
        method="text_ranges",
        ranges=[
            {"start": 9, "end": 15},
            {"start": 0, "end": 5},
        ],
    )
    result = render_redaction(b"Alice OK secret amount=42", "text/plain", spec)
    assert result.content.decode() == "[已脱敏] OK [已脱敏] amount=42"


@pytest.mark.parametrize(
    "ranges",
    [
        [{"start": 0, "end": 7}],
        [{"start": 0, "end": 3}, {"start": 3, "end": 7}],
        [{"start": 0, "end": 8}],
    ],
)
def test_full_text_or_out_of_bounds_cannot_claim_business_reviewable(ranges):
    with pytest.raises(RedactionError) as error:
        render_redaction(
            b"private", "text/plain", RedactionSpec(method="text_ranges", ranges=ranges)
        )
    assert error.value.code in {"REDACTION_FULL_COVERAGE", "REDACTION_RANGE_INVALID"}


def test_html_is_a_plain_text_derivative_and_selected_secret_is_removed():
    source = b'<script>alert("secret")</script><p>amount=42</p>'
    start = source.index(b"secret")
    spec = RedactionSpec(method="text_ranges", ranges=[{"start": start, "end": start + 6}])
    result = render_redaction(source, "text/html", spec)
    assert result.content_type == "text/plain; charset=utf-8"
    assert b"secret" not in result.content and b"amount=42" in result.content


def test_json_paths_preserve_typed_business_values_arrays_and_escaped_keys():
    source = {
        "account": {"phone": "13800138000", "name": "Alice"},
        "payment": {"amount": 42.5, "paid": False},
        "events": [{"secret": "token", "status": "completed"}],
        "a/b": {"~key": "credential"},
    }
    original = json.dumps(source, ensure_ascii=False).encode()
    spec = RedactionSpec(
        method="json_pointers",
        pointers=[
            "/account/phone",
            "/account/name",
            "/events/0/secret",
            "/a~1b/~0key",
        ],
    )
    result = render_redaction(original, "application/json", spec)
    value = json.loads(result.content)
    assert value["account"] == {"phone": MASK_TEXT, "name": MASK_TEXT}
    assert value["payment"] == {"amount": 42.5, "paid": False}
    assert value["events"] == [{"secret": MASK_TEXT, "status": "completed"}]
    assert value["a/b"]["~key"] == MASK_TEXT
    for secret in [b"13800138000", b"Alice", b"token", b"credential"]:
        assert secret not in result.content
    assert json.loads(original) == source


def test_json_exact_numbers_are_not_rounded_when_other_fields_are_redacted():
    source = b'{"secret":"private","amount":12.345678901234567890,"id":123456789012345678901}'
    result = render_redaction(
        source, "application/json", RedactionSpec(method="json_pointers", pointers=["/secret"])
    )
    assert b"12.345678901234567890" in result.content
    assert b"123456789012345678901" in result.content
    assert b"private" not in result.content


@pytest.mark.parametrize(
    "pointer", ["/missing", "/events/01", "/events/-", "/events/2", "/amount/a"]
)
def test_json_paths_are_exact_existing_members(pointer):
    with pytest.raises(RedactionError) as error:
        render_redaction(
            b'{"events":["secret"],"amount":42}',
            "application/json",
            RedactionSpec(method="json_pointers", pointers=[pointer]),
        )
    assert error.value.code == "REDACTION_POINTER_INVALID"


@pytest.mark.parametrize(
    "source",
    [
        b'{"a":"secret","a":"business"}',
        b'{"a":"secret","other":{"nested":1,"nested":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b"[" * 33 + b"0" + b"]" * 33,
        b"not-json",
        b"\xff\xfe",
    ],
)
def test_json_invalid_ambiguous_or_deep_sources_are_rejected_before_any_mask(source):
    with pytest.raises(RedactionError) as error:
        render_redaction(source, "application/json", RedactionSpec(method="reviewed_unchanged"))
    assert error.value.code == "REDACTION_SOURCE_INVALID"
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "fmt,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_explicit_unchanged_reencodes_images_and_discards_source_metadata(fmt, media):
    result = render_redaction(
        image_source(fmt, metadata=True), media, RedactionSpec(method="reviewed_unchanged")
    )
    assert result.strategy == "reviewed_unchanged" and result.business_reviewable
    assert b"private-" not in result.content
    with Image.open(BytesIO(result.content)) as image:
        assert image.mode == "RGB" and image.format == "PNG" and image.info == {}


def test_explicit_unchanged_html_has_no_active_content_type_and_json_is_normalized():
    spec = RedactionSpec(method="reviewed_unchanged")
    html = b"<script>alert('business')</script>"
    assert render_redaction(html, "text/html", spec).content_type == "text/plain; charset=utf-8"
    assert render_redaction(html, "text/html", spec).content == html
    result = render_redaction(b'{"business":42,"ok":true}', "application/json", spec)
    assert json.loads(result.content) == {"business": 42, "ok": True}


@pytest.mark.parametrize(
    "source,media,spec",
    [
        (b"plain", "application/pdf", RedactionSpec(method="reviewed_unchanged")),
        (b"plain", "image/png", image_spec()),
        (image_source(), "image/jpeg", image_spec()),
        (b"\xff\xfe", "text/plain", RedactionSpec(method="reviewed_unchanged")),
        (b"plain", "text/plain", image_spec()),
        (
            b'{"a":1}',
            "application/json",
            RedactionSpec(method="text_ranges", ranges=[{"start": 0, "end": 1}]),
        ),
    ],
)
def test_unsupported_mismatched_or_malformed_source_fails_closed(source, media, spec):
    with pytest.raises(RedactionError) as error:
        render_redaction(source, media, spec)
    assert error.value.code.startswith("REDACTION_")


def test_model_construct_cannot_bypass_local_selection_contract():
    with pytest.raises(RedactionError) as error:
        render_redaction(
            b"private", "text/plain", RedactionSpec.model_construct(method="text_ranges")
        )
    assert error.value.code == "REDACTION_SPEC_INVALID"


def test_json_pointer_empty_key_and_escaped_tilde_are_exact_rfc_members():
    source = b'{"":"private-empty","a~1b":"private-tilde","a/b":"business"}'
    spec = RedactionSpec(method="json_pointers", pointers=["/", "/a~01b"])
    result = render_redaction(source, "application/json", spec)
    assert json.loads(result.content) == {"": MASK_TEXT, "a~1b": MASK_TEXT, "a/b": "business"}


def test_source_and_json_node_limits_fail_without_returning_a_partial_derivative(monkeypatch):
    import services.api.app.application.evidence_redaction_render as renderer

    spec = RedactionSpec(method="reviewed_unchanged")
    monkeypatch.setattr(renderer, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(RedactionError) as byte_error:
        render_redaction(b"business9", "text/plain", spec)
    assert byte_error.value.code == "REDACTION_SOURCE_INVALID"
    monkeypatch.setattr(renderer, "MAX_SOURCE_BYTES", 1000)
    monkeypatch.setattr(renderer, "MAX_JSON_NODES", 2)
    with pytest.raises(RedactionError) as node_error:
        render_redaction(b'{"a":1,"b":2}', "application/json", spec)
    assert node_error.value.code == "REDACTION_SOURCE_INVALID"


def test_a_repeated_recipe_produces_identical_complete_bytes():
    source = b'{"secret":"private","business":{"amount":42.5000,"status":"paid"}}'
    spec = RedactionSpec(method="json_pointers", pointers=["/secret"])
    first = render_redaction(source, "application/json", spec)
    second = render_redaction(source, "application/json", spec)
    assert first == second
    assert b"42.5000" in first.content and b"paid" in first.content
