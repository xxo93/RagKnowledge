"""M2 向量检索召回回归（真实 ApiEmbedder / text-embedding-v3，不打补丁）。

覆盖架构 §3.2 检索链路（仅召回与来源层，不含 M3 工具决策 / M4 LLM 生成）：
1. 两个主题不同的真实 Markdown 入库（status=1、chunk_count>0）；
2. rag.retrieve 语义召回：返回字段齐全、score 单调不增、目标主题文档出现在命中集合；
3. doc_ids 过滤：限定文档 B 时结果不得出现文档 A（F-04 检索范围 / iterative scan）；
4. 空问题短路返回 []；
5. 空库空命中：库为空时检索返回 []（F-09：无命中不调 LLM、不编造）。

运行（cwd=backend，账户 Embedding Key 可用）：
    ..\\.venv\\Scripts\\python.exe scripts\\check_retrieval.py
退出码：0=全部通过；1=失败；2=数据库未就绪。测试 DB 记录在脚本内自动删除。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.services import ingest, rag, vector_store as vs  # noqa: E402

# 两篇主题刻意不重叠：A=年假请假，B=考勤补卡/旷工
DOC_A = (
    "# 员工年假与请假制度\n\n"
    "员工入职满一年享有带薪年假五天，工作满十年的员工年假为十天，满二十年为十五天。\n"
    "年假申请需提前三个工作日在办公系统提交，经直属主管审批通过后方可休假；"
    "未休年假按公司规则折算，不得跨年度累积。\n"
    "病假需提供医院证明并按病假流程审批；事假当日不计薪，连续事假超过三天需部门负责人复核。\n"
)
DOC_B = (
    "# 考勤与补卡管理规则\n\n"
    "工作日迟到十五分钟以内的，可在当日内通过补卡流程登记；每人每月补卡次数不得超过三次，"
    "超出部分按迟到处理。\n"
    "旷工按缺勤半天至一天计扣工资，并影响当月全勤奖；连续旷工三天按严重违纪处理。\n"
    "标准工作时间为上午九点至下午六点，午休一小时；外勤需提前在系统报备并定位打卡。\n"
)

REQUIRED_FIELDS = {"doc_id", "doc_name", "chunk_index", "score", "snippet", "content"}


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先完成 M0")
        return 2

    results: list[tuple[str, bool]] = []
    doc_ids: list[int] = []
    try:
        # 1) 入库两篇主题文档
        out_a = await ingest.process("m2_annual_leave.md", DOC_A.encode("utf-8"))
        out_b = await ingest.process("m2_attendance.md", DOC_B.encode("utf-8"))
        doc_ids = [out_a["doc_id"], out_b["doc_id"]]
        id_a, id_b = out_a["doc_id"], out_b["doc_id"]
        results.append((
            f"两篇文档入库 status=1（A={out_a['status']}/{out_a['chunk_count']}片, "
            f"B={out_b['status']}/{out_b['chunk_count']}片）",
            out_a["status"] == 1 and out_b["status"] == 1
            and out_a["chunk_count"] > 0 and out_b["chunk_count"] > 0,
        ))

        # 2) 年假主题召回：字段齐全 + 降序 + A 出现在命中集合
        hits_a = await rag.retrieve("年假有多少天，需要提前多久申请")
        fields_ok = all(REQUIRED_FIELDS.issubset(h.keys()) for h in hits_a)
        scores_a = [h["score"] for h in hits_a]
        sorted_ok = all(scores_a[i] >= scores_a[i + 1] - 1e-9 for i in range(len(scores_a) - 1))
        a_hit = any(h["doc_id"] == id_a for h in hits_a)
        results.append((
            f"年假召回：{len(hits_a)} 片、字段齐全={fields_ok}、降序={sorted_ok}、命中文档A={a_hit}；"
            f"top1={hits_a[0]['doc_name'] if hits_a else '无'} score={scores_a[0] if scores_a else '-'}",
            bool(hits_a) and fields_ok and sorted_ok and a_hit,
        ))

        # 3) 考勤主题召回：B 出现在命中集合
        hits_b = await rag.retrieve("迟到补卡次数限制，旷工如何计扣")
        b_hit = any(h["doc_id"] == id_b for h in hits_b)
        fields_ok_b = all(REQUIRED_FIELDS.issubset(h.keys()) for h in hits_b)
        results.append((
            f"考勤召回：{len(hits_b)} 片、字段齐全={fields_ok_b}、命中文档B={b_hit}；"
            f"top1={hits_b[0]['doc_name'] if hits_b else '无'}",
            bool(hits_b) and fields_ok_b and b_hit,
        ))

        # 4) doc_ids 过滤：只限定 B，结果不得出现 A
        hits_filter = await rag.retrieve("年假请假申请", doc_ids=[id_b])
        leak_a = any(h["doc_id"] == id_a for h in hits_filter)
        results.append((
            f"doc_ids=[B] 过滤召回 {len(hits_filter)} 片、未泄漏文档A={not leak_a}",
            not leak_a,
        ))

        # 5) 空问题短路
        empty_hits = await rag.retrieve("   ")
        results.append((f"空问题短路返回 []（实际 {len(empty_hits)} 片）", empty_hits == []))

        # 6) 空库空命中（仅当删除本次两篇后库内无其他文档时执行，避免误判/误删他人数据）
        for did in doc_ids:
            await vs.delete_document(did)
        doc_ids = []
        total_docs = await vs.count_stat("total_docs")
        if total_docs == 0:
            after = await rag.retrieve("一个知识库中不存在的生僻主题词")
            results.append((f"空库检索返回 []（实际 {len(after)} 片，F-09 不编造）", after == []))
        else:
            results.append((f"空库断言跳过：库内仍有 {total_docs} 篇其他文档（非本次创建）", True))
    except Exception as exc:  # noqa: BLE001
        results.append((f"检索回归异常：{type(exc).__name__}: {exc}", False))
    finally:
        for did in doc_ids:
            try:
                await vs.delete_document(did)
            except Exception:  # noqa: BLE001
                pass
        await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("RETRIEVAL_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
