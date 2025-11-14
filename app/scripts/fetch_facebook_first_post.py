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

SEE_MORE_KEYWORDS = [
    "see more",
    "se mere",
    "vis mere",
    "læs mere",
    "read more",
    "show more",
]

COMMENT_EXACT_MATCHES = {
    "skriv en kommentar",
    "se flere kommentarer",
    "kommentarer",
    "kommentar",
    "kommenter",
    "write a comment",
    "view more comments",
    "see more comments",
    "reply",
    "svar",
    "del",
    "vis flere kommentarer",
    "vis alle",
    "show more comments",
}

COMMENT_CONTAINS_KEYWORDS = (
    "synes godt om",  # likes
    "liker dette",
    "likes",
    " synes godt om",
    " kommenter",
    " kommentar",
    "jeg er bange for",  # Common comment starter
    "redigeret",  # "Edited" in comments
    "se mere",  # "See more" in comments (but we want it in posts too, so be careful)
)


def _click_see_more(article, driver):
    """Click 'See more' / 'Læs mere' button to expand post text."""
    try:
        candidates = article.find_elements(
            By.XPATH,
            ".//*[self::div or self::span or self::a][contains(text(),'Se mere') or contains(text(),'See more') or contains(text(),'Vis mere') or contains(text(),'Læs mere') or contains(text(),'Read more') or contains(text(),'Show more')]",
        )
        for element in candidates:
            try:
                text = (element.text or "").strip().lower()
            except Exception:
                text = ""
            if not text:
                continue
            if any(keyword in text for keyword in SEE_MORE_KEYWORDS):
                try:
                    driver.execute_script("arguments[0].click();", element)
                    sleep(0.2)
                except Exception:
                    try:
                        element.click()
                        sleep(0.2)
                    except Exception:
                        continue
    except Exception:
        pass


def _clean_post_text(text: str) -> str:
    """Clean post text by removing comment-related content."""
    if not text:
        return ""
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        
        # Skip exact matches
        if lower in COMMENT_EXACT_MATCHES:
            continue
        
        # Skip lines starting with comment keywords
        if any(lower.startswith(keyword) for keyword in COMMENT_EXACT_MATCHES):
            continue
        
        # Skip short lines that contain comment keywords (likely UI elements)
        if any(keyword in lower for keyword in COMMENT_CONTAINS_KEYWORDS) and len(lower) <= 60:
            continue
        
        # Skip lines that look like comment metadata (e.g., "Redigeret", "6 t.")
        if lower in ["redigeret", "edited"] and len(lines) > 0:
            # If we have content and this is just "edited", skip it
            continue
        
        # Skip lines that are just timestamps or engagement counts
        if re.match(r'^\d+[.,]?\d*\s*(tusind|t\.|timer|hours?|min\.|minutes?)$', lower):
            continue
        
        lines.append(line)
    
    # Join and clean up
    result = "\n".join(lines).strip()
    
    # Remove trailing comment indicators
    result = re.sub(r'\s*(Se mere|See more|Vis flere kommentarer|View more comments)\s*$', '', result, flags=re.IGNORECASE)
    
    return result


