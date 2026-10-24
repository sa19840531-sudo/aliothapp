"""同帧像素轮廓定位不依赖 DOM、站点或固定位置，未知编号不能执行。"""

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFilter

from services.api.app.agents.explorer.web_account_agent import WebAccountVisualAgent
from services.api.app.application.web_account_grounding import (
    _candidate_cards,
    build_grounding_frame,
)


def scene(center):
    image = Image.new("RGB", (1920, 1080), "#333333")
    draw = ImageDraw.Draw(image)
    x, y = center
    draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill="#111111")
    draw.ellipse((x - 5, y - 10, x + 5, y), fill="white")
    draw.ellipse((x - 9, y + 1, x + 9, y + 10), fill="white")
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def test_dense_strokes_preserve_closed_control_without_truncating_candidates():
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    for x in range(230, 560, 24):
        for y in range(174, 420, 24):
            if abs(x - 400) < 35 and abs(y - 300) < 35:
                continue
            draw.line(
                [(x, y), (x + 4, y + 18), (x + 9, y + 6), (x + 14, y + 18), (x + 18, y)],
                fill="black",
                width=2,
            )
    draw.ellipse((380, 280, 420, 320), outline="black", width=2)
    image = image.filter(ImageFilter.GaussianBlur(0.6))
    stream = BytesIO()
    image.save(stream, "PNG")
    frame = build_grounding_frame(stream.getvalue(), (0.48, 0.48, 0.52, 0.52))
    assert frame.boxes
    assert frame.filter_facts["raw_candidate_count"] > 64
    assert frame.filter_facts["minimum_solidity"] == 0.5
    assert any(
        abs(sum(frame.resolve(k)[::2]) / 2 - 0.5) < 0.01
        and abs(sum(frame.resolve(k)[1::2]) / 2 - 0.5) < 0.01
        for k in frame.boxes
    )


def test_tight_submission_region_preserves_button_amid_dense_surroundings():
    image = Image.new("RGB", (800, 600), "#222222")
    draw = ImageDraw.Draw(image)
    for x in range(145, 650, 18):
        for y in range(190, 490, 18):
            if 275 < x < 525 and 365 < y < 455:
                continue
            draw.rectangle((x, y, x + 12, y + 12), fill="white")
    draw.rounded_rectangle((300, 390, 500, 430), radius=8, fill="#ffc400")
    stream = BytesIO()
    image.save(stream, "PNG")
    proposal = (0.38, 0.65, 0.62, 0.72)
    wide = build_grounding_frame(stream.getvalue(), proposal)
    tight = build_grounding_frame(stream.getvalue(), proposal, margin=0.06)
    assert wide.filter_facts["filtered_candidate_count"] > 64
    assert tight.boxes
    assert tight.filter_facts["filtered_candidate_count"] <= 64
    assert any(
        abs((box[0] + box[2]) / 2 - 0.5) < 0.03
        and abs((box[1] + box[3]) / 2 - 0.683) < 0.03
        for box in (tight.resolve(key) for key in tight.boxes)
    )


@pytest.mark.parametrize("center", [(1659, 39), (100, 960), (940, 520)])
def test_geometry_follows_pixels_at_any_position(center):
    x, y = center
    proposal = (
        max(0, x / 1920 - 0.02),
        max(0, y / 1080 - 0.02),
        min(1, x / 1920 + 0.02),
        min(1, y / 1080 + 0.02),
    )
    frame = build_grounding_frame(scene(center), proposal)
    boxes = [frame.resolve(key) for key in frame.boxes]
    assert any(
        abs((box[0] + box[2]) * 960 - x) < 3 and abs((box[1] + box[3]) * 540 - y) < 3
        for box in boxes
    )
    with pytest.raises(ValueError, match="GROUNDING_TARGET"):
        frame.resolve(999999)


