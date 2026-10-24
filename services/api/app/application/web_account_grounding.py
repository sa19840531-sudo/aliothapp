"""以同帧像素轮廓提供坐标事实，入口和字段含义仍由视觉模型判定。"""

from dataclasses import dataclass
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass
class GroundingFrame:
    png: bytes
    region: tuple[int, int, int, int]
    boxes: dict[int, tuple[int, int, int, int]]
    size: tuple[int, int]
    filter_facts: dict
    context_png: bytes
    sheets: tuple[bytes, ...]
    card_regions: dict

    def resolve(self, candidate_id):
        if candidate_id not in self.boxes:
            raise ValueError("WEB_ACCOUNT_GROUNDING_TARGET_INVALID")
        x1, y1, x2, y2 = self.boxes[candidate_id]
        left, top, _, _ = self.region
        width, height = self.size
        return ((left + x1) / width, (top + y1) / height, (left + x2) / width, (top + y2) / height)


def _png(image):
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _candidate_cards(image, boxes):
    # 编号放在各自卡片的独立标题栏，不能覆盖邻近输入框、提示气泡或候选文字。
    sheets, regions = [], {}
    items = list(boxes.items())
    for start in range(0, len(items), 8):
        batch = items[start : start + 8]
        sheet = Image.new("RGB", (1280, ((len(batch) + 1) // 2) * 160), "white")
        draw = ImageDraw.Draw(sheet)
        for offset, (number, (x1, y1, x2, y2)) in enumerate(batch):
            left, top = (offset % 2) * 640 + 8, (offset // 2) * 160
            draw.text(
                (left, top + 4), f"ID {number}", fill="black", font=ImageFont.load_default(size=20)
            )
            origin = (max(0, x1 - 6), max(0, y1 - 6))
            crop = image.crop((*origin, min(image.width, x2 + 6), min(image.height, y2 + 6)))
            ImageDraw.Draw(crop).rectangle(
                (x1 - origin[0], y1 - origin[1], x2 - origin[0] - 1, y2 - origin[1] - 1),
                outline="cyan",
                width=2,
            )
            crop.thumbnail((448, 120))
            sheet.paste(crop, (left, top + 32))
            # 同款控件可有相同像素，单独位置图保留其与外置标签的空间关联。
            position_map = image.copy()
            position_map.thumbnail((160, 120))
            sx, sy = position_map.width / image.width, position_map.height / image.height
            ImageDraw.Draw(position_map).rectangle(
                (
                    int(x1 * sx),
                    int(y1 * sy),
                    max(int(x1 * sx) + 1, int(x2 * sx)),
                    max(int(y1 * sy) + 1, int(y2 * sy)),
                ),
                outline="red",
                width=2,
            )
            sheet.paste(position_map, (left + 464, top + 32))
            regions[number] = (
                len(sheets),
                left,
                top + 32,
                left + crop.width,
                top + 32 + crop.height,
            )
        sheets.append(sheet)
    # 派生证据包含原局部图和全部候选卡片，不替代原始完整视口截图。
    combined = Image.new(
        "RGB", (max(image.width, 1280), image.height + sum(s.height for s in sheets)), "white"
    )
    combined.paste(image, (0, 0))
    top = image.height
    for sheet in sheets:
        combined.paste(sheet, (0, top))
        top += sheet.height
    return _png(combined), tuple(_png(sheet) for sheet in sheets), regions


def build_grounding_frame(screenshot, proposal, *, margin=0.2):
    source = Image.open(BytesIO(screenshot)).convert("RGB")
    x1, y1, x2, y2 = proposal
    region = (
        max(0, int((x1 - margin) * source.width)),
        max(0, int((y1 - margin) * source.height)),
        min(source.width, int((x2 + margin) * source.width)),
        min(source.height, int((y2 + margin) * source.height)),
    )
    image = source.crop(region)
    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    contours, _ = cv2.findContours(cv2.Canny(gray, 10, 35), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    solidity = {}
    for contour in contours:
        rectangle = cv2.boundingRect(contour)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        score = cv2.contourArea(contour) / hull_area if hull_area else 0
        solidity[rectangle] = max(solidity.get(rectangle, 0), score)
    rectangles = sorted(solidity, key=lambda box: box[2] * box[3], reverse=True)
    kept = []
    for x, y, width, height in rectangles:
        if min(width, height) < 12 or width > image.width * 0.9 or height > image.height * 0.9:
            continue
        duplicate = False
        for a, b, w, h in kept:
            intersection = max(0, min(x + width, a + w) - max(x, a)) * max(
                0, min(y + height, b + h) - max(y, b)
            )
            if intersection / (width * height + w * h - intersection) > 0.8:
                duplicate = True
                break
        if not duplicate:
            kept.append((x, y, width, height))
    facts = {"raw_candidate_count": len(kept), "minimum_solidity": 0}
    # 密集笔画先按通用几何填充度去噪，不限制控件形状或挑选前若干候选。
    # 去噪可能遗漏开放轮廓，模型必须重新看图确认；仍过密就拒绝。
    if len(kept) > 64:
        facts["minimum_solidity"] = 0.5
        kept = [box for box in kept if solidity[box] >= 0.5]
    facts["filtered_candidate_count"] = len(kept)
    if len(kept) > 64:
        kept = []
    boxes = {}
    for index, (x, y, width, height) in enumerate(sorted(kept, key=lambda b: (b[1], b[0])), 1):
        boxes[index] = (x, y, x + width, y + height)
    png, sheets, regions = _candidate_cards(image, boxes)
    return GroundingFrame(png, region, boxes, source.size, facts, _png(image), sheets, regions)
