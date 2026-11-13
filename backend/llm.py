"""统一 LLM 客户端（架构 ADR-009：OpenAI 兼容协议，Provider 可配）。

- 内部使用 openai SDK，通过 base_url 切换 DeepSeek / 豆包 / 通义 / OpenAI；
- 业务层只允许经本模块访问 LLM。
- 缺失密钥、依赖缺失、超时或接口异常统一转换为 BizError，由调用方决定降级策略。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Optional

from .config import settings
from .errors import BizError, ERR_LLM_UNAVAILABLE
from .services import io_log

logger = logging.getLogger("rag.llm")

_client = None
_small_client = None


def _build_client(api_key: str, base_url: str):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise BizError(ERR_LLM_UNAVAILABLE, "缺少 openai 依赖，请先安装 requirements.txt") from exc
    return OpenAI(api_key=api_key, base_url=base_url, timeout=settings.LLM_TIMEOUT)


def get_client():
    """惰性创建主模型 OpenAI 兼容客户端；未配置 Key 时抛出统一业务错误。"""
    global _client
    if _client is not None:
        return _client
    if not settings.LLM_API_KEY:
        raise BizError(ERR_LLM_UNAVAILABLE, "未配置 LLM_API_KEY，模型服务不可用", http_status=500)
    _client = _build_client(settings.LLM_API_KEY, settings.LLM_BASE_URL)
    return _client


def get_small_client():
    """轻任务独立客户端（如百炼 Qwen）；BaseURL/Key/模型名都来自 .env。

    三者任一为空即视为未配置，调用方回落主模型，保持旧环境兼容。
    """
    global _small_client
    if _small_client is not None:
        return _small_client
    if not (settings.LLM_SMALL_MODEL and settings.LLM_SMALL_BASE_URL and settings.LLM_SMALL_API_KEY):
        return None
    _small_client = _build_client(settings.LLM_SMALL_API_KEY, settings.LLM_SMALL_BASE_URL)
    return _small_client


def warm_small_model() -> bool:
    """启动时预热轻任务客户端（如百炼 Qwen），避免首轮改写承担建连/冷启动延迟。

    只发一次极短请求，不写 llm_call_log；失败只告警，不影响服务。
    """
    client = get_small_client()
    if client is None:
        return False
    try:
        client.chat.completions.create(
            model=settings.LLM_SMALL_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8,
            timeout=15,
            extra_body={"enable_thinking": False},
        )
        logger.info("轻任务模型已预热：%s", settings.LLM_SMALL_MODEL)
        return True
    except Exception:  # noqa: BLE001 - 预热失败交给运行时回落
        logger.warning("轻任务模型预热失败，将在首次调用时重试", exc_info=True)
        return False


def _generation_params(temperature: Optional[float]) -> tuple[float, int]:
    """解析生成参数：显式入参优先，否则读运行时配置（配置中心可在线调）。

    同步函数（chat 在线程里跑）不能 await 数据库，因此只读 app_config 的进程内缓存；
    缓存由检索/配置接口等异步入口按 5s TTL 刷新，保存配置时也会立即刷新。
    """
    try:
        from .services import app_config

        t = app_config.get_cached("LLM_TEMPERATURE")
        max_tokens = app_config.get_cached("LLM_MAX_TOKENS")
    except Exception:  # noqa: BLE001 - 配置读取失败不得影响生成
        t = settings.LLM_TEMPERATURE
        max_tokens = settings.LLM_MAX_TOKENS
    resolved_t = float(t) if temperature is None else float(temperature)
    return resolved_t, int(max_tokens)


def _model_for(purpose: str) -> str:
    """WP5-2：按用途选择模型；小模型未配置/路由关闭时回落主模型。

    模型名统一读 .env（settings），配置中心只保留“是否启用分级/用途清单”；
    避免 .env 与数据库两处模型名不一致。
    """
    primary = settings.LLM_MODEL
    small = str(settings.LLM_SMALL_MODEL or "").strip()
    if not settings.LLM_ROUTING_ENABLED or not small:
        return primary
    try:
        from .services import app_config

        enabled = bool(app_config.get_cached("LLM_ROUTING_ENABLED"))
        purposes = str(app_config.get_cached("LLM_SMALL_PURPOSES") or settings.LLM_SMALL_PURPOSES)
    except Exception:  # noqa: BLE001
        enabled = bool(settings.LLM_ROUTING_ENABLED)
        purposes = str(settings.LLM_SMALL_PURPOSES or "")
    if not enabled:
        return primary
    allowed = {p.strip().lower() for p in purposes.split(",") if p.strip()}
    return small if str(purpose).lower() in allowed else primary


def _client_for(purpose: str) -> tuple[object, str, bool]:
    """返回 (client, model, 是否小模型)；小模型未配全时直接回落主模型。"""
    model = _model_for(purpose)
    if model != settings.LLM_MODEL:
        client = get_small_client()
        if client is not None:
            return client, model, True
        logger.warning("小模型 %s 未配置完整 BaseURL/Key，回落主模型 %s", model, settings.LLM_MODEL)
    return get_client(), settings.LLM_MODEL, False


def _usage_of(resp) -> tuple[Optional[int], Optional[int]]:
    """从响应里取 usage；Provider 未返回时为 None，不能伪造成 0。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)


