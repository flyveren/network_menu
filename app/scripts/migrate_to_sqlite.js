#!/usr/bin/env node
/**
 * Optional migration script to move existing JSON posts to SQLite database
 * 
 * NOTE: If you don't need old posts, you can skip this script.
 * The database will be created automatically when the first post is scraped.
 */

import { readFile, existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { insertPost } from "../db.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const DATA_DIR = join(__dirname, "..", "data");

async function migrate() {
  console.log("[MIGRATION] Starting migration from JSON to SQLite...");
  
  // Migrate from facebook_party_posts.json
  const partyPostsFile = join(DATA_DIR, "facebook_party_posts.json");
  if (existsSync(partyPostsFile)) {
    try {
      const content = await readFile(partyPostsFile, "utf8");
      const data = JSON.parse(content);
      
      let migrated = 0;
      for (const [partyCode, posts] of Object.entries(data)) {
        if (Array.isArray(posts)) {
          for (const post of posts) {
            if (insertPost(post, partyCode)) {
              migrated++;
            }
          }
        }
      }
      console.log(`[MIGRATION] Migrated ${migrated} posts from facebook_party_posts.json`);
    } catch (error) {
      console.error(`[MIGRATION] Error migrating facebook_party_posts.json:`, error);
    }
  }
  
  // Migrate from individual party files
  const fs = await import("node:fs/promises");
  const files = await fs.readdir(DATA_DIR);
  const partyFiles = files.filter(f => 
    f.startsWith("facebook_group_") && 
    f.endsWith(".json") && 
    f !== "facebook_all_posts.json"
  );
  
  for (const file of partyFiles) {
    const filePath = join(DATA_DIR, file);
    try {
      const content = await readFile(filePath, "utf8");
      const data = JSON.parse(content);
      
      // Extract party code from filename
      const slug = file.replace("facebook_group_", "").replace(".json", "");
      const PARTY_MAP = {
        "socialdemokratiet": "A",
        "radikalevenstre": "B",
        "konservative": "C",
        "sfparti": "F",
        "profile": "H",
        "liberalalliance": "I",
        "moderaterne": "M",
        "danskfolkeparti": "O",
        "venstre.dk": "V",
        "partietdd": "Æ",
        "enhedslisten": "Ø",
        "alternativet.dk": "Å",
      };
      
      let partyCode = null;
      for (const [slugKey, code] of Object.entries(PARTY_MAP)) {
        if (slugKey === slug || slug.includes(slugKey)) {
          partyCode = code;
          break;
        }
      }
      
      if (!partyCode) {
        console.log(`[MIGRATION] Skipping ${file} - no party mapping found`);
        continue;
      }
      
      // Handle both formats: {posts: [...]} and [...]
      let posts = [];
      if (data.posts && Array.isArray(data.posts)) {
        posts = data.posts;
      } else if (Array.isArray(data)) {
        posts = data;
      }
      
      let migrated = 0;
      for (const post of posts) {
        if (insertPost(post, partyCode)) {
          migrated++;
        }
      }
      
      if (migrated > 0) {
        console.log(`[MIGRATION] Migrated ${migrated} posts from ${file} (party ${partyCode})`);
      }
    } catch (error) {
      console.error(`[MIGRATION] Error migrating ${file}:`, error);
    }
  }
  
  console.log("[MIGRATION] Migration complete!");
}

migrate().then(() => {
  process.exit(0);
}).catch(err => {
  console.error("[MIGRATION] Error:", err);
  process.exit(1);
});

