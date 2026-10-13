# -*- coding: utf-8 -*-
"""
极简 XLSX 写出器 —— 纯标准库（zipfile + XML），不依赖 openpyxl / pandas

支持：多工作表、表头样式、列宽、冻结首行、数值/文本自动区分。
"""
from __future__ import annotations

import re
import zipfile
from xml.sax.saxutils import escape

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col_letter(idx):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _clean(v):
    if isinstance(v, str):
        return _ILLEGAL.sub("", v)
    return v


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheet_overrides}
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
<font><sz val="11"/><name val="微软雅黑"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="微软雅黑"/></font>
<font><sz val="11"/><color rgb="FF1F3864"/><name val="微软雅黑"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF2F5597"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right><top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>
</cellXfs>
</styleSheet>"""


def _sheet_xml(header, rows, widths):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetViews><sheetView workbookViewId="0">',
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>',
             '</sheetView></sheetViews>']
    if widths:
        parts.append("<cols>")
        for i, w in enumerate(widths):
            parts.append('<col min="%d" max="%d" width="%s" customWidth="1"/>'
                         % (i + 1, i + 1, w))
        parts.append("</cols>")
    parts.append("<sheetData>")

    def cell(ref, value, style):
        v = _clean(value)
        if isinstance(v, bool):
            return '<c r="%s" s="%d" t="b"><v>%d</v></c>' % (ref, style, 1 if v else 0)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, v)
        if v is None or v == "":
            return '<c r="%s" s="%d"/>' % (ref, style)
        return ('<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (ref, style, escape(str(v))))

    # 表头
    parts.append('<row r="1" ht="24" customHeight="1">')
    for i, name in enumerate(header):
        parts.append(cell("%s1" % _col_letter(i), name, 1))
    parts.append("</row>")

    for ri, row in enumerate(rows, start=2):
        parts.append('<row r="%d">' % ri)
        for ci, val in enumerate(row):
            parts.append(cell("%s%d" % (_col_letter(ci), ri), val, 2))
        parts.append("</row>")

    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_xlsx(path, sheets, autofit=True):
    """写出 xlsx。

    sheets: [(sheet_name, header_list, rows_list), ...]
    """
    n = len(sheets)
    overrides = "\n".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        % (i + 1) for i in range(n))

    sheet_tags = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
        % (escape(name[:31] or ("Sheet%d" % (i + 1))), i + 1, i + 1)
        for i, (name, _h, _r) in enumerate(sheets))

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets>%s</sheets></workbook>' % sheet_tags)

    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(n):
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                    % (i + 1, i + 1))
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    rels.append("</Relationships>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   CONTENT_TYPES.format(sheet_overrides=overrides))
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        z.writestr("xl/styles.xml", STYLES)
        for i, (_name, header, rows) in enumerate(sheets):
            widths = None
            if autofit:
                widths = []
                for ci, h in enumerate(header):
                    m = _vis_width(h)
                    for r in rows[:200]:
                        if ci < len(r):
                            m = max(m, _vis_width(r[ci]))
                    widths.append(min(max(m + 3, 8), 42))
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1),
                       _sheet_xml(header, rows, widths))
    return path


def _vis_width(v):
    """粗略显示宽度：中文按 2 个字符宽。"""
    if v is None:
        return 0
    s = str(v)
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def write_csv(path, header, rows):
    """备用导出：UTF-8 BOM CSV，Excel 可直接打开。"""
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return path
