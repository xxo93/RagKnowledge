"""意图决策纯函数单测（架构 §4.3 关键词兜底；离线、不连数据库/网络）。

运行（backend/）：python -m pytest tests/test_agent_plan.py -v
"""
from backend.agent.decision import keyword_decision, infer_stat_type


def test_calculator_route_for_pure_arithmetic():
    d = keyword_decision("1234*56")
    assert d["tool"] == "calculator"
    assert d["args"]["expression"] == "1234*56"


def test_calculator_handles_parentheses():
    d = keyword_decision("(1+2)*3")
    assert d["tool"] == "calculator"


def test_doc_stats_route_failed_docs():
    d = keyword_decision("文档里一共有多少份失败的")
    assert d["tool"] == "doc_stats"
    assert d["args"]["stat_type"] == "failed_docs"


def test_doc_stats_route_total_docs_default():
    d = keyword_decision("知识库一共有多少份文档")
    assert d["tool"] == "doc_stats"
    assert d["args"]["stat_type"] == "total_docs"


def test_infer_stat_type_variants():
    assert infer_stat_type("切片总数是多少") == "total_chunks"
    assert infer_stat_type("成功文档有多少") == "success_docs"
    assert infer_stat_type("随便问文档数量") == "total_docs"


def test_default_knowledge_search_for_question():
    assert keyword_decision("年假怎么申请")["tool"] == "knowledge_search"
    assert keyword_decision("请假制度")["tool"] == "knowledge_search"


def test_non_digit_non_arithmetic_not_misrouted_to_calc():
    # 纯符号/中文问句不得误判为算式
    assert keyword_decision("怎么算一算")["tool"] != "calculator"
    assert keyword_decision("")["tool"] == "knowledge_search"
