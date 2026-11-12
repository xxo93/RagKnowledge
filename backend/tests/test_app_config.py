"""运行时配置中心离线单测（不连数据库、不调外部服务）。

覆盖：
1. 库中无覆盖时，所有参数返回 .env / 代码默认值；
2. 类型与范围校验（浮点越界、整数给小数、布尔给 1、未知 key、空批量）统一 40001；
3. 保存后立即热生效（同一进程内下一条读取就是新值），并写 CONFIG_UPDATE 审计；
4. 恢复默认后回到默认值；
5. 缓存 TTL 内不重复读库。

数据库访问由 monkeypatch 替换为内存字典，因此可在无 pg 环境下跑。
"""
from __future__ import annotations

import asyncio

import pytest

from backend.errors import BizError, ERR_PARAM
from backend.services import app_config, audit


@pytest.fixture()
def env(monkeypatch):
    """用内存字典替换 app_config 的存储层，并重置进程内缓存。"""
    rows: dict[str, object] = {}
    audit_calls: list[dict] = []
    load_calls = {"n": 0}

    async def fake_load():
        load_calls["n"] += 1
        return dict(rows)

    async def fake_upsert(items, actor_user_id=None):
        for spec, value in items:
            rows[spec.key] = value

    async def fake_delete(keys):
        for key in list(keys):
            rows.pop(key, None)

    async def fake_audit(actor_user_id, action, target_type=None, target_id=None, result=1, detail=None):
        audit_calls.append({"action": action, "detail": detail, "target_id": target_id})

    monkeypatch.setattr(app_config, "_load_from_db", fake_load)
    monkeypatch.setattr(app_config, "_upsert_rows", fake_upsert)
    monkeypatch.setattr(app_config, "_delete_rows", fake_delete)
    monkeypatch.setattr(audit, "log_operation", fake_audit)
    monkeypatch.setattr(app_config, "_cache", {})
    monkeypatch.setattr(app_config, "_overridden", set())
    monkeypatch.setattr(app_config, "_cache_loaded_at", 0.0)
    monkeypatch.setattr(app_config, "_cache_loaded", False)
    monkeypatch.setattr(app_config, "_cache_error_logged", False)

    return {"rows": rows, "audit": audit_calls, "load_calls": load_calls}


def test_defaults_when_no_override(env):
    snap = asyncio.run(app_config.load_snapshot())
    labels = {g["key"]: g["label"] for g in snap["groups"]}
    assert labels == {
        "retrieval": "检索",
        "generation": "生成",
        "upload": "上传",
        "fallback": "兜底",
        "biz": "业务库",
    }

    params = {p["key"]: p for g in snap["groups"] for p in g["params"]}
    assert len(params) == 27
    # 默认值来自 settings（.env 调过则跟随 .env，这里只断言"等于默认且未标记覆盖"）
    for key, item in params.items():
        assert item["current"] == item["default"], key
        assert item["overridden"] is False
    assert params["RETRIEVAL_MIN_SCORE"]["current"] == pytest.approx(0.5)
    assert params["RETRIEVAL_CANDIDATES"]["current"] == 20
    assert params["TOP_K"]["current"] == 5
    # 上传组（M2 接入）5 项：单片/总上限、默认分块、默认并发、过期清理
    assert params["UPLOAD_CHUNK_MAX_MB"]["current"] == 32
    assert params["UPLOAD_TOTAL_MAX_MB"]["current"] == 100
    assert params["UPLOAD_DEFAULT_CHUNK_MB"]["current"] == 4
    assert params["UPLOAD_DEFAULT_CONCURRENCY"]["current"] == 3
    assert params["UPLOAD_CHUNK_STALE_HOURS"]["current"] == 24


def test_get_value_falls_back_to_default(env):
    assert asyncio.run(app_config.get_value("TOP_K")) == 5
    assert asyncio.run(app_config.get_value("LLM_TEMPERATURE")) == pytest.approx(0.3)


