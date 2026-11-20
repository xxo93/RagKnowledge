"""文档解析：PDF / DOCX / Markdown / Excel / CSV / DOC 文本提取。

- 按扩展名分派；解析库在函数内延迟导入，避免缺依赖时模块加载即失败；
- 扫描版 PDF 无文字层时返回空串，由 ingest 判定为“无可提取文本”失败；
- 表格类统一转成“工作表名 + Markdown 表格”，保证切片后仍能定位来源；
- `.doc` 先转 `.docx`（LibreOffice 或 Windows Word COM），再复用 python-docx；
- 本模块只负责“抽取原始文本”，清洗交给 cleaner.py，职责单一。
"""
from __future__ import annotations

import csv
import io
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path

from ..config import settings
from ..errors import BizError, ERR_INGEST_FAILED

logger = logging.getLogger("rag.loader")

SUPPORTED_EXTS = {".pdf", ".docx", ".md", ".xlsx", ".xlsm", ".xls", ".csv", ".doc"}


def extract_text(file_path: str | Path) -> str:
    """根据后缀分派解析，返回拼接后的纯文本。"""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BizError(40002, f"不支持的文件类型：{ext or '未知'}", http_status=400)

    if ext == ".md":
        return _extract_md(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext in {".xlsx", ".xlsm"}:
        return _extract_xlsx(path)
    if ext == ".xls":
        return _extract_xls(path)
    if ext == ".csv":
        return _extract_csv(path)
    if ext == ".doc":
        return _extract_doc(path)
    return ""


def _extract_md(path: Path) -> str:
    """Markdown 直接按文本读取（UTF-8 优先，失败回退 GBK）。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise BizError(ERR_INGEST_FAILED, "Markdown 文件编码无法识别", http_status=500)


def _extract_pdf(path: Path) -> str:
    """PDF 逐页抽取文本（M1 接入 PyPDF2；当前为契约实现）。"""
    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:
        raise BizError(ERR_INGEST_FAILED, "缺少 PyPDF2 依赖，无法解析 PDF", http_status=500) from exc

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _extract_docx(path: Path) -> str:
    """DOCX 按段落 + 表格抽取文本；表格按行拼接，保留可检索的单元格值。"""
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise BizError(ERR_INGEST_FAILED, "缺少 python-docx 依赖，无法解析 DOCX", http_status=500) from exc

    document = docx.Document(str(path))
    parts: list[str] = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _cell_text(value: object, max_chars: int) -> str:
    """把 Excel/CSV 单元格转成可读文本，并做 Markdown 表格转义。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif isinstance(value, datetime):
        # 日期与时间分开输出，避免把 00:00:00 的日期写成带时间的字符串
        text = value.strftime("%Y-%m-%d %H:%M:%S") if value.time() != dt_time(0, 0) else value.strftime("%Y-%m-%d")
    elif isinstance(value, date):
        text = value.strftime("%Y-%m-%d")
    elif isinstance(value, dt_time):
        text = value.strftime("%H:%M:%S")
    elif isinstance(value, float) and value.is_integer():
        # 编号/数量在 Excel 里常是 1.0，去掉无意义的 .0
        text = str(int(value))
    else:
        text = str(value)

    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def _normalize_headers(raw_headers: list[object], cols: int) -> list[str]:
    headers = [_cell_text(v, settings.EXCEL_MAX_CELL_CHARS) for v in raw_headers[:cols]]
    if len(headers) < cols:
        headers.extend([""] * (cols - len(headers)))
    return [h if h else f"列{i + 1}" for i, h in enumerate(headers)]


def _find_header(rows: list[tuple[int, list[object]]]) -> tuple[int, list[object], list[str]]:
    """在前若干行里找表头：至少两个不同非空值的行才算表头。

    表格首行经常是跨列合并的大标题（同一个值铺满整行），不能当表头；把它收集为
    上下文，继续找真正的表头。若整张表只有一列/找不到多值行，则退回首个非空行。
    """
    first_nonempty: tuple[int, list[object]] | None = None
    context: list[str] = []
    for row_index, values in rows:
        nonempty = [_cell_text(v, 0) for v in values if _cell_text(v, 0)]
        if not nonempty:
            continue
        if first_nonempty is None:
            first_nonempty = (row_index, values)
        if len(set(nonempty)) >= 2:
            return row_index, values, context
        context.append(nonempty[0])
    if first_nonempty is not None:
        return first_nonempty[0], first_nonempty[1], []
    return 0, [], []


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """生成 Markdown 表格；列数至少为 1，避免空表产生非法结构。"""
    width = max(1, len(headers))
    if len(headers) < width:
        headers = headers + [f"列{i + 1}" for i in range(len(headers), width)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in rows:
        values = (row + [""] * width)[:width]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _cell_or_formula(cell, formula_cell) -> object:
    """取单元格值；公式没有缓存值时保留公式原文，避免丢内容。

    openpyxl 的 data_only=True 只返回 Excel 上次计算并保存的缓存值。若文件由
    程序生成或从未在 Excel 中打开，缓存为空，公式单元格会读成 None；此时用
    data_only=False 的工作簿补出 `=SUM(...)` 原文，检索时仍能命中公式本身。
    """
    value = cell.value
    if value is None and formula_cell is not None:
        formula_value = formula_cell.value
        if isinstance(formula_value, str) and formula_value.startswith("="):
            return formula_value
    return value


def _extract_xlsx(path: Path) -> str:
    """xlsx/xlsm：逐工作表读取，合并单元格按左上角填充，超限截断并标记。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise BizError(ERR_INGEST_FAILED, "缺少 openpyxl 依赖，无法解析 Excel", http_status=500) from exc

    wb = None
    wb_formula = None
    try:
        # 两份工作簿：data_only 拿缓存值，data_only=False 补无缓存公式的原文
        wb = load_workbook(str(path), data_only=True, read_only=False)
        wb_formula = load_workbook(str(path), data_only=False, read_only=False, keep_links=False)
    except Exception as exc:
        if wb is not None:
            wb.close()
        logger.error("Excel 打开失败 %s：%s", path.name, exc)
        raise BizError(
            ERR_INGEST_FAILED,
            "Excel 文件无法解析，请确认文件未损坏或未加密",
            http_status=500,
        ) from exc

    parts: list[str] = []
    try:
        for sheet_index, ws in enumerate(wb.worksheets):
            if sheet_index >= settings.EXCEL_MAX_SHEETS:
                parts.append(
                    f"> 已截断：工作簿共 {len(wb.worksheets)} 个工作表，仅解析前 "
                    f"{settings.EXCEL_MAX_SHEETS} 个"
                )
                break
            formula_ws = wb_formula[ws.title] if ws.title in wb_formula.sheetnames else None
            text = _sheet_to_markdown(ws, formula_ws)
            if text:
                parts.append(text)
    finally:
        wb.close()
        wb_formula.close()

    if not parts:
        raise BizError(ERR_INGEST_FAILED, "Excel 没有可提取内容", http_status=500)
    return "\n\n".join(parts)


def _sheet_to_markdown(ws, formula_ws=None) -> str:
    """单个工作表转 Markdown；返回空串表示该表无有效内容。

    formula_ws 用来补公式缓存为空时的公式原文，可为 None（如 xls 路径不需要）。
    """
    max_cols = max(1, min(int(ws.max_column or 1), settings.EXCEL_MAX_COLS))
    max_rows = max(1, settings.EXCEL_MAX_ROWS_PER_SHEET)
    max_row = int(ws.max_row or 0)

    def cell_of(r: int, c: int):
        return _cell_or_formula(
            ws.cell(r, c),
            formula_ws.cell(r, c) if formula_ws is not None else None,
        )

    # 合并单元格：先把左上角值铺到整个区域；只处理预览范围内的区域
    merged: dict[tuple[int, int], object] = {}
    for rng in ws.merged_cells.ranges:
        top_left = cell_of(rng.min_row, rng.min_col)
        for r in range(rng.min_row, min(rng.max_row, max_row) + 1):
            for c in range(rng.min_col, min(rng.max_col, max_cols) + 1):
                merged[(r, c)] = top_left

    def value_at(r: int, c: int):
        return merged.get((r, c), cell_of(r, c))

    # 首个“至少两个不同非空值”的行作为表头；跨列大标题被收集为上下文
    candidate_rows: list[tuple[int, list[object]]] = []
    for r in range(1, min(max_row, 20) + 1):
        candidate_rows.append((r, [value_at(r, c) for c in range(1, max_cols + 1)]))
    header_row, raw_headers, context = _find_header(candidate_rows)
    if header_row is None or not raw_headers:
        return ""

    headers = _normalize_headers(raw_headers, max_cols)
    rows: list[list[str]] = []
    for r in range(header_row + 1, max_row + 1):
        values = [value_at(r, c) for c in range(1, max_cols + 1)]
        if not any(_cell_text(v, 0) for v in values):
            continue
        rows.append([_cell_text(v, settings.EXCEL_MAX_CELL_CHARS) for v in values])
        if len(rows) >= max_rows:
            break

    truncated = max_row - header_row > max_rows
    title = f"## 工作表：{ws.title}"
    parts = [title]
    if context:
        parts.append("> 标题：" + " / ".join(context))
    table = _markdown_table(headers, rows)
    if truncated:
        table += (
            f"\n\n> 已截断：本工作表共约 {max_row - header_row} 行，"
            f"仅保留前 {max_rows} 行"
        )
    parts.append(table)
    return "\n\n".join(parts)


def _extract_xls(path: Path) -> str:
    """xls：xlrd 2.x 只支持旧版二进制；逐 sheet 转 Markdown。"""
    try:
        import xlrd
    except ImportError as exc:
        raise BizError(ERR_INGEST_FAILED, "缺少 xlrd 依赖，无法解析 XLS", http_status=500) from exc

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:
        logger.error("XLS 打开失败 %s：%s", path.name, exc)
        raise BizError(
            ERR_INGEST_FAILED,
            "Excel 文件无法解析，请确认文件未损坏或未加密",
            http_status=500,
        ) from exc

    parts: list[str] = []
    try:
        for sheet_index in range(book.nsheets):
            if sheet_index >= settings.EXCEL_MAX_SHEETS:
                parts.append(
                    f"> 已截断：工作簿共 {book.nsheets} 个工作表，仅解析前 "
                    f"{settings.EXCEL_MAX_SHEETS} 个"
                )
                break
            sheet = book.sheet_by_index(sheet_index)
            text = _xls_sheet_to_markdown(sheet)
            if text:
                parts.append(text)
    finally:
        book.release_resources()

    if not parts:
        raise BizError(ERR_INGEST_FAILED, "Excel 没有可提取内容", http_status=500)
    return "\n\n".join(parts)


def _xls_sheet_to_markdown(sheet) -> str:
    """xlrd sheet 转 Markdown；空表返回空串。"""
    if sheet.nrows <= 0 or sheet.ncols <= 0:
        return ""
    max_cols = max(1, min(int(sheet.ncols), settings.EXCEL_MAX_COLS))
    max_rows = max(1, settings.EXCEL_MAX_ROWS_PER_SHEET)

    header_row = None
    raw_headers: list[object] = []
    candidate_rows: list[tuple[int, list[object]]] = []
    for r in range(min(sheet.nrows, 20)):
        candidate_rows.append((r, [sheet.cell_value(r, c) for c in range(max_cols)]))
    header_row, raw_headers, context = _find_header(candidate_rows)
    if header_row is None or not raw_headers:
        return ""

    headers = _normalize_headers(raw_headers, max_cols)
    rows: list[list[str]] = []
    for r in range(header_row + 1, sheet.nrows):
        values = [sheet.cell_value(r, c) for c in range(max_cols)]
        if not any(_cell_text(v, 0) for v in values):
            continue
        rows.append([_cell_text(v, settings.EXCEL_MAX_CELL_CHARS) for v in values])
        if len(rows) >= max_rows:
            break

    truncated = sheet.nrows - header_row > max_rows
    title = f"## 工作表：{sheet.name}"
    parts = [title]
    if context:
        parts.append("> 标题：" + " / ".join(context))
    table = _markdown_table(headers, rows)
    if truncated:
        table += (
            f"\n\n> 已截断：本工作表共约 {sheet.nrows - header_row} 行，"
            f"仅保留前 {max_rows} 行"
        )
    parts.append(table)
    return "\n\n".join(parts)


def _read_csv_text(path: Path) -> str:
    """CSV 编码回退：utf-8-sig → utf-8 → gb18030；全部失败明确报错。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise BizError(
        ERR_INGEST_FAILED,
        "CSV 编码无法识别，请另存为 UTF-8 后上传",
        http_status=500,
    )


def _extract_csv(path: Path) -> str:
    """CSV 单表解析：识别分隔符，首行作表头，超限截断。"""
    text = _read_csv_text(path)
    if not text.strip():
        raise BizError(ERR_INGEST_FAILED, "CSV 文件为空，没有可提取内容", http_status=500)

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    max_rows = max(1, settings.EXCEL_MAX_ROWS_PER_SHEET)
    max_cols = max(1, settings.EXCEL_MAX_COLS)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    headers: list[str] = []
    rows: list[list[str]] = []
    truncated = False

    for row in reader:
        if not row:
            continue
        values = row[:max_cols]
        if not any(str(v).strip() for v in values):
            continue
        if not headers:
            headers = _normalize_headers(values, len(values))
            continue
        rows.append([_cell_text(v, settings.EXCEL_MAX_CELL_CHARS) for v in values])
        if len(rows) >= max_rows:
            truncated = True
            break

    if not headers:
        raise BizError(ERR_INGEST_FAILED, "CSV 没有可提取内容", http_status=500)

    table = _markdown_table(headers, rows)
    if truncated:
        table += f"\n\n> 已截断：仅保留前 {max_rows} 行"
    return f"## 工作表：CSV\n\n{table}"


def _extract_doc(path: Path) -> str:
    """`.doc`：先转 `.docx`，再复用 Word 提取；转换器缺失时明确报错。"""
    with tempfile.TemporaryDirectory(prefix="rag_doc_") as tmp:
        converted = _doc_to_docx(path, Path(tmp))
        return _extract_docx(converted)


def _doc_to_docx(path: Path, out_dir: Path) -> Path:
    """把旧版 .doc 转成 .docx；优先 LibreOffice，Windows 回退 Word COM。"""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        cmd = [
            soffice,
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(out_dir),
            str(path),
        ]
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=max(1, settings.DOC_CONVERT_TIMEOUT),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BizError(
                ERR_INGEST_FAILED,
                "DOC 转换超时，请确认文件未损坏或另存为 DOCX",
                http_status=500,
            ) from exc
        out = out_dir / f"{path.stem}.docx"
        if out.exists() and out.stat().st_size > 0:
            return out
        raise BizError(
            ERR_INGEST_FAILED,
            "DOC 转换失败，请确认文件未损坏或加密",
            http_status=500,
        )

    if os.name == "nt":
        return _doc_to_docx_with_word(path, out_dir)

    raise BizError(
        ERR_INGEST_FAILED,
        "当前环境缺少 DOC 转换器，请另存为 DOCX 后上传",
        http_status=500,
    )


def _doc_to_docx_with_word(path: Path, out_dir: Path) -> Path:
    """Windows Word COM 转换；禁用宏与弹窗，确保异常时释放 Word 进程。"""
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise BizError(
            ERR_INGEST_FAILED,
            "当前环境缺少 Word COM 依赖，请另存为 DOCX 后上传",
            http_status=500,
        ) from exc

    out = out_dir / f"{path.stem}.docx"
    pythoncom.CoInitialize()
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            # 3 = msoAutomationSecurityForceDisable，禁用宏
            word.AutomationSecurity = 3
        except Exception:  # noqa: BLE001 - 个别 Word 版本不支持该属性
            pass
        # 只用文件名调用：WPS 的 Word.Application 兼容层不接受额外的
        # ReadOnly/AddToRecentFiles/PasswordDocument 参数，会直接报“文档打开失败”。
        # 必须传绝对路径，WPS 对相对路径同样会报“文档打开失败”。
        document = word.Documents.Open(str(path.resolve()))
        # 16 = wdFormatDocumentDefault（.docx）
        document.SaveAs2(str(out), FileFormat=16)
    except Exception as exc:
        logger.error("DOC 转换失败 %s：%s", path.name, exc)
        raise BizError(
            ERR_INGEST_FAILED,
            "DOC 转换失败，请确认文件未损坏或未加密，或另存为 DOCX",
            http_status=500,
        ) from exc
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:  # noqa: BLE001
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass

    if not out.exists() or out.stat().st_size <= 0:
        raise BizError(ERR_INGEST_FAILED, "DOC 转换后没有生成有效内容", http_status=500)
    return out
