"""WP5-2 模型分级路由离线单测：不连数据库、不调外部服务。"""
from __future__ import annotations

import pytest

from backend import llm
from backend.config import settings
from backend.services import app_config

PRIMARY = "primary-model"
SMALL = "small-model"


@pytest.fixture(autouse=True)
def _stable_primary(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MODEL", PRIMARY)
    monkeypatch.setattr(settings, "LLM_SMALL_MODEL", SMALL)


def _cache(monkeypatch, *, enabled=True, purposes="summary,memory_extract,title"):
    values = {
        "LLM_TEMPERATURE": 0.3,
        "LLM_MAX_TOKENS": 1024,
        "LLM_ROUTING_ENABLED": enabled,
        "LLM_SMALL_PURPOSES": purposes,
    }
    monkeypatch.setattr(app_config, "get_cached", lambda key: values.get(key))


def test_route_hit_returns_small_model(monkeypatch):
    _cache(monkeypatch)
    assert llm._model_for("summary") == SMALL
    assert llm._model_for("memory_extract") == SMALL
    assert llm._model_for("SUMMARY") == SMALL


def test_answer_and_workflow_stay_on_primary(monkeypatch):
    _cache(monkeypatch)
    assert llm._model_for("answer") == PRIMARY
    assert llm._model_for("workflow_plan") == PRIMARY


def test_disabled_or_unconfigured_falls_back_to_primary(monkeypatch):
    _cache(monkeypatch, enabled=False)
    assert llm._model_for("summary") == PRIMARY
    monkeypatch.setattr(settings, "LLM_SMALL_MODEL", "")
    _cache(monkeypatch)
    assert llm._model_for("summary") == PRIMARY


def test_unknown_purpose_falls_back_to_primary(monkeypatch):
    _cache(monkeypatch, purposes="summary")
    assert llm._model_for("memory_extract") == PRIMARY


class _Msg:
    content = "ok"


class _Choice:
    message = _Msg()


class _Resp:
    choices = [_Choice()]
    usage = None


class _FakeClient:
    def __init__(self):
        self.calls: list[str] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs["model"])
        if kwargs["model"] == SMALL:
            raise RuntimeError("small model down")
        return _Resp()


def test_small_model_failure_falls_back_and_logs_primary(monkeypatch):
    _cache(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    monkeypatch.setattr(llm, "get_small_client", lambda: fake)
    monkeypatch.setattr(llm, "_log_call", lambda *a, **k: None)

    text = llm.chat([{"role": "user", "content": "hi"}], purpose="summary")

    assert text == "ok"
    assert fake.calls == [SMALL, PRIMARY]


def test_small_model_without_base_or_key_falls_back_primary(monkeypatch):
    _cache(monkeypatch)
    fake = _FakeClient()
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    monkeypatch.setattr(llm, "get_small_client", lambda: None)
    monkeypatch.setattr(llm, "_log_call", lambda *a, **k: None)

    text = llm.chat([{"role": "user", "content": "hi"}], purpose="summary")

    assert text == "ok"
    assert fake.calls == [PRIMARY]
