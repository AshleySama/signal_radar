"""Build the static feed consumed by the GitHub Pages site.

The workflow reads public publisher and developer-community RSS/Atom feeds.
It stores headlines, short descriptions, timestamps and links, rather than
republishing the source articles.
"""

from __future__ import annotations

import html
import hashlib
import json
import random
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "radar.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
ARCHIVE_INDEX = ARCHIVE_DIR / "index.json"
LATEST_ARCHIVE = ARCHIVE_DIR / "latest.json"
READER_DIR = ROOT / "data" / "readers"
NOW = datetime.now(timezone.utc)

SOURCES = (
    # Official sources are used only for high-impact technical primary releases.
    {"name": "Stack Overflow Blog", "url": "https://stackoverflow.blog/feed/", "weight": 10, "kind": "editorial", "track": "developer"},
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 15, "kind": "official", "track": "regular"},
    {"name": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml", "weight": 15, "kind": "official", "track": "regular"},
)
HTML_SOURCES = (
    {"name": "量子位", "url": "https://www.qbitai.com/wp-json/wp/v2/posts?per_page=24&_fields=date,link,title,excerpt", "weight": 12, "format": "wordpress", "track": "regular"},
    {"name": "AI 前线 / InfoQ 中文", "url": "https://www.infoq.cn/", "weight": 12, "track": "developer"},
    {
        "name": "InfoQ 中文 · 架构与云原生",
        "url": "https://www.infoq.cn/topic/architecture",
        "weight": 12,
        "track": "developer",
        "include_keywords": ("架构", "云原生", "微服务", "平台工程", "DevOps", "可观测", "系统设计", "AI", "开源", "安全"),
    },
    {
        "name": "腾讯云开发者社区 · 软件架构",
        "url": "https://developer.cloud.tencent.com/tag/17420",
        "weight": 8,
        "track": "developer",
        "include_keywords": ("架构", "云原生", "微服务", "Serverless", "Kubernetes", "数据库", "系统设计", "性能", "安全", "开源", "Rust", "AI"),
    },
    # The public RSS endpoint currently serves a data-service page instead of articles.
    # Keep this probe visible in job logs; it can be enabled when the publisher restores it.
    {"name": "机器之心", "url": "https://www.jiqizhixin.com/", "weight": 12, "track": "regular"},
)

KEYWORDS = (
    "model", "reasoning", "agent", "api", "codex", "claude", "gemini",
    "release", "open source", "benchmark", "research", "developer", "code",
    "模型", "推理", "智能体", "开发", "发布", "开源", "研究", "架构", "云原生",
    "微服务", "平台工程", "devops", "可观测", "系统设计", "数据库", "黑客松", "训练营",
)
BLOCKED = (
    "election", "president", "politic", "war", "military", "weapon", "terror",
    "violence", "sexual", "porn", "policy", "government", "politics", "政治", "选举", "战争", "军事", "武器",
    "暴力", "色情",
)
LOW_SIGNAL = (
    "pixel", "game", "shopping", "travel", "music", "celebrity", "giveaway", "jobs",
    "抽奖", "游戏攻略", "明星", "旅游", "购物",
    "产品推荐官", "厂家直供", "技术选型服务", "扫码咨询", "商务合作",
)
MAX_AGE_DAYS = 3
REGULAR_MAX_ITEMS = 20
DEVELOPER_MIN_ITEMS = 5
DEVELOPER_MAX_ITEMS = 10
ENGLISH_MAX_ITEMS = 2
FALLBACK_SOURCE_LIMIT = 7
MAX_READER_CHARS = 6000
MAX_READER_PARAGRAPHS = 16
BACKGROUND_ASSETS = (
    "./assets/backgrounds/paper-signal.png",
    "./assets/backgrounds/scientific-archive.png",
)
ROUTINE_RELEASE = re.compile(r"^(?:release\s+)?v?\d+\.\d+\.\d+", re.IGNORECASE)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str | None) -> str:
    text = decode_escaped_text(value)
    text = re.sub(r"<\s*\\?/?\s*[A-Za-z][^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def decode_escaped_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), text)
    return text.replace("\\/", "/").replace("\\\"", "\"").replace("\\n", "\n").replace("\\t", " ")


def clean_reader_text(value: str | None) -> str:
    text = decode_escaped_text(value)
    text = re.sub(r"<\s*\\?/?\s*[A-Za-z][^>]*>", " ", text)
    return clean_text(text)


def first_child_text(node: ET.Element, names: set[str]) -> str:
    for child in node:
        if local_name(child.tag) in names:
            value = clean_text(child.text)
            if value:
                return value
    return ""


def entry_link(node: ET.Element) -> str:
    for child in node:
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"alternate", ""}:
            return href
        if clean_text(child.text):
            return clean_text(child.text)
    return ""