@pytest.mark.parametrize(
    "payload",
    [
        {"RETRIEVAL_MIN_SCORE": 1.5},          # 超过 1.0
        {"RETRIEVAL_MIN_SCORE": -0.1},         # 小于 0
        {"TOP_K": 0},                          # 低于 1
        {"TOP_K": 12},                         # 高于 10
        {"TOP_K": 3.5},                        # 整数参数给小数
        {"RERANK_ENABLED": 1},                 # 布尔参数给数字
        {"LLM_TEMPERATURE": "0.5"},            # 数字参数给字符串
        {"UNKNOWN_PARAM": 1},                  # 未登记的 key
        {},                                    # 空批量
    ],
)
def test_validation_rejects_bad_values(env, payload):
    with pytest.raises(BizError) as excinfo:
        asyncio.run(app_config.set_values(payload, actor_user_id=1))
    assert excinfo.value.code == ERR_PARAM
    assert env["rows"] == {}  # 校验失败不得落库


def test_save_takes_effect_immediately_and_audits(env):
    result = asyncio.run(
        app_config.set_values({"TOP_K": 8, "RETRIEVAL_MIN_SCORE": 0.62}, actor_user_id=1)
    )
    assert result["changed"] == ["RETRIEVAL_MIN_SCORE", "TOP_K"]

    assert asyncio.run(app_config.get_value("TOP_K")) == 8
    assert asyncio.run(app_config.get_value("RETRIEVAL_MIN_SCORE")) == pytest.approx(0.62)
    assert env["rows"] == {"TOP_K": 8, "RETRIEVAL_MIN_SCORE": 0.62}

    snap = asyncio.run(app_config.load_snapshot())
    params = {p["key"]: p for g in snap["groups"] for p in g["params"]}
    assert params["TOP_K"]["overridden"] is True
    assert params["TOP_K"]["current"] == 8
    # 未改的项仍是默认且未被标记覆盖
    assert params["RERANK_TOP_N"]["overridden"] is False

    assert env["audit"][-1]["action"] == audit.CONFIG_UPDATE
    assert env["audit"][-1]["detail"]["changes"]["TOP_K"] == {"old": 5, "new": 8}


def test_reset_restores_default(env):
    asyncio.run(app_config.set_values({"TOP_K": 9}, actor_user_id=1))
    assert env["rows"]["TOP_K"] == 9
    asyncio.run(app_config.reset(keys=["TOP_K"], actor_user_id=1))
    assert env["rows"] == {}
    assert asyncio.run(app_config.get_value("TOP_K")) == 5
    assert env["audit"][-1]["target_id"] == "reset"


def test_reset_by_group(env):
    asyncio.run(app_config.set_values({"TOP_K": 9, "LLM_TEMPERATURE": 0.9}, actor_user_id=1))
    asyncio.run(app_config.reset(group="generation", actor_user_id=1))
    # 只清掉生成组
    assert env["rows"] == {"TOP_K": 9}
    assert asyncio.run(app_config.get_value("LLM_TEMPERATURE")) == pytest.approx(0.3)


def test_cache_avoids_repeated_db_reads(env):
    asyncio.run(app_config.get_value("TOP_K"))
    asyncio.run(app_config.get_value("TOP_K"))
    asyncio.run(app_config.get_value("RERANK_TOP_N"))
    assert env["load_calls"]["n"] == 1

    asyncio.run(app_config.refresh(force=True))
    assert env["load_calls"]["n"] == 2


def test_upload_cross_field_validation(env):
    """上传组的三个尺寸参数必须自洽：单片 ≤ 总上限、默认分块 ≤ 单片上限。"""
    with pytest.raises(BizError) as excinfo:
        asyncio.run(app_config.set_values({"UPLOAD_CHUNK_MAX_MB": 200}, actor_user_id=1))
    assert excinfo.value.code == ERR_PARAM
    assert env["rows"] == {}

    with pytest.raises(BizError):
        asyncio.run(app_config.set_values({"UPLOAD_DEFAULT_CHUNK_MB": 40}, actor_user_id=1))
    assert env["rows"] == {}

    # 一起调大总上限与单片上限是允许的
    result = asyncio.run(
        app_config.set_values(
            {"UPLOAD_TOTAL_MAX_MB": 300, "UPLOAD_CHUNK_MAX_MB": 64}, actor_user_id=1
        )
    )
    assert result["changed"] == ["UPLOAD_CHUNK_MAX_MB", "UPLOAD_TOTAL_MAX_MB"]
    assert asyncio.run(app_config.get_value("UPLOAD_CHUNK_MAX_MB")) == 64


