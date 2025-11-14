import Database from "better-sqlite3";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const DB_PATH = join(__dirname, "data", "facebook_posts.db");

let db = null;

export function getDatabase() {
  if (!db) {
    db = new Database(DB_PATH);
    db.pragma("journal_mode = WAL"); // Enable Write-Ahead Logging for better concurrency
    initializeDatabase(db);
  }
  return db;
}

function initializeDatabase(db) {
  // Create posts table
  db.exec(`
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
    );
    
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
    );
    
    CREATE INDEX IF NOT EXISTS idx_party_code ON posts(party_code);
    CREATE INDEX IF NOT EXISTS idx_scraped_at ON posts(scraped_at DESC);
    CREATE INDEX IF NOT EXISTS idx_post_link ON posts(post_link);
    CREATE INDEX IF NOT EXISTS idx_author_name ON posts(author_name);
    CREATE INDEX IF NOT EXISTS idx_polling_party_code ON polling_data(party_code);
    CREATE INDEX IF NOT EXISTS idx_polling_scraped_at ON polling_data(scraped_at DESC);
  `);
  
  console.log("[DB] Database initialized at", DB_PATH);
}

export function insertPost(post, partyCode) {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    INSERT OR REPLACE INTO posts (
      post_id, party_code, author_name, post_text, post_time,
      post_link, video_url, video_thumbnail, scraped_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  
  try {
    stmt.run(
      post.post_id || `post_${Date.now()}`,
      partyCode,
      post.author_name || "Unknown",
      post.post_text || "",
      post.post_time || "",
      post.post_link || "",
      post.video_url || "",
      post.video_thumbnail || "",
      post.scraped_at || new Date().toISOString()
    );
    return true;
  } catch (error) {
    console.error("[DB] Error inserting post:", error);
    return false;
  }
}

export function getAllPosts(limit = 1000) {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    SELECT DISTINCT
      post_id,
      party_code,
      author_name,
      post_text,
      post_time,
      post_link,
      video_url,
      video_thumbnail,
      scraped_at
    FROM posts
    ORDER BY scraped_at DESC
    LIMIT ?
  `);
  
  return stmt.all(limit).map(row => ({
    post_id: row.post_id,
    party_code: row.party_code,
    author_name: row.author_name,
    post_text: row.post_text,
    post_time: row.post_time,
    post_link: row.post_link,
    video_url: row.video_url,
    video_thumbnail: row.video_thumbnail,
    scraped_at: row.scraped_at,
  }));
}

export function getPostsByParty(partyCode, limit = 100) {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    SELECT DISTINCT
      post_id,
      party_code,
      author_name,
      post_text,
      post_time,
      post_link,
      video_url,
      video_thumbnail,
      scraped_at
    FROM posts
    WHERE party_code = ?
    ORDER BY scraped_at DESC
    LIMIT ?
  `);
  
  return stmt.all(partyCode, limit).map(row => ({
    post_id: row.post_id,
    party_code: row.party_code,
    author_name: row.author_name,
    post_text: row.post_text,
    post_time: row.post_time,
    post_link: row.post_link,
    video_url: row.video_url,
    video_thumbnail: row.video_thumbnail,
    scraped_at: row.scraped_at,
  }));
}

export function getLatestScrapedAt() {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    SELECT MAX(scraped_at) as latest FROM posts
  `);
  
  const result = stmt.get();
  return result?.latest || null;
}

export function getPostCount() {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    SELECT COUNT(DISTINCT post_link) as count FROM posts WHERE post_link != ''
  `);
  
  const result = stmt.get();
  return result?.count || 0;
}

export function deletePost(postId, partyCode = null) {
  const db = getDatabase();
  
  try {
    let stmt;
    if (partyCode) {
      stmt = db.prepare(`
        DELETE FROM posts WHERE post_id = ? AND party_code = ?
      `);
      const result = stmt.run(postId, partyCode);
      return result.changes > 0;
    }
    stmt = db.prepare(`
      DELETE FROM posts WHERE post_id = ?
    `);
    const result = stmt.run(postId);
    return result.changes > 0;
  } catch (error) {
    console.error("[DB] Error deleting post:", error);
    return false;
  }
}

export function insertPollingData(pollingData) {
  const db = getDatabase();
  
  const stmt = db.prepare(`
    INSERT OR REPLACE INTO polling_data (
      party_code, seneste_maaling_value, seneste_maaling_date,
      forrige_maaling_value, forrige_maaling_date,
      maaned_siden_value, valget_2022_value, scraped_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `);
  
  try {
    stmt.run(
      pollingData.party_code,
      pollingData.seneste_maaling_value,
      pollingData.seneste_maaling_date || null,
      pollingData.forrige_maaling_value,
      pollingData.forrige_maaling_date || null,
      pollingData.maaned_siden_value,
      pollingData.valget_2022_value,
      pollingData.scraped_at || new Date().toISOString()
    );
    return true;
  } catch (error) {
    console.error("[DB] Error inserting polling data:", error);
    return false;
  }
}

export function getLatestPollingData(partyCode = null) {
  const db = getDatabase();
  
  if (partyCode) {
    const stmt = db.prepare(`
      SELECT * FROM polling_data
      WHERE party_code = ?
      ORDER BY scraped_at DESC
      LIMIT 1
    `);
    const result = stmt.get(partyCode);
    return result ? {
      party_code: result.party_code,
      seneste_maaling_value: result.seneste_maaling_value,
      seneste_maaling_date: result.seneste_maaling_date,
      forrige_maaling_value: result.forrige_maaling_value,
      forrige_maaling_date: result.forrige_maaling_date,
      maaned_siden_value: result.maaned_siden_value,
      valget_2022_value: result.valget_2022_value,
      scraped_at: result.scraped_at,
    } : null;
  } else {
    const stmt = db.prepare(`
      SELECT * FROM polling_data
      WHERE scraped_at = (SELECT MAX(scraped_at) FROM polling_data)
      ORDER BY party_code
    `);
    return stmt.all().map(row => ({
      party_code: row.party_code,
      seneste_maaling_value: row.seneste_maaling_value,
      seneste_maaling_date: row.seneste_maaling_date,
      forrige_maaling_value: row.forrige_maaling_value,
      forrige_maaling_date: row.forrige_maaling_date,
      maaned_siden_value: row.maaned_siden_value,
      valget_2022_value: row.valget_2022_value,
      scraped_at: row.scraped_at,
    }));
  }
}

export function closeDatabase() {
  if (db) {
    db.close();
    db = null;
  }
}

