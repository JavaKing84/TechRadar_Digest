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
import random
import re
import time
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from news_ai_science.contracts.models import Article
from news_ai_science.core.errors import CrawlError
from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import collapse_spaces

logger = get_logger(__name__)

# [表面] 单次 page.goto 的超时（毫秒）
# [本质] 原值 45000 是按「家宽偶发抖动」定的，但线上实测 techcrunch 的列表页
#        在 Camoufox 下永远等不到 domcontentloaded（同一容器里 urllib 直取却是
#        200 / 1.0s）。这类站点不是"慢"，是对浏览器指纹不友好，等 45s 还是 45s。
#        而 45s × retries(2) = 90s+ 全耗在一个源上，一轮的取名额度被它独占。
# [后果] 15s 覆盖正常站点首屏绰绰有余（实测正文页 2~5s），坏源 90s → 30s，
#        整轮从 ~120s 降到 ~60s。确有慢站需要放宽时改 config.yaml 的
#        crawl.page_timeout_ms，不必动代码。
PAGE_TIMEOUT_MS = 15000
LIST_WAIT_MS = 25000

# [表面] 单个源的总耗时预算（秒），0 表示不限
# [本质] 超时只管"单次导航"，一个源仍可以靠「列表页重试 + 多篇正文」累加，
#        把整轮拖到分钟级。按源设总预算才是真正的止损：超了就把已拿到的留下、
#        剩下的链接直接放弃，不能让一个源决定整轮时长。
# [后果] 无论站点怎么磨蹭，每轮总时长上限 = 源数 × 预算，可预期
DEFAULT_SOURCE_BUDGET_S = 60.0

# [表面] 导航失败时重试几次、每次间隔多久
# [本质] 新闻站偶发 502/连接重置/首包超时，重试一次就能过；重试太多
#        会把「站点已下线」拖成整轮超时，所以默认 2 次、指数退避
# [后果] 抖动被吸收，真挂了也不会无限拖
DEFAULT_RETRIES = 2
RETRY_BASE_DELAY = 1.5

# [表面] 同一站点两次请求之间的最小间隔（秒）
# [本质] 连续快速请求是触发限流/封 IP 的主要原因；1.2 秒 ≈ 50 次/分钟，
#        远低于常见站点的阈值，一轮 6 篇多花几秒但换来稳定性
DEFAULT_REQUEST_INTERVAL = 1.2

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
# 链接规范化 / 重试 / 限速
# =====================================================================

# [表面] 这些查询参数不改变「是哪篇文章」
# [本质] 列表页会挂 utm_*/fbclid/spm 之类的追踪参数，同一篇文章因此产生
#        多个不同 URL；不归一化就会在库里存成两条、同一轮重复抓两次
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_")
TRACKING_KEYS = frozenset(
    {"fbclid", "gclid", "msclkid", "yclid", "mc_cid", "mc_eid", "ref", "referrer",
     "spm", "share_token", "_hsenc", "_hsmi", "__twitter_impression", "cmpid"}
)


def canonical_url(url: str) -> str:
    """
    # [表面] 把 URL 归一化成「去重用的规范形式」
    # [本质] 只做四件安全的事：丢 #片段、主机名和协议小写、去掉追踪参数、
    #        去掉末尾多余的 /。绝不改路径本身（有些站点 /a 和 /a/ 是两篇）。
    # [后果] 同一篇文章在列表页 / 分享链接 / 站内跳转里拿到的三种写法会合并成一条
    """
    if not url:
        return ""
    clean, _frag = urldefrag(url.strip())
    parsed = urlparse(clean)
    if not parsed.scheme or not parsed.netloc:
        return clean

    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS
    ]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        query=urlencode(sorted(query_pairs)),
    ).geturl()


