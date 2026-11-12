"""B 档（V52）Rerank 增益与阈值复评：扩充可标注测试语料，纯向量 vs Rerank 双模式对比。

与 assess_retrieval.py（留存 4 篇样料基线）互补：本脚本用脚本内嵌的 `beval_*` 短文档扩大
候选规模，在【真实 Embedding 入库】与【可选真实 Rerank】下量化：
- Hit@1 / Hit@K / MRR / Top1 平均向量余弦分（score 恒为向量余弦分，Rerank 只改顺序、不改 score）；
- ON(Rerank) 相对 OFF(纯向量) 的 Hit@1/MRR 差值与首名翻转题数；
- 纯向量模式下 gold 命中分与非 gold 候选最高分的可分性，给出阈值松紧【建议】（只打印建议、
  绝不修改 settings.RETRIEVAL_MIN_SCORE 生产阈值）。

数据与费用纪律：
- 语料为脚本内嵌中文短文档（每篇一个独立主题与一个独特事实/数字，便于人工标注 gold），
  入库后【自动删除 DB 记录与本地落盘文件】，不污染留存 4 篇样料（doc 9/10/11/13）；
- 进程内临时切换 settings.RERANK_ENABLED，结束复位为原值，不写 .env；
- 不调用 LLM 生成答案、不写会话/记录表；测试结束权威计数复原 document=4/chunk=4/session=0/record=0。

运行（cwd=backend；需要 Embedding Key；Rerank 费用仅 ON 模式每题一次 Top-K 候选打分）：
    ..\\.venv\\Scripts\\python.exe scripts\\assess_rerank_gain.py
退出码：0=评测完成且清理复原；1=异常或清理未复原；2=数据库未就绪。
"""
from __future__ import annotations

import asyncio
import selectors
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.config import settings, ROOT_DIR  # noqa: E402
from backend.services import ingest, rag, vector_store as vs  # noqa: E402

# 留存基线（正式样料 4 篇、各 1 片；脚本只临时 +N 测试文档并复原）
BASELINE_DOCS = 4
BASELINE_CHUNKS = 4

# 内嵌测试语料：文件名 -> 正文（<500 字保证单片；独特事实/唯一数字便于标注 gold）
CORPUS: list[tuple[str, str]] = [
    ("beval_01_vpn.md",
     "远程办公与 VPN 接入规范。员工异地接入内网须先提交远程办公申请，经直属主管书面审批后开通 VPN 账号；"
     "登录 VPN 必须启用多因素认证（MFA），输入账号口令后再校验手机动态验证码；连续 5 次验证码错误会锁定账号 15 分钟；"
     "禁止将 VPN 凭证转借他人，下线时须主动断开连接，离开工位锁定终端。"),
    ("beval_02_password.md",
     "账号口令安全策略。所有系统口令长度不少于 8 位，且须同时包含大写字母、小写字母与数字三类字符；"
     "每 90 天强制更换，不得重复使用最近 5 次历史口令；同一账号连续输错 5 次口令将临时锁定 15 分钟；"
     "口令不得写在便签、聊天记录或明文表格中，发现疑似泄露须立即通过服务台重置。"),
    ("beval_03_print.md",
     "打印与耗材领用规范。日常打印默认黑白单面以节约成本；确需彩色或双面打印须在工单注明用途并经部门主管审批；"
     "A4 纸、硒鼓等耗材按月度定额领用，超出定额需说明原因；机密文件打印后须立即取走，废弃打印件使用碎纸机销毁，"
     "不得直接丢入普通废纸篓。"),
    ("beval_04_visitor.md",
     "访客接待与门禁登记流程。外部访客须提前 1 个工作日在线提交来访预约，注明被访人、事由与进出时段；"
     "到达后由前台核验身份、登记来访单并发放临时门禁卡；临时卡仅限授权区域和预约时段使用，离场交回并注销权限；"
     "被访人须全程陪同，不得让访客单独进入机房、档案室等受控区域。"),
    ("beval_05_meeting.md",
     "会议室预约使用规则。内部会议室最多提前 7 天在预订系统中预约，单次以 30 分钟为最小起约单位；"
     "预约后 15 分钟未签到系统自动释放；会议结束须关闭投影、空调与照明；高频大会议室由行政统一排期，"
     "临时冲突以主管审批的变更单为准。"),
    ("beval_06_asset.md",
     "办公设备领用与归还制度。笔记本电脑、显示器等设备领用时登记资产编号并签字确认；员工离职或调岗须在 3 个工作日内"
     "办理归还交接；设备非正常损坏按使用年限折旧计算赔偿；借用外设不得私自拆解、安装来历不明软件，"
     "归还时由资产管理员核验外观与配件完整性。"),
    ("beval_07_classification.md",
     "信息数据分级标准。公司数据由高到低划分为公开、内部、秘密、机密四个级别；公开数据可对外发布，内部数据仅限员工访问；"
     "秘密级数据外发须部门负责人审批，机密级数据外发须分管高管审批并加密；数据目录每季度复核一次，"
     "定级变更须留痕并通知安全管理员。"),
    ("beval_08_email.md",
     "外发邮件与附件规范。普通内部通知可直接群发；秘密级及以上内容不得作为明文附件外发，须压缩加密并随附单独审批；"
     "禁止在外部公开邮箱传输机密数据；单封邮件附件总量建议不超过 10MB；发现可疑钓鱼邮件不得点击链接，"
     "应转发安全邮箱并删除本地副本。"),
    ("beval_09_onboarding.md",
     "新员工入职安全培训。新员工到岗后 3 个工作日内须完成信息安全线上培训并参加考试，考试满分 100、80 分及格；"
     "未通过者一周内补考；培训覆盖口令策略、钓鱼识别、数据分级与设备使用规范；培训记录与考试成绩由人力资源部归档，"
     "作为开通系统权限的前置条件。"),
    ("beval_10_travel.md",
     "差旅与报销管理。境内出差机票原则上预订经济舱，高铁按二等座标准执行；住宿标准按城市级别分级；"
     "报销发票须在行程结束后 15 个工作日内提交，逾期须补充说明；差旅申请事前审批，超标部分原则上自付；"
     "报销单据须附审批单与真实票据，不得虚开或重复报销。"),
]

