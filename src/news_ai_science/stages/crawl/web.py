# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/crawler.py
[职责] 用 Camoufox（反指纹 Firefox）抓科技新闻：
      先开列表页找文章链接，再逐篇进正文页抽全文与配图。

[硬约束] 本文件不含 RSS、不含 feedparser、没有 requests 降级分支。
      浏览器起不来 -> 抛 CrawlError 退出，人工介入。
      单个站点/单篇文章抓不到 -> 打 ERROR 日志并跳过，不让一个站拖死整轮；
      但如果所有源都失败，整轮照样抛 CrawlError（不假装成功）。
=====================================================================
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from news_ai_science.contracts.models import Article
from news_ai_science.core.errors import CrawlError
from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import collapse_spaces

logger = get_logger(__name__)

PAGE_TIMEOUT_MS = 45000
LIST_WAIT_MS = 25000

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# [表面] 明显不是文章页的路径片段
# [本质] 导航、栏目、作者、标签、订阅、登录这些页面也有标题和正文，
#        混进来会顶掉真正的新闻
# [后果] 抽到的链接基本都是文章
SKIP_PATTERNS = (
    "/tag/", "/tags/", "/author/", "/authors/", "/category/", "/topics/",
    "/about", "/contact", "/privacy", "/terms", "/subscribe", "/newsletter",
    "/login", "/signin", "/signup", "/account", "/rss", "/feed", "/shop",
    "/podcast", "/video/", "/videos/", "/events", "/jobs", "/advertise",
    "/search", "/page/", "/amp/",
)

DATE_PATH_RE = re.compile(r"/20\d{2}/\d{1,2}/")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){3,}$")

# [表面] 正文最短长度
# [本质] 短于这个数说明抽错了容器（抓到导航或版权声明），不如不要
MIN_BODY_CHARS = 300
MAX_BODY_CHARS = 8000

REMOVE_TAGS = (
    "script", "style", "noscript", "iframe", "svg", "form",
    "nav", "header", "footer", "aside", "figure", "figcaption",
)

BODY_SELECTORS = (
    "article",
    "[role='article']",
    ".article-content",
    ".article-body",
    ".post-content",
    ".entry-content",
    ".content-body",
    "main",
)






@contextlib.contextmanager
def _null_context():
    """空上下文：让「绕代理 / 不绕代理」两种情况共用同一套缩进。"""
    yield


@contextlib.contextmanager
def _bypass_env_proxy():
    """
    # [表面] 在 with 块内临时清掉代理环境变量
    # [本质] Camoufox 启动的是独立子进程，只能通过环境变量影响它；
    #        os.environ 是进程级的，必须用完恢复
    # [后果] 直连可用的机器上单页能从十几秒降到一两秒；默认不开
    """
    saved = {}
    for key in PROXY_KEYS:
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    had_no_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "*"
    try:
        yield
    finally:
        os.environ.pop("NO_PROXY", None)
        if had_no_proxy is not None:
            os.environ["NO_PROXY"] = had_no_proxy
        os.environ.update(saved)


# =====================================================================
# 正文抽取（纯解析，不发请求）
# =====================================================================

def extract_main_text(html: str) -> str:
    """
    # [表面] HTML -> 正文纯文本
    # [本质] 两级策略：先信任语义容器（article/main 及常见 class），
    #        没有就统计每个节点下「有效段落」字数，取最重的那个
    # [后果] 抽不出足够长度时返回空串，由调用方判定为失败
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()

    best = None
    for selector in BODY_SELECTORS:
        node = soup.select_one(selector)
        if node is not None and len(collapse_spaces(node.get_text(" ", strip=True))) >= MIN_BODY_CHARS:
            best = node
            break
    if best is None:
        best = _pick_paragraph_rich_container(soup)
    if best is None:
        return ""

    paragraphs = [
        collapse_spaces(p.get_text(" ", strip=True))
        for p in best.find_all(["p", "h2", "h3", "li"])
    ]
    text = "\n".join(p for p in paragraphs if p)
    if not text:
        text = "\n".join(
            collapse_spaces(line) for line in best.get_text("\n", strip=True).splitlines()
        )
    text = "\n".join(line for line in text.splitlines() if len(line) >= 8)
    return text[:MAX_BODY_CHARS]


def _pick_paragraph_rich_container(soup: BeautifulSoup):
    """
    # [表面] 找「有效段落总字数最多」的容器
    # [本质] 逐个 <p> 向上统计父节点累计字数，取分数最高的父节点，
    #        比直接取 body 更抗广告位干扰
    # [后果] 没有语义标签的老站点也能抽到正文
    """
    scores: dict[int, tuple[object, int]] = {}
    for p in soup.find_all("p"):
        length = len(collapse_spaces(p.get_text(" ", strip=True)))
        if length < 40:
            continue
        parent = p.parent
        if parent is None:
            continue
        node, total = scores.get(id(parent), (parent, 0))
        scores[id(parent)] = (node, total + length)
    if not scores:
        return None
    node, total = max(scores.values(), key=lambda pair: pair[1])
    if total < MIN_BODY_CHARS:
        return None
    return node


# =====================================================================
# 链接收集
# =====================================================================

def _is_article_url(url: str, base_netloc: str) -> bool:
    """判断一个链接像不像文章详情页。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc.replace("www.", "") != base_netloc.replace("www.", ""):
        return False
    path = parsed.path.lower()
    if not path or path == "/":
        return False
    for skip in SKIP_PATTERNS:
        if skip in path:
            return False
    if DATE_PATH_RE.search(path):
        return True
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(SLUG_RE.match(last))


