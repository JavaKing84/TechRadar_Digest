# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] stages/generate/client.py
[职责] OpenAI 兼容协议的薄封装：超时/重试/熔断/关思考模式。
=====================================================================
"""
from __future__ import annotations

import time

from news_ai_science.core.errors import LLMError
from news_ai_science.core.logger import get_logger

logger = get_logger(__name__)

# 默认输出预算；config.yaml 的 llm.max_tokens 会覆盖它
DEFAULT_MAX_TOKENS = 6000


class LLMClient:
    """
    # [表面] 包一层 OpenAI SDK
    # [本质] 私有部署 qwen 走 OpenAI 兼容协议；401/403 立刻熔断，不白等
    # [后果] 模型抖动时最多耗 (重试+1)×timeout 秒就进入 fallback
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 90, max_retries: int = 1,
                 max_tokens: int = DEFAULT_MAX_TOKENS, disable_thinking: bool = True) -> None:
        from openai import OpenAI

        self.model = model
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.disabled = False
        self.client = OpenAI(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def _request(self, messages: list[dict], temperature: float,
                 max_tokens: int, disable_thinking: bool):
        """
        # [表面] 真正发请求的地方，带「关不关思考」这个开关
        # [本质] qwen3 系模型在 vLLM 上会先花 completion 预算做 reasoning：
        #        实测 2796 字英文正文能把 3500 token 预算全烧在思考上，
        #        正文返回空串且 finish_reason=length。chat_template_kwargs 是
        #        vLLM 标准的关思考入口，比「无限加大 max_tokens」可靠得多
        # [后果] 关掉思考后同样输入 completion 从 1663 降到 114，且不再空，
        #        单条耗时从 15 秒降到 2 秒
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return self.client.chat.completions.create(**kwargs)

    def chat(self, messages: list[dict], *, temperature: float = 0.2,
             max_tokens: int | None = None, extra_retries: int = 2) -> str:
        """发一轮对话拿文本回复；全部失败抛 LLMError。"""
        from openai import AuthenticationError, PermissionDeniedError

        if max_tokens is None:
            max_tokens = self.max_tokens
        if self.disabled:
            raise LLMError("LLM 已因鉴权失败熔断，本次运行内直接走兜底")

        disable = self.disable_thinking
        last_error: Exception | None = None
        for attempt in range(extra_retries + 1):
            try:
                response = self._request(messages, temperature, max_tokens, disable)
                choice = response.choices[0]
                content = (choice.message.content or "").strip()
                # [表面] 空内容换一种思考模式再试
                # [本质] finish_reason=length 就是「预算被思考吃光」的症状；
                #        这时把思考关掉（或关掉时打开）再打一次，成功率远高于原样重发
                # [后果] 空内容不再直接降级，而是自动换模式补救，日志里留证据
                if not content:
                    finish = getattr(choice, "finish_reason", "") or ""
                    usage = getattr(response, "usage", None)
                    reason = (
                        f"模型返回空内容(finish_reason={finish}, disable_thinking={disable}, "
                        f"usage={usage})"
                    )
                    if attempt < extra_retries:
                        wait = 2.0 * (attempt + 1)
                        logger.warning("%s，换思考模式后 %.1fs 重试", reason, wait)
                        last_error = LLMError(reason)
                        disable = not disable
                        time.sleep(wait)
                        continue
                    raise LLMError(reason)
                return content
            except LLMError:
                raise
            except (AuthenticationError, PermissionDeniedError) as exc:
                self.disabled = True
                raise LLMError("LLM 鉴权失败（检查 llm.api_key / llm.base_url）: %s" % exc) from exc
            except Exception as exc:
                last_error = exc
                if attempt < extra_retries:
                    wait = 2.0 * (attempt + 1)
                    logger.warning("LLM 调用失败(第 %d 次)，%.1fs 后重试: %s", attempt + 1, wait, exc)
                    time.sleep(wait)
        raise LLMError(f"LLM 连续失败: {last_error}") from last_error