def _log_call(
    trace_id: str,
    purpose: str,
    started: float,
    status: str,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    error_code: Optional[str] = None,
    temperature: Optional[float] = None,
    model: Optional[str] = None,
) -> None:
    """一次 LLM 调用一条日志；同时把 token 累计到该 trace，供轮次记录汇总。"""
    try:
        io_log.note_llm_usage(trace_id, prompt_tokens, completion_tokens)
        io_log.enqueue_llm(
            trace_id=trace_id,
            purpose=purpose,
            model=model or settings.LLM_MODEL,
            temperature=temperature,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            status=status,
            error_code=error_code,
        )
    except Exception:  # noqa: BLE001 - 日志失败不得影响生成
        pass


def chat(
    messages: list[dict],
    *,
    temperature: Optional[float] = None,
    json_mode: bool = False,
    purpose: str = "answer",
    timeout: Optional[float] = None,
) -> str:
    """同步一次性生成；用于意图判断、短文本汇总等不需要 token 流的场景。

    temperature=None 时取配置中心的当前值（默认 0.3），不再硬编码在函数默认参数里。
    purpose 标记调用用途（plan/answer/replan/title/summary/memory_extract/workflow_*），
    用于 `llm_call_log` 的按用途统计。
    """
    client, model, is_small = _client_for(purpose)
    resolved_t, max_tokens = _generation_params(temperature)
    trace_id = io_log.get_trace_id()
    started = time.perf_counter()
    primary = settings.LLM_MODEL
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": resolved_t,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if is_small:
        # 百炼 Qwen3 默认开思考，会把 500ms 级改写拖到数秒；轻任务固定关思考
        kwargs["extra_body"] = {"enable_thinking": False}
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    try:
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception:  # noqa: BLE001
            if not is_small:
                raise
            # 小模型失败不影响体验：记一条失败日志后切回主模型客户端重试
            _log_call(trace_id, purpose, started, "error", None, None, "LLM_UNAVAILABLE", resolved_t, model)
            logger.warning("小模型 %s 调用失败，回落主模型 %s（purpose=%s）", model, primary, purpose)
            client = get_client()
            kwargs["model"] = primary
            kwargs.pop("extra_body", None)
            model = primary
            resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        _log_call(trace_id, purpose, started, "error", None, None, "LLM_UNAVAILABLE", resolved_t, model)
        raise BizError(ERR_LLM_UNAVAILABLE, "模型服务调用失败，请稍后重试", http_status=503) from exc
    prompt_tokens, completion_tokens = _usage_of(resp)
    _log_call(trace_id, purpose, started, "ok", prompt_tokens, completion_tokens, None, resolved_t, model)
    return resp.choices[0].message.content or ""


async def chat_stream(
    messages: list[dict],
    *,
    temperature: Optional[float] = None,
    purpose: str = "answer",
) -> AsyncIterator[str]:
    """流式生成 SSE token。

    OpenAI SDK 迭代是同步阻塞调用；这里在线程中生产内容，经 asyncio.Queue
    桥接为异步生成器。模型错误统一转换为 BizError，避免击穿 SSE 主链路。

    开启 `stream_options={"include_usage": True}` 以拿到最后一个 usage chunk；
    调用方提前中断（用户点停止）时记录 status='abort'，已生成的 token 数仍保留。
    """
    client = get_client()
    resolved_t, max_tokens = _generation_params(temperature)
    trace_id = io_log.get_trace_id()
    started = time.perf_counter()
    primary_model = settings.LLM_MODEL
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    outcome: dict = {"status": "abort", "prompt": None, "completion": None, "error": None}

    def _produce() -> None:
        try:
            stream = client.chat.completions.create(
                model=primary_model,
                messages=messages,
                temperature=resolved_t,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            for chunk in stream:
                # usage chunk 通常 choices 为空：必须先取 usage，再判断正文
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    outcome["prompt"] = getattr(usage, "prompt_tokens", None)
                    outcome["completion"] = getattr(usage, "completion_tokens", None)
                if not chunk.choices:
                    continue
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
            outcome["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 统一经队列上抛，转业务错误
            outcome["status"] = "error"
            outcome["error"] = "LLM_UNAVAILABLE"
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    worker = asyncio.create_task(asyncio.to_thread(_produce))
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise BizError(ERR_LLM_UNAVAILABLE, "模型流式生成失败，请稍后重试", http_status=503) from item
            yield item
    except asyncio.CancelledError:
        outcome["status"] = "abort"
        raise
    finally:
        if outcome["status"] != "ok" and outcome["error"] is None and outcome.get("prompt") is None:
            # 中途取消：生产端可能已收到 usage，等线程收尾后再记账（不阻塞主链路）
            pass
        _log_call(
            trace_id,
            purpose,
            started,
            outcome["status"],
            outcome.get("prompt"),
            outcome.get("completion"),
            outcome.get("error"),
            resolved_t,
            primary_model,
        )
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
