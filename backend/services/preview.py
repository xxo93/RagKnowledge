"""原文档定位与在线上预览（V5-2 / R4）。

预览只读原件，不触发清洗、切片、向量化或重解析。所有路径都由 doc_id 对应的
`document.file_path` 推导，并在解析后强制校验仍位于 `settings.upload_dir` 内，
避免前端传路径或 ../、绝对路径、符号链接逃逸。
"""
from __future__ import annotations

import csv
import io
import logging
import mimetypes
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..config import ROOT_DIR, settings
from ..errors import BizError, ERR_FILE_MISSING, ERR_FILE_TYPE, ERR_INGEST_FAILED
from . import loader

logger = logging.getLogger("rag.preview")

PREVIEW_TEXT_LIMIT = 200_000
PREVIEW_MAX_ROWS = 100
PREVIEW_MAX_SHEETS = 10
PREVIEW_MAX_COLS = 80

TEXT_EXTS = {".md", ".docx", ".doc"}
SHEET_EXTS = {".xlsx", ".xlsm", ".xls", ".csv"}


def resolve_original(meta: dict) -> Path:
    """把数据库里的相对路径解析为受控绝对路径；不存在返回 40407。"""
    stored = str(meta.get("file_path") or "").strip()
    if not stored:
        raise BizError(ERR_FILE_MISSING, "原文件不存在，可能已被清理", http_status=404)

    rel = Path(stored)
    if rel.is_absolute():
        # 数据库只应存 data/uploads/... 相对路径；绝对路径一律拒绝，不泄露服务器目录
        raise BizError(ERR_FILE_MISSING, "原文件不存在，可能已被清理", http_status=404)

    root = settings.upload_dir.resolve()
    try:
        candidate = (ROOT_DIR / rel).resolve()
    except OSError as exc:
        logger.warning("原文件路径解析失败 doc_id=%s：%s", meta.get("doc_id"), exc)
        raise BizError(ERR_FILE_MISSING, "原文件不存在，可能已被清理", http_status=404) from exc

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        # 包含 ../、符号链接指向目录外等情况；不返回目录内容
        logger.warning("拒绝越界原文件路径 doc_id=%s", meta.get("doc_id"))
        raise BizError(ERR_FILE_MISSING, "原文件不存在，可能已被清理", http_status=404) from exc

    if not candidate.is_file():
        raise BizError(ERR_FILE_MISSING, "原文件不存在，可能已被清理", http_status=404)
    return candidate


def safe_download_name(doc_name: str, ext: str) -> str:
    """下载名只保留基名，剔除 CR/LF，避免响应头注入；保留中文原名。"""
    base = Path(str(doc_name or "")).name.replace("\r", "").replace("\n", "").strip()
    if not base:
        base = f"document{ext or '.bin'}"
    return base


def guess_media_type(ext: str) -> str:
    """下载用 media type；未知统一 octet-stream，避免浏览器误开。"""
    return mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"


def is_pdf(meta: dict) -> bool:
    return Path(str(meta.get("doc_name") or "")).suffix.lower() == ".pdf"


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _meta_fields(meta: dict) -> dict:
    ext = Path(str(meta.get("doc_name") or "")).suffix.lower()
    return {
        "doc_id": meta.get("doc_id"),
        "doc_name": meta.get("doc_name"),
        "ext": ext.lstrip("."),
        "file_size": meta.get("file_size"),
        "create_time": _iso(meta.get("create_time")),
        "chunk_count": meta.get("chunk_count"),
        "status": meta.get("status"),
    }


def _read_text(path: Path, ext: str) -> tuple[str, bool]:
    """文本类预览：Markdown 用原文，DOCX/DOC 复用 loader 抽取正文。"""
    text = loader.extract_text(path) if ext in {".docx", ".doc"} else loader._extract_md(path)
    truncated = len(text) > PREVIEW_TEXT_LIMIT
    return text[:PREVIEW_TEXT_LIMIT], truncated