def _collect_links(page, list_url: str, selector: dict | None, limit: int) -> list[str]:
    """
    # [表面] 从列表页取文章链接
    # [本质] 优先用配置里的选择器（精确、稳）；没配就走通用启发式：
    #        遍历所有 <a>，同域 + 路径像文章 + 锚文本够长
    # [后果] 加新源可以先不写选择器，跑通再补
    """
    base_netloc = urlparse(list_url).netloc
    selector = selector or {}
    item_selector = selector.get("item")
    title_selector = selector.get("title")
    link_attr = selector.get("link_attr", "href")

    links: list[str] = []
    seen: set[str] = set()

    if item_selector and title_selector:
        rows = page.locator(item_selector)
        for i in range(min(rows.count(), limit * 3)):
            try:
                href = rows.nth(i).locator(title_selector).first.get_attribute(link_attr)
            except Exception:
                continue
            if not href:
                continue
            absolute = urljoin(list_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= limit:
                return links
        return links

    anchors = page.locator("a")
    total = min(anchors.count(), 600)
    for i in range(total):
        try:
            node = anchors.nth(i)
            href = node.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        absolute = urljoin(list_url, href)
        if absolute in seen:
            continue
        try:
            text = collapse_spaces(node.inner_text() or "")
        except Exception:
            text = ""
        if len(text) < 16:
            continue
        if not _is_article_url(absolute, base_netloc):
            continue
        seen.add(absolute)
        links.append(absolute)
        if len(links) >= limit:
            break
    return links


def _page_meta(page, source_name: str = "") -> str:
    """
    # [表面] 取文章标题（并去掉站点名后缀）
    # [本质] 列表页锚文本常被截断或加后缀，正文页的 og:title / h1 更准；
    #        但 og:title 往往自带「| TechCrunch」这类尾巴，卡片上很碍眼
    # [后果] 卡片标题干净，不会出现「xxx | TechCrunch」反复占用一行
    """
    title = ""
    try:
        title = page.get_attribute("meta[property='og:title']", "content") or ""
    except Exception:
        title = ""
    if not title:
        try:
            title = page.inner_text("h1")
        except Exception:
            title = ""
    return _clean_title(collapse_spaces(title or ""), source_name)


# [表面] 常见站点的品牌词
# [本质] 只认这些品牌词做后缀剥离，避免把
#        「AI - the next frontier」这种正常标题的后半截误删
# [后果] 剥离精确，不会伤到正文语义
BRAND_WORDS = (
    "techcrunch", "wired", "the verge", "verge", "ars technica",
    "mit technology review", "technology review", "venturebeat",
    "the next web", "engadget", "reuters", "bloomberg", "36kr",
)


def _clean_title(title: str, source_name: str = "") -> str:
    """去掉 og:title 尾部的「| 站点名」/「- 站点名」。"""
    text = collapse_spaces(title or "")
    for sep in (" | ", "｜", " - ", " – ", " — ", " » "):
        if sep not in text:
            continue
        head, _, tail = text.rpartition(sep)
        if not head.strip():
            continue
        low = tail.strip().lower()
        if low == (source_name or "").strip().lower() or any(brand in low for brand in BRAND_WORDS):
            return head.strip()
    return text


# [表面] 判断「这张图是不是装饰性素材」的 URL 特征
# [本质] 站点会在正文里混入 logo / 图标 / 头像 / 占位图 / 统计像素
# [后果] 卡片配图不会突然变成某个站点的 logo 或 1x1 像素
IMAGE_NOISE_RE = re.compile(
    r"(logo|icon|avatar|sprite|placeholder|spacer|pixel|emoji|badge|"
    r"advert|/ads?/|banner|share|social|gravatar|profile|blank|default)",
    re.IGNORECASE,
)

# [表面] 在页面里挑「最像正文配图」的那张
# [本质] 三个判据：①在 <article>/<main> 里 ②尺寸够大（≥400x200、宽高比 0.5~3）
#        ③URL 不像装饰素材。优先正文内面积最大的一张，拿不到才退 og:image。
#        注意开了 block_images 时 naturalWidth 恒为 0，所以尺寸靠
#        getBoundingClientRect 的布局盒来读——不真正下载图片也能判断占位多大
# [后果] 配图是文章里真实存在、与内容相关的大图，而不是站点默认分享图
PICK_IMAGE_JS = r"""
() => {
  const noise = /(logo|icon|avatar|sprite|placeholder|spacer|pixel|emoji|badge|advert|\/ads?\/|banner|share|social|gravatar|profile|blank|default)/i;
  const main = document.querySelector('article') || document.querySelector('main') || document.body;
  const picked = [];
  Array.from(document.images || []).forEach((img, order) => {
    const url = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
    if (!url || url.startsWith('data:') || noise.test(url)) return;
    const rect = img.getBoundingClientRect();
    const w = img.naturalWidth || rect.width || 0;
    const h = img.naturalHeight || rect.height || 0;
    if (w < 400 || h < 200) return;
    const ratio = w / h;
    if (ratio < 0.5 || ratio > 3.0) return;
    picked.push({url: url, area: w * h, in_main: main.contains(img), order: order});
  });
  picked.sort((a, b) => (b.in_main - a.in_main) || (b.area - a.area) || (a.order - b.order));
  const ogNode = document.querySelector('meta[property="og:image"]') ||
                 document.querySelector('meta[name="twitter:image"]');
  return {best: picked.length ? picked[0].url : '', og: ogNode ? (ogNode.content || '') : ''};
}
"""


def _cover_image(page) -> str:
    """
    # [表面] 给文章挑一张配图
    # [本质] 先用正文内的大图，拿不到才用 og:image 兜底；两者都过噪声词过滤，
    #        避免把站点默认分享图（往往是一张几年的老截图）当成新闻配图
    # [后果] 有图就用最相关的那张，真没有返回空串由上层跳过该条新闻
    """
    try:
        result = page.evaluate(PICK_IMAGE_JS) or {}
    except Exception:
        result = {}
    best = str(result.get("best") or "").strip()
    og = str(result.get("og") or "").strip()

    if best:
        return best
    if og and not IMAGE_NOISE_RE.search(og) and not og.startswith("data:"):
        return og
    return ""


# =====================================================================
# 抓取
# =====================================================================

def crawl_source(browser, source: dict, max_items: int, min_body_chars: int,
                 seen=None) -> list[Article]:
    """
    # [表面] 抓一个源：列表页 -> 文章页 -> Article 列表
    # [本质] 列表页没取到链接就抛 CrawlError（由 crawl_all 记录并继续下一个源）；
    #        单篇文章失败只打 ERROR 跳过，因为下游要的是全文，抽不到宁可不收，
    #        绝不会把只有标题的条目放进流水线。
    #        seen 是「这个链接是否已入库」的判定函数：库里去重先于进正文页，
    #        已发过的文章连正文都不抓，省掉最贵的一步
    # [后果] 一个源坏了不影响其他源，但坏源在日志里一定留红字
    """
    source_id = str(source.get("id") or "web")
    source_name = str(source.get("name") or source_id)
    list_url = str(source.get("url") or "")
    if not list_url:
        raise CrawlError(f"[{source_id}] 源配置缺少 url")

    list_page = None
    try:
        list_page = browser.new_page()
        list_page.goto(list_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        # [表面] 轮询等候选链接出现，而不是死等固定秒数
        # [本质] 列表页快慢差 10 倍以上（命中缓存 2 秒，冷启动 20 秒）
        # [后果] 快站 2 秒往下走，慢站最多等 LIST_WAIT_MS
        links: list[str] = []
        deadline = time.time() + LIST_WAIT_MS / 1000.0
        while True:
            links = _collect_links(list_page, list_url, source.get("selector"), max_items)
            if links or time.time() >= deadline:
                break
            list_page.wait_for_timeout(800)
        logger.info("[%s] 列表页取到 %d 个候选链接", source_id, len(links))
        if not links:
            raise CrawlError(f"[{source_id}] 列表页未取到文章链接: {list_url}")
    except CrawlError:
        raise
    except Exception as exc:
        raise CrawlError(f"[{source_id}] 列表页抓取失败: {list_url}: {exc}") from exc
    finally:
        if list_page is not None:
            try:
                list_page.close()
            except Exception:
                pass

    # [表面] 进正文页之前先按库里去重
    # [本质] 正文页是最贵的一步（每篇 1~3 秒 + 一个页面）；
    #        列表页里绝大多数链接都是老朋友，先剔掉再进详情页
    # [后果] 一轮抓取只花在真正的新文章上，已发的连打开都不打开
    if seen is not None:
        before = len(links)
        links = [link for link in links if not seen(link)]
        if len(links) < before:
            logger.info(
                "[%s] 去重跳过 %d 个已入库链接（剩 %d 个候选）",
                source_id, before - len(links), len(links),
            )
        if not links:
            logger.info("[%s] 候选链接全部已入库，跳过该源", source_id)
            return []

    articles: list[Article] = []
    for link in links:
        if len(articles) >= max_items:
            break
        page = None
        try:
            page = browser.new_page()
            page.goto(link, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            # [表面] 只等极短一段
            # [本质] 正文在首屏 HTML 里就有了，等再久也等不到更多文字
            # [后果] 单篇耗时从 ~20 秒降到个位数
            page.wait_for_timeout(400)
            body = extract_main_text(page.content())
            if len(body) < min_body_chars:
                logger.warning("[%s] 正文过短(%d 字)，丢弃该篇: %s", source_id, len(body), link)
                continue
            title = _page_meta(page, source_name)
            if not title:
                logger.warning("[%s] 标题为空，丢弃该篇: %s", source_id, link)
                continue
            articles.append(
                Article(
                    source=source_id,
                    source_name=source_name,
                    title=title,
                    url=link,
                    body=body,
                    cover_image=_cover_image(page),
                )
            )
            logger.info("[%s] 取到正文 %d 字 | %s", source_id, len(body), title[:36])
        except Exception as exc:
            # [表面] 单篇失败只记错跳过
            # [本质] 不是「降级到 requests/RSS」，只是让已抓到的成果落库；
            #        整轮是否失败由 crawl_all 按「全失败」判定
            # [后果] 一篇 404 不会毁掉整轮
            logger.error("[%s] 文章页抓取失败: %s | %s", source_id, link[:70], exc)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        time.sleep(0.3)
    return articles


def crawl_all(cfg: dict, seen=None) -> tuple[list[Article], list[str]]:
    """
    # [表面] 一轮抓完所有启用的新闻源
    # [本质] 整轮只启动一次浏览器（Camoufox 冷启动 5~10 秒）；
    #        单源失败记录后继续；全部源都失败才抛 CrawlError。
    #        seen 透传到 crawl_source，让「已入库」在链接层就被挡掉
    # [后果] 返回 (文章列表, 错误列表)；错误列表非空说明有源挂了
    """
    sources = cfg.get("enabled_sources") or []
    crawl_cfg = cfg.get("crawl") or {}
    max_items = int(crawl_cfg.get("max_items_per_source") or 2)
    total_limit = int(crawl_cfg.get("total_limit") or 6)
    headless = bool(crawl_cfg.get("headless", True))
    bypass = bool(crawl_cfg.get("bypass_proxy", False))
    min_body = int(crawl_cfg.get("min_body_chars") or MIN_BODY_CHARS)

    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox

    articles: list[Article] = []
    errors: list[str] = []

    proxy_ctx = _bypass_env_proxy() if bypass else _null_context()
    with proxy_ctx:
        try:
            browser_ctx = Camoufox(
                humanize=True,
                headless=headless,
                exclude_addons=[DefaultAddons.UBO],
                # [表面] 不渲染图片 + 开磁盘缓存
                # [本质] 列表页一半带宽耗在图片字体上；配图只需要 URL，
                #        不需要真的画出来。enable_cache 让同站点二轮起命中缓存
                # [后果] 单源从 88 秒降到十几秒
                block_images=True,
                enable_cache=True,
                i_know_what_im_doing=True,
            )
        except Exception as exc:
            raise CrawlError(f"Camoufox 启动失败: {exc}") from exc

        with browser_ctx as browser:
            for source in sources:
                if len(articles) >= total_limit:
                    break
                remain = min(max_items, total_limit - len(articles))
                if remain <= 0:
                    break
                try:
                    got = crawl_source(browser, source, remain, min_body, seen)
                    articles.extend(got)
                except CrawlError as exc:
                    logger.error("[Crawl] 源失败（继续下一个源）: %s", exc)
                    errors.append(str(exc))
                except Exception as exc:
                    logger.error("[Crawl] 源异常（继续下一个源）: %s | %s", source.get("id"), exc)
                    errors.append(f"{source.get('id')}: {exc}")

    if not articles and errors:
        raise CrawlError("所有新闻源均未取到正文：" + " ; ".join(errors[:3]))
    return articles, errors
