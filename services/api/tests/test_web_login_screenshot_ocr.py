"""登录账户截图 OCR 只生成文字线索，不生成点击许可。"""

import pytest

from services.api.app.application.web_login_screenshot_ocr import (
    WebLoginScreenshotOcr,
)


@pytest.mark.asyncio
async def test_ocr_groups_recharge_text_and_normalizes_its_box():
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t100\t20\t25\t20\t92\t去\n"
        "5\t1\t1\t1\t1\t2\t125\t20\t50\t20\t90\t充值\n"
    )

    def runner(command, **kwargs):
        return type("Result", (), {"returncode": 0, "stdout": tsv.encode()})()

    ocr = WebLoginScreenshotOcr(executable="tesseract", runner=runner)
    image = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    hits = await ocr.read(image, width=200, height=100)
    assert hits == ({"text": "去充值", "box": (0.5, 0.2, 0.875, 0.4)},)
