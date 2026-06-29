#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPN UK 信息源观察面板：每日自动更新脚本

运行方式：
    python scripts/update_dashboard.py

它会：
1. 读取 config/sources.csv 的信息源池。
2. 按来源类型抓取公开网页、Reddit、App Store、YouTube/X/Google 的可选 API。
3. 将原始内容按“重要性、受众关切、行业相关、增长需求点”打分。
4. 生成 docs/index.html、docs/data/latest.json、docs/archive/YYYY-MM-DD.json。
5. 在 GitHub Actions 中每日自动提交更新，从而让 GitHub Pages 自动刷新网页。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
ARCHIVE_DIR = DOCS_DIR / "archive"
REPORTS_DIR = DOCS_DIR / "reports"
STATUS_DIR = DOCS_DIR / "status"

TZ_NAME = os.getenv("DASHBOARD_TIMEZONE", "Asia/Singapore")
DEFAULT_HEADERS = {
    "User-Agent": os.getenv(
        "DASHBOARD_USER_AGENT",
        "Mozilla/5.0 (compatible; VPNDailyDashboard/1.0; +https://github.com/)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-GB,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}
REQUEST_TIMEOUT = float(os.getenv("DASHBOARD_TIMEOUT", "20"))
MAX_ITEMS_PER_SOURCE = int(os.getenv("MAX_ITEMS_PER_SOURCE", "15"))
MAX_RAW_ITEMS = int(os.getenv("MAX_RAW_ITEMS", "240"))


@dataclass
class FetchResult:
    source_name: str
    platform: str
    url: str
    status: str
    item_count: int = 0
    note: str = ""
    elapsed_ms: int = 0


def now_sg() -> dt.datetime:
    if ZoneInfo is None:
        return dt.datetime.utcnow() + dt.timedelta(hours=8)
    return dt.datetime.now(ZoneInfo(TZ_NAME))


def ensure_dirs() -> None:
    for p in [DATA_DIR, ARCHIVE_DIR, REPORTS_DIR, STATUS_DIR]:
        p.mkdir(parents=True, exist_ok=True)


def clean_text(value: Any, limit: Optional[int] = None) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def stable_id(*parts: str) -> str:
    raw = "|".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def load_sources(path: Path = CONFIG_DIR / "sources.csv") -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"找不到来源配置：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [{k: clean_text(v) for k, v in row.items()} for row in reader]


def load_seed() -> Dict[str, Any]:
    p = CONFIG_DIR / "seed_dashboard.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def should_fetch_source(source: Dict[str, str], today: dt.date, full_scan: bool = False) -> bool:
    if full_scan:
        return True
    frequency = source.get("追踪频率", "")
    layer = source.get("日报层级", "")
    sustainable = source.get("是否可持续追踪", "")

    # 默认每天抓取“核心日报源”，这样页面每天都会真实刷新；
    # 月更/低频来源仍按表格节奏控制，避免不必要请求。
    if layer == "核心日报源":
        return True
    if "每日" in frequency:
        return True
    if "每周" in frequency:
        return today.weekday() == 0
    if "每月" in frequency:
        return today.day == 1
    if "低频" in frequency:
        return today.day == 1 and today.month in {1, 4, 7, 10}
    return sustainable == "是" and today.weekday() == 0


def source_priority(source: Dict[str, str]) -> int:
    raw = source.get("监控优先级分", "")
    try:
        return int(float(raw))
    except Exception:
        layer = source.get("日报层级", "")
        if layer == "核心日报源":
            return 8
        if layer == "周更重点源":
            return 5
        return 2


def is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def request_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    if requests is None:
        raise RuntimeError("缺少 requests 依赖")
    resp = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def request_text(url: str) -> Tuple[str, str]:
    if requests is None:
        raise RuntimeError("缺少 requests 依赖")
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text, resp.headers.get("content-type", "")


def to_item(
    *,
    source: Dict[str, str],
    title: str,
    url: str,
    snippet: str = "",
    published: str = "",
    item_type: str = "web",
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    title = clean_text(title, 260)
    snippet = clean_text(snippet, 900)
    source_name = source.get("来源名称", "")
    source_url = source.get("URL/入口", "")
    item_url = url or source_url
    return {
        "id": stable_id(source_name, item_url, title),
        "title": title or source_name,
        "url": item_url,
        "source": source_name,
        "platform": source.get("平台", ""),
        "source_category": source.get("来源类别", ""),
        "target_user": source.get("目标用户", ""),
        "source_note": source.get("备注", ""),
        "key_info": source.get("关键信息", ""),
        "source_priority": source_priority(source),
        "tier": source.get("日报层级", ""),
        "published": published,
        "snippet": snippet,
        "type": item_type,
        "metrics": metrics or {},
    }


def load_manual_items(today: dt.date) -> List[Dict[str, Any]]:
    """Load optional human notes from config/manual_inputs.csv.

    Columns: date, source, title, url, note, theme, priority.
    Rows dated today are included. Blank date rows are always included.
    """
    path = CONFIG_DIR / "manual_inputs.csv"
    if not path.exists():
        return []
    items: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_date = clean_text(row.get("date", ""))
                if row_date and row_date != today.strftime("%Y-%m-%d"):
                    continue
                source = {
                    "来源名称": clean_text(row.get("source", "手工补充")),
                    "平台": "Manual",
                    "来源类别": "手工补充",
                    "目标用户": "",
                    "备注": clean_text(row.get("theme", "")),
                    "关键信息": clean_text(row.get("priority", "")),
                    "监控优先级分": "8",
                    "日报层级": "核心日报源",
                    "URL/入口": clean_text(row.get("url", "")),
                }
                title = clean_text(row.get("title", ""))
                if not title:
                    continue
                items.append(
                    to_item(
                        source=source,
                        title=title,
                        url=clean_text(row.get("url", "")),
                        snippet=clean_text(row.get("note", "")),
                        published=row_date,
                        item_type="manual",
                        metrics={"priority": clean_text(row.get("priority", "")), "theme": clean_text(row.get("theme", ""))},
                    )
                )
    except Exception:
        return []
    return items


def fetch_reddit(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    url = source.get("URL/入口", "")
    parsed = urlparse(url)
    path = parsed.path or ""
    match = re.search(r"/r/([^/]+)/?", path, re.I)
    if not match:
        return [], "未识别 subreddit"

    subreddit = match.group(1)
    query = ""
    sort = "new"
    if "search" in path:
        qs = parse_qs(parsed.query)
        query = (qs.get("q") or ["vpn"])[0]
        sort = (qs.get("sort") or ["new"])[0]
        endpoint = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {"q": query or "vpn", "restrict_sr": "1", "sort": sort, "limit": MAX_ITEMS_PER_SOURCE}
    else:
        endpoint = f"https://www.reddit.com/r/{subreddit}/new.json"
        params = {"limit": MAX_ITEMS_PER_SOURCE}

    items: List[Dict[str, Any]] = []
    try:
        data = request_json(endpoint, params=params)
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            title = d.get("title") or ""
            permalink = d.get("permalink") or ""
            item_url = "https://www.reddit.com" + permalink if permalink.startswith("/") else d.get("url", url)
            created_utc = d.get("created_utc")
            published = ""
            if created_utc:
                published = dt.datetime.utcfromtimestamp(float(created_utc)).replace(tzinfo=dt.timezone.utc).isoformat()
            snippet = d.get("selftext") or d.get("link_flair_text") or ""
            metrics = {
                "score": d.get("score"),
                "comments": d.get("num_comments"),
                "subreddit": d.get("subreddit"),
            }
            items.append(to_item(source=source, title=title, url=item_url, snippet=snippet, published=published, item_type="reddit", metrics=metrics))
        return items, "Reddit JSON"
    except Exception as exc_json:
        # RSS fallback is useful when the JSON endpoint is rate-limited.
        if feedparser is None:
            return [], f"Reddit 抓取失败：{type(exc_json).__name__}"
        try:
            if query:
                rss_url = f"https://www.reddit.com/r/{subreddit}/search.rss?{urlencode({'q': query, 'restrict_sr': 1, 'sort': sort})}"
            else:
                rss_url = f"https://www.reddit.com/r/{subreddit}/new/.rss"
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
                items.append(
                    to_item(
                        source=source,
                        title=getattr(entry, "title", ""),
                        url=getattr(entry, "link", url),
                        snippet=getattr(entry, "summary", ""),
                        published=getattr(entry, "published", ""),
                        item_type="reddit-rss",
                    )
                )
            return items, "Reddit RSS fallback"
        except Exception as exc_rss:
            return [], f"Reddit 抓取失败：JSON={type(exc_json).__name__}; RSS={type(exc_rss).__name__}"


def fetch_x(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        return [], "需要 X_BEARER_TOKEN；公开搜索页通常需要登录或会触发反爬"
    if requests is None:
        return [], "缺少 requests 依赖"

    url = source.get("URL/入口", "")
    parsed = urlparse(url)
    query = (parse_qs(parsed.query).get("q") or ["VPN UK"])[0]
    endpoint = "https://api.twitter.com/2/tweets/search/recent"
    params = {
        "query": query,
        "max_results": min(max(MAX_ITEMS_PER_SOURCE, 10), 50),
        "tweet.fields": "created_at,public_metrics,lang,author_id",
    }
    try:
        resp = requests.get(
            endpoint,
            params=params,
            headers={"Authorization": f"Bearer {token}", "User-Agent": DEFAULT_HEADERS["User-Agent"]},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for d in data.get("data", []):
            tweet_id = d.get("id", "")
            metrics = d.get("public_metrics", {}) or {}
            items.append(
                to_item(
                    source=source,
                    title=clean_text(d.get("text", ""), 140),
                    url=f"https://x.com/i/web/status/{tweet_id}",
                    snippet=d.get("text", ""),
                    published=d.get("created_at", ""),
                    item_type="x-api",
                    metrics=metrics,
                )
            )
        return items, "X API"
    except Exception as exc:
        return [], f"X API 抓取失败：{type(exc).__name__}"


def fetch_youtube(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    url = source.get("URL/入口", "")
    parsed = urlparse(url)
    query = (parse_qs(parsed.query).get("search_query") or ["vpn uk"])[0]
    if api_key and requests is not None:
        try:
            data = request_json(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "type": "video",
                    "order": "relevance",
                    "q": query,
                    "regionCode": "GB",
                    "maxResults": min(MAX_ITEMS_PER_SOURCE, 25),
                    "key": api_key,
                },
            )
            items = []
            for row in data.get("items", []):
                vid = (row.get("id") or {}).get("videoId", "")
                sn = row.get("snippet", {})
                items.append(
                    to_item(
                        source=source,
                        title=sn.get("title", ""),
                        url=f"https://www.youtube.com/watch?v={vid}" if vid else url,
                        snippet=sn.get("description", ""),
                        published=sn.get("publishedAt", ""),
                        item_type="youtube-api",
                        metrics={"channel": sn.get("channelTitle")},
                    )
                )
            return items, "YouTube Data API"
        except Exception as exc:
            return [], f"YouTube API 抓取失败：{type(exc).__name__}"

    # No-key fallback: parse search result HTML where possible.
    try:
        text, _ = request_text(url)
        titles = []
        # This regex is intentionally loose because YouTube HTML changes often.
        for m in re.finditer(r'"title"\s*:\s*\{"runs"\s*:\s*\[\{"text"\s*:\s*"([^"]{8,160})"', text):
            title = bytes(m.group(1), "utf-8").decode("unicode_escape", errors="ignore")
            if "vpn" in title.lower() and title not in titles:
                titles.append(clean_text(title, 180))
            if len(titles) >= MAX_ITEMS_PER_SOURCE:
                break
        items = [to_item(source=source, title=t, url=url, snippet=f"YouTube search: {query}", item_type="youtube-search") for t in titles]
        if items:
            return items, "YouTube HTML fallback"
        return [], "建议配置 YOUTUBE_API_KEY；网页搜索结果结构经常变化"
    except Exception as exc:
        return [], f"YouTube 抓取失败：{type(exc).__name__}"


def fetch_app_store(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    try:
        data = request_json(
            "https://itunes.apple.com/search",
            params={"term": "vpn", "country": "gb", "entity": "software", "limit": MAX_ITEMS_PER_SOURCE},
        )
        items = []
        for app in data.get("results", []):
            title = app.get("trackName", "")
            snippet = "；".join(
                clean_text(x, 160)
                for x in [
                    app.get("sellerName", ""),
                    app.get("primaryGenreName", ""),
                    app.get("averageUserRating", ""),
                    app.get("userRatingCount", ""),
                ]
                if str(x)
            )
            items.append(
                to_item(
                    source=source,
                    title=title,
                    url=app.get("trackViewUrl", "https://apps.apple.com/gb/search?term=vpn"),
                    snippet=snippet,
                    item_type="app-store",
                    metrics={
                        "rating": app.get("averageUserRating"),
                        "rating_count": app.get("userRatingCount"),
                    },
                )
            )
        return items, "Apple iTunes Search API"
    except Exception as exc:
        return [], f"App Store 抓取失败：{type(exc).__name__}"


def fetch_google_play(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    try:
        from google_play_scraper import search  # type: ignore

        results = search("vpn", lang="en", country="gb", n_hits=MAX_ITEMS_PER_SOURCE)
        items = []
        for app in results:
            app_id = app.get("appId", "")
            items.append(
                to_item(
                    source=source,
                    title=app.get("title", ""),
                    url=f"https://play.google.com/store/apps/details?id={app_id}" if app_id else "https://play.google.com/store/search?q=vpn&c=apps",
                    snippet=clean_text(app.get("summary", ""), 300),
                    item_type="google-play",
                    metrics={"score": app.get("score"), "installs": app.get("installs")},
                )
            )
        return items, "google-play-scraper"
    except Exception as exc:
        return [], f"Google Play 需要 google-play-scraper 或手工/第三方 ASO 工具：{type(exc).__name__}"


def fetch_google_serp(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        return [], "Google 搜索结果建议配置 SERPAPI_KEY；直接抓 Google 搜索结果不稳定"
    url = source.get("URL/入口", "")
    parsed = urlparse(url)
    query = (parse_qs(parsed.query).get("q") or ["best vpn uk"])[0]
    try:
        data = request_json(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "google_domain": "google.co.uk", "gl": "uk", "hl": "en", "api_key": key},
        )
        items = []
        for row in data.get("organic_results", [])[:MAX_ITEMS_PER_SOURCE]:
            items.append(
                to_item(
                    source=source,
                    title=row.get("title", ""),
                    url=row.get("link", url),
                    snippet=row.get("snippet", ""),
                    item_type="google-serp",
                    metrics={"position": row.get("position")},
                )
            )
        return items, "SerpApi Google UK"
    except Exception as exc:
        return [], f"SerpApi 抓取失败：{type(exc).__name__}"


LINK_KEYWORDS = [
    "vpn", "nord", "express", "proton", "surfshark", "windscribe", "mullvad",
    "x-vpn", "privacy", "security", "streaming", "netflix", "iplayer", "uk",
    "age", "online safety", "review", "best", "free", "deal", "discount",
]


def extract_article_links(base_url: str, soup: Any, limit: int = 8) -> List[Tuple[str, str]]:
    links: List[Tuple[str, str]] = []
    seen = set()
    for a in soup.find_all("a"):
        text = clean_text(a.get_text(" ", strip=True), 180)
        href = a.get("href") or ""
        if len(text) < 8 or not href:
            continue
        low = text.lower()
        if not any(k in low for k in LINK_KEYWORDS):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        links.append((text, full))
        if len(links) >= limit:
            break
    return links


def fetch_generic_page(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    url = source.get("URL/入口", "")
    if not is_http_url(url):
        return [], "非公开 URL 或需要手工输入"
    try:
        text, content_type = request_text(url)
        if BeautifulSoup is None:
            title = clean_text(re.sub(r"<[^>]+>", " ", text), 160)
            return [to_item(source=source, title=title or source.get("来源名称", ""), url=url, snippet="", item_type="web")], "web text"
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = clean_text(soup.title.string, 220)
        h1 = soup.find("h1")
        if h1:
            title = clean_text(h1.get_text(" ", strip=True), 220) or title
        metas = []
        for name in ["description", "og:description", "twitter:description"]:
            meta = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
            if meta and meta.get("content"):
                metas.append(clean_text(meta.get("content"), 300))
        paragraphs = [clean_text(p.get_text(" ", strip=True), 300) for p in soup.find_all(["h2", "h3", "p"])[:40]]
        body_text = " ".join([m for m in metas if m] + [p for p in paragraphs if p])
        items = [to_item(source=source, title=title or source.get("来源名称", ""), url=url, snippet=body_text, item_type="web-page")]
        for link_text, link_url in extract_article_links(url, soup, limit=6):
            items.append(to_item(source=source, title=link_text, url=link_url, snippet=f"页面内相关链接：{title}", item_type="web-link"))
        return items[:MAX_ITEMS_PER_SOURCE], f"web page ({content_type})"
    except Exception as exc:
        return [], f"网页抓取失败：{type(exc).__name__}"


def fetch_source(source: Dict[str, str]) -> Tuple[List[Dict[str, Any]], FetchResult]:
    start = time.time()
    platform = source.get("平台", "")
    category = source.get("来源类别", "")
    name = source.get("来源名称", "")
    url = source.get("URL/入口", "")
    items: List[Dict[str, Any]] = []
    note = ""
    status = "fetched"

    try:
        low_platform = platform.lower()
        low_category = category.lower()
        low_url = url.lower()

        if not is_http_url(url):
            status = "limited"
            note = "非公开链接、Discord、应用商店手工入口或需补充邀请/API"
        elif "twitter" in low_platform or "x.com" in low_url:
            items, note = fetch_x(source)
            status = "fetched" if items else "limited"
        elif "reddit" in low_platform or "reddit.com" in low_url:
            items, note = fetch_reddit(source)
            status = "fetched" if items else "failed"
        elif "youtube" in low_platform or "youtube.com" in low_url:
            items, note = fetch_youtube(source)
            status = "fetched" if items else "limited"
        elif "tiktok" in low_platform or "tiktok.com" in low_url:
            # TikTok public search is often protected; try generic once and mark partial.
            items, note = fetch_generic_page(source)
            status = "partial" if items else "limited"
        elif "google search" in name.lower() or "google.co" in low_url:
            items, note = fetch_google_serp(source)
            status = "fetched" if items else "limited"
        elif "app store" in platform.lower() or "apple app store" in name.lower():
            items, note = fetch_app_store(source)
            status = "fetched" if items else "failed"
        elif "google play" in platform.lower() or "google play" in name.lower():
            items, note = fetch_google_play(source)
            status = "fetched" if items else "limited"
        else:
            items, note = fetch_generic_page(source)
            status = "fetched" if items else "failed"
    except Exception as exc:  # keep the whole run alive
        status = "failed"
        note = f"抓取异常：{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.time() - start) * 1000)
    return items, FetchResult(
        source_name=name,
        platform=platform,
        url=url,
        status=status,
        item_count=len(items),
        note=note,
        elapsed_ms=elapsed_ms,
    )


THEMES: Dict[str, Dict[str, Any]] = {
    "policy": {
        "name": "政策 / 年龄验证 / VPN 合法性",
        "keywords": [
            "online safety", "ofcom", "ico", "age assurance", "age verification", "under 16",
            "children", "child safety", "regulation", "law", "government", "bill", "act",
            "vpn ban", "verify age", "legal", "compliance", "uk government", "政策", "年龄验证", "合规"
        ],
        "title": "政策与年龄验证议题继续升温，VPN 绕过和合规表述需要重点监控",
        "audience": "家长、学生、英国本地用户、隐私关注用户、媒体读者",
        "action": "准备“合规、安全、隐私不冲突”的内容话术；避免鼓励规避监管，聚焦隐私保护、公共 Wi‑Fi 安全和家长教育。",
        "tags": ["政策", "UK", "年龄验证", "合规"],
        "owner": "政策监控 + 内容",
        "impact": 94,
        "industry": 95,
        "growth": 88,
    },
    "streaming": {
        "name": "流媒体可用性 / 平台检测 / IP 信誉",
        "keywords": [
            "netflix", "prime video", "amazon prime", "bbc iplayer", "iplayer", "disney", "hulu",
            "streaming", "unblock", "geo", "region", "detected", "blocked", "proxy", "ip reputation",
            "captcha", "residential ip", "youtube premium", "流媒体", "解锁", "地区", "被识别"
        ],
        "title": "流媒体、地区切换和 IP 被识别仍是最靠近转化的高频痛点",
        "audience": "流媒体用户、旅行用户、价格敏感用户、游戏/娱乐用户",
        "action": "建立 UK/US 流媒体可用性监控；内容页突出“可用节点推荐、自动切换、失败排查、IP 信誉”。",
        "tags": ["Streaming", "IP reputation", "Netflix", "BBC iPlayer"],
        "owner": "产品 + 运营",
        "impact": 91,
        "industry": 90,
        "growth": 96,
    },
    "competitor": {
        "name": "竞品口碑 / 续费 / 取消 / 客服",
        "keywords": [
            "nordvpn", "nord vpn", "expressvpn", "express vpn", "protonvpn", "proton vpn",
            "windscribe", "mullvad", "surfshark", "pia", "private internet access",
            "x-vpn", "xvpn", "refund", "renewal", "subscription", "cancel", "support",
            "客服", "续费", "退款", "取消", "竞品"
        ],
        "title": "竞品用户抱怨集中在续费透明、取消路径、客服响应和节点稳定",
        "audience": "竞品存量用户、价格敏感用户、购买前搜索用户",
        "action": "用“透明价格、到期提醒、易取消、故障排查响应快”做竞品转化页和社区回复脚本。",
        "tags": ["竞品", "Churn", "Renewal", "Support"],
        "owner": "增长 + 客服 + 支付",
        "impact": 88,
        "industry": 90,
        "growth": 93,
    },
    "uk_nodes": {
        "name": "英国节点 / 城市节点 / CAPTCHA",
        "keywords": [
            "uk server", "uk servers", "london", "manchester", "united kingdom", "british",
            "gb", "great britain", "captcha", "ip blocked", "server down", "slow server",
            "英国节点", "伦敦", "曼彻斯特"
        ],
        "title": "英国城市级节点、CAPTCHA 和网站兼容性是 UK 市场的底层体验指标",
        "audience": "英国本地用户、留学生、旅行用户、远程办公用户",
        "action": "建立 London/Manchester 等城市节点健康度看板，监控 CAPTCHA、银行/地图/流媒体兼容和速度。",
        "tags": ["UK servers", "CAPTCHA", "London", "Manchester"],
        "owner": "网络 + 增长",
        "impact": 87,
        "industry": 89,
        "growth": 91,
    },
    "price_free": {
        "name": "免费版 / 价格敏感 / Deal",
        "keywords": [
            "free vpn", "free", "deal", "discount", "coupon", "cheap", "pricing", "price",
            "student", "family plan", "trial", "money back", "refund", "免费", "价格", "优惠", "学生价"
        ],
        "title": "免费额度、学生价、家庭套餐和退款承诺仍是增长入口",
        "audience": "学生、轻度用户、价格敏感用户、免费 VPN 搜索用户",
        "action": "测试“免费额度 → UK/流媒体试用 → 限时付费”的路径，并突出无套路取消和价格透明。",
        "tags": ["Free VPN", "Deals", "学生", "价格敏感"],
        "owner": "增长 + 支付",
        "impact": 82,
        "industry": 79,
        "growth": 95,
    },
    "public_wifi": {
        "name": "公共 Wi‑Fi / 校园 / 移动网络",
        "keywords": [
            "public wifi", "wi-fi", "wifi", "airport", "hotel", "school", "university",
            "eduroam", "campus", "5g", "mobile", "bank", "maps", "公共", "机场", "酒店", "校园", "留学"
        ],
        "title": "公共 Wi‑Fi、校园网、5G 和旅行场景需要更少打扰的自动化体验",
        "audience": "学生、旅行/出差用户、远程办公、移动端用户",
        "action": "产品和内容围绕“自动连接、分应用/分流、低电量、兼容银行/地图/音乐 App”打包。",
        "tags": ["Public Wi‑Fi", "Student", "Mobile", "Travel"],
        "owner": "内容 + 产品",
        "impact": 83,
        "industry": 82,
        "growth": 87,
    },
    "privacy_trust": {
        "name": "隐私信任 / 审计 / 泄漏风险",
        "keywords": [
            "no logs", "no-log", "audit", "privacy policy", "webrtc", "dns leak", "leak",
            "wireguard", "open source", "closed source", "transparency", "jurisdiction",
            "tracking", "logs", "隐私", "审计", "泄漏", "日志"
        ],
        "title": "隐私信任、审计、WebRTC/DNS 泄漏和透明报告继续影响购买前判断",
        "audience": "隐私安全用户、技术用户、媒体评测读者",
        "action": "准备审计/透明报告/协议说明/WebRTC 修复证据包，让外部评测和购买前页面有可引用材料。",
        "tags": ["Privacy", "Audit", "WebRTC", "No logs"],
        "owner": "品牌 + 法务 + 安全",
        "impact": 86,
        "industry": 88,
        "growth": 84,
    },
    "gaming": {
        "name": "游戏 / Ping / Discord / 路由",
        "keywords": [
            "gaming", "game", "ping", "latency", "discord", "fortnite", "valorant", "steam",
            "packet loss", "routing", "游戏", "延迟", "掉线"
        ],
        "title": "游戏 Ping、Discord 和路由稳定性是低频但高情绪强度需求",
        "audience": "游戏玩家、宿舍/校园网络用户、Discord 社群",
        "action": "用真实节点延迟、游戏分流、Discord 语音稳定性做小规模 KOC 测试和案例页。",
        "tags": ["Gaming", "Ping", "Discord", "Routing"],
        "owner": "用户研究 + KOC",
        "impact": 73,
        "industry": 72,
        "growth": 78,
    },
    "censorship": {
        "name": "审查地区 / 协议 / 可用性测试",
        "keywords": [
            "china", "gfw", "iran", "russia", "dpi", "obfuscation", "shadowsocks", "vless",
            "reality", "trojan", "blocked country", "censorship", "审查", "中国", "伊朗", "俄罗斯", "协议"
        ],
        "title": "审查地区用户更关注协议、可用性反馈和真实测试状态",
        "audience": "高技术用户、审查地区用户、可用性测试用户",
        "action": "避免高风险承诺，改做透明可用性状态和授权测试用户反馈池。",
        "tags": ["Censorship", "Protocol", "Testing", "KOC"],
        "owner": "用户研究 + 网络",
        "impact": 76,
        "industry": 83,
        "growth": 80,
    },
    "content_affiliate": {
        "name": "媒体榜单 / KOC / Affiliate 内容",
        "keywords": [
            "best vpn", "review", "comparison", "top vpn", "affiliate", "youtube", "tiktok",
            "ranking", "guide", "how to", "评测", "榜单", "推荐"
        ],
        "title": "媒体榜单和 KOC 内容仍在重塑购买前认知，适合做场景化反向选题",
        "audience": "购买前搜索用户、YouTube/TikTok 受众、联盟评测读者",
        "action": "拆解榜单维度，制作“按场景选 VPN”：UK 节点、隐私、流媒体、价格、学生、游戏。",
        "tags": ["SEO", "Affiliate", "KOC", "Review"],
        "owner": "内容 + KOC",
        "impact": 80,
        "industry": 86,
        "growth": 89,
    },
}


def classify_item(item: Dict[str, Any]) -> List[Tuple[str, int, List[str]]]:
    text = " ".join(
        [
            item.get("title", ""),
            item.get("snippet", ""),
            item.get("source", ""),
            item.get("source_note", ""),
            item.get("key_info", ""),
            item.get("target_user", ""),
        ]
    ).lower()
    results: List[Tuple[str, int, List[str]]] = []
    for theme_id, rule in THEMES.items():
        hits: List[str] = []
        for kw in rule["keywords"]:
            if kw.lower() in text:
                hits.append(kw)
        if hits:
            score = len(set(hits)) * 10 + min(item.get("source_priority", 1), 10) * 3
            if item.get("tier") == "核心日报源":
                score += 12
            if item.get("type", "").startswith("reddit"):
                score += 7
            if item.get("type", "") in {"app-store", "google-play", "youtube-api", "google-serp"}:
                score += 5
            results.append((theme_id, score, hits[:8]))
    return sorted(results, key=lambda x: x[1], reverse=True)


def score_and_bucket_items(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in THEMES}
    for item in items:
        classifications = classify_item(item)
        item["themes"] = [{"id": tid, "score": score, "hits": hits} for tid, score, hits in classifications[:3]]
        for tid, score, hits in classifications[:3]:
            enriched = dict(item)
            enriched["theme_score"] = score
            enriched["theme_hits"] = hits
            buckets[tid].append(enriched)
    for tid in buckets:
        buckets[tid].sort(key=lambda x: (x.get("theme_score", 0), x.get("source_priority", 0)), reverse=True)
    return buckets


def compact_title_list(items: List[Dict[str, Any]], n: int = 3) -> str:
    parts = []
    for item in items[:n]:
        src = item.get("source") or item.get("platform")
        title = clean_text(item.get("title", ""), 80)
        if title:
            parts.append(f"{src}《{title}》")
    return "；".join(parts)


def make_fallback_dashboard(
    items: List[Dict[str, Any]],
    sources: List[Dict[str, str]],
    fetch_results: List[FetchResult],
    previous: Dict[str, Any],
    generated_at: dt.datetime,
    seed: Dict[str, Any],
) -> Dict[str, Any]:
    buckets = score_and_bucket_items(items)
    findings: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []

    prev_signal_map = {s.get("name"): s for s in previous.get("signals", []) if isinstance(s, dict)}
    for theme_id, bucket in buckets.items():
        if not bucket:
            continue
        rule = THEMES[theme_id]
        volume_score = min(len(bucket) * 7, 30)
        top_score = min(bucket[0].get("theme_score", 0), 60)
        heat = int(min(99, max(55, (rule["impact"] * 0.35 + rule["industry"] * 0.25 + rule["growth"] * 0.25 + volume_score * 0.5 + top_score * 0.25))))
        confidence = int(min(95, 55 + min(len(bucket), 8) * 4 + min(len({x.get("source") for x in bucket}), 6) * 3))
        priority = "P0" if heat >= 88 else ("P1" if heat >= 76 else "P2")
        evidence = (
            f"今日抓取到 {len(bucket)} 条相关信号，主要来自 "
            f"{', '.join(sorted({x.get('source') for x in bucket[:8] if x.get('source')}))}。"
            f"代表内容：{compact_title_list(bucket, 3)}。"
        )
        top = bucket[0]
        finding = {
            "priority": priority,
            "theme": rule["name"],
            "title": rule["title"],
            "source": top.get("source", ""),
            "url": top.get("url", ""),
            "evidence": clean_text(evidence, 460),
            "audience": rule["audience"],
            "impact": int(min(99, rule["impact"] + min(len(bucket), 5))),
            "industry": int(min(99, rule["industry"] + min(len({x.get('source') for x in bucket}), 4))),
            "growth": int(min(99, rule["growth"] + (3 if priority == "P0" else 0))),
            "action": rule["action"],
            "tags": rule["tags"],
            "supporting_items": [
                {
                    "title": x.get("title"),
                    "url": x.get("url"),
                    "source": x.get("source"),
                    "snippet": clean_text(x.get("snippet", ""), 220),
                    "score": x.get("theme_score"),
                    "hits": x.get("theme_hits", []),
                }
                for x in bucket[:6]
            ],
        }
        findings.append(finding)

        old_heat = (prev_signal_map.get(rule["name"]) or {}).get("heat")
        delta = heat - old_heat if isinstance(old_heat, int) else None
        if delta is None:
            direction = "新信号"
        elif delta >= 5:
            direction = f"上升 +{delta}"
        elif delta <= -5:
            direction = f"下降 {delta}"
        else:
            direction = "稳定"
        signals.append(
            {
                "name": rule["name"],
                "heat": heat,
                "confidence": confidence,
                "direction": direction,
                "owner": rule["owner"],
                "item_count": len(bucket),
            }
        )

    findings.sort(key=lambda f: ({"P0": 3, "P1": 2, "P2": 1}.get(f["priority"], 0), f["impact"] + f["growth"] + f["industry"]), reverse=True)
    findings = findings[:10]
    signals.sort(key=lambda s: s["heat"], reverse=True)
    signals = signals[:10]

    actions = []
    seen_action = set()
    for f in findings[:8]:
        tid = None
        for k, r in THEMES.items():
            if r["name"] == f["theme"]:
                tid = k
                break
        rule = THEMES.get(tid or "", {})
        lane = (rule.get("owner") or "运营").split("+")[0].strip()
        task = f["action"]
        if task in seen_action:
            continue
        seen_action.add(task)
        actions.append({"lane": lane, "task": task, "why": f"来自今日 {f['theme']} 信号", "priority": f["priority"]})

    if not findings and seed.get("findings"):
        findings = seed.get("findings", [])
        signals = seed.get("signals", [])
        actions = seed.get("actions", [])
        for f in findings:
            f.setdefault("evidence", "")
        seed_note = {
            "source": "自动抓取",
            "status": "回退",
            "note": "本次没有抓到足够公开信号，页面沿用种子日报结构；请检查网络、API Key 或来源配置。",
        }
    else:
        seed_note = None

    fetched = [r for r in fetch_results if r.status == "fetched"]
    partial = [r for r in fetch_results if r.status == "partial"]
    failed = [r for r in fetch_results if r.status == "failed"]
    limited = [r for r in fetch_results if r.status == "limited"]
    skipped = [r for r in fetch_results if r.status == "skipped"]

    limitations: List[Dict[str, str]] = []
    if seed_note:
        limitations.append(seed_note)
    for r in limited[:12]:
        limitations.append({"source": r.source_name, "status": "受限", "note": r.note})
    for r in failed[:12]:
        limitations.append({"source": r.source_name, "status": "失败", "note": r.note})

    citation_links = []
    seen_url = set()
    for f in findings:
        if f.get("url") and f["url"] not in seen_url:
            citation_links.append({"label": f.get("source") or f.get("theme"), "url": f["url"]})
            seen_url.add(f["url"])
        for it in f.get("supporting_items", [])[:3]:
            u = it.get("url")
            if u and u not in seen_url:
                citation_links.append({"label": it.get("source") or it.get("title"), "url": u})
                seen_url.add(u)
    if not citation_links and seed.get("citation_links"):
        citation_links = seed.get("citation_links", [])

    stats = {
        "total_sources": len(sources),
        "due_sources": len(fetch_results),
        "fetched_sources": len(fetched),
        "partial_sources": len(partial),
        "limited_sources": len(limited),
        "failed_sources": len(failed),
        "skipped_sources": len(skipped),
        "raw_items": len(items),
        "candidate": sum(1 for s in sources if s.get("是否候选") == "是"),
        "sustainable": sum(1 for s in sources if s.get("是否可持续追踪") == "是"),
        "daily": sum(1 for s in sources if "每日" in s.get("追踪频率", "")),
        "weekly": sum(1 for s in sources if "每周" in s.get("追踪频率", "")),
        "monthly": sum(1 for s in sources if "每月" in s.get("追踪频率", "")),
        "low": sum(1 for s in sources if "低频" in s.get("追踪频率", "")),
    }

    return {
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M Asia/Singapore"),
        "generated_at_iso": generated_at.isoformat(),
        "date": generated_at.strftime("%Y-%m-%d"),
        "timezone": TZ_NAME,
        "stats": stats,
        "findings": findings,
        "signals": signals,
        "actions": actions,
        "limitations": limitations,
        "source_health": [r.__dict__ for r in fetch_results],
        "sources": sources,
        "raw_items": sorted(items, key=lambda x: (x.get("source_priority", 0), len(x.get("themes", []))), reverse=True)[:MAX_RAW_ITEMS],
        "citation_links": citation_links[:60],
        "method": {
            "summary": "公开来源自动抓取 + 规则打分；若配置 OPENAI_API_KEY 且 OPENAI_MODEL 可启用 LLM 二次整合。",
            "principles": ["最重要", "受众最关切", "行业最相关", "增长需求点"],
        },
    }


def try_llm_enhance(data: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        return data

    # This block is optional. If it fails, the deterministic dashboard remains valid.
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=api_key)
        items = data.get("raw_items", [])[:80]
        prompt = {
            "role": "system",
            "content": (
                "你是 VPN 行业增长情报分析师。请只输出 JSON，不要 Markdown。"
                "根据输入的公开来源条目，按四个原则：最重要、受众最关切、行业最相关、增长需求点，"
                "生成中文日报面板字段：findings, signals, actions。"
            ),
        }
        user = {
            "role": "user",
            "content": json.dumps(
                {
                    "current_stats": data.get("stats"),
                    "existing_findings": data.get("findings", [])[:10],
                    "raw_items": [
                        {
                            "title": x.get("title"),
                            "source": x.get("source"),
                            "url": x.get("url"),
                            "snippet": x.get("snippet"),
                            "themes": x.get("themes"),
                            "metrics": x.get("metrics"),
                        }
                        for x in items
                    ],
                    "required_schema": {
                        "findings": "array of {priority,theme,title,source,url,evidence,audience,impact,industry,growth,action,tags}",
                        "signals": "array of {name,heat,confidence,direction,owner,item_count}",
                        "actions": "array of {lane,task,why,priority}",
                    },
                },
                ensure_ascii=False,
            )[:120000],
        }
        resp = client.chat.completions.create(
            model=model,
            messages=[prompt, user],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        enhanced = json.loads(text)
        for key in ["findings", "signals", "actions"]:
            if isinstance(enhanced.get(key), list) and enhanced[key]:
                data[key] = enhanced[key]
        data["method"]["llm_enhanced"] = True
        data["method"]["llm_model"] = model
        return data
    except Exception as exc:
        data.setdefault("limitations", []).insert(
            0,
            {"source": "LLM 二次整合", "status": "回退到规则引擎", "note": f"{type(exc).__name__}: {exc}"},
        )
        data["method"]["llm_enhanced"] = False
        return data


def priority_class(p: str) -> str:
    return {"P0": "p0", "P1": "p1", "P2": "p2"}.get(p, "")


def make_bar(value: Any) -> str:
    try:
        v = max(0, min(100, int(float(value))))
    except Exception:
        v = 0
    return f'<span class="bar"><i style="width:{v}%"></i><b>{v}</b></span>'


def render_html(data: Dict[str, Any]) -> str:
    j = json.dumps(data, ensure_ascii=False)
    stats = data.get("stats", {})
    findings = data.get("findings", [])
    signals = data.get("signals", [])
    actions = data.get("actions", [])
    source_health = data.get("source_health", [])
    raw_items = data.get("raw_items", [])
    limitations = data.get("limitations", [])
    sources = data.get("sources", [])
    generated = data.get("generated_at", "")
    date_text = data.get("date", "")

    finding_cards = []
    for f in findings:
        scores = (
            f'<div class="scores"><label>重要性 {make_bar(f.get("impact", 0))}</label>'
            f'<label>行业相关 {make_bar(f.get("industry", 0))}</label>'
            f'<label>增长价值 {make_bar(f.get("growth", 0))}</label></div>'
        )
        tags = "".join(f"<span>{html.escape(str(t))}</span>" for t in f.get("tags", []))
        support = ""
        for it in f.get("supporting_items", [])[:3]:
            support += f'<li><a href="{html.escape(it.get("url",""))}" target="_blank" rel="noopener">{html.escape(clean_text(it.get("title",""), 96))}</a> <em>{html.escape(it.get("source",""))}</em></li>'
        if support:
            support = f"<details><summary>查看支撑条目</summary><ul>{support}</ul></details>"
        finding_cards.append(
            f"""
            <article class="card finding" data-priority="{html.escape(f.get("priority",""))}" data-search="{html.escape(json.dumps(f, ensure_ascii=False).lower())}">
              <div class="card-top"><span class="pill {priority_class(f.get("priority",""))}">{html.escape(f.get("priority",""))}</span><span class="theme">{html.escape(f.get("theme",""))}</span><span class="score">综合 {(f.get("impact",0)+f.get("industry",0)+f.get("growth",0))//3}</span></div>
              <h3><a href="{html.escape(f.get("url",""))}" target="_blank" rel="noopener">{html.escape(f.get("title",""))}</a></h3>
              <p class="evidence">{html.escape(f.get("evidence",""))}</p>
              <p class="audience"><b>受众：</b>{html.escape(f.get("audience",""))}</p>
              {scores}
              <p class="action"><b>建议动作：</b>{html.escape(f.get("action",""))}</p>
              {support}
              <div class="tags">{tags}</div>
            </article>
            """
        )

    signal_rows = []
    for s in signals:
        signal_rows.append(
            f'<tr><td>{html.escape(s.get("name",""))}</td><td>{make_bar(s.get("heat",0))}</td><td>{make_bar(s.get("confidence",0))}</td><td>{html.escape(str(s.get("direction","")))}</td><td>{html.escape(s.get("owner",""))}</td><td>{html.escape(str(s.get("item_count","")))}</td></tr>'
        )

    action_cards = []
    for a in actions:
        action_cards.append(
            f"""
            <div class="action-card">
              <div><span class="pill {priority_class(a.get("priority",""))}">{html.escape(a.get("priority",""))}</span><b>{html.escape(a.get("lane",""))}</b></div>
              <p>{html.escape(a.get("task",""))}</p>
              <small>{html.escape(a.get("why",""))}</small>
            </div>
            """
        )

    health_rows = []
    for r in source_health:
        cls = r.get("status", "")
        health_rows.append(
            f'<tr class="{html.escape(cls)}"><td>{html.escape(r.get("source_name",""))}</td><td>{html.escape(r.get("platform",""))}</td><td>{html.escape(r.get("status",""))}</td><td>{html.escape(str(r.get("item_count","")))}</td><td>{html.escape(str(r.get("elapsed_ms","")))}</td><td>{html.escape(r.get("note",""))}</td></tr>'
        )

    raw_rows = []
    for it in raw_items[:120]:
        theme_text = ", ".join(t.get("id", "") for t in it.get("themes", [])[:2])
        raw_rows.append(
            f'<tr data-search="{html.escape(json.dumps(it, ensure_ascii=False).lower())}"><td>{html.escape(it.get("source",""))}</td><td>{html.escape(it.get("platform",""))}</td><td><a href="{html.escape(it.get("url",""))}" target="_blank" rel="noopener">{html.escape(clean_text(it.get("title",""), 110))}</a><br><small>{html.escape(clean_text(it.get("snippet",""), 160))}</small></td><td>{html.escape(theme_text)}</td><td>{html.escape(str(it.get("source_priority","")))}</td></tr>'
        )

    source_rows = []
    for s in sources:
        source_rows.append(
            f'<tr data-tier="{html.escape(s.get("日报层级",""))}" data-search="{html.escape(json.dumps(s, ensure_ascii=False).lower())}"><td>{html.escape(s.get("来源类别",""))}</td><td>{html.escape(s.get("平台",""))}</td><td>{html.escape(s.get("来源名称",""))}</td><td><a href="{html.escape(s.get("URL/入口",""))}" target="_blank" rel="noopener">{html.escape(clean_text(s.get("URL/入口",""),80))}</a></td><td>{html.escape(s.get("追踪频率",""))}</td><td>{html.escape(s.get("日报层级",""))}</td><td>{html.escape(str(s.get("监控优先级分","")))}</td><td>{html.escape(s.get("备注",""))}</td></tr>'
        )

    limitation_rows = []
    for l in limitations:
        limitation_rows.append(f'<li><b>{html.escape(l.get("source",""))}</b>：{html.escape(l.get("status",""))} — {html.escape(l.get("note",""))}</li>')

    citations = []
    for c in data.get("citation_links", [])[:50]:
        citations.append(f'<li><a href="{html.escape(c.get("url",""))}" target="_blank" rel="noopener">{html.escape(c.get("label",""))}</a></li>')

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>VPN UK 信息源观察面板｜{html.escape(date_text)} 自动日报</title>
<style>
:root{{--bg:#f6f7fb;--panel:#fff;--ink:#14213d;--muted:#64748b;--line:#e5e7eb;--brand:#2447f9;--brand2:#00a896;--warn:#f59e0b;--danger:#ef4444;--soft:#eef2ff;--shadow:0 16px 40px rgba(15,23,42,.08);--radius:20px}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(180deg,#edf2ff 0,#f8fafc 280px,var(--bg) 100%);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",Arial,sans-serif}}
a{{color:var(--brand);text-decoration:none}} a:hover{{text-decoration:underline}}
header,main,footer{{max-width:1440px;margin:auto}} header{{padding:32px 32px 18px}} main{{padding:0 32px 48px;display:grid;gap:22px}} footer{{padding:0 32px 36px;color:var(--muted)}}
.hero{{display:grid;grid-template-columns:1.45fr .95fr;gap:22px}} .hero-main,.hero-side,.panel,.card{{background:rgba(255,255,255,.94);border:1px solid rgba(148,163,184,.25);border-radius:var(--radius);box-shadow:var(--shadow)}}
.hero-main{{padding:30px;position:relative;overflow:hidden}} .hero-main:after{{content:"";position:absolute;right:-110px;top:-120px;width:280px;height:280px;border-radius:50%;background:radial-gradient(circle,rgba(36,71,249,.20),transparent 70%)}}
h1{{font-size:32px;line-height:1.16;margin:0 0 10px;letter-spacing:-.02em}} h2{{font-size:22px;margin:0 0 18px}} h3{{font-size:17px;margin:8px 0}}
.subtitle{{color:var(--muted);max-width:860px;margin:0}} .hero-side{{padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.kpi{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px}} .kpi b{{display:block;font-size:28px;line-height:1;color:var(--brand);margin-bottom:6px}} .kpi span{{color:var(--muted)}}
.controls{{max-width:1440px;margin:0 auto 20px;padding:0 32px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}}
.control{{border:1px solid var(--line);background:white;border-radius:999px;padding:9px 14px;box-shadow:0 6px 20px rgba(15,23,42,.04)}} input.control{{min-width:280px;outline:none}} select.control,button.control{{outline:none;cursor:pointer}}
.grid-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}} .grid-2{{display:grid;grid-template-columns:1.05fr .95fr;gap:20px}} .panel{{padding:22px}}
.card{{padding:18px;display:flex;flex-direction:column;gap:9px}} .card-top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.pill{{display:inline-flex;align-items:center;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;background:#e2e8f0;color:#334155}} .p0{{background:#fee2e2;color:#b91c1c}} .p1{{background:#fef3c7;color:#92400e}} .p2{{background:#dcfce7;color:#166534}}
.theme{{font-size:12px;color:var(--muted)}} .score{{margin-left:auto;font-weight:700;color:var(--brand2);font-size:13px}} .evidence{{margin:0;color:#334155}} .audience{{margin:0;color:#475569}}
.scores{{display:grid;gap:6px}} .scores label{{display:grid;grid-template-columns:78px 1fr;gap:8px;align-items:center;color:var(--muted);font-size:12px}}
.bar{{display:grid;grid-template-columns:1fr 34px;gap:8px;align-items:center}} .bar:before{{content:"";height:8px;background:#e5e7eb;border-radius:999px;grid-column:1;grid-row:1}} .bar i{{display:block;height:8px;background:linear-gradient(90deg,var(--brand),var(--brand2));border-radius:999px;grid-column:1;grid-row:1}} .bar b{{font-size:12px;color:#475569}}
.action{{margin:0;padding:10px 12px;border-radius:12px;background:#f8fafc;color:#334155}} .tags{{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto}} .tags span{{font-size:12px;background:#eef2ff;color:#3730a3;border-radius:999px;padding:3px 8px}}
.action-card{{border:1px solid var(--line);border-radius:16px;background:#fff;padding:14px;margin-bottom:10px}} .action-card p{{margin:8px 0 4px}} .action-card small{{color:var(--muted)}} details{{font-size:12px;color:#475569}} details ul{{padding-left:18px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px}} table{{width:100%;border-collapse:collapse;background:white}} th,td{{border-bottom:1px solid var(--line);padding:10px 11px;text-align:left;vertical-align:top}} th{{background:#f8fafc;color:#475569;font-size:12px;text-transform:uppercase;letter-spacing:.02em}} td{{font-size:13px}} tr:hover td{{background:#fafafa}} small{{color:var(--muted)}} tr.failed td{{background:#fff7f7}} tr.limited td{{background:#fffbeb}} tr.partial td{{background:#f0fdf4}}
.notice{{border-left:4px solid var(--warn);background:#fffbeb;padding:12px 14px;border-radius:12px;color:#92400e}} .source-list{{columns:2}} .source-list li{{break-inside:avoid;margin:4px 0}}
@media(max-width:980px){{.hero,.grid-2,.grid-3{{grid-template-columns:1fr}} header,main,footer,.controls{{padding-left:16px;padding-right:16px}}}}
</style>
</head>
<body>
<header>
  <div class="hero">
    <section class="hero-main">
      <h1>VPN UK 信息源观察面板</h1>
      <p class="subtitle">自动日报版。按“最重要、受众最关切、行业最相关、增长需求点”聚合公开来源、社区、媒体评测、搜索/应用商店信号。生成时间：{html.escape(generated)}</p>
    </section>
    <aside class="hero-side">
      <div class="kpi"><b>{stats.get("raw_items",0)}</b><span>今日抓取条目</span></div>
      <div class="kpi"><b>{stats.get("fetched_sources",0)}</b><span>成功来源</span></div>
      <div class="kpi"><b>{stats.get("limited_sources",0)}</b><span>受限来源</span></div>
      <div class="kpi"><b>{len(findings)}</b><span>重点判断</span></div>
    </aside>
  </div>
</header>

<div class="controls">
  <input id="searchBox" class="control" placeholder="搜索主题 / 来源 / 竞品 / 场景…" />
  <select id="priorityFilter" class="control"><option value="all">全部优先级</option><option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option></select>
  <select id="tierFilter" class="control"><option value="all">全部来源层级</option><option>核心日报源</option><option>周更重点源</option><option>观察池</option></select>
  <button class="control" onclick="downloadJSON()">下载今日 JSON</button>
</div>

<main>
  <section class="panel">
    <h2>今日重点判断</h2>
    <div class="grid-3">{"".join(finding_cards)}</div>
  </section>

  <section class="grid-2">
    <div class="panel">
      <h2>信号热度矩阵</h2>
      <div class="table-wrap"><table><thead><tr><th>信号</th><th>热度</th><th>置信</th><th>趋势</th><th>建议 Owner</th><th>条目数</th></tr></thead><tbody>{"".join(signal_rows)}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>增长行动板</h2>
      {"".join(action_cards)}
    </div>
  </section>

  <section class="panel">
    <h2>原始条目池</h2>
    <div class="table-wrap"><table id="rawTable"><thead><tr><th>来源</th><th>平台</th><th>标题/摘要</th><th>主题</th><th>来源权重</th></tr></thead><tbody>{"".join(raw_rows)}</tbody></table></div>
  </section>

  <section class="grid-2">
    <div class="panel">
      <h2>抓取健康度</h2>
      <div class="table-wrap"><table><thead><tr><th>来源</th><th>平台</th><th>状态</th><th>条目</th><th>耗时 ms</th><th>备注</th></tr></thead><tbody>{"".join(health_rows)}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>受限与人工补充</h2>
      <p class="notice">X/TikTok/Discord/部分搜索和商店评论可能需要 API、登录权限或第三方监听工具。脚本会自动保留受限说明，不会中断日报生成。</p>
      <ul>{"".join(limitation_rows)}</ul>
    </div>
  </section>

  <section class="panel">
    <h2>来源池</h2>
    <div class="table-wrap"><table id="sourceTable"><thead><tr><th>类别</th><th>平台</th><th>来源</th><th>入口</th><th>频率</th><th>层级</th><th>优先级</th><th>备注</th></tr></thead><tbody>{"".join(source_rows)}</tbody></table></div>
  </section>

  <section class="panel">
    <h2>证据来源索引</h2>
    <ol class="source-list">{"".join(citations)}</ol>
  </section>
</main>

<footer>
  <p>自动更新机制：GitHub Actions 每天按 UTC cron 运行脚本，脚本生成静态 HTML/JSON 并提交到仓库，GitHub Pages 从 docs 目录发布。</p>
</footer>

<script>
const DATA = {j};
function applyFilters() {{
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  const p = document.getElementById('priorityFilter').value;
  const t = document.getElementById('tierFilter').value;
  document.querySelectorAll('.finding').forEach(card => {{
    const okP = p === 'all' || card.dataset.priority === p;
    const okQ = !q || card.dataset.search.includes(q);
    card.style.display = (okP && okQ) ? '' : 'none';
  }});
  document.querySelectorAll('#sourceTable tbody tr').forEach(row => {{
    const okT = t === 'all' || row.dataset.tier === t;
    const okQ = !q || row.dataset.search.includes(q);
    row.style.display = (okT && okQ) ? '' : 'none';
  }});
  document.querySelectorAll('#rawTable tbody tr').forEach(row => {{
    const okQ = !q || row.dataset.search.includes(q);
    row.style.display = okQ ? '' : 'none';
  }});
}}
document.getElementById('searchBox').addEventListener('input', applyFilters);
document.getElementById('priorityFilter').addEventListener('change', applyFilters);
document.getElementById('tierFilter').addEventListener('change', applyFilters);
function downloadJSON() {{
  const blob = new Blob([JSON.stringify(DATA, null, 2)], {{type:'application/json;charset=utf-8'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'vpn_source_observation_data_' + DATA.date + '.json'; a.click();
  URL.revokeObjectURL(url);
}}
</script>
</body>
</html>"""
    return html_doc


def render_markdown_report(data: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# VPN UK 信息源自动日报｜{data.get('date')}")
    lines.append("")
    lines.append(f"生成时间：{data.get('generated_at')}")
    lines.append("")
    lines.append("## 今日重点")
    for f in data.get("findings", [])[:8]:
        lines.append(f"- **{f.get('priority')}｜{f.get('theme')}**：{f.get('title')}")
        lines.append(f"  - 证据：{f.get('evidence')}")
        lines.append(f"  - 动作：{f.get('action')}")
    lines.append("")
    lines.append("## 信号热度")
    for s in data.get("signals", [])[:10]:
        lines.append(f"- {s.get('name')}：热度 {s.get('heat')}，置信 {s.get('confidence')}，趋势 {s.get('direction')}，Owner {s.get('owner')}")
    lines.append("")
    lines.append("## 来源健康度")
    st = data.get("stats", {})
    lines.append(f"- 成功来源：{st.get('fetched_sources')}；部分可用：{st.get('partial_sources')}；受限：{st.get('limited_sources')}；失败：{st.get('failed_sources')}；原始条目：{st.get('raw_items')}")
    lines.append("")
    lines.append("## 受限说明")
    for l in data.get("limitations", [])[:12]:
        lines.append(f"- {l.get('source')}：{l.get('status')} — {l.get('note')}")
    lines.append("")
    return "\n".join(lines)


def dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output = []
    for it in items:
        key = it.get("url") or (it.get("source", "") + "|" + it.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(it)
    return output


def run(offline: bool = False, full_scan: bool = False) -> Dict[str, Any]:
    ensure_dirs()
    generated_at = now_sg()
    today = generated_at.date()
    seed = load_seed()
    sources = load_sources()
    previous = {}
    latest_path = DATA_DIR / "latest.json"
    if latest_path.exists():
        try:
            previous = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    all_items: List[Dict[str, Any]] = []
    fetch_results: List[FetchResult] = []

    manual_items = load_manual_items(today)
    if manual_items:
        all_items.extend(manual_items)
        fetch_results.append(
            FetchResult(
                source_name="手工补充",
                platform="Manual",
                url=str(CONFIG_DIR / "manual_inputs.csv"),
                status="fetched",
                item_count=len(manual_items),
                note="已纳入 config/manual_inputs.csv 中当天或无日期的手工信号",
                elapsed_ms=0,
            )
        )

    for source in sources:
        due = should_fetch_source(source, today, full_scan=full_scan)
        if offline:
            due = False
        if not due:
            fetch_results.append(
                FetchResult(
                    source_name=source.get("来源名称", ""),
                    platform=source.get("平台", ""),
                    url=source.get("URL/入口", ""),
                    status="skipped",
                    item_count=0,
                    note="未到该来源追踪频率",
                    elapsed_ms=0,
                )
            )
            continue
        items, result = fetch_source(source)
        fetch_results.append(result)
        all_items.extend(items)

    all_items = dedupe_items(all_items)

    data = make_fallback_dashboard(
        items=all_items,
        sources=sources,
        fetch_results=fetch_results,
        previous=previous,
        generated_at=generated_at,
        seed=seed,
    )
    data = try_llm_enhance(data)

    html_doc = render_html(data)
    (DOCS_DIR / "index.html").write_text(html_doc, encoding="utf-8")
    latest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARCHIVE_DIR / f"{data['date']}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS_DIR / f"{data['date']}.md").write_text(render_markdown_report(data), encoding="utf-8")
    (STATUS_DIR / "last_run.json").write_text(
        json.dumps(
            {
                "generated_at": data.get("generated_at"),
                "date": data.get("date"),
                "stats": data.get("stats"),
                "method": data.get("method"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the daily VPN source observation dashboard.")
    parser.add_argument("--offline", action="store_true", help="Do not fetch the web; use seed/fallback content. Useful for testing.")
    parser.add_argument("--full-scan", action="store_true", help="Ignore source frequency and scan every configured source.")
    args = parser.parse_args(argv)
    data = run(offline=args.offline, full_scan=args.full_scan)
    print(json.dumps({"ok": True, "date": data.get("date"), "stats": data.get("stats")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