def test_adjacent_field_and_validation_bubble_have_separate_unobscured_cards():
    image = Image.new("RGB", (1000, 700), "#333333")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((250, 280, 750, 330), radius=6, fill="#555555", outline="white")
    draw.rectangle((420, 332, 650, 375), fill="white")
    stream = BytesIO()
    image.save(stream, "PNG")
    frame = build_grounding_frame(stream.getvalue(), (0.25, 0.4, 0.75, 0.47))
    original = Image.open(BytesIO(frame.context_png))
    assert original.tobytes() == image.crop(frame.region).tobytes()
    field = next(k for k in frame.boxes if abs(frame.resolve(k)[0] - 0.25) < 0.005)
    bubble = next(k for k in frame.boxes if abs(frame.resolve(k)[0] - 0.42) < 0.005)
    assert field != bubble
    assert set(frame.card_regions) == set(frame.boxes)
    colors = []
    for key in (field, bubble):
        page, left, top, right, bottom = frame.card_regions[key]
        sheet = Image.open(BytesIO(frame.sheets[page]))
        colors.append(sheet.getpixel(((left + right) // 2, (top + bottom) // 2)))
    assert colors == [(85, 85, 85), (255, 255, 255)]


def test_identical_fields_keep_distinct_original_position_maps():
    image = Image.new("RGB", (800, 400), "#333333")
    draw = ImageDraw.Draw(image)
    for y in (100, 250):
        draw.rectangle((200, y, 600, y + 40), fill="white")
    _, sheets, _ = _candidate_cards(image, {1: (200, 100, 600, 140), 2: (200, 250, 600, 290)})
    sheet = Image.open(BytesIO(sheets[0]))
    first_map = sheet.crop((472, 32, 632, 152))
    second_map = sheet.crop((1112, 32, 1272, 152))
    assert first_map.tobytes() != second_map.tobytes()
    assert (255, 0, 0) in first_map.getdata()
    assert (255, 0, 0) in second_map.getdata()


@pytest.mark.asyncio
async def test_visual_selection_replaces_inaccurate_coordinates_with_same_frame_geometry():
    screenshot = scene((1659, 39))
    proposal = (0.93, 0.02, 0.98, 0.09)
    frame = build_grounding_frame(screenshot, proposal)
    selected = min(frame.boxes, key=lambda key: abs(sum(frame.resolve(key)[::2]) * 960 - 1659))

    class ProposalAgent:
        async def run(self, *args, **kwargs):
            return SimpleNamespace(
                text='{"action":"click","intent":"open_account",'
                '"state":"unknown","observation":"人物图标",'
                '"confidence":0.75,"box":[0.93,0.02,0.98,0.09]}'
            )

    class GroundingAgent:
        async def run(self, *args, **kwargs):
            message = args[0]
            assert len(message.contents) == 2 + len(frame.sheets)
            return SimpleNamespace(
                text='{"candidate_id":' + str(selected) + ',"confidence":0.9,'
                '"reason":"本帧该轮廓包含人物图标","matches_intent":true}'
            )

    model = object.__new__(WebAccountVisualAgent)
    model.agent, model.grounding_agent = ProposalAgent(), GroundingAgent()
    result = await model.decide(screenshot, [])
    assert abs(result.point[0] * 1920 - 1659) < 3
    assert result.confidence == 0.9
    assert result.intent_confidence == 0.75
    assert model.last_grounding["proposal"]["confidence"] == 0.75
    assert model.last_grounding["source_sha256"]


@pytest.mark.asyncio
async def test_unknown_visual_number_is_rejected_before_action():
    class Agent:
        def __init__(self, text):
            self.text = text

        async def run(self, *args, **kwargs):
            return SimpleNamespace(text=self.text)

    model = object.__new__(WebAccountVisualAgent)
    model.agent = Agent(
        '{"action":"click","intent":"open_account","state":"unknown",'
        '"observation":"入口","confidence":0.95,"box":[0.8,0,0.9,0.1]}'
    )
    model.grounding_agent = Agent(
        '{"candidate_id":999999,"confidence":1,'
        '"matches_intent":true,"reason":"无法对应当前帧的编号"}'
    )
    with pytest.raises(ValueError, match="GROUNDING_TARGET"):
        await model.decide(scene((1659, 39)), [])
