# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] core/text.py
[职责] 纯文本函数：空白折叠、截断、去标签、中英判断。无副作用、无依赖。
=====================================================================
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_WHITESPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SLASH_RE = re.compile(r"/{2,}")

# [表面] 汉字的 Unicode 区间（基本区 + 扩展 A + 兼容汉字）
# [本质] 只统计"汉字"，不统计 ★ 全角标点（0x3000 段）和 ★ 日文假名：
#        前者会让「，」也算中文，后者会把日文误判成中文
# [后果] 中文占比只反映"字"，不掺标点水分
_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
)


def collapse_spaces(text: str) -> str:
    """连续空白压成一个空格。卡片 markdown 里换行缩进毫无意义还撑版面。"""
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """按字符截断并加省略号。中文 len() 就是字符数，够用。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + suffix


def strip_html(text: str) -> str:
    """去标签 + 折叠空白。"""
    if not text:
        return ""
    return collapse_spaces(_TAG_RE.sub(" ", text))


def cjk_stats(text: str) -> tuple[int, int, float]:
    """
    # [表面] 统计文本里的汉字数、拉丁字母数、汉字在「汉字+拉丁字母」中的占比
    # [本质] 判断"这段文字是不是中文"不能只数汉字个数：英文标题里也可能
    #        夹一个中文书名号，中文标题里也可能全是品牌名（GitHub Copilot
    #        Fusion 混合模型…）。比值才能同时挡住两种误判
    # [后果] 返回 (cjk, latin, ratio)，ratio 在没有任何字母时为 0.0
    """
    value = text or ""
    cjk = 0
    latin = 0
    for ch in value:
        code = ord(ch)
        if any(low <= code <= high for low, high in _CJK_RANGES):
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    letters = cjk + latin
    ratio = cjk / letters if letters else 0.0
    return cjk, latin, ratio


def is_chinese_text(text: str, *, min_cjk: int = 4, min_share: float = 0.20) -> bool:
    """
    # [表面] 这段文字是否「以中文写成」（够多的汉字 + 汉字占比不过低）
    # [本质] 双重门槛：绝对汉字数挡住纯英文；占比挡住"英文正文里掺几个汉字"
    #        的伪中文。min_share 故意放得比较松（0.20）——中文标题里
    #        夹 GitHub Copilot Fusion / Opus 5.5 这类专名是常态，
    #        收得太紧会把正常译文判成英文，反而把好内容丢掉
    # [后果] 纯英文（cjk=0）必然 False；正常中文标题/摘要必然 True
    """
    cjk, _latin, ratio = cjk_stats(text)
    if cjk < min_cjk:
        return False
    return ratio >= min_share


def looks_english(text: str) -> bool:
    """
    # [表面] 判断文本是否以英文为主
    # [本质] 统计前 300 字符里 CJK 占比，超过一成即视为中文为主
    # [后果] 决定要不要让模型翻译标题
    """
    sample = (text or "")[:300]
    if not sample:
        return False
    cjk, latin, ratio = cjk_stats(sample)
    if cjk + latin == 0:
        return False
    return ratio < 0.10


def canonical_url(url: str) -> str:
    """
    # [表面] 把 URL 归一化成去重键：host + path（丢 scheme、query、fragment）
    # [本质] 同一篇文章常被不同入口带不同尾巴：
    #          ?utm_source=rss / ?ref=home / #comments / 结尾多一个 /
    #        这些差异会让「按 URL 去重」失效，同一篇被推第二遍。
    #        标题更不能当键——不同媒体会报道同一件事，翻译也会抖动。
    #        归一化到 host+path 后，`?utm_*` 这类噪声自然被丢掉，
    #        path 里的 slug 仍在键里，仍是「按 URL 路径去重」。
    # [后果] 同文同路径只入库一次；跨站同题不同路径不会被误杀（不做标题去重）
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else "https://" + raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = _MULTI_SLASH_RE.sub("/", parsed.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return f"{host}{path}" if host else raw


def url_slug(url: str) -> str:
    """
    # [表面] 取 URL 路径的最后一段（slug）
    # [本质] 日志里打全 URL 太长，slug 足够人眼识别是哪篇
    # [后果] 去重命中时日志形如「已存在 slug=openai-new-model」
    """
    path = urlparse((url or "").strip()).path.rstrip("/")
    return path.rsplit("/", 1)[-1].lower()
