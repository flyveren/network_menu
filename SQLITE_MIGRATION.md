# SQLite Migration Guide

## Overview

The Facebook posts system has been migrated from JSON file storage to **SQLite database** for better performance, real-time updates, and scalability.

## What Changed

### 1. **Database Storage**
- Posts are now stored in `app/data/facebook_posts.db` (SQLite database)
- Replaces `facebook_party_posts.json` and individual `facebook_group_*.json` files
- JSON files are still created as backups, but the database is the source of truth

### 2. **Real-Time Updates**
- **Server-Sent Events (SSE)** for instant updates when new posts are scraped
- No more 30-second polling delay - updates appear immediately
- Automatic reconnection if connection drops

### 3. **API Changes**
- `/api/facebook/posts` - Still works, now reads from SQLite
- `/api/facebook/posts/stream` - New SSE endpoint for real-time updates

## Installation

### 1. **Rebuild Docker Container**
The Dockerfile has been updated to include SQLite and `better-sqlite3`:

```bash
docker-compose build
docker-compose up -d
```

### 2. **Run Migration Script**
Migrate existing JSON data to SQLite:

```bash
docker exec -it navigation-navigation-1 node scripts/migrate_to_sqlite.js
```

Or from inside the container:
```bash
cd /usr/src/app
node scripts/migrate_to_sqlite.js
```

## Database Schema

```sql
CREATE TABLE posts (
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
);

-- Indexes for fast queries
CREATE INDEX idx_party_code ON posts(party_code);
CREATE INDEX idx_scraped_at ON posts(scraped_at DESC);
CREATE INDEX idx_post_link ON posts(post_link);
CREATE INDEX idx_author_name ON posts(author_name);
```

## How It Works

### Scraper (`fetch_facebook_first_post.py`)
1. Scrapes post from Facebook
2. Checks if post exists in database (by link/text)
3. Inserts post into SQLite if new
4. Also saves JSON backup file

### API Server (`server.mjs`)
1. `/api/facebook/posts` - Returns all posts from SQLite
2. `/api/facebook/posts/stream` - SSE stream for real-time updates

### Frontend (`index.html`)
1. Connects to SSE endpoint when posts window opens
2. Receives real-time updates when new posts are added
3. Automatically reconnects if connection drops
4. Shows notification when new posts arrive

## Benefits

✅ **Real-Time Updates** - Posts appear instantly when scraped (no 30-second delay)  
✅ **Better Performance** - SQLite queries are faster than reading/parsing JSON files  
✅ **Scalability** - Can handle thousands of posts efficiently  
✅ **Data Integrity** - Database constraints prevent duplicates  
✅ **Indexed Queries** - Fast filtering by party, date, author  
✅ **Concurrent Access** - WAL mode allows multiple readers/writers  

## Files Changed

### New Files
- `app/db.js` - Database helper module
- `app/scripts/db_helper.py` - Python database helper for scraper
- `app/scripts/migrate_to_sqlite.js` - Migration script

### Modified Files
- `Dockerfile` - Added SQLite and build tools
- `app/package.json` - Added `better-sqlite3` dependency
- `app/server.mjs` - Updated to read from SQLite, added SSE endpoint
- `app/scripts/fetch_facebook_first_post.py` - Updated to write to SQLite
- `app/index.html` - Updated to use SSE instead of polling

## Testing

1. **Check database exists:**
   ```bash
   docker exec -it navigation-navigation-1 ls -lh /usr/src/app/data/facebook_posts.db
   ```

2. **Query posts:**
   ```bash
   docker exec -it navigation-navigation-1 sqlite3 /usr/src/app/data/facebook_posts.db "SELECT COUNT(*) FROM posts;"
   ```

3. **Test scraper:**
   ```bash
   docker exec -it navigation-navigation-1 python3 scripts/fetch_facebook_first_post.py --group-url https://www.facebook.com/socialdemokratiet --headless
   ```

4. **Check SSE endpoint:**
   ```bash
   curl http://localhost:8081/api/facebook/posts/stream
   ```

## Rollback (if needed)

If you need to rollback to JSON files:
1. The scraper still creates JSON backup files
2. Update `server.mjs` to read from JSON files instead of SQLite
3. Remove SQLite-related code

## Troubleshooting

### Database locked errors
- SQLite uses WAL mode for better concurrency
- If you see lock errors, check for long-running queries

### Migration fails
- Ensure data directory exists: `mkdir -p app/data`
- Check file permissions
- Verify JSON files are valid

### SSE not working
- Check browser console for errors
- Verify `/api/facebook/posts/stream` endpoint is accessible
- Check server logs for SSE connection messages

## Next Steps

1. Run migration script to move existing posts
2. Test scraper to ensure it writes to database
3. Open posts window and verify SSE connection
4. Trigger a scrape and verify real-time update appears