def _cell(value) -> str:
    return loader._cell_text(value, 0)


def _row_values(cells, cols: int) -> list[str]:
    values = [_cell(v) for v in list(cells)[:cols]]
    if len(values) < cols:
        values.extend([""] * (cols - len(values)))
    return values


def _xlsx_sheets(path: Path) -> tuple[list[dict], bool]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(str(path), data_only=True, read_only=False)
        wb_formula = load_workbook(str(path), data_only=False, read_only=False, keep_links=False)
    except Exception as exc:
        logger.warning("预览 Excel 打开失败 %s：%s", path.name, exc)
        raise BizError(ERR_INGEST_FAILED, "原文件解析失败，请下载原件查看", http_status=500) from exc

    sheets: list[dict] = []
    truncated = False
    try:
        for index, ws in enumerate(wb.worksheets):
            if index >= PREVIEW_MAX_SHEETS:
                truncated = True
                break
            max_cols = max(1, min(int(ws.max_column or 1), PREVIEW_MAX_COLS))
            max_row = int(ws.max_row or 0)
            if max_row <= 0:
                continue
            formula_ws = wb_formula[ws.title] if ws.title in wb_formula.sheetnames else None

            # 合并单元格按左上角填充，只处理预览列范围
            merged: dict[tuple[int, int], object] = {}
            for rng in ws.merged_cells.ranges:
                top_left = loader._cell_or_formula(
                    ws.cell(rng.min_row, rng.min_col),
                    formula_ws.cell(rng.min_row, rng.min_col) if formula_ws else None,
                )
                for r in range(rng.min_row, min(rng.max_row, PREVIEW_MAX_ROWS + 1) + 1):
                    for c in range(rng.min_col, min(rng.max_col, max_cols) + 1):
                        merged[(r, c)] = top_left

            def value_at(r: int, c: int):
                if (r, c) in merged:
                    return merged[(r, c)]
                return loader._cell_or_formula(
                    ws.cell(r, c), formula_ws.cell(r, c) if formula_ws else None
                )

            headers = [_cell(v) for v in (value_at(1, c) for c in range(1, max_cols + 1))]
            if not any(headers):
                # 第一行空时用首行非空数据兜底，避免整表被丢弃
                headers = [f"列{i + 1}" for i in range(max_cols)]

            # 与入库解析一致：跳过跨列大标题，找到“至少两个不同非空值”的真实表头
            candidates = [
                (r, [value_at(r, c) for c in range(1, max_cols + 1)])
                for r in range(1, min(max_row, 20) + 1)
            ]
            header_row, raw_headers, _ = loader._find_header(candidates)
            if raw_headers:
                headers = [
                    _cell(v) or f"列{i + 1}"
                    for i, v in enumerate(list(raw_headers)[:max_cols])
                ]
                if len(headers) < max_cols:
                    headers.extend([f"列{i + 1}" for i in range(len(headers), max_cols)])

            rows: list[list[str]] = []
            for r in range(header_row + 1, max_row + 1):
                values = _row_values((value_at(r, c) for c in range(1, max_cols + 1)), max_cols)
                if not any(values):
                    continue
                rows.append(values)
                if len(rows) >= PREVIEW_MAX_ROWS:
                    break
            data_total = max(0, max_row - header_row)
            if data_total > PREVIEW_MAX_ROWS:
                truncated = True
            sheets.append({
                "name": ws.title,
                "headers": headers,
                "rows": rows,
                "total_rows": data_total,
                "truncated": data_total > PREVIEW_MAX_ROWS,
            })
    finally:
        wb.close()
        wb_formula.close()
    return sheets, truncated