def parse_time(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_xml(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": "FuncDance-Signal-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        xml_text = response.read().decode("utf-8", errors="replace")
    # A few publisher feeds contain bare ampersands despite declaring XML.
    xml_text = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]+;)", "&amp;", xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    return ET.fromstring(xml_text)


def github_trending_page_entries() -> list[dict]:
    request = urllib.request.Request(
        "https://github.com/trending?since=weekly",
        headers={"User-Agent": "FuncDance-Signal-Radar/1.0"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        page = response.read().decode("utf-8", errors="replace")
    articles = re.findall(r"<article[^>]*Box-row[^>]*>(.*?)</article>", page, re.DOTALL)
    leaders: list[str] = []
    for article in articles:
        repository = re.search(r'href="/([^"/]+/[^"/?#]+)"', article)
        if not repository:
            continue
        name = repository.group(1)
        description = clean_text(next(iter(re.findall(r"<p[^>]*>(.*?)</p>", article, re.DOTALL)), ""))
        language = clean_text(next(iter(re.findall(r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', article, re.DOTALL)), ""))
        stars_this_week = re.search(r"([\d,]+)\s+stars\s+this\s+week", article)
        stars_label = stars_this_week.group(1) if stars_this_week else "近期热门"
        label = name + (f"（+{stars_label} stars）" if stars_label != "近期热门" else "")
        leaders.append(label)
    if not leaders:
        return []
    return [{
        "title": "GitHub Trending 周榜",
        "titleZh": "GitHub Trending 周榜",
        "summary": description,
        "summaryZh": "本周开源热度：" + " · ".join(leaders[:5]),
        "source": "GitHub Trending",
        "url": "https://github.com/trending?since=weekly",
        "publishedAt": NOW.isoformat().replace("+00:00", "Z"),
        "score": 18,
        "language": "zh",
        "track": "developer",
    }]


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "FuncDance-Signal-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def structured_page_text(page: str) -> list[str]:
    values: list[str] = []
    for raw in re.findall(r"<script\b[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.DOTALL | re.IGNORECASE):
        try:
            payload = json.loads(html.unescape(raw).strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key in ("articleBody", "description"):
                    if isinstance(value.get(key), str):
                        values.append(value[key])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
    values.extend(re.findall(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\']([^"\']+)',
        page,
        re.IGNORECASE,
    ))
    return values


def reader_paragraphs(page: str, fallback: str) -> list[str]:
    """Keep a short on-site reading extract, not a mirrored full article."""
    candidates = structured_page_text(page)
    page = re.sub(r"<(?:script|style|svg|noscript)[^>]*>.*?</(?:script|style|svg|noscript)>", " ", page, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(re.findall(r"<p[^>]*>(.*?)</p>", page, re.DOTALL | re.IGNORECASE))
    candidates = [clean_reader_text(value) for value in candidates]
    output: list[str] = []
    used: set[str] = set()
    length = 0
    for paragraph in candidates:
        key = paragraph.lower()
        if len(paragraph) < 55 or key in used:
            continue
        if length + len(paragraph) > MAX_READER_CHARS:
            break
        output.append(paragraph)
        used.add(key)
        length += len(paragraph)
        if len(output) == MAX_READER_PARAGRAPHS:
            break
    return output or ([fallback] if fallback else ["该来源暂未生成可读节选。"])


def cache_reader_items(items: list[dict]) -> None:
    READER_DIR.mkdir(parents=True, exist_ok=True)
    for item in items:
        digest = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()[:16]
        filename = f"{digest}.json"
        fallback = item.get("summaryZh") or item.get("summary") or ""
        if item["source"] == "GitHub Trending":
            paragraphs = [fallback]
        else:
            try:
                paragraphs = reader_paragraphs(fetch_text(item["url"]), fallback)
            except Exception as error:
                print(f"Reader cache {item['source']}: {error}", file=sys.stderr)
                paragraphs = [fallback] if fallback else ["该来源暂未生成可读节选。"]
        reader_payload = {
            "title": item.get("titleZh") or item.get("title", ""),
            "source": item["source"],
            "publishedAt": item["publishedAt"],
            "url": item["url"],
            "paragraphs": paragraphs,
            "isExcerpt": True,
        }
        (READER_DIR / filename).write_text(
            json.dumps(reader_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        item["readerFile"] = f"./data/readers/{filename}"


def meta_content(page: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            page,
            re.IGNORECASE,
        )
        if not match:
            match = re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']',
                page,
                re.IGNORECASE,
            )
        if match:
            return clean_text(match.group(1))
    return ""


def page_date(page: str, fallback: str = "") -> datetime | None:
    visible_date = re.search(
        r'class=["\'][^"\']*date-channel-detail[^"\']*["\'][^>]*>.*?\b(20\d{2}-\d{1,2}-\d{1,2})\b',
        page,
        re.DOTALL,
    )
    candidates = [visible_date.group(1)] if visible_date else []
    candidates.extend(re.findall(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", page))
    if fallback:
        candidates.append(fallback)
    parsed_dates: list[datetime] = []
    for value in candidates:
        normalized = value.replace("/", "-").replace(" ", "T")
        try:
            parsed_dates.append(datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    if not parsed_dates:
        return None
    # InfoQ serializes unrelated historical dates in page state. Prefer the
    # visible article date; otherwise take the newest non-future candidate.
    if visible_date:
        return parsed_dates[0]
    eligible = [value for value in parsed_dates if value <= NOW + timedelta(days=1)]
    return max(eligible or parsed_dates)


def html_source_candidates(source_name: str, page: str) -> list[tuple[str, str]]:
    if source_name == "量子位":
        links = re.findall(r'href=["\'](https://www\.qbitai\.com/20\d{2}/\d{2}/\d+\.html)["\']', page)
    elif source_name in {"AI 前线 / InfoQ 中文", "InfoQ 中文 · 架构与云原生"}:
        links = re.findall(r'href=["\'](https://www\.infoq\.cn/article/[A-Za-z0-9]+)["\']', page)
    elif source_name == "腾讯云开发者社区 · 软件架构":
        links = re.findall(r'href=["\'](https://developer\.cloud\.tencent\.com/article/\d+[^"\']*)["\']', page)
    else:
        links = []
    return list(dict.fromkeys((link, link) for link in links))[:24]


def html_source_entries(source_name: str, url: str, weight: int, include_keywords: tuple[str, ...] = ()) -> list[dict]:
    homepage = fetch_text(url)
    output: list[dict] = []
    if source_name == "量子位":
        for article in json.loads(homepage):
            title = clean_text(article.get("title", {}).get("rendered", ""))
            summary = clean_text(article.get("excerpt", {}).get("rendered", ""))
            published_at = parse_time(article.get("date", ""))
            combined = f"{title} {summary}".lower()
            if (not title or published_at is None or any(term in combined for term in BLOCKED)
                    or any(term in combined for term in LOW_SIGNAL)
                    or (include_keywords and not any(term.lower() in combined for term in include_keywords))):
                continue
            age_days = max((NOW - published_at).total_seconds() / 86400, 0)
            if age_days > MAX_AGE_DAYS:
                continue
            keyword_score = sum(term in combined for term in KEYWORDS)
            output.append({
                "title": title[:180],
                "summary": summary[:280],
                "source": source_name,
                "url": article.get("link", ""),
                "publishedAt": published_at.isoformat().replace("+00:00", "Z"),
                "score": round(weight + min(keyword_score, 5) * 2 + max(0, 8 - age_days * 1.2), 2),
                "language": "zh",
            })
        return [item for item in output if item["url"]]
    for link, _ in html_source_candidates(source_name, homepage):
        article = fetch_text(link)
        title = meta_content(article, ("og:title", "twitter:title"))
        summary = meta_content(article, ("og:description", "description"))
        fallback_date = ""
        if source_name == "量子位":
            matched = re.search(r"/(20\d{2}/\d{2}/\d+)/", link)
            fallback_date = matched.group(1) if matched else ""
        published_at = page_date(article, fallback_date)
        combined = f"{title} {summary}".lower()
        if (not title or published_at is None or any(term in combined for term in BLOCKED)
                or any(term in combined for term in LOW_SIGNAL)
                or (include_keywords and not any(term.lower() in combined for term in include_keywords))):
            continue
        age_days = max((NOW - published_at).total_seconds() / 86400, 0)
        if age_days > MAX_AGE_DAYS:
            continue
        keyword_score = sum(term in combined for term in KEYWORDS)
        output.append({
            "title": title[:180],
            "summary": summary[:280],
            "source": source_name,
            "url": link,
            "publishedAt": published_at.isoformat().replace("+00:00", "Z"),
            "score": round(weight + min(keyword_score, 5) * 2 + max(0, 8 - age_days * 1.2), 2),
            "language": "zh",
        })
    return output


def source_entries(source_name: str, url: str, weight: int, source_kind: str) -> list[dict]:
    root = fetch_xml(url)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    output: list[dict] = []
    latest_overdue_allowed = source_name == "Stack Overflow Blog"
    for node in entries:
        title = first_child_text(node, {"title"})
        link = entry_link(node)
        summary = first_child_text(node, {"description", "summary", "content", "subtitle"})
        published = first_child_text(node, {"published", "updated", "pubdate", "date"})
        combined = f"{title} {summary}".lower()
        if not title or not link or any(term in combined for term in BLOCKED) or any(term in combined for term in LOW_SIGNAL):
            continue
        published_at = parse_time(published)
        if published_at is None:
            continue
        age_days = max((NOW - published_at).total_seconds() / 86400, 0)
        if age_days > MAX_AGE_DAYS and (not latest_overdue_allowed or output):
            continue
        title_lower = title.lower()
        if source_name in {"OpenAI Codex", "Claude Code", "Gemini CLI"} and (
            "nightly" in title_lower or "preview" in title_lower or ROUTINE_RELEASE.match(title)
        ):
            continue
        keyword_score = sum(term in combined for term in KEYWORDS)
        community_bonus = 3 if source_kind == "community" else 0
        score = weight + community_bonus + min(keyword_score, 5) * 2 + max(0, 8 - age_days * 1.2)
        output.append({
            "title": title[:180],
            "summary": summary[:280],
            "source": source_name,
            "url": link,
            "publishedAt": published_at.isoformat().replace("+00:00", "Z"),
            "score": round(score if age_days <= MAX_AGE_DAYS else 1, 2),
            "language": "zh" if is_chinese(f"{title} {summary}") else "en",
        })
    return output


def write_archive(payload: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    edition_date = NOW.strftime("%Y-%m-%d")
    archive_file = ARCHIVE_DIR / f"{edition_date}.json"
    archive_payload = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    archive_file.write_text(archive_payload, encoding="utf-8")
    # Lets the archive page request its initial edition alongside the index.
    LATEST_ARCHIVE.write_text(archive_payload, encoding="utf-8")

    existing = {"editions": []}
    if ARCHIVE_INDEX.exists():
        existing = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8"))
    editions = [edition for edition in existing.get("editions", []) if edition.get("date") != edition_date]
    editions.append({
        "date": edition_date,
        "file": f"./{edition_date}.json",
        "count": len(payload["items"]),
    })
    editions.sort(key=lambda edition: edition["date"], reverse=True)
    ARCHIVE_INDEX.write_text(json.dumps({"editions": editions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def previous_edition_urls() -> set[str]:
    if not ARCHIVE_INDEX.exists():
        return set()
    try:
        editions = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8")).get("editions", [])
    except json.JSONDecodeError:
        return set()
    current_date = NOW.strftime("%Y-%m-%d")
    urls: set[str] = set()
    for edition in editions:
        if edition.get("date") == current_date:
            continue
        archive_file = ARCHIVE_DIR / Path(edition.get("file", "")).name
        if not archive_file.exists():
            continue
        try:
            archived = json.loads(archive_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        urls.update(item.get("url", "") for item in archived.get("items", []))
    return urls


def main() -> int:
    all_entries: list[dict] = []
    for source in SOURCES:
        try:
            fetched = source_entries(source["name"], source["url"], source["weight"], source["kind"])
            for item in fetched:
                item["track"] = source.get("track", "regular")
            print(f"{source['name']}: {len(fetched)} entries")
            all_entries.extend(fetched)
        except Exception as error:  # One unavailable source should not stop updates.
            print(f"{source['name']}: {error}", file=sys.stderr)
    for source in HTML_SOURCES:
        try:
            fetched = html_source_entries(
                source["name"], source["url"], source["weight"], tuple(source.get("include_keywords", ()))
            )
            for item in fetched:
                item["track"] = source.get("track", "regular")
            print(f"{source['name']}: {len(fetched)} entries")
            all_entries.extend(fetched)
        except Exception as error:  # One unavailable source should not stop updates.
            print(f"{source['name']}: {error}", file=sys.stderr)
    try:
        github_items = github_trending_page_entries()
        source_label = "GitHub Trending"
    except Exception as error:
        print(f"GitHub Trending: {error}", file=sys.stderr)
        github_items = []
        source_label = "GitHub"
    print(f"{source_label}: {len(github_items)} entries")
    all_entries.extend(github_items)
    prior_urls = previous_edition_urls()
    unique: dict[str, dict] = {}
    for item in sorted(all_entries, key=lambda value: (value["score"], value["publishedAt"]), reverse=True):
        if item["url"] in prior_urls:
            continue
        unique.setdefault(item["url"], item)

    regular = [item for item in unique.values() if item.get("track") != "developer"]
    developer = [item for item in unique.values() if item.get("track") == "developer"]
    items: list[dict] = []
    source_counts: dict[str, int] = {}
    english_count = 0

    def add_item(item: dict, source_cap: int) -> bool:
        nonlocal english_count
        source = item["source"]
        if item in items or source_counts.get(source, 0) >= source_cap:
            return False
        if item["language"] == "en" and english_count >= ENGLISH_MAX_ITEMS:
            return False
        source_counts[source] = source_counts.get(source, 0) + 1
        items.append(item)
        if item["language"] == "en":
            english_count += 1
        return True

    def fill_pool(pool: list[dict], maximum: int, source_cap: int, chinese_only: bool = False) -> int:
        picked = 0
        for item in pool:
            if picked >= maximum:
                break
            if chinese_only and item["language"] == "en":
                continue
            if add_item(item, source_cap):
                picked += 1
        return picked

    # Regular editorial signals and hands-on developer posts are two independent
    # pools: a full technical-post selection never consumes the 20 regular places.
    regular_count = fill_pool(regular, REGULAR_MAX_ITEMS, source_cap=4, chinese_only=True)
    developer_count = fill_pool(developer, DEVELOPER_MAX_ITEMS, source_cap=4, chinese_only=True)

    # Fill remaining capacity from trusted sources when a publisher is quiet.
    if regular_count < REGULAR_MAX_ITEMS:
        regular_count += fill_pool(regular, REGULAR_MAX_ITEMS - regular_count, FALLBACK_SOURCE_LIMIT)
    if developer_count < DEVELOPER_MAX_ITEMS:
        developer_count += fill_pool(developer, DEVELOPER_MAX_ITEMS - developer_count, FALLBACK_SOURCE_LIMIT)

    # Keep the limited overseas signals together at the tail of every edition.
    items.sort(key=lambda item: (item["language"] == "en", -item["score"], item["publishedAt"]), reverse=False)
    print(f"Selected {regular_count} regular + {developer_count} developer signals (target {DEVELOPER_MIN_ITEMS}-{DEVELOPER_MAX_ITEMS}); English: {english_count}/{ENGLISH_MAX_ITEMS}")
    payload = {
        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
        "items": items,
        "background": random.choice(BACKGROUND_ASSETS),
    }
    if not items:
        print("No qualifying items; keeping the previous published edition.", file=sys.stderr)
        return 1
    cache_reader_items(items)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_archive(payload)
    print(f"Wrote {len(items)} items to {OUTPUT}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
