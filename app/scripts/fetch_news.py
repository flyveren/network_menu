#!/usr/bin/env python3
"""Fetch the latest headlines from wallnot.dk RSS and persist them as JSON."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


RSS_URL = "https://wallnot.dk/rss"
USER_AGENT = "navigation-demo/1.0 (+https://wallnot.dk/rss)"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news.json"


def fetch_feed() -> list[dict[str, str]]:
    """Fetch and parse RSS, returning a list of news dictionaries."""
    request = Request(RSS_URL, headers={"User-Agent": USER_AGENT})

    with urlopen(request, timeout=15) as response:
        content = response.read()

    soup = BeautifulSoup(content, "xml")
    items = []

    for node in soup.find_all("item", limit=6):
        title = (node.title.get_text(strip=True) if node.title else "").strip()
        link = (node.link.get_text(strip=True) if node.link else "").strip()
        pub_date = (
            node.pubDate.get_text(strip=True) if node.pubDate else ""
        ).strip()

        if not title:
            title = "Untitled"

        items.append(
            {
                "title": title,
                "link": link,
                "pubDate": pub_date,
            }
        )

    return items


def write_payload(items: list[dict[str, str]]) -> None:
    """Write items to a JSON file atomically."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": RSS_URL,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    tmp_path = OUTPUT_PATH.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(OUTPUT_PATH)


def main() -> int:
    try:
        items = fetch_feed()
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"[fetch_news] Failed to fetch RSS: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_news] Unexpected error: {exc}", file=sys.stderr)
        return 1

    if not items:
        print("[fetch_news] RSS contained no items", file=sys.stderr)

    try:
        write_payload(items)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_news] Failed to write payload: {exc}", file=sys.stderr)
        return 1

    print(f"[fetch_news] Wrote {len(items)} items to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