# 标注问答集：期望文件名 + 自然语言问题（围绕该篇独特事实）
CASES = [
    {"file": "beval_01_vpn.md",       "query": "远程接入内网的 VPN 是否需要多因素认证，验证码输错几次会锁多久"},
    {"file": "beval_02_password.md",   "query": "系统口令至少多少位、多久必须更换，输错几次会锁定"},
    {"file": "beval_03_print.md",     "query": "彩色打印需要什么审批，废弃机密打印件如何处理"},
    {"file": "beval_04_visitor.md",   "query": "外部访客提前多久预约，临时门禁卡离场后怎么处理"},
    {"file": "beval_05_meeting.md",   "query": "会议室最多提前几天预订，最小起约单位是多少"},
    {"file": "beval_06_asset.md",     "query": "离职归还办公设备的时限是几天，损坏如何赔偿"},
    {"file": "beval_07_classification.md", "query": "公司数据从高到低分哪几个级别，机密级外发需要谁审批"},
    {"file": "beval_08_email.md",     "query": "秘密级以上邮件附件外发为什么不能明文发送"},
    {"file": "beval_09_onboarding.md","query": "新员工安全培训考试多少分及格，未通过怎么办"},
    {"file": "beval_10_travel.md",   "query": "差旅报销发票须在行程结束多少个工作日内提交"},
]


async def _count_all() -> dict:
    pool = db.get_pool()
    out: dict[str, int] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for t in ("document", "document_chunk", "chat_session", "chat_record"):
                await cur.execute(f"SELECT count(*) FROM {t}")
                out[t] = int((await cur.fetchone())[0])
    return out


async def _list_beval() -> list[dict]:
    pool = db.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT doc_id, doc_name, file_path FROM document "
                "WHERE doc_name LIKE 'beval_%' ORDER BY doc_id"
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def _run_mode(label: str, enabled: bool, name2id: dict, cases: list[dict]) -> dict:
    settings.RERANK_ENABLED = enabled
    k = int(settings.TOP_K)
    n = len(cases)
    hit1 = hitk = 0
    rr = 0.0
    gold_scores: list[float] = []
    best_non: list[float] = []
    top1_names: list[str] = []
    rows: list[dict] = []

    for c in cases:
        exp_id = name2id[c["file"]]
        hits = await rag.retrieve(c["query"])
        rank = next((i + 1 for i, h in enumerate(hits) if h["doc_id"] == exp_id), None)
        gold_score = next((float(h["score"]) for h in hits if h["doc_id"] == exp_id), None)
        non_scores = [float(h["score"]) for h in hits if h["doc_id"] != exp_id]

        if rank is not None:
            hit1 += int(rank == 1)
            hitk += int(rank <= k)
            rr += 1.0 / rank
        if gold_score is not None:
            gold_scores.append(gold_score)
        if non_scores:
            best_non.append(max(non_scores))
        top1_names.append(hits[0]["doc_name"] if hits else "-")

        rows.append({
            "q": c["query"], "expect": c["file"], "rank": rank,
            "top1": top1_names[-1],
            "score": round(gold_score, 4) if gold_score is not None else None,
        })

    return {
        "label": label, "n": n, "k": k, "hit1": hit1, "hitk": hitk, "rr": rr,
        "hit1_rate": hit1 / n, "hitk_rate": hitk / n, "mrr": rr / n,
        "gold_scores": gold_scores, "best_non": best_non,
        "top1_names": top1_names, "rows": rows,
    }