def _is_valid_post_text(text: str) -> bool:
    """Check if text is valid post content (not a comment or UI element)."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 20:
        return False
    lower = stripped.lower()
    
    # Skip exact matches
    if lower in COMMENT_EXACT_MATCHES:
        return False
    
    # Skip if text starts with common comment patterns
    comment_starters = [
        "jeg er bange for",
        "i am afraid",
        "jeg synes",
        "i think",
        "jeg mener",
        "i believe",
        "undergøg om",  # "Investigate whether" - from the comment in the image
    ]
    if any(lower.startswith(starter) for starter in comment_starters):
        # Only reject if it's clearly a comment (short or has comment indicators)
        if len(stripped) < 100 or any(keyword in lower for keyword in ["redigeret", "edited", "svar", "reply"]):
            return False
    
    # Skip if text contains comment metadata patterns
    if re.search(r'\bredigeret\b|\bedited\b', lower) and len(stripped) < 150:
        return False
    
    return True


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
            _click_see_more(article, driver)

            post_text = ""
            
            # Helper function to check if element is in a comment section
            def is_in_comment_section(element):
                """Check if element is inside a comment structure."""
                try:
                    # Check parent elements for comment indicators
                    parent = element
                    for _ in range(10):  # Check up to 10 levels up
                        try:
                            parent = parent.find_element(By.XPATH, "./..")
                        except:
                            break
                        
                        # Check for comment-related attributes and classes
                        try:
                            parent_id = parent.get_attribute("id") or ""
                            parent_class = parent.get_attribute("class") or ""
                            parent_role = parent.get_attribute("role") or ""
                            
                            # Check for comment indicators
                            if any(indicator in parent_id.lower() for indicator in ["comment", "reply", "svar"]):
                                return True
                            if any(indicator in parent_class.lower() for indicator in ["comment", "reply", "svar", "ufi"]):
                                return True
                            if "comment" in parent_role.lower():
                                return True
                            
                            # Check for comment action buttons nearby
                            try:
                                comment_buttons = parent.find_elements(By.XPATH, 
                                    ".//a[contains(., 'Svar') or contains(., 'Reply') or contains(., 'Kommenter')] | "
                                    ".//button[contains(., 'Svar') or contains(., 'Reply')]")
                                if comment_buttons:
                                    return True
                            except:
                                pass
                        except:
                            continue
                    return False
                except:
                    return False
            
            # Prioritized selectors for post text (not comments)
            main_text_selectors = [
                ".//div[@data-ad-preview='message']//span[@dir='auto']",
                ".//div[@data-ad-preview='message']",
                ".//div[contains(@data-ad-preview,'message')]",
                # Try to find text in the upper part of article (before comments)
                ".//div[@role='article']//div[contains(@class,'x1y1aw1k')]//span[@dir='auto'][1]",
                ".//div[contains(@class,'x19h7ccj')]//span[@dir='auto'][1]",
            ]

            seen_elements = set()
            text_candidates = []
            
            # First pass: find candidates with prioritized selectors
            for selector in main_text_selectors:
                try:
                    elements = article.find_elements(By.XPATH, selector)
                except Exception:
                    continue
                for element in elements:
                    # Skip if in comment section
                    if is_in_comment_section(element):
                        continue
                    
                    elem_id = getattr(element, "id", None)
                    if elem_id and elem_id in seen_elements:
                        continue
                    seen_elements.add(elem_id)
                    text_candidates.append(element)

            # If no candidates found, try broader search but exclude comments
            if not text_candidates:
                try:
                    all_text_elements = article.find_elements(By.XPATH, 
                        ".//div[@dir='auto' and string-length(normalize-space(text())) > 20] | "
                        ".//span[@dir='auto' and string-length(normalize-space(text())) > 20]")
                    for element in all_text_elements[:10]:  # Limit to first 10
                        if is_in_comment_section(element):
                            continue
                        elem_id = getattr(element, "id", None)
                        if elem_id and elem_id in seen_elements:
                            continue
                        seen_elements.add(elem_id)
                        text_candidates.append(element)
                except:
                    pass

            # Evaluate candidates - prefer text from upper part of article
            best_candidate = None
            best_score = -1
            
            for element in text_candidates:
                try:
                    candidate = (element.text or "").strip()
                except Exception:
                    continue
                
                # Clean and validate
                candidate = _clean_post_text(candidate)
                if not _is_valid_post_text(candidate):
                    continue
                
                # Calculate score: prefer longer text and text higher in the article
                score = len(candidate)
                
                # Bonus for being in upper part of article (post text is usually before comments)
                try:
                    # Get element position relative to article
                    element_y = element.location['y']
                    article_y = article.location['y']
                    article_height = article.size['height']
                    
                    # If element is in upper 60% of article, give bonus
                    relative_position = (element_y - article_y) / max(article_height, 1)
                    if relative_position < 0.6:
                        score += 100  # Bonus for being in upper part
                except:
                    pass
                
                # Prefer text that doesn't look like a comment
                if not any(starter in candidate.lower()[:50] for starter in ["jeg er bange", "i am afraid", "undergøg om"]):
                    score += 50
                
                if score > best_score:
                    best_score = score
                    best_candidate = candidate
            
            if best_candidate:
                post_text = best_candidate[:1000]

            if not post_text:
                # Fallback: get all text and try to extract post text (not comments)
                try:
                    js_text = driver.execute_script("return arguments[0].innerText || arguments[0].textContent || '';", article)
                    js_text = (js_text or "").strip()
                    
                    # Split by common comment section markers
                    comment_markers = [
                        "kommentarer",
                        "comments",
                        "vis flere kommentarer",
                        "view more comments",
                        "se flere kommentarer",
                        "see more comments",
                    ]
                    
                    # Try to find where comments start
                    lines = js_text.split('\n')
                    post_lines = []
                    found_comment_section = False
                    
                    for line in lines:
                        line_lower = line.strip().lower()
                        # If we hit a comment marker, stop collecting
                        if any(marker in line_lower for marker in comment_markers):
                            found_comment_section = True
                            break
                        # Skip empty lines and UI elements
                        if line.strip() and len(line.strip()) > 3:
                            post_lines.append(line.strip())
                    
                    # If we found comment section, use text before it
                    if found_comment_section and post_lines:
                        js_text = '\n'.join(post_lines)
                    else:
                        # Otherwise use cleaned version of all text
                        js_text = _clean_post_text(js_text)
                    
                    if _is_valid_post_text(js_text):
                        post_text = js_text[:1000]
                except Exception:
                    post_text = ""

            if not post_text:
                print(f"[DEBUG] No good text found (length: 0)", flush=True)
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

