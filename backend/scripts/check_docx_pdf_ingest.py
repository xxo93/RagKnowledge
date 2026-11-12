"""J1：PDF / DOCX 真实联网入库 + 向量检索命中实测（授权产生少量 embedding 调用）。

与 check_ingest_real.py（Markdown）互补，端到端覆盖两类二进制格式：
1. 真实 DOCX（python-docx 原生中文）走 T1→解析(段落抽取)→清洗→切片→真实 text-embedding-v3→T2，
   返回 status=1、chunk_count>0；
2. 真实 PDF（合法最小文本层、Helvetica 英文独特词）经 PyPDF2 抽取同样走完入库链路；
3. rag.retrieve 向量召回能命中本次两类测试文档（为控费脚本内关闭 rerank，仅验证向量命中）；
4. 收尾删除测试文档 DB 记录与落盘原文件，计数复原到留存基线 document=4/chunk=4。

运行（cwd=backend）：..\\.venv\\Scripts\\python.exe scripts\\check_docx_pdf_ingest.py
退出码：0=全部通过；1=失败；2=数据库未就绪跳过。脚本内自动清理测试记录与本地文件。
"""
from __future__ import annotations

import asyncio
import io
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.config import settings, ROOT_DIR  # noqa: E402
from backend.errors import BizError  # noqa: E402
from backend.services import ingest, vector_store as vs, rag  # noqa: E402

# 实测目标：仅验证向量召回通道，关闭 rerank 以避免额外重排调用费
settings.RERANK_ENABLED = False

DOCX_NAME = "format_probe_visitor_seal.docx"
PDF_NAME = "format_probe_visitor_seal.pdf"
# 独特生造词，避免被留存的 4 篇 md 语义抢走
DOCX_QUERY = "访客门禁登记审批 印章外带审批 格式实测样例"
PDF_QUERY = "Visitor Badge Approval Seal Outbound Workflow XQ17"

# 留存基线（正式样料 4 篇、各 1 切片）；脚本只做 +2 测试文档并复原
BASELINE_DOCS = 4
BASELINE_CHUNKS = 4


def _build_docx_bytes() -> bytes:
    import docx as docx_lib

    document = docx_lib.Document()
    document.add_paragraph("访客门禁登记审批规定（格式实测样例 XQ17）")
    document.add_paragraph(
        "人员进入机房及办公敏感区域须提前填写访客登记单，注明来访事由、被访人与进出时段，"
        "经部门负责人书面审批后，由行政前台发放临时门禁卡；访客离场须交回临时门禁卡，"
        "并由前台注销临时通行权限，登记单按月归档备查。"
    )
    document.add_paragraph(
        "印章外带审批流程（格式实测样例 XQ17）：公章、合同章原则上不得带离办公场所；"
        "确需外带的须经总经理书面审批，登记印章编号、外带事由、携带人与归还时限，"
        "归队后由行政专员清点章体并核销审批单，未核销不得再次外带。"
    )
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _build_pdf_bytes() -> bytes:
    """构造合法最小单页 PDF（ASCII / Helvetica，PyPDF2 可抽文本层）。"""
    lines = [
        "Visitor Badge Approval Procedure XQ17",
        "Visitors must register with host and obtain a temporary badge",
        "Seal Outbound Approval Workflow XQ17",
        "Official seals leave office only after written director approval",
    ]
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream_lines = ["BT", "/F1 12 Tf", "72 740 Td"]
    for i, line in enumerate(lines):
        if i > 0:
            stream_lines.append("0 -18 Td")
        stream_lines.append(f"({line}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    out = b"%PDF-1.4\n"
    offsets: list[int] = [0]
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    size = len(objs) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out


async def _doc_file_paths(doc_ids: list[int]) -> dict[int, str]:
    pool = db.get_pool()
    result: dict[int, str] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT doc_id, file_path FROM document WHERE doc_id = ANY(%s)", (doc_ids,)
            )
            for row in await cur.fetchall():
                result[int(row[0])] = row[1]
    return result


