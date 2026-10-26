# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] contracts/models.py
[职责] 新闻数据契约。抓取产出 Article，翻译与卡片都只认这个结构。
=====================================================================
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Article:
    """
    # [表面] 一篇抓下来的文章
    # [本质] 字段与下游一一对应：body 给模型、cover_image 给图床上传、
    #        url 做去重主键
    # [后果] 卡片需要用到的素材在抓取阶段就齐了，后面不再回头补
    """
    source: str
    source_name: str
    title: str
    url: str
    body: str
    cover_image: str
