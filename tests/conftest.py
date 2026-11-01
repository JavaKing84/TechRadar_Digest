# -*- coding: utf-8 -*-
"""
[文件] tests/conftest.py
[职责] 把所有用例与本机真实环境隔离：禁止读项目根的 .env，清空密钥类环境变量。
[背景] 本地开发目录里有真实的 .env（webhook / LLM key），若不隔离，
       它会通过 load_config 注入到临时配置里，导致
       「缺 webhook 应报 ConfigError」之类的用例永远不触发——
       测试结果会随"本机有没有 .env"而漂移。
"""
from __future__ import annotations

import pytest

SECRET_ENVS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_THINKING_OFF_STYLE",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "FEISHU_WEBHOOK_URL",
    "FEISHU_WEBHOOK_SECRET",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """默认关掉 .env 加载并清空密钥环境变量；用例内可自行 setenv 覆盖。"""
    monkeypatch.setenv("SKIP_DOTENV", "1")
    for key in SECRET_ENVS:
        monkeypatch.delenv(key, raising=False)
    yield
