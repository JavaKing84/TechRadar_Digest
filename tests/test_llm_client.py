# -*- coding: utf-8 -*-
"""
LLM 客户端：服务商无关的「关思考」载荷选择 + 请求体组装。
[背景] 2026-11 底层模型从私有部署 qwen 换成 DeepSeek 官方 API。
       qwen(vLLM) 用 chat_template_kwargs.enable_thinking，
       DeepSeek 用顶层 thinking.type=disabled；发错参数不会报错、只会静默失效，
       所以这里锁死「按服务商选载荷」的行为，防止日后改回 qwen 时踩坑。
"""
from __future__ import annotations

from news_ai_science.stages.generate.client import (
    THINKING_OFF_PAYLOADS,
    LLMClient,
    resolve_thinking_style,
)


class _StubCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = type("M", (), {"content": "ok"})()
        choice = type("C", (), {"message": message, "finish_reason": "stop"})()
        return type("R", (), {"choices": [choice], "usage": None})()


class _StubClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _StubCompletions()})()


def test_auto_style_detects_deepseek():
    assert resolve_thinking_style("auto", "https://api.deepseek.com") == "deepseek"
    assert resolve_thinking_style(None, "https://api.deepseek.com/v1") == "deepseek"


def test_auto_style_falls_back_to_vllm():
    assert resolve_thinking_style("auto", "http://59.65.232.180:18000/v1") == "vllm"
    assert resolve_thinking_style("auto", "") == "vllm"


def test_explicit_style_wins_over_url():
    assert resolve_thinking_style("vllm", "https://api.deepseek.com") == "vllm"
    assert resolve_thinking_style("deepseek", "http://127.0.0.1:8000/v1") == "deepseek"


def test_none_style_disables_the_switch():
    assert resolve_thinking_style("none", "https://api.deepseek.com") == "none"
    assert resolve_thinking_style("off", "https://api.deepseek.com") == "none"


def test_deepseek_request_carries_thinking_disabled():
    """DeepSeek 走顶层 thinking.type=disabled，且不带 vLLM 的 chat_template_kwargs。"""
    llm = LLMClient(base_url="https://api.deepseek.com", api_key="k", model="deepseek-flash")
    stub = _StubClient()
    llm.client = stub

    assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"

    sent = stub.chat.completions.calls[0]
    assert sent["model"] == "deepseek-flash"
    assert sent["extra_body"] == THINKING_OFF_PAYLOADS["deepseek"]
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "chat_template_kwargs" not in sent["extra_body"]


def test_vllm_request_keeps_legacy_payload():
    """旧的私有部署 qwen 参数保持不变，回滚时无需改代码。"""
    llm = LLMClient(base_url="http://59.65.232.180:18000/v1", api_key="k", model="qwen3.6-35b-a3b")
    stub = _StubClient()
    llm.client = stub

    llm.chat([{"role": "user", "content": "hi"}])

    sent = stub.chat.completions.calls[0]
    assert sent["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_thinking_off_style_none_sends_no_extra_body():
    llm = LLMClient(base_url="https://api.deepseek.com", api_key="k",
                    model="deepseek-flash", thinking_off_style="none")
    stub = _StubClient()
    llm.client = stub

    llm.chat([{"role": "user", "content": "hi"}])

    assert "extra_body" not in stub.chat.completions.calls[0]
