# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] core/errors.py
[职责] 项目级异常。三者语义严格区分，上层据此决定「重试 / 降级 / 直接崩」。
  ConfigError  配置缺失或非法   -> 启动即抛，不带病运行
  CrawlError   浏览器抓取失败   -> 打红字堆栈，本轮不再采集（绝不降级 requests/RSS）
  LLMError     模型调用最终失败 -> 该条走兜底，不影响整轮
=====================================================================
"""
from __future__ import annotations


class ConfigError(RuntimeError):
    """配置缺失/非法。启动即抛，不允许带病运行。"""


class CrawlError(RuntimeError):
    """浏览器抓取失败。项目约定：抓不到就抛，绝不降级到 requests / RSS。"""


class LLMError(Exception):
    """LLM 调用最终失败的归一化异常，上层据此走兜底。"""
