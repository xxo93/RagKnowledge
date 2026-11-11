"""真实 LLM 联通测试（可选执行）。

历史问题：本文件原先是“导入即请求接口”的手工脚本，且从 `backend/tests/.env` 读配置。
后果是 `pytest backend/tests` 在收集阶段就抛 RuntimeError，整套用例无法运行；
若配了 Key 则每次跑测试都会真实请求外部 LLM 并产生费用。

现在改为标准 pytest 用例：
- 默认跳过，只有在设置 `RUN_LLM_TESTS=1` 且配置了对应 API Key 时才真实联网；
- 从项目根目录的 .env 读取配置，缺失时自动跳过，不再中断收集。
"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# 项目根目录 = backend/tests/../../
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ROOT_ENV)

LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="联网 LLM 测试默认跳过；设置 RUN_LLM_TESTS=1 后执行",
)


@pytest.mark.skipif(not LLM_API_KEY, reason="未配置 LLM_API_KEY/DEEPSEEK_API_KEY")
def test_llm_api_connectivity():
    """最小联通性验证：能拿到非空回复即视为链路可用（不校验具体内容）。"""
    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": "你好，用一句话介绍你自己"}],
        stream=False,
    )
    content = (resp.choices[0].message.content or "").strip()
    assert content, "LLM 返回内容为空"
