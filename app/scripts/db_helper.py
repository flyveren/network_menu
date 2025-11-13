#!/usr/bin/env python3
"""
SQLite helper for storing Facebook posts
"""

import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timezone

# Get database path
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "facebook_posts.db"


def get_db():
    """Get database connection and ensure schema exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")  # Enable WAL for better concurrency
    
    # Create table if it doesn't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            party_code TEXT NOT NULL,
            author_name TEXT NOT NULL,
            post_text TEXT,
            post_time TEXT,
            post_link TEXT,
            video_url TEXT,
            video_thumbnail TEXT,
            scraped_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(post_id, party_code, post_link)
        )
    """)
    
    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_party_code ON posts(party_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scraped_at ON posts(scraped_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_link ON posts(post_link)")
    
    conn.commit()
    return conn


def insert_post(post, party_code):
    """Insert or replace a post in the database."""
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO posts (
                post_id, party_code, author_name, post_text, post_time,
                post_link, video_url, video_thumbnail, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post.get("post_id") or f"post_{int(datetime.now(timezone.utc).timestamp())}",
            party_code,
            post.get("author_name") or "Unknown",
            post.get("post_text") or "",
            post.get("post_time") or "",
            post.get("post_link") or "",
            post.get("video_url") or "",
            post.get("video_thumbnail") or "",
            post.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] Error inserting post: {e}", flush=True)
        return False
    finally:
        conn.close()


def post_exists(post, party_code):
    """Check if a post already exists."""
    conn = get_db()
    try:
        cursor = conn.execute("""
            SELECT COUNT(*) FROM posts
            WHERE party_code = ? AND (
                (post_link != '' AND post_link = ?) OR
                (post_text != '' AND post_text = ?)
            )
        """, (
            party_code,
            post.get("post_link") or "",
            post.get("post_text") or "",
        ))
        count = cursor.fetchone()[0]
        return count > 0
    finally:
        conn.close()

