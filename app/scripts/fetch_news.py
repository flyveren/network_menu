#!/usr/bin/env python3
"""Fetch the latest headlines from wallnot.dk RSS and persist them as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


BASE_RSS_URL = "https://wallnot.dk/rss"
USER_AGENT = "navigation-demo/1.0 (+https://wallnot.dk/rss)"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWN_CATEGORIES = {"all", "indland", "udland", "kultur", "debat"}


def build_rss_url(category: str) -> str:
    if not category or category.lower() == "all":
        return BASE_RSS_URL
    return f"{BASE_RSS_URL}?category={category}"


def resolve_output_path(category: str, override: str | None) -> Path:
    if override:
        return Path(override)
    suffix = "" if not category or category == "all" else f"_{category}"
    return DATA_DIR / f"news{suffix}.json"


def fetch_feed(url: str) -> list[dict[str, str]]:
    """Fetch and parse RSS, returning a list of news dictionaries."""
    request = Request(url, headers={"User-Agent": USER_AGENT})

    with urlopen(request, timeout=15) as response:
        content = response.read()

    soup = BeautifulSoup(content, "xml")
    items = []

    for node in soup.find_all("item", limit=24):
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


def write_payload(
    items: list[dict[str, str]],
    category: str,
    source_url: str,
    output_path: Path,
) -> None:
    """Write items to a JSON file atomically."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": source_url,
        "category": category,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(output_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch headlines from wallnot.dk and persist to JSON."
    )
    parser.add_argument(
        "--category",
        default="all",
        help="News category to fetch (default: all). Known values: all, indland, udland, kultur, debat.",
    )
    parser.add_argument(
        "--output",
        help="Optional output file path. Defaults to data/news_<category>.json",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    category = (args.category or "all").strip().lower()
    if category not in KNOWN_CATEGORIES:
        print(
            f"[fetch_news] Warning: Unknown category '{category}'. Proceeding with custom slug.",
            file=sys.stderr,
        )
    rss_url = build_rss_url(category)
    output_path = resolve_output_path(category, args.output)

    try:
        items = fetch_feed(rss_url)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"[fetch_news] Failed to fetch RSS '{rss_url}': {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_news] Unexpected error: {exc}", file=sys.stderr)
        return 1

    if not items:
        print("[fetch_news] RSS contained no items", file=sys.stderr)

    try:
        write_payload(items, category, rss_url, output_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_news] Failed to write payload: {exc}", file=sys.stderr)
        return 1

    print(f"[fetch_news] Wrote {len(items)} items to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