def _mean(values: list[float]) -> float:
    return round(statistics.mean(values), 4) if values else 0.0


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先完成 M0")
        return 2

    saved_rerank = settings.RERANK_ENABLED
    before = await _count_all()
    evaluated = False
    exception: str | None = None

    try:
        print(f"=== B 档 Rerank 增益复评（基线 document={before['document']} chunk={before['document_chunk']}）===")
        name2id: dict[str, int] = {}
        for name, text in CORPUS:
            out = await ingest.process(name, text.encode("utf-8"))
            name2id[name] = out["doc_id"]
            print(f"[INGEST] {name} -> doc_id={out['doc_id']} status={out['status']} chunks={out['chunk_count']}")

        off = await _run_mode("OFF 纯向量余弦", False, name2id, CASES)
        on = await _run_mode("ON  gte-rerank-v2 重排", True, name2id, CASES)
        evaluated = True

        for m in (off, on):
            print(f"\n--- {m['label']}：n={m['n']} K={m['k']} ---")
            for r in m["rows"]:
                flag = "HIT1" if r["rank"] == 1 else ("TOPK" if r["rank"] is not None else "MISS")
                print(f"[{flag}] rank={r['rank'] or '-'} gold_score={r['score']} top1={r['top1']}")
            print(f"Hit@1={m['hit1_rate']:.3f}  Hit@{m['k']}={m['hitk_rate']:.3f}  MRR={m['mrr']:.3f}")

        # 增益对比
        d_hit1 = on["hit1_rate"] - off["hit1_rate"]
        d_mrr = on["mrr"] - off["mrr"]
        flips = sum(1 for a, b in zip(off["top1_names"], on["top1_names"]) if a != b)
        print("\n--- Rerank 增益对比（ON - OFF）---")
        print(f"ΔHit@1={d_hit1:+.3f}  ΔMRR={d_mrr:+.3f}  首名翻转题数={flips}/{off['n']}")

        # 纯向量分数分布与阈值建议（只建议、不改配置）
        g_min, g_mean, g_max = min(off["gold_scores"]), _mean(off["gold_scores"]), max(off["gold_scores"])
        n_max = max(off["best_non"]) if off["best_non"] else 0.0
        gap = g_min - n_max
        print("\n--- 纯向量分数分布（OFF）与阈值建议 ---")
        print(f"gold 命中分：min={g_min:.4f} mean={g_mean} max={g_max:.4f}（n={len(off['gold_scores'])}）")
        print(f"非 gold 候选最高分：max={n_max:.4f}（n={len(off['best_non'])}）；最小 gold-非gold 间隙={gap:+.4f}")
        if gap > 0.02 and off["hit1_rate"] >= 0.8:
            print(f"阈值建议：gold 与非 gold 存在正间隙（约 [{n_max:.4f}, {g_min:.4f})），后续大语料可在该区间"
                  f"小步试调 RETRIEVAL_MIN_SCORE；当前脚本【不修改生产阈值】，仍维持 0.0。")
        else:
            print("阈值建议：gold 与非 gold 分数交叠或间隙过小，不建议收紧阈值，维持 RETRIEVAL_MIN_SCORE=0.0；"
                  "是否收紧需更大真实业务语料复评。")
    except Exception as exc:  # noqa: BLE001
        exception = f"{type(exc).__name__}: {exc}"
        print(f"EVAL_EXCEPTION = {exception}")
    finally:
        settings.RERANK_ENABLED = saved_rerank
        # 清理：删除所有 beval_* 的 DB 记录与本地落盘文件（含中途失败残留）
        removed = 0
        rows = await _list_beval()
        for row in rows:
            try:
                await vs.delete_document(row["doc_id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[CLEANUP-WARN] delete doc_id={row['doc_id']}: {exc}")
            local = ROOT_DIR / row["file_path"]
            try:
                local.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[CLEANUP-WARN] unlink {local}: {exc}")
        after = await _count_all()
        await db.close_pool()

    cleanup_ok = (
        after["document"] == BASELINE_DOCS
        and after["document_chunk"] == BASELINE_CHUNKS
        and after["chat_session"] == 0
        and after["chat_record"] == 0
    )
    print(f"\n清理：删除 beval 文档 {removed} 篇；清理后计数 document={after['document']} "
          f"chunk={after['document_chunk']} session={after['chat_session']} record={after['chat_record']}")
    print("RERANK_CLEANUP =", "RESTORED(4,4,0,0)" if cleanup_ok else "NOT_RESTORED")
    print("RERANK_GAIN_RESULT =", "OK" if evaluated and cleanup_ok else "HAS_FAIL")
    return 0 if evaluated and cleanup_ok else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