def _xls_sheets(path: Path) -> tuple[list[dict], bool]:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:
        logger.warning("预览 XLS 打开失败 %s：%s", path.name, exc)
        raise BizError(ERR_INGEST_FAILED, "原文件解析失败，请下载原件查看", http_status=500) from exc

    sheets: list[dict] = []
    truncated = False
    try:
        for index in range(min(book.nsheets, PREVIEW_MAX_SHEETS)):
            sheet = book.sheet_by_index(index)
            if sheet.nrows <= 0 or sheet.ncols <= 0:
                continue
            cols = min(sheet.ncols, PREVIEW_MAX_COLS)
            headers = [_cell(sheet.cell_value(0, c)) for c in range(cols)]
            if not any(headers):
                headers = [f"列{i + 1}" for i in range(cols)]

            candidates = [
                (r, [sheet.cell_value(r, c) for c in range(cols)])
                for r in range(min(sheet.nrows, 20))
            ]
            header_row, raw_headers, _ = loader._find_header(candidates)
            if raw_headers:
                headers = [
                    _cell(v) or f"列{i + 1}"
                    for i, v in enumerate(list(raw_headers)[:cols])
                ]
                if len(headers) < cols:
                    headers.extend([f"列{i + 1}" for i in range(len(headers), cols)])

            rows: list[list[str]] = []
            for r in range(header_row + 1, sheet.nrows):
                values = _row_values((sheet.cell_value(r, c) for c in range(cols)), cols)
                if not any(values):
                    continue
                rows.append(values)
                if len(rows) >= PREVIEW_MAX_ROWS:
                    break
            data_total = max(0, sheet.nrows - header_row - 1)
            sheet_truncated = data_total > PREVIEW_MAX_ROWS
            truncated = truncated or sheet_truncated
            sheets.append({
                "name": sheet.name,
                "headers": headers,
                "rows": rows,
                "total_rows": data_total,
                "truncated": sheet_truncated,
            })
        if book.nsheets > PREVIEW_MAX_SHEETS:
            truncated = True
    finally:
        book.release_resources()
    return sheets, truncated


def _csv_sheets(path: Path) -> tuple[list[dict], bool]:
    text = loader._read_csv_text(path)
    if not text.strip():
        raise BizError(ERR_INGEST_FAILED, "原文件为空，没有可预览内容", http_status=500)
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    headers: list[str] = []
    rows: list[list[str]] = []
    total_rows = 0
    truncated = False
    for row in reader:
        if not row or not any(str(v).strip() for v in row):
            continue
        values = [str(v) for v in row[:PREVIEW_MAX_COLS]]
        if not headers:
            headers = loader._normalize_headers(values, len(values))
            continue
        total_rows += 1
        if len(rows) < PREVIEW_MAX_ROWS:
            rows.append([_cell(v) for v in values])
        else:
            truncated = True

    if not headers:
        raise BizError(ERR_INGEST_FAILED, "原文件为空，没有可预览内容", http_status=500)
    return [{
        "name": "CSV",
        "headers": headers,
        "rows": rows,
        "total_rows": total_rows,
        "truncated": truncated,
    }], truncated


def build_preview(meta: dict) -> dict:
    """构建非 PDF 的 JSON 预览结构；PDF 由路由直接 inline 返回。"""
    path = resolve_original(meta)
    ext = Path(str(meta.get("doc_name") or "")).suffix.lower()
    base = _meta_fields(meta)

    if ext in TEXT_EXTS:
        content, truncated = _read_text(path, ext)
        kind = "markdown" if ext == ".md" else "text"
        return {**base, "kind": kind, "content": content, "truncated": truncated, "sheets": []}

    if ext in SHEET_EXTS:
        if ext == ".csv":
            sheets, truncated = _csv_sheets(path)
        elif ext in {".xlsx", ".xlsm"}:
            sheets, truncated = _xlsx_sheets(path)
        else:
            sheets, truncated = _xls_sheets(path)
        return {**base, "kind": "sheet", "content": "", "truncated": truncated, "sheets": sheets}

    raise BizError(ERR_FILE_TYPE, "该格式暂不支持在线预览，请下载原文件", http_status=400)
