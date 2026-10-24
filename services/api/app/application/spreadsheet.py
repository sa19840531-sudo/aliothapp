"""不依赖第三方库的 xlsx 读写（产品 Excel 导入 / 导入模板）。

xlsx 就是一个 zip 包里放几份 XML：读的时候只取第一张工作表，把共享字符串、内联字符串、
数字和布尔都转成字符串；写的时候只写内联字符串和数字，Excel / WPS / LibreOffice 都能打开。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class SpreadsheetError(ValueError):
    """xlsx 结构不对或不是 xlsx。"""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        values.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
    return values


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    names = set(archive.namelist())
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return "xl/worksheets/sheet1.xml"
    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels
        if _local(rel.tag) == "Relationship"
    }
    for sheets in workbook:
        if _local(sheets.tag) != "sheets":
            continue
        for sheet in sheets:
            rel_id = sheet.get(f"{{{_REL_NS}}}id")
            target = targets.get(rel_id or "", "")
            if not target:
                continue
            path = target.lstrip("/")
            if not path.startswith("xl/"):
                path = "xl/" + path
            if path in names:
                return path
    return "xl/worksheets/sheet1.xml"


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t") or ""
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t").strip()
    value = ""
    for child in cell:
        if _local(child.tag) == "v":
            value = child.text or ""
            break
    if kind == "s":
        try:
            return shared[int(value)].strip()
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "是" if value == "1" else "否"
    value = value.strip()
    if re.fullmatch(r"-?\d+\.0+", value):
        value = value.split(".", 1)[0]
    return value


def read_sheet_rows(raw: bytes) -> list[list[str]]:
    """读第一张工作表，返回每行的单元格文本（去掉行尾空单元格，跳过整行为空的行）。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError("文件不是有效的 xlsx（请用 Excel 另存为 .xlsx）") from exc
    with archive:
        sheet_path = _first_sheet_path(archive)
        if sheet_path not in archive.namelist():
            raise SpreadsheetError("xlsx 里没有工作表")
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells: list[str] = []
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            index = _column_index(cell.get("r") or "")
            while len(cells) < index:
                cells.append("")
            text = _cell_text(cell, shared)
            if len(cells) == index:
                cells.append(text)
            else:
                cells[index] = text
        while cells and not cells[-1].strip():
            cells.pop()
        if any(cell.strip() for cell in cells):
            rows.append(cells)
    return rows


def _column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def build_xlsx(rows: list[list[object]], *, sheet_name: str = "Sheet1", column_widths: list[int] | None = None) -> bytes:
    """把二维数组写成单表 xlsx；字符串走内联字符串，int/float 走数字单元格。"""
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row):
            ref = f"{_column_letters(column_index)}{row_index}"
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                text = escape(str(value))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    cols_xml = ""
    if column_widths:
        cols = "".join(
            f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(column_widths)
        )
        cols_xml = f"<cols>{cols}</cols>"
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}">{cols_xml}<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_MAIN_NS}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()
