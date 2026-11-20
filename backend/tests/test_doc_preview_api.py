"""V5-2 原文档预览/下载接口离线回归：权限、40407、路径安全、文件响应。"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.config import ROOT_DIR, settings
from backend.services import account_store, audit, preview as preview_service, vector_store as vs


def _user(role: str = "admin") -> dict:
    return {
        "user_id": 1,
        "username": "admin",
        "display_name": "系统管理员",
        "role": role,
        "status": 1,
    }


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture()
def upload_file():
    """在真实 upload_dir 下建临时原件，测试结束删除；不写数据库。"""
    names: list[Path] = []

    def _make(name: str, content: bytes) -> dict:
        stored = f"_test_{uuid.uuid4().hex[:8]}{Path(name).suffix.lower()}"
        path = settings.upload_dir / stored
        path.write_bytes(content)
        names.append(path)
        rel = path.relative_to(ROOT_DIR).as_posix()
        return {
            "doc_id": 99,
            "doc_name": name,
            "file_path": rel,
            "file_size": len(content),
            "status": 1,
            "chunk_count": 2,
            "summary": "",
            "create_time": None,
        }

    yield _make
    for path in names:
        path.unlink(missing_ok=True)


def _login_as(monkeypatch, role: str = "admin"):
    user = _user(role)

    async def fake_get_active_session(token_hash, renew_ttl_seconds=0):
        return user

    monkeypatch.setattr(account_store, "get_active_session", fake_get_active_session)
    return user


def test_preview_markdown_for_member(client: TestClient, monkeypatch, upload_file):
    """.md 预览对普通已登录用户开放，返回 markdown 原文。"""
    _login_as(monkeypatch, role="member")
    meta = upload_file("制度.md", "# 差旅制度\n住宿上限 500 元。".encode("utf-8"))

    async def fake_meta(doc_id):
        return meta

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/preview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["kind"] == "markdown"
    assert "住宿上限 500" in body["data"]["content"]
    assert body["data"]["doc_name"] == "制度.md"


def test_preview_xlsx_returns_sheets(client: TestClient, monkeypatch, upload_file):
    """Excel 预览返回工作表名、表头、行和总行数，并返回带合并/样式的 HTML。"""
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "差旅报销"
    ws.append(["项目", "上限"])
    ws.append(["住宿", 500])
    ws["A1"].font = Font(bold=True, color="FFFFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="FF1F4E79")
    ws.merge_cells("A3:B3")
    ws["A3"] = "合并标题"
    ws.column_dimensions["A"].width = 18
    path = settings.upload_dir / f"_test_{uuid.uuid4().hex[:8]}.xlsx"
    wb.save(path)
    meta = {
        "doc_id": 99, "doc_name": "报销.xlsx",
        "file_path": path.relative_to(ROOT_DIR).as_posix(),
        "file_size": path.stat().st_size, "status": 1, "chunk_count": 1,
        "summary": "", "create_time": None,
    }
    try:
        _login_as(monkeypatch)
        async def fake_meta(doc_id):
            return meta
        monkeypatch.setattr(vs, "get_document_meta", fake_meta)
        client.cookies.set("rag_session", "test-token")
        resp = client.get("/api/docs/99/preview")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "sheet"
        assert data["sheets"][0]["name"] == "差旅报销"
        assert data["sheets"][0]["headers"] == ["项目", "上限"]
        assert data["sheets"][0]["rows"][0] == ["住宿", "500"]
        assert data["sheets"][0]["total_rows"] == 2
        assert "colspan=\"2\"" in data["sheets"][0]["html"]
        assert "font-weight:600" in data["sheets"][0]["html"]
        assert "background:#1F4E79" in data["sheets"][0]["html"]
        assert '<th class="sheet-colhead">A</th>' in data["sheets"][0]["html"]
        assert '<th class="sheet-rowhead">1</th>' in data["sheets"][0]["html"]
        assert '<th class="sheet-corner"></th>' in data["sheets"][0]["html"]
        assert data["sheets"][0]["html_total_rows"] == 3
    finally:
        path.unlink(missing_ok=True)


def _write_sample_docx(path: Path) -> None:
    """生成同时含标题、加粗与表格的 DOCX，供按页预览与 HTML 降级测试复用。"""
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("差旅制度", level=1)
    p = doc.add_paragraph("住宿上限 ")
    p.add_run("500 元").bold = True
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "住宿"
    table.cell(1, 1).text = "500"
    doc.save(path)


def _docx_meta(path: Path) -> dict:
    return {
        "doc_id": 99, "doc_name": "差旅制度.docx",
        "file_path": path.relative_to(ROOT_DIR).as_posix(),
        "file_size": path.stat().st_size, "status": 1, "chunk_count": 1,
        "summary": "", "create_time": None,
    }


def test_preview_docx_returns_page_payload(client: TestClient, monkeypatch):
    """DOCX 默认返回按页渲染所需字节，前端用 docx-preview 还原分页、页边距与表格。"""
    import base64

    path = settings.upload_dir / f"_test_{uuid.uuid4().hex[:8]}.docx"
    _write_sample_docx(path)
    meta = _docx_meta(path)
    try:
        _login_as(monkeypatch)
        async def fake_meta(doc_id):
            return meta
        monkeypatch.setattr(vs, "get_document_meta", fake_meta)
        client.cookies.set("rag_session", "test-token")
        resp = client.get("/api/docs/99/preview")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "docx"
        assert data["docx_base64"]
        assert base64.b64decode(data["docx_base64"]).startswith(b"PK")
        assert data["content"] == ""
        assert data["truncated"] is False
    finally:
        path.unlink(missing_ok=True)


def test_preview_docx_html_fallback(client: TestClient, monkeypatch):
    """`?html=1` 返回语义 HTML，供前端按页渲染失败时降级。"""
    pytest.importorskip("mammoth")
    path = settings.upload_dir / f"_test_{uuid.uuid4().hex[:8]}.docx"
    _write_sample_docx(path)
    meta = _docx_meta(path)
    try:
        _login_as(monkeypatch)
        async def fake_meta(doc_id):
            return meta
        monkeypatch.setattr(vs, "get_document_meta", fake_meta)
        client.cookies.set("rag_session", "test-token")
        resp = client.get("/api/docs/99/preview?html=1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "html"
        assert "<h1>差旅制度</h1>" in data["content"]
        assert "<strong>500 元</strong>" in data["content"]
        assert "doc-table" in data["content"]
    finally:
        path.unlink(missing_ok=True)


def test_pdf_preview_inline(client: TestClient, monkeypatch, upload_file):
    """PDF 走原生 inline，不返回 JSON 包络。"""
    _login_as(monkeypatch, role="member")
    meta = upload_file("制度.pdf", b"%PDF-1.4 fake")

    async def fake_meta(doc_id):
        return meta

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/preview")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.headers["content-disposition"].startswith("inline")


def test_preview_missing_file_returns_40407(client: TestClient, monkeypatch):
    """数据库有记录但原件被清理，返回 40407；列表记录仍可删除。"""
    _login_as(monkeypatch)

    async def fake_meta(doc_id):
        return {
            "doc_id": 99, "doc_name": "gone.md",
            "file_path": "data/uploads/not_exists/gone.md",
            "file_size": 10, "status": 1, "chunk_count": 1,
            "summary": "", "create_time": None,
        }

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/preview")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40407


@pytest.mark.parametrize("bad_path", ["../../Windows/win.ini", "C:/Windows/win.ini"])
def test_preview_rejects_path_escape(client: TestClient, monkeypatch, bad_path):
    _login_as(monkeypatch)

    async def fake_meta(doc_id):
        return {
            "doc_id": 99, "doc_name": "evil.md", "file_path": bad_path,
            "file_size": 1, "status": 1, "chunk_count": 1,
            "summary": "", "create_time": None,
        }

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/preview")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40407


def test_member_cannot_download(client: TestClient, monkeypatch):
    _login_as(monkeypatch, role="member")
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/download")
    assert resp.status_code == 403
    assert resp.json()["code"] == 40303


def test_admin_download_and_audit(client: TestClient, monkeypatch, upload_file):
    """管理员下载返回原文件字节，中文名走 filename*，并写下载审计。"""
    _login_as(monkeypatch)
    meta = upload_file("报销制度.md", b"hello-original")
    captured: dict = {}

    async def fake_meta(doc_id):
        return meta

    async def fake_audit(*args, **kwargs):
        captured["action"] = args[1]
        captured["detail"] = args[5]

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    monkeypatch.setattr(audit, "log_operation", fake_audit)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/download")

    assert resp.status_code == 200
    assert resp.content == b"hello-original"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=utf-8''" in disposition.lower()
    assert captured["action"] == audit.DOC_DOWNLOAD
    assert captured["detail"] == {"file_name": "报销制度.md", "bytes": len(b"hello-original")}


def test_preview_processing_doc_returns_40001(client: TestClient, monkeypatch):
    _login_as(monkeypatch)

    async def fake_meta(doc_id):
        return {
            "doc_id": 99, "doc_name": "处理中.docx", "file_path": "x",
            "file_size": 1, "status": 0, "chunk_count": 0,
            "summary": "", "create_time": None,
        }

    monkeypatch.setattr(vs, "get_document_meta", fake_meta)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/99/preview")
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert "处理中" in resp.json()["msg"]
