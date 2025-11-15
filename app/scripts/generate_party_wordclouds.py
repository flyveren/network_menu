#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from wordcloud import WordCloud

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "facebook_posts.db"
OUTPUT_DIR = BASE_DIR / "wordclouds"

# Mapping for nicer filenames
PARTY_NAMES = {
    "A": "socialdemokratiet",
    "B": "radikale",
    "C": "konservative",
    "F": "sf",
    "H": "borgernes_parti",
    "I": "liberal_alliance",
    "M": "moderaterne",
    "O": "dansk_folkeparti",
    "V": "venstre",
    "Æ": "danmarksdemokraterne",
    "Ø": "enhedslisten",
    "Å": "alternativet",
    "ALL": "alle_partier",
}

WIDTH = 1000
HEIGHT = 1200

PARTY_COLORS = {
    "A": "#8B1A1A",
    "B": "#6A1B9A",
    "C": "#9ACD32",
    "F": "#FF69B4",
    "H": "#4682B4",
    "I": "#40E0D0",
    "M": "#4B0082",
    "O": "#FFD700",
    "V": "#1E3A8A",
    "Æ": "#87CEEB",
    "Ø": "#FF8C00",
    "Å": "#228B22",
    "ALL": "#3b82f6",
}


def build_colormap(base_hex: str) -> list[str]:
    """Create a 20-color list from base color to white for WordCloud."""
    base_hex = base_hex.lstrip("#")
    if len(base_hex) != 6:
        base_hex = "333333"
    base_rgb = tuple(int(base_hex[i : i + 2], 16) for i in (0, 2, 4))
    colors = []
    steps = 20
    for i in range(steps):
        ratio = i / (steps - 1)
        r = int(base_rgb[0] + (255 - base_rgb[0]) * ratio)
        g = int(base_rgb[1] + (255 - base_rgb[1]) * ratio)
        b = int(base_rgb[2] + (255 - base_rgb[2]) * ratio)
        colors.append(f"#{r:02x}{g:02x}{b:02x}")
    return colors


def fetch_tag_counters() -> dict[str, Counter]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT party_code, meta_tags FROM posts WHERE meta_tags IS NOT NULL AND meta_tags != ''"
    ).fetchall()
    conn.close()

    agg: dict[str, Counter] = defaultdict(Counter)
    for party, meta_json in rows:
        try:
            payload = json.loads(meta_json)
        except json.JSONDecodeError:
            continue
        tags = payload.get("meta_tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, str):
                normalized = tag.strip()
                if normalized:
                    agg[party][normalized] += 1
    # Build ALL aggregate
    all_counter = Counter()
    for counter in agg.values():
        all_counter.update(counter)
    if all_counter:
        agg["ALL"] = all_counter
    return agg


def generate_wordcloud(party_code: str, frequencies: Counter) -> Path | None:
    if not frequencies:
        print(f"[WORDCLOUD] Skipping {party_code}: no tags")
        return None

    filename = PARTY_NAMES.get(party_code, party_code).lower()
    output_path = OUTPUT_DIR / f"{filename}_wordcloud.png"

    colors = PARTY_COLORS.get(party_code, "#1f77b4")
    palette = build_colormap(colors)

    import random
    fallback_random = random.Random()

    def color_func(word, font_size, position, orientation, random_state=None, **kwargs):
        rs = random_state or wordcloud.random_state
        if rs is None:
            rs = fallback_random
        return palette[rs.randint(0, len(palette) - 1)]

    wordcloud = WordCloud(
        width=WIDTH,
        height=HEIGHT,
        mode="RGBA",
        background_color=None,
        prefer_horizontal=0.8,
        max_words=200,
    ).generate_from_frequencies(frequencies)

    wordcloud.recolor(color_func=color_func)

    wordcloud.to_file(str(output_path))
    print(f"[WORDCLOUD] Saved {output_path}")
    return output_path


def generate_party_wordclouds(target_party: str | None = None) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag_counters = fetch_tag_counters()
    if not tag_counters:
        print("[WORDCLOUD] No metadata found. Run the scraper/backfill first.")
        return 1

    saved = 0
    parties = [target_party] if target_party else sorted(tag_counters.keys())
    for party in parties:
        if not party or party not in tag_counters:
            continue
        if generate_wordcloud(party, tag_counters[party]):
            saved += 1
    print(f"[WORDCLOUD] Generated {saved} word clouds.")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate wordclouds per party.")
    parser.add_argument("--party", help="Optional party code to regenerate", default=None)
    args = parser.parse_args()
    raise SystemExit(generate_party_wordclouds(args.party))

