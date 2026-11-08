"""文档解析：PDF / DOCX / Markdown 文本提取（需求 F-01 第 2 步 / 架构 §2.5）。

- 按扩展名分派；解析库在函数内延迟导入，避免缺依赖时模块加载即失败；
- 扫描版 PDF 无文字层时返回空串，由 ingest 判定为“无可提取文本”失败（§8.2）；
- 本模块只负责“抽取原始文本”，清洗交给 cleaner.py，职责单一。
"""
from __future__ import annotations

from pathlib import Path

from errors import BizError, ERR_INGEST_FAILED

SUPPORTED_EXTS = {".pdf", ".docx", ".md"}


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
    """DOCX 按段落抽取文本（M1 接入 python-docx）。"""
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise BizError(ERR_INGEST_FAILED, "缺少 python-docx 依赖，无法解析 DOCX", http_status=500) from exc

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)
