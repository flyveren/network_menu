#!/usr/bin/env python3
"""
Optimized script to scrape the first Facebook post from a party page.
- Goes directly to party page
- Logs in if login modal appears
- Finds posts column (x19h7ccj)
- Scrapes first post with text (video preferred, but text-only OK)
"""

import os
import sys
import json
import re
import argparse
import hashlib
from time import sleep
from datetime import datetime, timezone
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Import database helper
sys.path.insert(0, str(Path(__file__).parent))
from db_helper import insert_post, post_exists

# Load .env file if it exists
def load_env():
    """Load environment variables from .env file."""
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

load_env()

# Data directory
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def setup_driver(headless: bool = True):
    """Setup browser driver (Firefox preferred, Chrome fallback)."""
    # Try Firefox first
    try:
        options = FirefoxOptions()
        if headless:
            options.add_argument("--headless")
        options.add_argument("--width=1920")
        options.add_argument("--height=1080")
        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("useAutomationExtension", False)
        driver = webdriver.Firefox(options=options)
        print("[INFO] Using Firefox driver", flush=True)
        return driver
    except Exception as e:
        print(f"[INFO] Firefox failed: {e}, trying Chrome...", flush=True)
    
    # Fallback to Chrome
    options = ChromeOptions()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    driver = webdriver.Chrome(options=options)
    print("[INFO] Using Chrome driver", flush=True)
    return driver


def login_to_facebook(driver, email: str, password: str) -> bool:
    """Login to Facebook if login modal appears."""
    try:
        # Wait for login modal or login page
        sleep(2)
        
        # Check if we're on login page
        if "/login" in driver.current_url.lower():
            print("[INFO] On login page, logging in...", flush=True)
        else:
            # Check for login modal
            login_modal = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"], [data-testid*="dialog"], [aria-modal="true"]')
            if not login_modal:
                print("[INFO] No login modal found", flush=True)
                return False
            print("[INFO] Login modal detected, logging in...", flush=True)
        
        # Find email input
        email_input = None
        email_selectors = [
            (By.CSS_SELECTOR, 'input[type="text"][name="email"]'),
            (By.CSS_SELECTOR, 'input[type="email"]'),
            (By.CSS_SELECTOR, 'input[placeholder*="email" i]'),
            (By.CSS_SELECTOR, 'input[aria-label*="email" i]'),
        ]
        for by, selector in email_selectors:
            try:
                email_input = driver.find_element(by, selector)
                break
            except:
                continue
        
        if not email_input:
            print("[WARNING] Could not find email input", flush=True)
            return False
        
        email_input.clear()
        email_input.send_keys(email)
        print("[INFO] Entered email", flush=True)
        sleep(0.5)
        
        # Find password input
        password_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"], input[name="pass"]')
        password_input.clear()
        password_input.send_keys(password)
        print("[INFO] Entered password", flush=True)
        sleep(0.5)
        
        # Find and click login button
        login_selectors = [
            (By.CSS_SELECTOR, 'button[type="submit"]'),
            (By.CSS_SELECTOR, 'button[name="login"]'),
            (By.XPATH, '//button[contains(., "Log")]'),
        ]
        login_btn = None
        for by, selector in login_selectors:
            try:
                login_btn = driver.find_element(by, selector)
                break
            except:
                continue
        
        if login_btn:
            driver.execute_script("arguments[0].click();", login_btn)
            print("[INFO] Clicked login button", flush=True)
            sleep(5)  # Wait for login to complete
            return True
        else:
            print("[WARNING] Could not find login button", flush=True)
            return False
            
    except Exception as e:
        print(f"[WARNING] Login failed: {e}", flush=True)
        return False


