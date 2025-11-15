#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from time import sleep

from post_metadata import generate_post_metadata

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "facebook_posts.db"


def load_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def fetch_pending_posts(conn: sqlite3.Connection, limit: int | None = None):
    sql = """
        SELECT id, party_code, author_name, post_text, post_link
        FROM posts
        WHERE (meta_tags IS NULL OR meta_tags = '')
          AND post_text IS NOT NULL
          AND TRIM(post_text) != ''
        ORDER BY scraped_at ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cursor = conn.execute(sql)
    rows = cursor.fetchall()
    return rows


def update_metadata(conn: sqlite3.Connection, post_id: int, metadata: dict) -> None:
    conn.execute(
        """
        UPDATE posts
        SET meta_tags = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False), post_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill OpenAI-generated meta tags for existing Facebook posts.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max number of posts to process")
    parser.add_argument("--sleep", type=float, default=0.5, help="Delay between API calls (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Print metadata without updating DB")
    args = parser.parse_args()

    load_env()
    if not os.getenv("OPENAI_API_KEY"):
        print("[META] OPENAI_API_KEY missing – cannot generate metadata.")
        return 1

    conn = get_connection()
    try:
        rows = fetch_pending_posts(conn, args.limit)
        if not rows:
            print("[META] No posts require backfilling.")
            return 0

        print(f"[META] Processing {len(rows)} posts...")
        processed = 0
        for post_id, party_code, author_name, post_text, post_link in rows:
            metadata = generate_post_metadata(post_text)
            if not metadata:
                print(f"[META] Skipped post {post_id} – no metadata returned")
                continue

            if args.dry_run:
                print(f"[META][DRY] {party_code} #{post_id}: {metadata}")
            else:
                update_metadata(conn, post_id, metadata)
                conn.commit()
                print(f"[META] Updated post {post_id} ({party_code}) with {len(metadata.get('meta_tags', []))} tags")

            processed += 1
            if args.sleep:
                sleep(args.sleep)

        if not args.dry_run:
            conn.commit()
        print(f"[META] Finished. Processed {processed} posts.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