def test_upload_group_values_are_effective(env):
    asyncio.run(
        app_config.set_values(
            {"UPLOAD_DEFAULT_CHUNK_MB": 8, "UPLOAD_DEFAULT_CONCURRENCY": 5}, actor_user_id=1
        )
    )
    assert asyncio.run(app_config.get_value("UPLOAD_DEFAULT_CHUNK_MB")) == 8
    assert asyncio.run(app_config.get_value("UPLOAD_DEFAULT_CONCURRENCY")) == 5
    snap = asyncio.run(app_config.load_snapshot())
    params = {p["key"]: p for g in snap["groups"] for p in g["params"]}
    assert params["UPLOAD_DEFAULT_CONCURRENCY"]["overridden"] is True

    asyncio.run(app_config.reset(group="upload", actor_user_id=1))
    assert env["rows"] == {}
    assert asyncio.run(app_config.get_value("UPLOAD_DEFAULT_CHUNK_MB")) == 4


def test_fallback_group_enum_and_text(env):
    """兜底组：枚举只接受三种模式，话术 ≤500 字，改完立即生效并可整组恢复。"""
    assert asyncio.run(app_config.get_value("FALLBACK_MODE")) == "web"

    # 非法枚举值直接 40001，不夹取
    with pytest.raises(BizError) as excinfo:
        asyncio.run(app_config.set_values({"FALLBACK_MODE": "auto"}, actor_user_id=1))
    assert excinfo.value.code == ERR_PARAM

    result = asyncio.run(
        app_config.set_values(
            {
                "FALLBACK_MODE": "guide",
                "FALLBACK_MIN_HITS": 3,
                "FALLBACK_TEXT_GUIDE": "请补充想查的文档名或业务字段。",
            },
            actor_user_id=1,
        )
    )
    assert result["changed"] == ["FALLBACK_MIN_HITS", "FALLBACK_MODE", "FALLBACK_TEXT_GUIDE"]
    assert asyncio.run(app_config.get_value("FALLBACK_MODE")) == "guide"
    assert asyncio.run(app_config.get_value("FALLBACK_MIN_HITS")) == 3

    asyncio.run(app_config.reset(group="fallback", actor_user_id=1))
    assert env["rows"] == {}
    assert asyncio.run(app_config.get_value("FALLBACK_MODE")) == "web"


def test_search_source_is_configurable(env):
    """搜索源可热切换：枚举限三家、改完立即生效、越界/非法值直接 40001。"""
    assert asyncio.run(app_config.get_value("WEB_SEARCH_PROVIDER")) == "bailian"

    with pytest.raises(BizError) as excinfo:
        asyncio.run(app_config.set_values({"WEB_SEARCH_PROVIDER": "bing"}, actor_user_id=1))
    assert excinfo.value.code == ERR_PARAM

    with pytest.raises(BizError) as excinfo:
        asyncio.run(app_config.set_values({"WEB_SEARCH_TIMEOUT": 90}, actor_user_id=1))
    assert excinfo.value.code == ERR_PARAM

    changed = asyncio.run(
        app_config.set_values(
            {
                "WEB_SEARCH_PROVIDER": "bocha",
                "WEB_SEARCH_MAX_RESULTS": 3,
                "WEB_SEARCH_TIMEOUT": 12.5,
            },
            actor_user_id=1,
        )
    )
    assert changed["changed"] == [
        "WEB_SEARCH_MAX_RESULTS",
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_TIMEOUT",
    ]
    assert asyncio.run(app_config.get_value("WEB_SEARCH_PROVIDER")) == "bocha"
    assert asyncio.run(app_config.get_value("WEB_SEARCH_MAX_RESULTS")) == 3
    # 同步链路（web_search）能立刻读到新值，不需要额外刷新
    from backend.services import web_search

    assert web_search.provider() == "bocha"
    assert web_search.max_results() == 3

    asyncio.run(app_config.reset(group="fallback", actor_user_id=1))
    assert asyncio.run(app_config.get_value("WEB_SEARCH_PROVIDER")) == "bailian"
    assert web_search.provider() == "bailian"


