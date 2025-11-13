import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const dataDir = join(__dirname, "..", "data");

function createPostKey(post) {
  const link = (post.post_link || "").trim().toLowerCase();
  const text = (post.post_text || "").trim().toLowerCase().substring(0, 100);
  if (link) return `link:${link}`;
  if (text) return `text:${text}`;
  return null;
}

async function rebuildAllPostsList() {
  const allPostsPath = join(dataDir, "facebook_all_posts.json");
  
  try {
    if (!existsSync(dataDir)) {
      console.log(`[FACEBOOK] Data directory doesn't exist, skipping rebuild`);
      return;
    }
    
    const fs = await import("node:fs/promises");
    const files = await fs.readdir(dataDir);
    const facebookFiles = files.filter(f => 
      f.startsWith("facebook_group_") && 
      f.endsWith(".json") && 
      f !== "facebook_all_posts.json"
    );
    
    console.log(`[FACEBOOK] Rebuilding all posts list from ${facebookFiles.length} party files`);
    
    const allPosts = [];
    const seenPostKeys = new Set();
    
    // First, read from facebook_party_posts.json (new format)
    const partyPostsPath = join(dataDir, "facebook_party_posts.json");
    if (existsSync(partyPostsPath)) {
      try {
        const partyPostsContent = await readFile(partyPostsPath, "utf8");
        const partyPostsData = JSON.parse(partyPostsContent);
        
        // Read posts from all party keys (A, B, C, etc.) and ALL
        for (const [partyKey, posts] of Object.entries(partyPostsData)) {
          if (Array.isArray(posts)) {
            console.log(`[FACEBOOK] Loading ${posts.length} posts from party ${partyKey} in facebook_party_posts.json`);
            
            for (const post of posts) {
              const key = createPostKey(post);
              
              // Skip if we've seen this post before
              if (key && seenPostKeys.has(key)) {
                continue;
              }
              
              if (key) {
                seenPostKeys.add(key);
              }
              
              allPosts.push(post);
            }
          }
        }
      } catch (err) {
        console.error(`[FACEBOOK] Error reading facebook_party_posts.json: ${err.message}`);
      }
    }
    
    // Also read all posts from individual party files (legacy format)
    for (const file of facebookFiles) {
      const filePath = join(dataDir, file);
      try {
        const fileContent = await readFile(filePath, "utf8");
        const data = JSON.parse(fileContent);
        
        // Handle both formats: {posts: [...]} and [...]
        let posts = [];
        if (data.posts && Array.isArray(data.posts)) {
          posts = data.posts;
        } else if (Array.isArray(data)) {
          posts = data;
        }
        
        if (posts.length > 0) {
          console.log(`[FACEBOOK] Loading ${posts.length} posts from ${file}`);
          
          for (const post of posts) {
            const key = createPostKey(post);
            
            // Skip if we've seen this post before
            if (key && seenPostKeys.has(key)) {
              continue;
            }
            
            if (key) {
              seenPostKeys.add(key);
            }
            
            allPosts.push(post);
          }
        }
      } catch (err) {
        console.error(`[FACEBOOK] Error reading file ${file}: ${err.message}`);
      }
    }
    
    // Sort by scraped_at date (newest first)
    allPosts.sort((a, b) => {
      const dateA = new Date(a.scraped_at || 0);
      const dateB = new Date(b.scraped_at || 0);
      return dateB - dateA;
    });
    
    const allPostsData = {
      source: "all_parties",
      scrapedAt: new Date().toISOString(),
      totalPosts: allPosts.length,
      posts: allPosts,
    };
    
    await writeFile(allPostsPath, JSON.stringify(allPostsData, null, 2), "utf8");
    console.log(`[FACEBOOK] Rebuilt all posts list: ${allPosts.length} unique posts from ${facebookFiles.length} party files`);
    console.log(`[FACEBOOK] Posts by author:`, allPosts.map(p => p.author_name).join(", "));
  } catch (error) {
    console.error(`[FACEBOOK] Error rebuilding all posts list: ${error.message}`);
    throw error;
  }
}

rebuildAllPostsList().then(() => {
  console.log("[SUCCESS] Rebuild complete");
  process.exit(0);
}).catch(err => {
  console.error("[ERROR]", err);
  process.exit(1);
});

