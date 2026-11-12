"""Rerank 层联网探针（不依赖数据库，仅验证 services.reranker.maybe_rerank）。

验证两件事：
1. RERANK_ENABLED=true 时调用百炼 gte-rerank-v2，候选按 relevance_score 降序，并附 rerank_score；
2. 接口不可达/报错时容错降级为原向量召回顺序，绝不抛错中断（重排是增强项）。

用法（cwd=backend）：python scripts/check_rerank.py
成功末行打印 RERANK_PROBE_OK。本脚本不读取/打印任何 API Key。
"""
from __future__ import annotations

import asyncio

from backend.config import settings
from backend.services import reranker

# 与真实来源结构对齐的最小候选（仅用于重排打分，不入库、不联网 Embedding）
CANDIDATES = [
    {
        "doc_name": "leave_policy.md",
        "chunk_index": 0,
        "score": 0.78,
        "content": "年假申请需提前三个工作日在办公系统提交请假单，填写起止日期与工作交接说明，经直属主管审批通过后方可休假；超过三天的年假还需部门负责人复核。",
        "snippet": "年假申请流程",
    },
    {
        "doc_name": "attendance_rule.md",
        "chunk_index": 0,
        "score": 0.58,
        "content": "标准工作时间为上午九点至下午六点；工作日迟到十五分钟以内可在当日内通过补卡流程登记，每人每月补卡不超过三次。",
        "snippet": "考勤补卡规则",
    },
    {
        "doc_name": "security_policy.md",
        "chunk_index": 0,
        "score": 0.48,
        "content": "机密及以上文档不得通过个人邮箱、即时聊天工具或公共网盘外发；对外提供须经数据安全负责人审批并加水印、脱敏。",
        "snippet": "信息安全保密分级",
    },
    {
        "doc_name": "it_account.md",
        "chunk_index": 0,
        "score": 0.45,
        "content": "居家或外勤远程访问内网需先申请 VPN 账号，在 IT 服务台提交申请单并注明用途与时长，审批通过后下载客户端、使用双因素认证登录。",
        "snippet": "VPN 与账号管理",
    },
]


async def main() -> None:
    settings.RERANK_ENABLED = True
    query = "年假申请要提前多久、走什么审批流程"

    before = [c["doc_name"] for c in CANDIDATES]
    ranked = await reranker.maybe_rerank(query, [dict(c) for c in CANDIDATES])
    after = [(h["doc_name"], h.get("rerank_score")) for h in ranked]

    print("MODEL:", settings.RERANK_MODEL)
    print("BEFORE_ORDER:", before)
    print("AFTER_ORDER_WITH_SCORE:", after)

    score_seq = [s for _, s in after if s is not None]
    score_monotone = all(score_seq[i] >= score_seq[i + 1] for i in range(len(score_seq) - 1))
    print("RERANK_SCORE_MONOTONE_DESC:", score_monotone)

    # 容错：把端点改成不可达地址，确认返回原序且不抛异常
    saved_url = settings.RERANK_BASE_URL
    settings.RERANK_BASE_URL = "https://invalid.example.invalid/api/v1/services/rerank/text-rerank/text-rerank"
    fallback = await reranker.maybe_rerank(query, [dict(c) for c in CANDIDATES])
    settings.RERANK_BASE_URL = saved_url
    fallback_kept = [h["doc_name"] for h in fallback] == before
    print("FALLBACK_ON_ERROR_KEEPS_ORIGINAL_ORDER:", fallback_kept)

    ok = bool(after) and score_monotone and fallback_kept and len(ranked) == len(CANDIDATES)
    print("RERANK_PROBE_OK" if ok else "RERANK_PROBE_FAIL")


if __name__ == "__main__":
    asyncio.run(main())
