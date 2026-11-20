"""格式保真预览：DOCX/DOC 转 HTML，Excel/CSV 生成带样式的 HTML 表格。

V5-2 补丁：预览从“纯文本 + el-table”升级为“保留标题、字体、表格、合并单元格、
列宽、底色、对齐”等可读版式；不追求 Office 分页和页眉页脚级还原。
"""
from __future__ import annotations

import datetime as dt
import html
import io
import re
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from . import loader

PREVIEW_HTML_LIMIT = 600_000

_MAMMOTH_STYLE_MAP = """
p[style-name='Title'] => h1.doc-title:fresh
p[style-name='Subtitle'] => p.doc-subtitle:fresh
p[style-name='Heading 1'] => h1:fresh
p[style-name='Heading 2'] => h2:fresh
p[style-name='Heading 3'] => h3:fresh
p[style-name='Heading 4'] => h4:fresh
p[style-name='Heading 5'] => h5:fresh
p[style-name='Heading 6'] => h6:fresh
p[style-name='Quote'] => blockquote:fresh
p[style-name='Intense Quote'] => blockquote:fresh
table => table.doc-table
"""


def _truncate_html(value: str, limit: int = PREVIEW_HTML_LIMIT) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    cut = value[:limit]
    # 尽量在一个完整闭合标签后截断，避免前端出现半截标签
    last = cut.rfind(">")
    if last > limit * 0.8:
        cut = cut[: last + 1]
    return cut, True


def _docx_stream_to_html(stream) -> tuple[str, bool]:
    """把 DOCX 二进制流交给 mammoth，作为浏览器按页渲染失败时的降级预览。"""
    try:
        import mammoth
    except ImportError as exc:  # 依赖缺失时给出可操作提示，不静默退化成纯文本
        raise RuntimeError("缺少 mammoth 依赖，无法按格式预览 DOCX") from exc

    try:
        result = mammoth.convert_to_html(stream, style_map=_MAMMOTH_STYLE_MAP)
        value = result.value or ""
    except Exception as exc:
        raise RuntimeError(f"DOCX 转 HTML 失败：{exc}") from exc
    return _truncate_html(value)


def docx_to_html(path: Path) -> tuple[str, bool]:
    """用 mammoth 把 DOCX 转成语义 HTML；段落、加粗、列表、表格和图片保留。"""
    with path.open("rb") as stream:
        return _docx_stream_to_html(stream)


def docx_bytes_to_html(data: bytes) -> tuple[str, bool]:
    return _docx_stream_to_html(io.BytesIO(data))


def doc_to_docx_bytes(path: Path) -> bytes:
    """`.doc` 先转 `.docx`，返回字节；临时文件随 with 清理。"""
    with tempfile.TemporaryDirectory(prefix="rag_preview_doc_") as tmp:
        converted = loader._doc_to_docx(path, Path(tmp))
        return converted.read_bytes()


def doc_to_html(path: Path) -> tuple[str, bool]:
    """`.doc` 的 HTML 降级预览：转换一次，直接复用已生成的 DOCX 字节。"""
    return docx_bytes_to_html(doc_to_docx_bytes(path))