def _goto(page, url: str, label: str = "", attempts: int = DEFAULT_RETRIES,
          timeout_ms: int = PAGE_TIMEOUT_MS) -> None:
    """
    # [表面] page.goto 的带重试版本
    # [本质] 只重试「可恢复」的失败——超时、连接被重置、5xx 都是抖动；
    #        地址本身写错（net::ERR_NAME_NOT_RESOLVED 之类）重试也没用，
    #        但无法可靠区分，所以统一重试并靠次数上限兜底。
    #        退避用 attempts 的指数（1.5s、3s…）加随机抖动，避免多源同时重试。
    #        timeout_ms 由调用方透传（crawl.page_timeout_ms），不再写死在函数里，
    #        否则调参只能改代码、改完还得重建镜像。
    # [后果] 单页抖动不再直接计为「该源失败」，日志里能看到第几次重试成功
    """
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if attempt > 1:
                logger.info("%s 第 %d 次尝试成功: %s", label or "[goto]", attempt, url[:70])
            return
        except Exception as exc:  # noqa: BLE001 - 需要吞掉所有导航异常以便重试
            last_exc = exc
            if attempt >= attempts:
                break
            delay = RETRY_BASE_DELAY * attempt + random.uniform(0, 0.6)
            logger.warning(
                "%s 导航失败（第 %d/%d 次），%.1fs 后重试: %s | %s",
                label or "[goto]", attempt, attempts, delay, url[:70], exc,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _pace(last_at: float, interval: float) -> float:
    """
    # [表面] 保证同一站点两次请求至少隔 interval 秒，返回新的时间戳
    # [本质] 限速必须是「两次请求之间」而不是「每个循环固定 sleep」：
    #        请求本身耗时不固定，固定 sleep 会让快站变慢、慢站仍过密。
    #        加 ±25% 抖动是为了不形成固定节奏（固定节奏本身也是特征）。
    # [后果] 一轮多花几秒，换来不容易被判成机器访问
    """
    if interval <= 0:
        return time.time()
    gap = interval * random.uniform(0.75, 1.25)
    wait = last_at + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    return time.time()


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
                 seen=None, retries: int = DEFAULT_RETRIES,
                 interval: float = DEFAULT_REQUEST_INTERVAL,
                 page_timeout_ms: int = PAGE_TIMEOUT_MS,
                 source_budget_s: float = 0.0) -> list[Article]:
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
    started = time.time()

    list_page = None
    try:
        list_page = browser.new_page()
        _goto(list_page, list_url, f"[{source_id}] 列表页", retries,
              timeout_ms=page_timeout_ms)
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

    # [表面] 先规范化链接并做「本轮内」去重，再按库里去重
    # [本质] 同一条新闻常在不同源/不同栏目里出现，URL 只差 utm 参数；
    #        只在单源内部去重挡不住跨源重复，必须在进正文页前用规范形式合并
    # [后果] 同一篇只开一次正文页，省掉最贵的一步
    normalized: list[str] = []
    round_seen: set[str] = set()
    for link in links:
        canon = canonical_url(link)
        if not canon or canon in round_seen:
            continue
        round_seen.add(canon)
        normalized.append(canon)
    if len(normalized) < len(links):
        logger.info("[%s] 规范化去重 %d 个重复链接", source_id, len(links) - len(normalized))
    links = normalized

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
    last_request_at = time.time()
    skipped_short = 0
    for link in links:
        if len(articles) >= max_items:
            break
        # [表面] 单源总预算：超了就收手，保留已抓到的
        # [本质] 逐一超时挡不住「每篇都慢一点」的累积；预算按源算才封得住顶。
        #        这里 break 而不是抛错：已经拿到的文章是有效产出，
        #        为了一个磨蹭的源把成果全丢掉是更差的选择
        # [后果] 单源耗时可控，整轮时长不再由最慢的站决定
        if source_budget_s > 0 and time.time() - started > source_budget_s:
            logger.warning(
                "[%s] 单源耗时超过预算 %.0fs，放弃剩余链接（已收 %d 篇）",
                source_id, source_budget_s, len(articles),
            )
            break
        # [表面] 每次进正文页之前先按站点间隔限速
        last_request_at = _pace(last_request_at, interval)
        page = None
        try:
            page = browser.new_page()
            _goto(page, link, f"[{source_id}] 正文页", retries,
                  timeout_ms=page_timeout_ms)
            # [表面] 只等极短一段
            # [本质] 正文在首屏 HTML 里就有了，等再久也等不到更多文字
            # [后果] 单篇耗时从 ~20 秒降到个位数
            page.wait_for_timeout(400)
            body = extract_main_text(page.content())
            if len(body) < min_body_chars:
                skipped_short += 1
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

    # [表面] 每个源收工时打一行汇总
    # [本质] 排错时最想知道的是「这个源是没链接、还是链接都旧、还是正文抽不出」，
    #        一行汇总比翻十几行零散日志快得多
    logger.info(
        "[%s] 源完成：候选 %d，入库 %d，正文过短 %d，耗时 %.1fs",
        source_id, len(links), len(articles), skipped_short, time.time() - started,
    )
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
    # [表面] 重试次数与站点限速都可以在 config.yaml 里调
    # [本质] 不同网络环境（学校专线 / 家宽 / 容器内）能承受的节奏不同，
    #        硬编码会让换环境就得改代码；配置项缺省时回落到模块常量
    retries = int(crawl_cfg.get("retries", DEFAULT_RETRIES))
    interval = float(crawl_cfg.get("request_interval", DEFAULT_REQUEST_INTERVAL))
    # [表面] 单页超时与单源预算都可由配置覆盖
    # [本质] 不同出口网络（学校专线 / 家宽 / 容器）对「多久算超时」的容忍度不同，
    #        写死在代码里意味着每次调参都要改代码 + 重建镜像。
    #        这里用 is None 判断而不是 `or`：0 是合法取值（预算 0 = 不限），
    #        `x or 默认值` 会把 0 悄悄吃掉——config 里 cooldown_hours: 0 就踩过这个坑。
    # [后果] 某个站需要放宽，只改 config.yaml 重启容器，不动代码
    timeout_raw = crawl_cfg.get("page_timeout_ms")
    page_timeout_ms = PAGE_TIMEOUT_MS if timeout_raw is None else int(timeout_raw)
    budget_raw = crawl_cfg.get("source_budget_s")
    source_budget_s = DEFAULT_SOURCE_BUDGET_S if budget_raw is None else float(budget_raw)

    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox

    articles: list[Article] = []
    errors: list[str] = []
    # [表面] 真正发起过请求的源个数（与 errors 对比才能判断"是不是全挂了"）
    # [本质] len(errors) 只说明失败几个，不知道总数；命中 total_limit 提前 break 时
    #        后面几个源压根没跑，拿 len(sources) 当分母会误判
    # [后果] "1 个源失败 + 3 个源正常"不会被当成整轮失败
    attempted = 0

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
            round_started = time.time()
            # [表面] 本轮已收的链接也参与去重
            # [本质] 跨源重复（同一篇被两个源收录）只在源内部去重是挡不住的；
            #        把本轮结果并进 seen 的判定里，第二篇就不会被抓第二次
            collected: set[str] = set()

            def round_seen(url: str) -> bool:
                if canonical_url(url) in collected:
                    return True
                return bool(seen and seen(url))

            for source in sources:
                if len(articles) >= total_limit:
                    break
                remain = min(max_items, total_limit - len(articles))
                if remain <= 0:
                    break
                attempted += 1
                try:
                    got = crawl_source(
                        browser, source, remain, min_body, round_seen,
                        retries=retries, interval=interval,
                        page_timeout_ms=page_timeout_ms,
                        source_budget_s=source_budget_s,
                    )
                    articles.extend(got)
                    collected.update(canonical_url(a.url) for a in got)
                except CrawlError as exc:
                    logger.error("[Crawl] 源失败（继续下一个源）: %s", exc)
                    errors.append(str(exc))
                except Exception as exc:
                    logger.error("[Crawl] 源异常（继续下一个源）: %s | %s", source.get("id"), exc)
                    errors.append(f"{source.get('id')}: {exc}")

            logger.info(
                "[Crawl] 本轮汇总：源 %d 个，成功入库 %d 篇，失败 %d 个，总耗时 %.1fs",
                len(sources), len(articles), len(errors), time.time() - round_started,
            )
            # [表面] 部分源失败但其余源正常：只是一条 WARNING，不算整轮失败
            # [本质] techcrunch 长期超时的日子里，旧逻辑「有 errors 且 0 篇」就会抛
            #        CrawlError，把「今天确实没有新文章」渲染成「采集异常」，
            #        真正的故障（全源挂掉）反而被这种天天出现的红字淹没
            # [后果] 日志里一眼能分清「少个源」和「全挂了」
            if errors and len(errors) < attempted:
                logger.warning(
                    "[Crawl] 部分源失败（%d/%d），其余源正常，本轮按正常处理", len(errors), attempted
                )

    # [表面] 只有「所有跑过的源都抛异常」才算整轮失败
    # [本质] 0 篇文章 + 无异常 = 今天的候选全被 canonical 去重挡掉（周末、已推完），
    #        这是正常状态，不该抛错；一个源都没跑（sources 为空 / 预算为 0）也不算失败
    # [后果] CrawlError 只在真故障时出现，调用方（main）不用再猜「异常」是什么
    if not articles and attempted and len(errors) >= attempted:
        raise CrawlError("所有新闻源均未取到正文：" + " ; ".join(errors[:3]))
    return articles, errors
