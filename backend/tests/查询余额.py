""" LLM（deepseek） 接口余额查询
api_key: DEEPSEEK_API_KEY
api_url: https://api.deepseek.com/user/balance
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# 从项目根目录的 .env 加载环境变量（.env 已在 .gitignore 中排除，不入库）
# 项目根目录 = backend/tests/../../
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ROOT_ENV)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("未找到环境变量 DEEPSEEK_API_KEY，请在项目根目录的 .env 中配置")

def get_deepseek_balance():
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    resp = requests.get("https://api.deepseek.com/user/balance", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        # 返回示例 {"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"10.23","granted_balance":"0","topped_up_balance":"10.23"}]}
        available = data["is_available"]
        balance_list = data["balance_infos"]
        for item in balance_list:
            print(f"币种：{item['currency']}")
            print(f"总余额：{item['total_balance']}")
            print(f"赠送余额：{item['granted_balance']}")
            print(f"充值余额：{item['topped_up_balance']}")
        print(f"账户是否可用：{available}")
        return data
    else:
        print(f"查询失败，状态码:{resp.status_code}, 响应:{resp.text}")
        return None


def chat_with_deepseek(prompt: str, min_balance=0.01):
    """
    先查余额，余额足够才发起对话请求
    :param prompt: 用户提问
    :param min_balance: 最低保留余额阈值，低于这个值不调用
    """
    balance_data = get_deepseek_balance()
    if balance_data is None:
        print("余额查询失败，中止请求")
        return None

    # 提取人民币余额
    cny_info = next((b for b in balance_data["balance_infos"] if b["currency"] == "CNY"), None)
    if not cny_info:
        print("未找到CNY余额信息")
        return None

    total_balance = float(cny_info["total_balance"])
    is_available = balance_data["is_available"]

    if total_balance < min_balance or not is_available:
        print(f"余额不足！当前余额：{total_balance}，阈值：{min_balance}，不发起API调用")
        return None

    # 余额充足，发起对话
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    resp = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload)
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    else:
        print(f"调用模型失败：{resp.status_code}, {resp.text}")
        return None


if __name__ == "__main__":
    # 测试余额查询
    get_deepseek_balance()
    # 测试对话，余额不足会自动跳过请求
    # res = chat_with_deepseek("你好")
    # print(res)
