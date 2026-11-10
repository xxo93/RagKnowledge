"""M4 会话/记录仓储 + SSE 生成链路回归（F-08/F-10/F-11 / 架构 §3.4、§3.5、§6.3）。

覆盖：
A. chat_store 仓储闭环：建会话 -> 插 2 条记录(含 JSONB 工具/来源) -> 正序回填 -> recent 倒序
   -> memory.load_recent 角色成对正序 -> 会话列表 -> 删除会话 -> 会话不存在且记录级联清空；
B. 确定性工具离线回归：calculator=69104、doc_stats 图只读收敛；
C. knowledge_search 无命中（patch 工具，离线）sources 为空；
D. rag.stream_answer：空命中走固定话术；模拟 LLM 抛错时降级为来源摘要、不抛异常；
E. SSE 帧格式：event:/data:/空行分隔，对齐前端 api/sse.js；
F. 可选真实 LLM 流式：仅当环境变量 REAL_LLM=1 且已配 Key 才调用 DeepSeek，默认关闭零费用。

费用/联网纪律：
- 默认 settings.LLM_PLAN_ENABLED=False，plan 走关键词兜底，不调 LLM；
- 默认不触发 Embedding/LLM 外网调用；真实 LLM 文本生成由 F 段 opt-in；
- 仓储只使用临时会话，结束即删，不在库内留测试文档/记录。

运行（cwd=backend，.venv Python 3.12.7）：
    python scripts/check_chat_sse.py            # 默认离线 + 本地 PG 仓储闭环
    REAL_LLM=1 python scripts/check_chat_sse.py # 额外验证真实 DeepSeek 流式（少量 token）
退出码：0=ALL_PASS；1=HAS_FAIL。
"""
from __future__ import annotations

import asyncio
import os
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先于业务导入：默认关闭 LLM 首选决策，保持离线确定性；REAL_LLM=1 时开启
from config import settings  # noqa: E402

settings.LLM_PLAN_ENABLED = os.environ.get("REAL_LLM") == "1"

import db  # noqa: E402
from services import chat_store, memory, rag as rag_svc  # noqa: E402
import agent.graph as graph  # noqa: E402
import llm as llm_mod  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((f"{name}{(' | ' + detail) if detail else ''}", bool(ok)))


