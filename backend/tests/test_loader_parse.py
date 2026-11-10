"""文档解析层离线单测（零外部调用：不入库、不调 Embedding/LLM/Rerank）。

覆盖：Markdown 抽取、不支持后缀失败码、DOCX 真实段落抽取与空文档、
PDF 后缀分派与空文本透传（PDF 解析用 monkeypatch 桩，不依赖真实 PDF 二进制）、
以及“无可提取文本在向量化之前即失败（50003）”的边界语义。
"""
from __future__ import annotations

import pytest

from services import loader, ingest
from errors import BizError, ERR_FILE_TYPE, ERR_INGEST_FAILED


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