def scrape_first_post(driver, page_url: str) -> dict | None:
    """Scrape the first post from the party page."""
    print(f"[INFO] Navigating to: {page_url}", flush=True)
    driver.get(page_url)
    sleep(3)
    
    # Get credentials and login if needed
    email = os.getenv("FACEBOOK_EMAIL")
    password = os.getenv("FACEBOOK_PASSWORD")
    
    print(f"[INFO] Current URL after navigation: {driver.current_url}", flush=True)
    
    # Check if we need to login (either on login page or modal appears)
    if email and password:
        current_url = driver.current_url.lower()
        if "/login" in current_url:
            print("[INFO] Detected login page, attempting login...", flush=True)
            logged_in = login_to_facebook(driver, email, password)
            if logged_in:
                sleep(3)
                # Navigate to page after login
                driver.get(page_url)
                sleep(3)
        else:
            # Check for login modal
            sleep(2)
            login_modal = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"], [data-testid*="dialog"], [aria-modal="true"]')
            if login_modal:
                print("[INFO] Detected login modal, attempting login...", flush=True)
                logged_in = login_to_facebook(driver, email, password)
                if logged_in:
                    sleep(3)
                    driver.get(page_url)
                    sleep(3)
    
    # Wait 5 seconds for content to load
    print("[INFO] Waiting 5 seconds for content to load...", flush=True)
    sleep(5)
    
    # Final check - ensure we're on the correct page
    current_url = driver.current_url.lower()
    if "/login" in current_url:
        print("[INFO] Still on login page after wait, attempting login again...", flush=True)
        if email and password:
            login_to_facebook(driver, email, password)
            sleep(5)
            driver.get(page_url)
            sleep(5)
    
    # Find posts container (x19h7ccj class)
    print("[INFO] Looking for posts container (x19h7ccj)...", flush=True)
    try:
        posts_container = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".x19h7ccj, div.x19h7ccj"))
        )
        print("[INFO] Found posts container", flush=True)
    except TimeoutException:
        print("[WARNING] Posts container not found, trying to find articles directly...", flush=True)
        posts_container = None
    
    # Small scroll to trigger lazy loading
    try:
        driver.execute_script("window.scrollTo(0, 300);")
        sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        sleep(1)
    except:
        pass
    
    # Find articles within posts container or page
    if posts_container:
        articles = posts_container.find_elements(By.XPATH, ".//div[@role='article']")
        print(f"[INFO] Found {len(articles)} articles in posts container", flush=True)
    else:
        articles = driver.find_elements(By.XPATH, "//div[@role='article']")
        print(f"[INFO] Found {len(articles)} articles on page", flush=True)
    
    if not articles:
        print("[ERROR] No articles found", flush=True)
        return None
    
    # Wait a bit more for content to load
    sleep(2)
    
    # Check first few articles for one with text
    for idx, article in enumerate(articles[:5]):
        print(f"[INFO] Checking article {idx+1}...", flush=True)
        
        try:
            # Extract text using JavaScript (most reliable)
            post_text = driver.execute_script("""
                var article = arguments[0];
                var textNodes = [];
                var walker = document.createTreeWalker(
                    article,
                    NodeFilter.SHOW_TEXT,
                    null,
                    false
                );
                var node;
                while (node = walker.nextNode()) {
                    var text = node.textContent.trim();
                    if (text && text.length > 30) {
                        // Skip UI elements
                        if (!text.match(/^(Synes godt om|Kommenter|Del|Like|Comment|Share|Alle reaktioner|All reactions|\\d+[mhd]|\\d+\\s*(min|hour|day))/i)) {
                            textNodes.push(text);
                        }
                    }
                }
                // Return the longest text node (likely post content)
                if (textNodes.length > 0) {
                    var longest = textNodes.sort((a, b) => b.length - a.length)[0];
                    // Filter out author names (2-3 words, all capitalized, no punctuation)
                    var words = longest.split(' ');
                    if (words.length <= 3 && words.every(w => w[0] && w[0] === w[0].toUpperCase()) && 
                        !/[.,!?:👇👆]/.test(longest)) {
                        // Might be author name, try next longest
                        if (textNodes.length > 1) {
                            return textNodes[1];
                        }
                    }
                    return longest;
                }
                return '';
            """, article)
            
            # Filter out author names
            if post_text:
                words = post_text.split()
                if len(words) <= 3 and all(w[0].isupper() if w else False for w in words) and not any(c in post_text for c in [".", ",", "!", "?", ":", "👇", "👆"]):
                    print(f"[DEBUG] Text '{post_text}' looks like author name, skipping...", flush=True)
                    post_text = ""
            
            if not post_text or len(post_text) < 20:
                print(f"[DEBUG] No good text found (length: {len(post_text) if post_text else 0})", flush=True)
                continue
            
            print(f"[INFO] Found text: {post_text[:100]}... (length: {len(post_text)})", flush=True)
            
            # Extract video URL (preferred)
            video_url = None
            video_links = article.find_elements(By.XPATH, ".//a[contains(@href, '/reel/')] | .//a[contains(@href, '/video/')] | .//a[contains(@href, '/watch/')]")
            if video_links:
                for link in video_links:
                    href = link.get_attribute("href") or ""
                    if href and not href.startswith("blob:") and ("/reel/" in href or "/video/" in href or "/watch/" in href):
                        if href.startswith("http"):
                            video_url = href.split("?")[0]
                        elif href.startswith("/"):
                            video_url = "https://www.facebook.com" + href.split("?")[0]
                        if "/reel/" in video_url:
                            reel_id = video_url.split("/reel/")[-1].split("/")[0].split("?")[0]
                            video_url = f"https://www.facebook.com/reel/{reel_id}"
                        print(f"[INFO] Found video URL: {video_url}", flush=True)
                        break
            
            # Extract image URL (fallback if no video)
            image_url = None
            if not video_url:
                img_elements = article.find_elements(By.CSS_SELECTOR, "img")
                for img in img_elements:
                    src = img.get_attribute("src") or ""
                    if src and "fbcdn.net" in src and len(src) > 100:
                        if "emoji" not in src.lower() and "icon" not in src.lower() and "profile" not in src.lower():
                            if "scontent" in src or "video" in src.lower() or "thumb" in src.lower():
                                image_url = src
                                print(f"[INFO] Found image URL", flush=True)
                                break
            
            # Extract author name
            author = "Unknown"
            author_selectors = [
                ".//h2//a",
                ".//strong//a",
                ".//a[contains(@href, 'socialdemokratiet')]",
            ]
            for selector in author_selectors:
                try:
                    author_elem = article.find_element(By.XPATH, selector)
                    author_text = author_elem.text.strip()
                    if author_text and len(author_text) > 2:
                        author = author_text
                        break
                except:
                    continue
            
            # Extract post link
            post_link = page_url
            link_elem = article.find_elements(By.XPATH, ".//a[contains(@href, '/posts/')] | .//a[contains(@href, '/reel/')]")
            if link_elem:
                href = link_elem[0].get_attribute("href") or ""
                if href and href.startswith("http"):
                    post_link = href.split("?")[0]
            
            # Create post object
            hash_source = "|".join([
                post_link or "",
                (post_text or "")[:200],
                video_url or "",
                author or "",
            ])
            post_id = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()[:16]
            post = {
                "post_id": post_id,
                "author_name": author,
                "post_text": post_text[:1000],  # Limit text length
                "post_time": "",
                "post_link": post_link,
                "video_url": video_url or "",
                "video_thumbnail": image_url or "",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            
            print(f"[SUCCESS] Extracted post: author='{author}', text_length={len(post_text)}, video={bool(video_url)}, image={bool(image_url)}", flush=True)
            return post
            
        except Exception as e:
            print(f"[DEBUG] Error extracting article {idx+1}: {e}", flush=True)
            continue
    
    print("[WARNING] No post with text found in first 5 articles", flush=True)
    return None


def main():
    parser = argparse.ArgumentParser(description="Scrape first Facebook post from party page")
    parser.add_argument("--group-url", required=True, help="Facebook page URL")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    args = parser.parse_args()
    
    # Extract page slug from URL and map to party code
    from urllib.parse import urlparse, parse_qs
    
    parsed = urlparse(args.group_url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    
    # Map URL patterns to party code
    PARTY_MAP = {
        "socialdemokratiet": "A",
        "radikalevenstre": "B",
        "konservative": "C",
        "sfparti": "F",
        "liberalalliance": "I",
        "moderaterne": "M",
        "danskfolkeparti": "O",
        "venstre.dk": "V",
        "partietdd": "Æ",
        "enhedslisten": "Ø",
        "alternativet.dk": "Å",
    }
    
    # Handle profile.php URLs (party H)
    if "profile.php" in parsed.path:
        party_code = "H"
        page_slug = "profile.php"
    else:
        # Extract slug from path
        page_slug = path_parts[-1].split("?")[0].lower() if path_parts else ""
        
        # Try to find party code from slug
        party_code = None
        for slug_key, code in PARTY_MAP.items():
            if slug_key in page_slug.lower():
                party_code = code
                break
        
        # Fallback: use slug uppercase if no mapping found
        if not party_code:
            party_code = page_slug.upper()[:3] if page_slug else "UNK"
            print(f"[WARNING] No party mapping found for '{page_slug}', using '{party_code}'", flush=True)
    
    print(f"[INFO] Mapped URL to party_code: {party_code}, page_slug: {page_slug}", flush=True)
    
    driver = None
    try:
        driver = setup_driver(headless=args.headless)
        
        # Scrape first post
        post = scrape_first_post(driver, args.group_url)
        
        if not post:
            print("[ERROR] Failed to scrape post", flush=True)
            sys.exit(1)
        
        # Ensure post_id includes party information for uniqueness
        hash_source = "|".join([
            party_code or "",
            post.get("post_link") or "",
            (post.get("post_text") or "")[:200],
            post.get("video_url") or "",
        ])
        post["post_id"] = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()[:16]
        post["party_code"] = party_code

        # Check if post already exists in database
        if post_exists(post, party_code):
            print(f"[INFO] Post already exists in database, skipping", flush=True)
        else:
            # Insert post into SQLite database
            if insert_post(post, party_code):
                print(f"[SUCCESS] Saved post to database (party: {party_code})", flush=True)
            else:
                print(f"[WARNING] Failed to save post to database", flush=True)
        
        # Also save to individual JSON file for fallback/backup
        individual_file = DATA_DIR / f"facebook_group_{page_slug}.json"
        individual_file.write_text(json.dumps([post], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] Saved backup to: {individual_file.name}", flush=True)
        
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()

