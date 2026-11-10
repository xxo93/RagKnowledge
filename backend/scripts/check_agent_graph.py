"""M3 Agent 状态图集成回归（架构 §4.2/§4.3/§4.4）。

覆盖（calculator 纯离线；doc_stats 只读 DB；knowledge_search 用确定性假结果，不依赖联网
Embedding/LLM——真实检索质量已由 M2 `check_retrieval.py` 覆盖，真实 LLM 决策/流式在 M4）：
1. calculator：1234*56=69104 走 calculator 节点，observation/tool_call/answer 正确；
2. calculator 失败语义：1/0 -> observation.ok=False、不抛异常、图正常收敛；
3. doc_stats：统计问句走 doc_stats 节点，真实只读 count_stat 对照、answer 含数值；
4. knowledge_search 命中分支：patch 工具返回 1 条来源 -> sources 回写、answer 汇总；
5. knowledge_search 无命中分支：sources=[] -> 走未找到兜底、不编造；
6. run_stream 事件结构：产出 tool/done 事件（M4 再补 token 事件）；
7. 状态结构：observation / tool_call 字段齐全，最终 finished=True。

运行（cwd=backend，`.venv` Python 3.12.7）：
    python scripts/check_agent_graph.py
退出码：0=全部通过（doc_stats 在 DB 不可用时记 SKIP 不判失败）；1=失败。
本脚本只读数据库、不写入、不删数据。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from config import settings  # noqa: E402
from services import vector_store as vs  # noqa: E402
import agent.graph as graph  # noqa: E402

# 离线确定性回归：关闭 LLM 首选决策，避免联网/产生 token 费用；LLM 决策由 check_chat_sse 真实验证
settings.LLM_PLAN_ENABLED = False

OBS_FIELDS = {"round", "tool", "ok", "result", "error"}
TC_FIELDS = {"name", "label", "status", "round"}

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((f"{name}{(' | ' + detail) if detail else ''}", bool(ok)))


async def main() -> int:
    db_ready = await db.init_pool()

    # ---- 1. calculator：1234*56 = 69104 ----
    st = await graph.run_once("1234*56")
    obs = (st.get("observations") or [])[-1]
    check("1 calculator 路由与结果",
          st.get("decision", {}).get("tool") == "calculator"
          and obs.get("ok") is True
          and obs.get("result", {}).get("result") == 69104
          and "69104" in (st.get("answer") or "")
          and st.get("finished") is True,
          f"decision={st.get('decision', {}).get('tool')} result={obs.get('result', {}).get('result')}")

    # ---- 2. calculator 失败（除零）不击穿图 ----
    st2 = await graph.run_once("1/0")
    obs2 = (st2.get("observations") or [])[-1]
    check("2 除零失败语义 ok=False 且收敛",
          obs2.get("tool") == "calculator" and obs2.get("ok") is False
          and "0" in (obs2.get("error") or "") and st2.get("finished") is True,
          f"error={obs2.get('error')}")

    # ---- 3. doc_stats：真实只读 count_stat 对照 ----
    if db_ready:
        expect = await vs.count_stat("total_docs")
        st3 = await graph.run_once("知识库一共有多少份文档")
        obs3 = (st3.get("observations") or [])[-1]
        check("3 doc_stats 路由且数值与 count_stat 一致",
              st3.get("decision", {}).get("tool") == "doc_stats"
              and obs3.get("ok") is True
              and obs3.get("result", {}).get("value") == expect
              and str(expect) in (st3.get("answer") or ""),
              f"stat={obs3.get('result')} answer={st3.get('answer')!r}")
    else:
        check("3 doc_stats 路由（DB 不可用，SKIP 真实值对照）", True, "db_ready=False")

    # ---- 4. knowledge_search 命中分支（patch 假结果，离线）----
    orig_run = graph.search_tool.run

    async def fake_hit(_query: str) -> dict:
        return {
            "ok": True,
            "answer": "（占位，M4 生成）",
            "sources": [{"doc_name": "年假制度.md", "chunk_index": 0, "score": 0.82, "snippet": "年假提前三天申请"}],
            "error": None,
        }

    async def fake_miss(_query: str) -> dict:
        return {"ok": True, "answer": "", "sources": [], "error": None}

    graph.search_tool.run = fake_hit
    st4 = await graph.run_once("年假怎么申请")
    graph.search_tool.run = orig_run
    src4 = st4.get("sources") or []
    check("4 knowledge_search 命中：sources 回写且汇总",
          st4.get("decision", {}).get("tool") == "knowledge_search"
          and len(src4) == 1 and src4[0].get("doc_name") == "年假制度.md"
          and "1 条" in (st4.get("answer") or ""),
          f"sources={len(src4)}")

    # ---- 5. knowledge_search 无命中：不编造 ----
    graph.search_tool.run = fake_miss
    st5 = await graph.run_once("一个知识库中不存在的生僻主题词")
    graph.search_tool.run = orig_run
    check("5 knowledge_search 无命中：sources 空且 answer 含未找到语义",
          st5.get("decision", {}).get("tool") == "knowledge_search"
          and not st5.get("sources")
          and ("未找到" in (st5.get("answer") or "")),
          f"answer={st5.get('answer')!r}")

    # ---- 6. run_stream 事件结构（tool / done；token 留 M4）----
    events = [e async for e in graph.run_stream("1234*56")]
    types = [e["type"] for e in events]
    tool_ev = next((e for e in events if e["type"] == "tool"), {})
    done_ev = next((e for e in events if e["type"] == "done"), {})
    check("6 run_stream 产出 tool 与 done 事件",
          "tool" in types and "done" in types
          and tool_ev.get("name") == "calculator"
          and tool_ev.get("status") == "done"
          and "elapsed_ms" in done_ev,
          f"types={types}")

    # ---- 7. 状态结构齐全 + 全部 finished ----
    def well_formed(state: dict) -> bool:
        for o in state.get("observations") or []:
            if not OBS_FIELDS.issubset(o.keys()):
                return False
        for t in state.get("tool_calls") or []:
            if not TC_FIELDS.issubset(t.keys()):
                return False
        return state.get("finished") is True

    all_well = all(well_formed(s) for s in (st, st2, st4, st5))
    check("7 observation/tool_call 字段齐全且 finished=True", all_well)

    await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("AGENT_GRAPH_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
