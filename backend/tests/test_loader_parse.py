"""文档解析层离线单测（零外部调用：不入库、不调 Embedding/LLM/Rerank）。

覆盖：Markdown 抽取、不支持后缀失败码、DOCX 真实段落抽取与空文档、
PDF 后缀分派与空文本透传（PDF 解析用 monkeypatch 桩，不依赖真实 PDF 二进制）、
以及“无可提取文本在向量化之前即失败（50003）”的边界语义。
"""
from __future__ import annotations

import pytest

from backend.services import loader, ingest
from backend.errors import BizError, ERR_FILE_TYPE, ERR_INGEST_FAILED


def test_md_utf8_extract(tmp_path):
    """Markdown 按 UTF-8 读取并保留正文。"""
    p = tmp_path / "annual_leave.md"
    p.write_text("# 年假政策\n满一年不满十年为五天。", encoding="utf-8")
    text = loader.extract_text(p)
    assert "年假政策" in text
    assert "满一年不满十年为五天" in text


def test_unsupported_extension_raises_40002(tmp_path):
    """不在白名单的后缀抛文件类型错误（40002），不落库。"""
    p = tmp_path / "note.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(BizError) as exc:
        loader.extract_text(p)
    assert exc.value.code == ERR_FILE_TYPE


def test_docx_extracts_paragraphs(tmp_path):
    """DOCX 真实生成临时文件，按段落抽取文本。"""
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("第一条 年假规定")
    doc.add_paragraph("第二条 考勤规则")
    p = tmp_path / "policy.docx"
    doc.save(str(p))

    text = loader.extract_text(p)
    assert "第一条 年假规定" in text
    assert "第二条 考勤规则" in text


def test_docx_empty_returns_empty_string(tmp_path):
    """无段落 DOCX 返回空串（不抛错；空文本判定交给 ingest 流水线）。"""
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    p = tmp_path / "empty.docx"
    doc.save(str(p))
    assert loader.extract_text(p) == ""


def test_pdf_dispatch_returns_extracted_text(monkeypatch, tmp_path):
    """.pdf 分派到 _extract_pdf 并原样返回抽取结果（桩替代真实二进制解析）。"""
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(loader, "_extract_pdf", lambda path: "PDF_BODY_TEXT")
    assert loader.extract_text(p) == "PDF_BODY_TEXT"


def test_pdf_dispatch_empty_text_passthrough(monkeypatch, tmp_path):
    """扫描件/无文字层 PDF 抽取为空串时透传空串，交由 ingest 判失败。"""
    p = tmp_path / "blank.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(loader, "_extract_pdf", lambda path: "")
    assert loader.extract_text(p) == ""


def test_ingest_empty_text_raises_50003_before_embedding(tmp_path, monkeypatch):
    """空白文档清洗/切片后为空：必须在调用向量化之前抛 50003，绝不触发 embedding。"""
    embed_called = {"flag": False}

    def _boom(*_args, **_kwargs):
        embed_called["flag"] = True
        raise AssertionError("空文本不应触发 embedding")

    monkeypatch.setattr(ingest.embed_mod, "get_embedder", _boom)

    p = tmp_path / "blank.md"
    p.write_text("   \n  \n\t\n", encoding="utf-8")
    with pytest.raises(BizError) as exc:
        ingest._build_rows(p)
    assert exc.value.code == ERR_INGEST_FAILED
    assert embed_called["flag"] is False


def test_xlsx_multisheet_merged_title_and_header(tmp_path):
    """xlsx：跨列大标题不能被当表头；工作表名、标题、真实表头都要保留。"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "差旅报销"
    ws.merge_cells("A1:C1")
    ws["A1"] = "差旅报销标准"
    ws.append(["项目", "上限", "备注"])
    ws.append(["住宿", 500, "一线城市"])
    ws2 = wb.create_sheet("日常报销")
    ws2.append(["项目", "上限"])
    ws2.append(["办公用品", 300])
    p = tmp_path / "policy.xlsx"
    wb.save(p)

    text = loader.extract_text(p)
    assert "## 工作表：差旅报销" in text
    assert "## 工作表：日常报销" in text
    assert "> 标题：差旅报销标准" in text
    assert "| 项目 | 上限 | 备注 |" in text
    assert "住宿" in text and "500" in text
    assert "办公用品" in text


def test_xlsx_truncates_beyond_limit(tmp_path, monkeypatch):
    """单表超过行数上限时保留前 N 行并给出截断标记。"""
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(loader.settings, "EXCEL_MAX_ROWS_PER_SHEET", 5)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["编号", "名称"])
    for i in range(12):
        ws.append([i, f"记录{i}"])
    p = tmp_path / "large.xlsx"
    wb.save(p)

    text = loader.extract_text(p)
    assert "记录4" in text
    assert "记录5" not in text
    assert "已截断" in text


def test_xlsx_formula_without_cache_keeps_formula_text(tmp_path):
    """公式单元格没有缓存值时保留公式原文，而不是输出空值。"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "公式"
    ws.append(["项目", "金额"])
    ws.append(["合计", "=100+200"])
    p = tmp_path / "formula.xlsx"
    wb.save(p)

    text = loader.extract_text(p)
    assert "=100+200" in text


def test_xls_extract(tmp_path):
    """xls：xlrd 读取旧版二进制表格。"""
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("人员")
    ws.write(0, 0, "姓名")
    ws.write(0, 1, "部门")
    ws.write(1, 0, "张三")
    ws.write(1, 1, "财务")
    p = tmp_path / "staff.xls"
    wb.save(str(p))

    text = loader.extract_text(p)
    assert "## 工作表：人员" in text
    assert "张三" in text and "财务" in text


def test_csv_utf8_and_gbk(tmp_path):
    """CSV：UTF-8 与 GBK 都能识别表头并转成表格文本。"""
    rows = "产品,数量,单价\n图片处理,12,3.5\n文档解析,8,2.0\n"
    utf8 = tmp_path / "utf8.csv"
    utf8.write_text(rows, encoding="utf-8")
    gbk = tmp_path / "gbk.csv"
    gbk.write_text(rows, encoding="gbk")

    for p in (utf8, gbk):
        text = loader.extract_text(p)
        assert "## 工作表：CSV" in text
        assert "图片处理" in text
        assert "3.5" in text


def test_doc_dispatch_converts_before_docx_extract(tmp_path, monkeypatch):
    """.doc 分派必须先转换再复用 docx 提取，不把二进制当纯文本读。"""
    docx = pytest.importorskip("docx")
    source = tmp_path / "converted.docx"
    doc = docx.Document()
    doc.add_paragraph("旧版 Word 段落")
    doc.save(source)

    def fake_convert(path, out_dir):
        assert path.suffix == ".doc"
        return source

    monkeypatch.setattr(loader, "_doc_to_docx", fake_convert)
    p = tmp_path / "legacy.doc"
    p.write_bytes(b"OLE fake")
    text = loader.extract_text(p)
    assert "旧版 Word 段落" in text
