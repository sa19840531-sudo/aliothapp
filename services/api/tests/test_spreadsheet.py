"""无第三方依赖的 xlsx 读写。"""

import io
import zipfile

import pytest

from services.api.app.application.spreadsheet import SpreadsheetError, build_xlsx, read_sheet_rows


def test_build_and_read_roundtrip_keeps_text_numbers_and_gaps() -> None:
    raw = build_xlsx(
        [
            ["产品编码", "产品名称", "所属部门", "数量", "启用"],
            ["MB-001", "手机银行 <Android>", "", 12, True],
            ["H5-001", "H5 活动页 & 更多", "一部", 3.5, False],
            [],
        ],
        sheet_name="产品导入",
        column_widths=[14, 24, 12, 8, 8],
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
    assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/styles.xml"} <= names

    rows = read_sheet_rows(raw)
    assert rows == [
        ["产品编码", "产品名称", "所属部门", "数量", "启用"],
        ["MB-001", "手机银行 <Android>", "", "12", "是"],
        ["H5-001", "H5 活动页 & 更多", "一部", "3.5", "否"],
    ]


def test_read_shared_strings_and_sheet_lookup() -> None:
    """Excel 保存的文件用共享字符串表，工作表路径来自 workbook.xml.rels。"""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="{rel_ns}"><sheets><sheet name="数据" sheetId="1" r:id="rId9"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Type="x" Target="worksheets/data.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{ns}"><si><t>产品编码</t></si><si><r><t>富文本</t></r><r><t>名称</t></r></si></sst>',
        )
        archive.writestr(
            "xl/worksheets/data.xml",
            f'<worksheet xmlns="{ns}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2"><v>7.0</v></c><c r="B2" t="inlineStr"><is><t> 内联 </t></is></c></row>'
            "</sheetData></worksheet>",
        )
    rows = read_sheet_rows(buffer.getvalue())
    assert rows == [["产品编码", "", "富文本名称"], ["7", "内联"]]


def test_invalid_bytes_raise_spreadsheet_error() -> None:
    with pytest.raises(SpreadsheetError):
        read_sheet_rows(b"PK-not-a-zip")