async def main() -> int:
    db_ready = await db.init_pool()
    tmp_sid: int | None = None

    # ---------------- A. 仓储闭环（本地 PG） ----------------
    if db_ready:
        try:
            created = await chat_store.create_session("M4回归临时会话")
            tmp_sid = int(created["session_id"])
            await chat_store.insert_record(
                tmp_sid, "问题A", "答案A",
                tool_call_info=[{"name": "calculator", "status": "done"}],
                source_ref=[{"doc_name": "d.md", "chunk_index": 0, "score": 0.9, "snippet": "来源摘要"}],
            )
            await chat_store.insert_record(tmp_sid, "问题B", "答案B")

            recs = await chat_store.list_records(tmp_sid)
            check("A1 插入2条且历史正序回填",
                  len(recs) == 2 and recs[0]["query"] == "问题A" and recs[1]["query"] == "问题B",
                  f"n={len(recs)}")

            r0 = recs[0]
            check("A2 JSONB 工具/来源回读为结构化对象",
                  isinstance(r0.get("tool_call_info"), list)
                  and isinstance(r0.get("source_ref"), list)
                  and r0["source_ref"][0]["doc_name"] == "d.md")

            recent = await chat_store.recent_records(tmp_sid, 5)
            check("A3 recent 倒序取最近记录",
                  len(recent) == 2 and recent[0]["query"] == "问题B", f"n={len(recent)}")

            msgs = await memory.load_recent(tmp_sid)
            roles = [m["role"] for m in msgs]
            check("A4 memory.load_recent 角色成对且时间正序",
                  roles == ["user", "assistant", "user", "assistant"], f"roles={roles}")

            sessions = await chat_store.list_sessions()
            check("A5 会话列表包含临时会话", any(s["session_id"] == tmp_sid for s in sessions))

            await chat_store.delete_session(tmp_sid)
            check("A6 删除会话后 get 返回 None", await chat_store.get_session(tmp_sid) is None)
            check("A7 删除会话后记录级联清空", await chat_store.list_records(tmp_sid) == [])
        finally:
            # 兜底清理：断言失败也尽力删除临时会话
            if tmp_sid is not None:
                try:
                    await chat_store.delete_session(tmp_sid)
                except Exception:  # noqa: BLE001
                    pass
    else:
        check("A 仓储闭环（DB 不可用，SKIP）", True, "db_ready=False")

    # ---------------- B. 确定性工具离线回归 ----------------
    st = await graph.run_once("1234*56")
    calc_obs = [o for o in (st.get("observations") or []) if o.get("tool") == "calculator"]
    check("B1 calculator 图结果=69104",
          bool(calc_obs) and calc_obs[-1].get("ok")
          and calc_obs[-1]["result"].get("result") == 69104
          and "69104" in (st.get("answer") or ""))

    st2 = await graph.run_once("知识库一共有多少份文档")
    check("B2 doc_stats 图只读并收敛",
          (st2.get("decision") or {}).get("tool") == "doc_stats" and st2.get("finished") is True)

    # ---------------- C. knowledge_search 无命中（patch，离线） ----------------
    orig_run = graph.search_tool.run

    async def _miss(_query: str) -> dict:
        return {"ok": True, "answer": "", "sources": [], "hits": [], "error": None}

    graph.search_tool.run = _miss
    st3 = await graph.run_once("不存在的生僻主题词xyz")
    graph.search_tool.run = orig_run
    check("C1 无命中 knowledge_search sources 为空",
          (st3.get("decision") or {}).get("tool") == "knowledge_search"
          and not st3.get("sources"))

    # ---------------- D. stream_answer 话术与降级 ----------------
    fa = None
    async for tok in rag_svc.stream_answer([], "任意问题"):
        fa = tok
        break
    check("D1 空命中 stream_answer 走固定话术", fa is not None and "未找到" in fa, (fa or "")[:24])

    async def _boom(_messages):
        # async generator：首次迭代即抛错，模拟真实 chat_stream 在生成期失败
        raise RuntimeError("simulated llm down")
        yield

    orig_cs = llm_mod.chat_stream
    llm_mod.chat_stream = _boom
    try:
        downgrade_parts: list[str] = []
        async for tok in rag_svc.stream_answer(
            [{"doc_name": "制度.md", "chunk_index": 0, "score": 0.8,
              "content": "正文片段用于降级摘要", "snippet": "片段摘要"}],
            "q",
        ):
            downgrade_parts.append(tok)
    finally:
        llm_mod.chat_stream = orig_cs
    joined = "".join(downgrade_parts)
    check("D2 LLM 异常降级为来源摘要且不抛异常",
          "参考" in joined and len(joined) > 0, f"chars={len(joined)}")

    # ---------------- E. SSE 帧格式（对齐前端 sse.js） ----------------
    from routers.chat import _frame  # noqa: E402

    f = _frame("token", {"text": "你好"})
    check("E1 SSE 帧 = event 行 + data JSON 行 + 空行结束",
          f.startswith("event: token\n") and 'data: ' in f and f.endswith("\n\n"))

    # ---------------- F. 可选真实 LLM 流式（默认 SKIP，零费用） ----------------
    if os.environ.get("REAL_LLM") == "1" and settings.LLM_API_KEY:
        async def _hit(_query: str) -> dict:
            return {
                "ok": True, "answer": "",
                "sources": [{"doc_name": "测试制度.md", "chunk_index": 0,
                             "score": 0.85, "snippet": "年假需提前3天申请"}],
                "hits": [{"doc_name": "测试制度.md", "chunk_index": 0, "score": 0.85,
                          "content": "公司年假制度：员工申请年假需提前3个工作日提交审批，由直属主管确认。",
                          "snippet": "年假需提前3天申请"}],
                "error": None,
            }

        graph.search_tool.run = _hit
        st4 = await graph.run_once("年假要提前多久申请")
        graph.search_tool.run = orig_run
        hits4 = []
        for obs in reversed(st4.get("observations") or []):
            if obs.get("tool") == "knowledge_search":
                hits4 = (obs.get("result") or {}).get("hits") or []
                break
        tokens: list[str] = []
        if hits4:
            async for t in rag_svc.stream_answer(hits4, "年假要提前多久申请", []):
                tokens.append(t)
        text = "".join(tokens)
        check("F1 真实 LLM 流式产出非空答案", len(text.strip()) > 0, f"chars={len(text)}")
    else:
        check("F 真实 LLM 流式（REAL_LLM=1 才执行；默认 SKIP 零费用）", True)

    await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("CHAT_SSE_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
