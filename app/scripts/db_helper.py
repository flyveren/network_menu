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
            meta_tags TEXT,
            scraped_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(post_id, party_code, post_link)
        )
    """)
    ensure_column(conn, "posts", "meta_tags", "TEXT")
    
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
                post_link, video_url, video_thumbnail, meta_tags, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post.get("post_id") or f"post_{int(datetime.now(timezone.utc).timestamp())}",
            party_code,
            post.get("author_name") or "Unknown",
            post.get("post_text") or "",
            post.get("post_time") or "",
            post.get("post_link") or "",
            post.get("video_url") or "",
            post.get("video_thumbnail") or "",
            serialize_meta_tags(post.get("meta_tags")),
            post.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] Error inserting post: {e}", flush=True)
        return False
    finally:
        conn.close()


def ensure_column(conn, table_name, column_name, column_type):
    """Ensure a column exists on a table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in cursor.fetchall()}
    if column_name not in columns:
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Ignore if column was added concurrently
            if "duplicate column name" not in str(exc).lower():
                raise


def serialize_meta_tags(meta_value):
    """Serialize meta tag structures to JSON strings."""
    if meta_value in (None, "", []):
        return ""
    if isinstance(meta_value, str):
        return meta_value
    try:
        return json.dumps(meta_value, ensure_ascii=False)
    except Exception:
        return ""


def post_exists(post, party_code):
    """Check if a post already exists."""
    conn = get_db()
    try:
        cursor = conn.execute("""
            SELECT COUNT(*) FROM posts
            WHERE party_code = ? AND (
                (post_id != '' AND post_id = ?) OR
                (post_link != '' AND post_link = ?) OR
                (post_text != '' AND post_text = ?)
            )
        """, (
            party_code,
            post.get("post_id") or "",
            post.get("post_link") or "",
            post.get("post_text") or "",
        ))
        count = cursor.fetchone()[0]
        return count > 0
    finally:
        conn.close()


def get_db_polling():
    """Get database connection and ensure polling_data table exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    
    # Create polling_data table if it doesn't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polling_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            party_code TEXT NOT NULL,
            seneste_maaling_value REAL NOT NULL,
            seneste_maaling_date TEXT,
            forrige_maaling_value REAL NOT NULL,
            forrige_maaling_date TEXT,
            maaned_siden_value REAL NOT NULL,
            valget_2022_value REAL NOT NULL,
            scraped_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(party_code, scraped_at)
        )
    """)
    
    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_polling_party_code ON polling_data(party_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_polling_scraped_at ON polling_data(scraped_at DESC)")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polling_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            party_code TEXT NOT NULL,
            maaling_date TEXT NOT NULL,
            maaling_value REAL NOT NULL,
            scraped_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(party_code, maaling_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_polling_history_party ON polling_history(party_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_polling_history_date ON polling_history(maaling_date)")
    
    conn.commit()
    return conn


def insert_polling_data(polling_data):
    """Insert or replace polling data in the database."""
    conn = get_db_polling()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO polling_data (
                party_code, seneste_maaling_value, seneste_maaling_date,
                forrige_maaling_value, forrige_maaling_date,
                maaned_siden_value, valget_2022_value, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            polling_data.get("party_code"),
            polling_data.get("seneste_maaling_value"),
            polling_data.get("seneste_maaling_date"),
            polling_data.get("forrige_maaling_value"),
            polling_data.get("forrige_maaling_date"),
            polling_data.get("maaned_siden_value"),
            polling_data.get("valget_2022_value"),
            polling_data.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] Error inserting polling data: {e}", flush=True)
        return False
    finally:
        conn.close()


def insert_polling_history(entry):
    """Insert or replace a single polling history data point."""
    conn = get_db_polling()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO polling_history (
                party_code, maaling_date, maaling_value, scraped_at
            ) VALUES (?, ?, ?, ?)
        """, (
            entry.get("party_code"),
            entry.get("maaling_date"),
            entry.get("maaling_value"),
            entry.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] Error inserting polling history: {e}", flush=True)
        return False
    finally:
        conn.close()