async def _counts() -> tuple[int, int]:
    pool = db.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM document")
            docs = int((await cur.fetchone())[0])
            await cur.execute("SELECT count(*) FROM document_chunk")
            chunks = int((await cur.fetchone())[0])
    return docs, chunks


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先完成 M0")
        return 2

    results: list[tuple[str, bool]] = []
    created: list[int] = []
    try:
        before_docs, before_chunks = await _counts()
        results.append((f"运行前基线 document={before_docs} chunk={before_chunks}",
                        before_docs == BASELINE_DOCS and before_chunks == BASELINE_CHUNKS))

        # --- DOCX 真实入库 ---
        docx_bytes = _build_docx_bytes()
        docx_out = await ingest.process(DOCX_NAME, docx_bytes)
        created.append(docx_out["doc_id"])
        results.append((
            f"DOCX 入库 status={docx_out['status']} chunk_count={docx_out['chunk_count']}",
            docx_out["status"] == 1 and docx_out["chunk_count"] > 0,
        ))

        # --- PDF 真实入库 ---
        pdf_bytes = _build_pdf_bytes()
        pdf_out = await ingest.process(PDF_NAME, pdf_bytes)
        created.append(pdf_out["doc_id"])
        results.append((
            f"PDF 入库 status={pdf_out['status']} chunk_count={pdf_out['chunk_count']}",
            pdf_out["status"] == 1 and pdf_out["chunk_count"] > 0,
        ))

        # --- 向量召回命中验证 ---
        docx_hits = await rag.retrieve(DOCX_QUERY)
        docx_hit = next((h for h in docx_hits if str(h["doc_name"]).endswith(".docx")), None)
        results.append((
            f"DOCX 向量检索命中（返回 {len(docx_hits)} 片，docx 命中 "
            f"{docx_hit['doc_name'] if docx_hit else '无'}，score="
            f"{docx_hit['score'] if docx_hit else '-'}）",
            docx_hit is not None,
        ))

        pdf_hits = await rag.retrieve(PDF_QUERY)
        pdf_hit = next((h for h in pdf_hits if str(h["doc_name"]).endswith(".pdf")), None)
        results.append((
            f"PDF 向量检索命中（返回 {len(pdf_hits)} 片，pdf 命中 "
            f"{pdf_hit['doc_name'] if pdf_hit else '无'}，score="
            f"{pdf_hit['score'] if pdf_hit else '-'}）",
            pdf_hit is not None,
        ))

        # --- 清理：DB 记录 + 落盘原文件 ---
        paths = await _doc_file_paths(created)
        for doc_id in created:
            await vs.delete_document(doc_id)
            rel = paths.get(doc_id)
            if rel:
                abs_path = ROOT_DIR / rel
                if abs_path.exists():
                    abs_path.unlink()
        created.clear()

        after_docs, after_chunks = await _counts()
        results.append((
            f"清理后复原 document={after_docs} chunk={after_chunks}",
            after_docs == BASELINE_DOCS and after_chunks == BASELINE_CHUNKS,
        ))
    except BizError as exc:
        results.append((f"入库抛业务错误 code={exc.code} msg={exc.msg}", False))
    except Exception as exc:  # noqa: BLE001
        results.append((f"脚本异常：{exc}", False))
    finally:
        # 异常路径兜底：清 DB + 本地文件
        if created:
            try:
                paths = await _doc_file_paths(created)
                for doc_id in created:
                    try:
                        await vs.delete_document(doc_id)
                    except Exception:  # noqa: BLE001
                        pass
                    rel = paths.get(doc_id)
                    if rel:
                        abs_path = ROOT_DIR / rel
                        if abs_path.exists():
                            abs_path.unlink()
            except Exception:  # noqa: BLE001
                pass
        await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("DOCX_PDF_INGEST_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