def _display_value(value: object, number_format: str = "") -> str:
    """单元格展示值：保留日期/百分比/千分位等常见格式，其余按文本输出。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fmt = number_format or ""
        if "%" in fmt:
            decimals = 2 if "0.00" in fmt else 1 if "0.0" in fmt else 0
            return f"{value * 100:.{decimals}f}%"
        if "," in fmt:
            decimals = 2 if "0.00" in fmt else 0
            return f"{value:,.0f}" if decimals == 0 else f"{value:,.{decimals}f}"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    text = str(value)
    if len(text) > 600:
        text = text[:600] + "…"
    return text


def _rgb(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    if raw in {"00000000", "FFFFFFFF", "00FFFFFF"}:
        return None
    if len(raw) == 8:
        raw = raw[2:]
    if re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        return f"#{raw.upper()}"
    return None


def _safe_font_name(value: object) -> str:
    """字体名来自不可信文档，只保留常见字符，避免破坏 style 属性。"""
    return re.sub(r"[^0-9A-Za-z _\-\u4e00-\u9fff]", "", str(value or ""))[:80]


def _border_css(border, prefix: str) -> str:
    side = getattr(border, prefix, None)
    if side is None or not getattr(side, "style", None):
        return ""
    color = _rgb(getattr(getattr(side, "color", None), "rgb", None)) or "#c9d1d9"
    width = "2px" if "medium" in str(side.style) else "1px"
    return f"border-{prefix}:{width} solid {color};"


def _xlsx_cell_style(cell) -> str:
    styles: list[str] = []
    font = cell.font
    if font.bold:
        styles.append("font-weight:600;")
    if font.italic:
        styles.append("font-style:italic;")
    if font.underline:
        styles.append("text-decoration:underline;")
    if font.strike:
        styles.append("text-decoration:line-through;")
    color = _rgb(getattr(getattr(font, "color", None), "rgb", None))
    if color:
        styles.append(f"color:{color};")
    if font.sz:
        styles.append(f"font-size:{font.sz}pt;")
    if font.name:
        name = _safe_font_name(font.name)
        if name:
            styles.append(f"font-family:'{name}',sans-serif;")

    fill = getattr(cell, "fill", None)
    if fill is not None and getattr(fill, "fill_type", None) == "solid":
        bg = _rgb(getattr(getattr(fill, "fgColor", None), "rgb", None))
        if bg:
            styles.append(f"background:{bg};")

    align = getattr(cell, "alignment", None)
    if align is not None:
        if align.horizontal in {"left", "center", "right"}:
            styles.append(f"text-align:{align.horizontal};")
        if align.vertical in {"top", "center", "bottom"}:
            styles.append(f"vertical-align:{'middle' if align.vertical == 'center' else align.vertical};")
        if align.wrap_text:
            styles.append("white-space:pre-wrap;")

    border = getattr(cell, "border", None)
    if border is not None:
        for side in ("left", "right", "top", "bottom"):
            styles.append(_border_css(border, side))
    return "".join(styles)


def _sheet_table_html(
    rows_html: list[str],
    widths: list[Optional[float]],
    max_col: int,
    letters: list[str],
) -> str:
    """给表格加 Excel 式列标与行号，便于横向滚动时保持位置感。"""
    data_widths = [max(56, (w or 9) * 7.5) for w in widths[:max_col]]
    if len(data_widths) < max_col:
        data_widths.extend([80] * (max_col - len(data_widths)))
    cols = "".join(f'<col style="width:{width:.0f}px">' for width in [44, *data_widths])
    head = "".join(f'<th class="sheet-colhead">{html.escape(letter)}</th>' for letter in letters)
    return (
        '<table class="xlsx-preview-table">'
        f'<colgroup>{cols}</colgroup>'
        f'<thead><tr><th class="sheet-corner"></th>{head}</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table>'
    )


def _xlsx_sheet_html(ws, formula_ws=None, max_rows: int = 100, max_cols: int = 80) -> dict:
    from openpyxl.utils import get_column_letter

    max_row = min(int(ws.max_row or 0), max_rows)
    max_col = min(int(ws.max_column or 1), max_cols)
    if max_row <= 0:
        return {"html": "", "html_total_rows": 0, "html_truncated": False, "col_widths": []}

    covered: set[tuple[int, int]] = set()
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for rng in ws.merged_cells.ranges:
        r1, r2 = rng.min_row, min(rng.max_row, max_row)
        c1, c2 = rng.min_col, min(rng.max_col, max_col)
        if r1 > max_row or c1 > max_col:
            continue
        spans[(r1, c1)] = (max(1, r2 - r1 + 1), max(1, c2 - c1 + 1))
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if (r, c) != (r1, c1):
                    covered.add((r, c))

    widths: list[Optional[float]] = []
    for c in range(1, max_col + 1):
        dim = ws.column_dimensions.get(get_column_letter(c))
        widths.append(dim.width if dim is not None and dim.width else None)

    rows_html: list[str] = []
    for r in range(1, max_row + 1):
        row_dim = ws.row_dimensions.get(r)
        height = row_dim.height if row_dim is not None and row_dim.height else None
        tr_style = f' style="height:{max(18, height * 1.35):.0f}px"' if height else ""
        cells_html: list[str] = [f'<th class="sheet-rowhead">{r}</th>']
        for c in range(1, max_col + 1):
            if (r, c) in covered:
                continue
            cell = ws.cell(r, c)
            formula_cell = formula_ws.cell(r, c) if formula_ws is not None else None
            value = loader._cell_or_formula(cell, formula_cell)
            text = html.escape(_display_value(value, getattr(cell, "number_format", "") or ""))
            style = _xlsx_cell_style(cell)
            rowspan, colspan = spans.get((r, c), (1, 1))
            attrs = f' style="{style}"' if style else ""
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            cells_html.append(f"<td{attrs}>{text}</td>")
        rows_html.append(f"<tr{tr_style}>{''.join(cells_html)}</tr>")

    total_rows = int(ws.max_row or 0)
    truncated = total_rows > max_rows or int(ws.max_column or 1) > max_cols
    table = _sheet_table_html(
        rows_html,
        widths,
        max_col,
        [get_column_letter(c) for c in range(1, max_col + 1)],
    )
    return {
        "html": table,
        "html_total_rows": total_rows,
        "html_truncated": truncated,
        "col_widths": [max(56, (w or 9) * 7.5) for w in widths],
    }


def _xlrd_colour(book, index: Optional[int]) -> Optional[str]:
    if index is None:
        return None
    rgb = book.colour_map.get(index)
    if not rgb:
        return None
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _xlrd_cell_style(book, sheet, r: int, c: int) -> str:
    try:
        xf = book.xf_list[sheet.cell_xf_index(r, c)]
        font = book.font_list[xf.font_index]
    except (IndexError, KeyError):
        return ""
    styles: list[str] = []
    if font.bold:
        styles.append("font-weight:600;")
    if font.italic:
        styles.append("font-style:italic;")
    if getattr(font, "underline_type", 0):
        styles.append("text-decoration:underline;")
    color = _xlrd_colour(book, getattr(font, "colour_index", None))
    if color:
        styles.append(f"color:{color};")
    if getattr(font, "height", 0):
        styles.append(f"font-size:{max(6, font.height / 20):.0f}pt;")
    if getattr(font, "name", None):
        name = _safe_font_name(font.name)
        if name:
            styles.append(f"font-family:'{name}',sans-serif;")
    bg = _xlrd_colour(book, getattr(getattr(xf, "background", None), "pattern_colour_index", None))
    pattern = getattr(getattr(xf, "background", None), "fill_pattern", 0)
    if bg and pattern not in {0, None}:
        styles.append(f"background:{bg};")
    align = getattr(xf, "alignment", None)
    hor = getattr(align, "hor_align", 0)
    if hor == 1:
        styles.append("text-align:left;")
    elif hor == 2:
        styles.append("text-align:center;")
    elif hor == 3:
        styles.append("text-align:right;")
    vert = getattr(align, "vert_align", 0)
    if vert == 1:
        styles.append("vertical-align:middle;")
    elif vert == 2:
        styles.append("vertical-align:bottom;")
    border = getattr(xf, "border", None)
    for side in ("left", "right", "top", "bottom"):
        line = getattr(border, side, None) if border is not None else None
        if line is not None and getattr(line, "line_style", 0):
            styles.append(f"border-{side}:1px solid #c9d1d9;")
    return "".join(styles)


def _xls_sheet_html(sheet, book, max_rows: int = 100, max_cols: int = 80) -> dict:
    import xlrd

    max_row = min(int(sheet.nrows or 0), max_rows)
    max_col = min(int(sheet.ncols or 0), max_cols)
    if max_row <= 0 or max_col <= 0:
        return {"html": "", "html_total_rows": 0, "html_truncated": False, "col_widths": []}

    covered: set[tuple[int, int]] = set()
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for rlo, rhi, clo, chi in getattr(sheet, "merged_cells", []) or []:
        if rlo >= max_row or clo >= max_col:
            continue
        r2 = min(rhi - 1, max_row - 1)
        c2 = min(chi - 1, max_col - 1)
        spans[(rlo, clo)] = (max(1, r2 - rlo + 1), max(1, c2 - clo + 1))
        for r in range(rlo, r2 + 1):
            for c in range(clo, c2 + 1):
                if (r, c) != (rlo, clo):
                    covered.add((r, c))

    widths: list[Optional[float]] = []
    for c in range(max_col):
        info = (getattr(sheet, "colinfo_map", {}) or {}).get(c)
        widths.append((info.width / 256) if info is not None and getattr(info, "width", 0) else None)

    rows_html: list[str] = []
    for r in range(max_row):
        cells_html: list[str] = [f'<th class="sheet-rowhead">{r + 1}</th>']
        for c in range(max_col):
            if (r, c) in covered:
                continue
            value = sheet.cell_value(r, c)
            if sheet.cell_type(r, c) == 3:  # XL_CELL_DATE
                try:
                    value = dt.datetime(*xlrd_xldate(book, value))
                except Exception:
                    pass
            text = html.escape(_display_value(value))
            style = _xlrd_cell_style(book, sheet, r, c)
            rowspan, colspan = spans.get((r, c), (1, 1))
            attrs = f' style="{style}"' if style else ""
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            cells_html.append(f"<td{attrs}>{text}</td>")
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")

    total_rows = int(sheet.nrows or 0)
    truncated = total_rows > max_rows or int(sheet.ncols or 0) > max_cols
    table = _sheet_table_html(
        rows_html,
        widths,
        max_col,
        [xlrd.formula.colname(c) for c in range(max_col)],
    )
    return {
        "html": table,
        "html_total_rows": total_rows,
        "html_truncated": truncated,
        "col_widths": [max(56, (w or 9) * 7.5) for w in widths],
    }


def xlrd_xldate(book, value: float) -> tuple:
    import xlrd

    return xlrd.xldate_as_tuple(value, book.datemode)


def _excel_col_letter(index: int) -> str:
    """1 → A，27 → AA；CSV 预览不依赖 openpyxl 也要有列标。"""
    letters = ""
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def csv_to_html(rows: Iterable[list[str]], headers: list[str] | None = None) -> str:
    body: list[str] = []
    max_col = max([len(headers or [])] + [len(row) for row in rows] + [1])
    row_no = 0
    if headers:
        row_no += 1
        cells = [str(headers[i]) if i < len(headers) else "" for i in range(max_col)]
        body.append(
            f'<tr><th class="sheet-rowhead">{row_no}</th>'
            + "".join(f'<th style="text-align:left">{html.escape(v)}</th>' for v in cells)
            + "</tr>"
        )
    for row in rows:
        row_no += 1
        cells = [str(row[i]) if i < len(row) else "" for i in range(max_col)]
        body.append(
            f'<tr><th class="sheet-rowhead">{row_no}</th>'
            + "".join(f'<td>{html.escape(v)}</td>' for v in cells)
            + "</tr>"
        )
    return _sheet_table_html(
        body,
        [None] * max_col,
        max_col,
        [_excel_col_letter(i) for i in range(1, max_col + 1)],
    )
