"""Backfill date-verified Signal Radar archives from public historical sources."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

from update_feed import ARCHIVE_DIR, ARCHIVE_INDEX, BACKGROUND_ASSETS, BLOCKED, LOW_SIGNAL, clean_text


START_DATE = date(2026, 5, 18)
CHINA_TIME = timezone(timedelta(hours=8))
MAX_ITEMS = 18


def fetch_json(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": "FuncDance-Signal-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def archive_background(edition_date: date) -> str:
    digest = hashlib.sha256(edition_date.isoformat().encode("utf-8")).digest()[0]
    return BACKGROUND_ASSETS[digest % len(BACKGROUND_ASSETS)]


def edition_payload(edition_date: date) -> dict:
    start = datetime.combine(edition_date, time.min).isoformat()
    end = datetime.combine(edition_date + timedelta(days=1), time.min).isoformat()
    endpoint = "https://www.qbitai.com/wp-json/wp/v2/posts?" + urllib.parse.urlencode({
        "after": start,
        "before": end,
        "per_page": 100,
        "_fields": "date,link,title,excerpt",
    })
    items: list[dict] = []
    for article in fetch_json(endpoint):
        title = clean_text(article.get("title", {}).get("rendered", ""))
        summary = clean_text(article.get("excerpt", {}).get("rendered", ""))
        combined = f"{title} {summary}".lower()
        if not title or any(term in combined for term in BLOCKED) or any(term in combined for term in LOW_SIGNAL):
            continue
        published = datetime.fromisoformat(article["date"]).replace(tzinfo=CHINA_TIME).astimezone(timezone.utc)
        relevance = sum(term in combined for term in ("模型", "智能体", "开源", "研究", "开发", "agent", "model", "open source"))
        items.append({
            "title": title[:180],
            "summary": summary[:280],
            "source": "量子位",
            "url": article["link"],
            "publishedAt": published.isoformat().replace("+00:00", "Z"),
            "score": relevance,
            "language": "zh",
        })
    updated_at = datetime.combine(edition_date, time(8), tzinfo=CHINA_TIME).astimezone(timezone.utc)
    return {
        "updatedAt": updated_at.isoformat().replace("+00:00", "Z"),
        "items": sorted(items, key=lambda item: (item["score"], item["publishedAt"]), reverse=True)[:MAX_ITEMS],
        "background": archive_background(edition_date),
        "historical": True,
    }


def edition_dates() -> list[date]:
    today = datetime.now(CHINA_TIME).date()
    cursor = START_DATE
    output: list[date] = []
    while cursor < today:
        if cursor.weekday() in {0, 2, 4}:
            output.append(cursor)
        cursor += timedelta(days=1)
    return output


def main() -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    existing_index = {"editions": []}
    if ARCHIVE_INDEX.exists():
        existing_index = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8"))
    editions = {entry["date"]: entry for entry in existing_index.get("editions", [])}
    written = 0
    for edition_date in edition_dates():
        filename = f"{edition_date.isoformat()}.json"
        target = ARCHIVE_DIR / filename
        if target.exists():
            continue
        payload = edition_payload(edition_date)
        if not payload["items"]:
            print(f"{edition_date}: no verifiable items; skipped")
            continue
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        editions[edition_date.isoformat()] = {"date": edition_date.isoformat(), "file": f"./{filename}", "count": len(payload["items"])}
        written += 1
        print(f"{edition_date}: {len(payload['items'])} items")
    archive_list = sorted(editions.values(), key=lambda entry: entry["date"], reverse=True)
    ARCHIVE_INDEX.write_text(json.dumps({"editions": archive_list}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Backfilled {written} editions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
