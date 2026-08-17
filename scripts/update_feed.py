"""Build the static feed consumed by the GitHub Pages site.

The workflow reads public publisher and developer-community RSS/Atom feeds.
It stores headlines, short descriptions, timestamps and links, rather than
republishing the source articles.
"""

from __future__ import annotations

import html
import json
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
NOW = datetime.now(timezone.utc)

SOURCES = (
    # Stack Overflow Blog is the only overseas publisher feed in this edition.
    {"name": "Stack Overflow Blog", "url": "https://stackoverflow.blog/feed/", "weight": 10, "kind": "editorial"},
)
HTML_SOURCES = (
    {"name": "量子位", "url": "https://www.qbitai.com/", "weight": 12},
    {"name": "AI 前线 / InfoQ 中文", "url": "https://www.infoq.cn/", "weight": 12},
    # The public RSS endpoint currently serves a data-service page instead of articles.
    # Keep this probe visible in job logs; it can be enabled when the publisher restores it.
    {"name": "机器之心", "url": "https://www.jiqizhixin.com/", "weight": 12},
)

KEYWORDS = (
    "model", "reasoning", "agent", "api", "codex", "claude", "gemini",
    "release", "open source", "benchmark", "research", "developer", "code",
    "模型", "推理", "智能体", "开发", "发布", "开源", "研究",
)
BLOCKED = (
    "election", "president", "politic", "war", "military", "weapon", "terror",
    "violence", "sexual", "porn", "政治", "选举", "战争", "军事", "武器",
    "暴力", "色情",
)
LOW_SIGNAL = (
    "pixel", "game", "shopping", "travel", "music", "celebrity", "giveaway",
    "抽奖", "游戏攻略", "明星", "旅游", "购物",
)
MAX_AGE_DAYS = 3
MAX_ITEMS = 15
ROUTINE_RELEASE = re.compile(r"^(?:release\s+)?v?\d+\.\d+\.\d+", re.IGNORECASE)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
        "score": 100,
        "language": "zh",
    }]


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "FuncDance-Signal-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


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
    candidates = re.findall(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", page)
    if fallback:
        candidates.append(fallback)
    for value in candidates:
        normalized = value.replace("/", "-").replace(" ", "T")
        try:
            return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def html_source_candidates(source_name: str, page: str) -> list[tuple[str, str]]:
    if source_name == "量子位":
        links = re.findall(r'href=["\'](https://www\.qbitai\.com/20\d{2}/\d{2}/\d+\.html)["\']', page)
    elif source_name == "AI 前线 / InfoQ 中文":
        links = re.findall(r'href=["\'](https://www\.infoq\.cn/article/[A-Za-z0-9]+)["\']', page)
    else:
        links = []
    return list(dict.fromkeys((link, link) for link in links))[:24]


def html_source_entries(source_name: str, url: str, weight: int) -> list[dict]:
    homepage = fetch_text(url)
    output: list[dict] = []
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
        if not title or published_at is None or any(term in combined for term in BLOCKED) or any(term in combined for term in LOW_SIGNAL):
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
        if age_days > MAX_AGE_DAYS:
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
            "score": round(score, 2),
            "language": "zh" if is_chinese(f"{title} {summary}") else "en",
        })
    return output


def write_archive(payload: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    edition_date = NOW.strftime("%Y-%m-%d")
    archive_file = ARCHIVE_DIR / f"{edition_date}.json"
    archive_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
            print(f"{source['name']}: {len(fetched)} entries")
            all_entries.extend(fetched)
        except Exception as error:  # One unavailable source should not stop updates.
            print(f"{source['name']}: {error}", file=sys.stderr)
    for source in HTML_SOURCES:
        try:
            fetched = html_source_entries(source["name"], source["url"], source["weight"])
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

    non_english = [item for item in unique.values() if item["language"] != "en"]
    english = [item for item in unique.values() if item["language"] == "en"]
    items: list[dict] = []
    source_counts: dict[str, int] = {}
    for item in non_english:
        source = item["source"]
        source_limit = 1 if source.startswith("GitHub") else 8
        if source_counts.get(source, 0) >= source_limit:
            continue
        source_counts[source] = source_counts.get(source, 0) + 1
        items.append(item)
        if len(items) == MAX_ITEMS:
            break
    # Keep English articles strictly below 20% of the final edition.
    english_limit = max(0, (len(items) - 1) // 4)
    for item in english:
        if len(items) == MAX_ITEMS or english_limit == 0:
            break
        source = item["source"]
        source_limit = 1 if source.startswith("GitHub") else 8
        if source_counts.get(source, 0) >= source_limit:
            continue
        source_counts[source] = source_counts.get(source, 0) + 1
        items.append(item)
        english_limit -= 1
    payload = {
        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
        "items": items,
    }
    if not items:
        print("No qualifying items; keeping the previous published edition.", file=sys.stderr)
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_archive(payload)
    print(f"Wrote {len(items)} items to {OUTPUT}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
