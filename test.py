# Please install OpenAI SDK first: `pip3 install openai`
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 从项目根目录的 .env 加载环境变量（.env 不入库）
load_dotenv(Path(__file__).resolve().parent / ".env")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("未找到环境变量 DEEPSEEK_API_KEY，请在项目根目录的 .env 中配置")

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

response = client.chat.completions.create(
    model="deepseek-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "你好"},
    ],
    stream=False,
    reasoning_effort="high",
    extra_body={"thinking": {"type": "enabled"}},
)

print(response.choices[0].message.content)
