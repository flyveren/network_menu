# Auto-Refresh Implementation

## Current Solution: Polling-Based Auto-Refresh ✅

I've implemented automatic refresh using **polling** (checking for updates every 30 seconds). This is the simplest solution that works with the current JSON file-based storage.

### How it works:
1. **Auto-refresh starts** when the Facebook posts window is opened
2. **Checks every 30 seconds** for new posts by calling `/api/facebook/posts`
3. **Detects new posts** by comparing:
   - Post count (if it increases)
   - `scrapedAt` timestamp (if it changes)
4. **Automatically updates** the page when new posts are found
5. **Shows notification** ("🆕 X new post(s) found!") when new posts are detected
6. **Stops auto-refresh** when the window is closed

### Benefits:
- ✅ Simple implementation (no database needed)
- ✅ Works with existing JSON file storage
- ✅ Automatic updates without user interaction
- ✅ Visual feedback when new posts arrive
- ✅ Low server load (only checks every 30 seconds)

### Configuration:
- Refresh interval: **30 seconds** (can be adjusted in `startFacebookPostsAutoRefresh()`)
- Location: `app/index.html` lines ~3277-3298

---

## Alternative: Database Solution

If you need **real-time updates** or **better scalability**, consider switching to a database:

### Database Options:

#### 1. **SQLite** (Recommended for small-medium scale)
- **Pros**: 
  - Simple file-based database (no separate server)
  - Fast for read-heavy workloads
  - Easy to backup (just copy the file)
  - Built into Node.js
- **Cons**: 
  - Not ideal for high concurrent writes
  - Single file can become a bottleneck

#### 2. **PostgreSQL** (Recommended for production)
- **Pros**:
  - Excellent performance and scalability
  - Supports real-time notifications (LISTEN/NOTIFY)
  - ACID compliance
  - Great for concurrent access
- **Cons**:
  - Requires separate database server
  - More complex setup

#### 3. **MongoDB** (Good for document storage)
- **Pros**:
  - Natural fit for JSON-like documents
  - Easy to query
  - Good Node.js support
- **Cons**:
  - Requires separate server
  - Less structured than SQL

### Database Implementation Benefits:
- ✅ **Real-time updates** via WebSockets or Server-Sent Events
- ✅ **Better querying** (filter by date, party, author, etc.)
- ✅ **Better performance** with indexes
- ✅ **Concurrent access** handling
- ✅ **Data integrity** with transactions

### Database Implementation Would Require:
1. Database schema design
2. Migration script (JSON → Database)
3. Update scraper to write to database
4. Update API endpoints to query database
5. Add WebSocket/SSE for real-time updates
6. Update frontend to listen for real-time events

---

## Recommendation

**Start with the polling solution** (already implemented):
- It's working now
- No infrastructure changes needed
- 30-second delay is acceptable for this use case
- Can always upgrade to database later if needed

**Consider database if**:
- You need updates faster than 30 seconds
- You have many concurrent users
- You need complex queries/filtering
- You want to store historical data long-term

---

## Testing the Auto-Refresh

1. Open the Facebook posts window
2. Wait 30 seconds
3. Run a scrape (manually or via cron)
4. Within 30 seconds, you should see:
   - Console log: `[FACEBOOK] New posts detected!`
   - Notification: `🆕 X new post(s) found!`
   - Page automatically updates with new posts

