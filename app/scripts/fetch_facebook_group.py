#!/usr/bin/env python3
"""Scrape posts from a public Facebook group and persist them as JSON."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

try:
    from bs4 import BeautifulSoup
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError as e:
    print(f"[ERROR] Missing required dependency: {e}", file=sys.stderr)
    print("[INFO] Install with: pip install beautifulsoup4 selenium webdriver-manager", file=sys.stderr)
    sys.exit(1)


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SCROLL_PAUSE = 2
DEFAULT_MAX_POSTS = 50


def setup_chrome_driver(headless: bool = False) -> webdriver.Chrome:
    """Set up and return a Chrome WebDriver instance."""
    import os
    from pathlib import Path
    
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Use system Chromium/ChromeDriver if available (Alpine Linux)
    chromedriver_path = None
    chromium_path = None
    
    # Check for system chromedriver (Alpine)
    for path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]:
        if Path(path).exists():
            chromedriver_path = path
            break
    
    # Check for system chromium/chrome binary (Alpine)
    for path in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
        if Path(path).exists():
            chromium_path = path
            chrome_options.binary_location = path
            break
    
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        # Fallback to ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.maximize_window()
    return driver


def simulate_human_typing(element, text: str) -> None:
    """Simulate human-like typing patterns to avoid detection."""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.1, 0.3))
        if random.random() < 0.1:  # 10% chance of longer pause
            time.sleep(random.uniform(0.3, 0.7))


def login_to_facebook(
    driver: webdriver.Chrome,
    email: str | None,
    password: str | None,
    wait_timeout: int = 30,
) -> bool:
    """Log in to Facebook. Returns True if successful, False otherwise."""
    if not email or not password:
        print("[INFO] No credentials provided, attempting to access as guest...")
        return False

    try:
        driver.get("https://www.facebook.com/login")
        sleep(2)

        # Accept cookies if present
        try:
            cookies_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//button[contains(@class, "_42ft") and contains(text(), "Accept")]')
                )
            )
            cookies_button.click()
            sleep(1)
        except Exception:
            pass  # Cookies button might not be present

        # Fill in login form with human-like typing
        email_field = WebDriverWait(driver, wait_timeout).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        simulate_human_typing(email_field, email)

        password_field = driver.find_element(By.NAME, "pass")
        simulate_human_typing(password_field, password)

        sleep(random.uniform(0.5, 1.5))

        # Click login button with mouse movement simulation
        login_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver)\
            .move_to_element(login_button)\
            .pause(random.uniform(0.2, 0.4))\
            .click()\
            .perform()

        sleep(15)  # Wait for page to load

        # Check if login was successful (not on login page anymore)
        current_url = driver.current_url.lower()
        if "login" not in current_url and "checkpoint" not in current_url:
            # Also check for common post-login elements
            try:
                # Look for feed or home page indicators
                page_source = driver.page_source.lower()
                if any(indicator in page_source for indicator in ["feed", "home", "watch", "marketplace"]):
                    print("[SUCCESS] Logged in to Facebook - detected feed/home page")
                    return True
                elif "login" not in current_url:
                    print("[SUCCESS] Logged in to Facebook - not on login page")
                    return True
            except:
                pass
        
        print("[WARNING] Login may have failed - still on login/checkpoint page")
        print(f"[INFO] Current URL: {driver.current_url}", flush=True)
        return False

    except Exception as e:
        print(f"[WARNING] Login failed: {e}", file=sys.stderr)
        return False


def slow_scroll(driver: webdriver.Chrome, step: int = 500) -> None:
    """Scroll the page slowly in increments to trigger lazy loading."""
    driver.execute_script(f"window.scrollBy(0, {step});")
    sleep(2)


def create_post_key(post: dict) -> str:
    """Create a unique key for a post to detect duplicates."""
    # Use post link as primary identifier
    if post.get("post_link"):
        post_link = post["post_link"]
        if "/posts/" in post_link:
            post_id = post_link.split("/posts/")[-1].split("/")[0].split("?")[0]
            return f"link:{post_id}"
        elif "/permalink/" in post_link:
            post_id = post_link.split("/permalink/")[-1].split("/")[0].split("?")[0]
            return f"link:{post_id}"
    
    # Fallback: use text + time
    text = post.get("post_text", "").strip().lower()
    time_str = post.get("post_time", "").strip().lower()
    if text and time_str:
        text_normalized = re.sub(r'\s+', ' ', text)[:200]
        return f"text_time:{hash(text_normalized + time_str)}"
    
    # Last resort: just text hash
    if text:
        text_normalized = re.sub(r'\s+', ' ', text)[:200]
        return f"text:{hash(text_normalized)}"
    
    return f"fallback:{hash(str(post))}"


def scrape_facebook_group_posts(
    driver: webdriver.Chrome,
    group_url: str,
    max_posts: int = DEFAULT_MAX_POSTS,
    scroll_pause: float = DEFAULT_SCROLL_PAUSE,
) -> list[dict[str, str]]:
    """Scrape posts from a Facebook group."""
    print(f"[INFO] Navigating to group: {group_url}", flush=True)
    driver.get(group_url)
    sleep(5)  # Wait longer for initial load
    
    # Wait for content to load
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except:
        pass
    
    print(f"[INFO] Page loaded, current URL: {driver.current_url}", flush=True)

    # Extract page/group name from URL first (needed for filtering)
    page_name_from_url = None
    if group_url:
        if "/groups/" in group_url:
            page_name_from_url = group_url.split("/groups/")[-1].split("/")[0].split("?")[0].lower()
        elif "/pages/" in group_url:
            page_name_from_url = group_url.split("/pages/")[-1].split("/")[0].split("?")[0].lower()
        elif "facebook.com/" in group_url:
            page_name_from_url = group_url.split("facebook.com/")[-1].split("/")[0].split("?")[0].lower()
        # Handle profile.php?id=XXXXX
        if "profile.php?id=" in group_url:
            match = re.search(r'profile\.php\?id=(\d+)', group_url)
            if match:
                # Try to get page name from current URL after redirect
                current_url = driver.current_url
                if "/people/" in current_url:
                    page_name_from_url = current_url.split("/people/")[-1].split("/")[0].split("?")[0].lower()
    
    print(f"[INFO] Extracted page name from URL: '{page_name_from_url}'", flush=True)
    
    # Scroll to load more posts - scroll aggressively to get at least max_posts
    # Use incremental scrolling like the article suggests
    last_height = driver.execute_script("return document.body.scrollHeight")
    scrolls = 0
    max_scrolls = 100  # Increased from 50 to scroll more (up to 100 times)
    consecutive_no_change = 0

    print(f"[INFO] Starting to scroll (initial height: {last_height}, max scrolls: {max_scrolls})", flush=True)
    
    while scrolls < max_scrolls:
        
        # Scroll incrementally (like article method - 500px at a time)
        driver.execute_script("window.scrollBy(0, 500);")
        sleep(2)  # Wait for content to load
        
        # Also scroll to bottom occasionally to trigger lazy loading
        if scrolls % 3 == 0:  # Every 3rd scroll
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            sleep(3)
        
        # Also try scrolling back up a bit and down again to trigger loading
        if scrolls % 7 == 0 and scrolls > 0:
            driver.execute_script("window.scrollBy(0, -200);")
            sleep(1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            sleep(3)
        
        # Check if page height changed
        new_height = driver.execute_script("return document.body.scrollHeight")
        
        if new_height == last_height:
            consecutive_no_change += 1
            # Try scrolling a bit more to trigger loading
            driver.execute_script("window.scrollBy(0, 1000);")
            sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            
            # Only break if we've had no change for many consecutive scrolls
            # But be more patient - Facebook sometimes takes time to load
            if consecutive_no_change >= 20:  # Increased from 15 to 20
                print(f"[INFO] No more content loading after {consecutive_no_change} consecutive scrolls", flush=True)
                break
        else:
            consecutive_no_change = 0  # Reset counter if we got new content
            print(f"[INFO] Content loaded! Height changed from {last_height} to {new_height}", flush=True)
            
        last_height = new_height
        scrolls += 1
        
        # Print progress every 5 scrolls
        if scrolls % 5 == 0:
            print(f"[INFO] Scroll {scrolls}/{max_scrolls}, height: {new_height}, consecutive no-change: {consecutive_no_change}", flush=True)

    print(f"[INFO] Finished scrolling: {scrolls} scrolls completed, final height: {last_height}", flush=True)
    
    # Scroll a few more times at the end to ensure all content is loaded
    print(f"[INFO] Doing final scrolls to ensure all posts are loaded...", flush=True)
    for final_scroll in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        sleep(2)
        driver.execute_script("window.scrollBy(0, -300);")
        sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        sleep(2)
    
    # Wait a bit more for content to load
    sleep(5)

    # Parse the page with BeautifulSoup
    soup = BeautifulSoup(driver.page_source, "html.parser")
    posts = []

    # Common Facebook post container classes (these may need updating if Facebook changes their structure)
    # Focus on actual post containers, not comments or UI
    # Added selector from article: {"class": "x1n2onr6 x1ja2u2z"}
    post_selectors = [
        {"class": "x1n2onr6 x1ja2u2z"},  # From article - reliable selector
        {"class": "du4w35lb k4urcfbm l9j0dhe7 sjgh65i0"},
        {"class": "x1y1aw1k x1n2onr6 xymvj8b x1odjw0f"},
        # Try to find posts by structure - posts usually have specific data attributes
        {"data-pagelet": lambda x: x and "FeedUnit" in str(x)},  # FeedUnit is usually a post
    ]

    # Try multiple selectors and combine results to get more posts
    all_post_elements = []
    found_ids = set()
    
    for selector in post_selectors:
        found = soup.find_all("div", selector)
        for elem in found:
            elem_id = id(elem)
            if elem_id not in found_ids:
                found_ids.add(elem_id)
                all_post_elements.append(elem)
        if found:
            print(f"[INFO] Found {len(found)} elements using selector: {selector}", flush=True)
    
    # Also try role="article" and add unique ones
    article_elements = soup.find_all("div", {"role": "article"})
    for elem in article_elements:
        elem_id = id(elem)
        if elem_id not in found_ids:
            found_ids.add(elem_id)
            all_post_elements.append(elem)
    
    if article_elements:
        print(f"[INFO] Found {len(article_elements)} elements with role='article'", flush=True)
    
    # Also try to find posts by looking for divs with specific data attributes
    # Facebook posts often have data-pagelet="FeedUnit_XXX"
    feed_unit_elements = soup.find_all("div", attrs={"data-pagelet": lambda x: x and "FeedUnit" in str(x)})
    for elem in feed_unit_elements:
        elem_id = id(elem)
        if elem_id not in found_ids:
            found_ids.add(elem_id)
            all_post_elements.append(elem)
    
    if feed_unit_elements:
        print(f"[INFO] Found {len(feed_unit_elements)} elements with FeedUnit data-pagelet", flush=True)
    
    # Also search for elements that contain page/group name as author
    # Look for links or text that contains page name followed by post content
    import re
    page_links = []
    if page_name_from_url:
        # Search for links containing page name or general page/group links
        page_links = soup.find_all("a", href=lambda x: x and (page_name_from_url in str(x).lower() or "/pages/" in str(x) or "/groups/" in str(x)))
    
    for link in page_links[:50]:  # Limit to avoid too many
        # Find the parent post container
        parent = link.find_parent("div", {"role": "article"}) or link.find_parent("div", class_=lambda x: x and isinstance(x, list))
        if parent:
            elem_id = id(parent)
            if elem_id not in found_ids:
                found_ids.add(elem_id)
                all_post_elements.append(parent)
                print(f"[INFO] Found post via page link", flush=True)
    
    # Also search for text nodes containing page name and get their parent containers
    if page_name_from_url:
        page_name_pattern = re.compile(re.escape(page_name_from_url), re.I)
        for text_node in soup.find_all(string=page_name_pattern):
            parent = text_node.find_parent("div")
            if parent:
                elem_id = id(parent)
                if elem_id not in found_ids and len(parent.get_text(strip=True)) > 30:
                    found_ids.add(elem_id)
                    all_post_elements.append(parent)
                    print(f"[INFO] Found post via page name text node", flush=True)
    
    # Try to find more elements using broader selectors
    # Look for divs that contain substantial text and might be posts
    print(f"[INFO] Searching for additional post elements with broader criteria...", flush=True)
    all_divs = soup.find_all("div", limit=500)  # Get more divs
    additional_found = 0
    for div in all_divs:
        text_content = div.get_text(strip=True)
        # Skip if too short or already found
        if len(text_content) < 30:
            continue
        elem_id = id(div)
        if elem_id in found_ids:
            continue
        
        # Check if it looks like a post (has some structure)
        # Skip if it's clearly UI (very short, contains UI words)
        text_lower = text_content.lower()
        ui_indicators = ["write a comment", "add a comment", "see all photos", "see all videos", "intro", "log in", "forgot account"]
        if any(ui in text_lower for ui in ui_indicators) and len(text_content) < 60:
            continue
        
        # Check if it has links or structure that suggests it's a post
        has_links = div.find_all("a", limit=5)
        # If it has substantial text (more than 40 chars) or has multiple links, consider it
        if len(text_content) > 40 or len(has_links) >= 2:
            # Additional check: make sure it's not nested inside another post element
            parent = div.find_parent()
            is_nested = False
            depth = 0
            while parent and depth < 3:
                if id(parent) in found_ids:
                    is_nested = True
                    break
                parent = parent.find_parent()
                depth += 1
            
            if not is_nested:
                all_post_elements.append(div)
                found_ids.add(elem_id)
                additional_found += 1
                if len(all_post_elements) >= max_posts * 4:  # Get plenty to filter
                    break
    
    if additional_found > 0:
        print(f"[INFO] Found {additional_found} additional elements with broader search", flush=True)
    
    print(f"[INFO] Total unique elements found: {len(all_post_elements)}", flush=True)
    
    # If we found very few elements, try even broader search
    if len(all_post_elements) < 5:
        print(f"[INFO] Found very few elements ({len(all_post_elements)}), trying broader search...", flush=True)
        # Try finding ANY divs with substantial content
        all_divs_broad = soup.find_all("div", limit=1000)
        for div in all_divs_broad:
            text_content = div.get_text(strip=True)
            # Look for divs with substantial text (more than 50 chars) that might be posts
            if len(text_content) > 50:
                # Skip if it's clearly UI
                text_lower = text_content.lower()
                ui_indicators = ["log in", "forgot account", "write a comment", "add a comment", "see all photos"]
                if any(ui in text_lower for ui in ui_indicators):
                    continue
                
                elem_id = id(div)
                if elem_id not in found_ids:
                    # Check if it's nested inside an already found element
                    is_nested = False
                    parent = div.find_parent()
                    depth = 0
                    while parent and depth < 5:
                        if id(parent) in found_ids:
                            is_nested = True
                            break
                        parent = parent.find_parent()
                        depth += 1
                    
                    if not is_nested:
                        found_ids.add(elem_id)
                        all_post_elements.append(div)
                        if len(all_post_elements) >= 50:  # Limit to avoid too many
                            break
        
        print(f"[INFO] After broader search: {len(all_post_elements)} elements found", flush=True)
    
    # Prioritize elements that match the page/group name - these are likely real posts
    matching_elements = []
    other_elements = []
    
    for elem in all_post_elements:
        text_content = elem.get_text(strip=True)
        text_lower = text_content.lower()
        
        # Check if element contains page/group name - prioritize these
        is_matching = False
        if page_name_from_url:
            # Normalize for comparison
            page_name_normalized = page_name_from_url.replace("-", "").replace("_", "")
            text_normalized = text_lower.replace("-", "").replace("_", "").replace(" ", "")
            
            if page_name_normalized in text_normalized or len(page_name_normalized) > 5 and text_normalized.startswith(page_name_normalized[:5]):
                is_matching = True
        
        if is_matching:
            # Check if it's not just UI text
            if len(text_content) > 30 and not any(ui in text_lower for ui in ["log in", "forgot account", "followers", "following"]):
                matching_elements.append(elem)
            else:
                other_elements.append(elem)
        else:
            other_elements.append(elem)
    
    print(f"[INFO] Found {len(matching_elements)} elements matching page name '{page_name_from_url}'", flush=True)
    print(f"[INFO] Found {len(other_elements)} other elements", flush=True)
    
    # Reorder: matching elements first, then others
    all_post_elements = matching_elements + other_elements
    
    # Filter out non-post elements (like UI sections, navigation, comments, etc.)
    filtered_elements = []
    seen_texts = set()  # Track seen post texts to avoid duplicates
    
    # UI patterns to exclude
    ui_exclusion_patterns = [
        "log in", "forgot account", "facebook log in", "followers", "following",
        "verified account", "reels", "more posts", "about", "photos", "see all photos",
        "page ·", "political party", "intro", "page insights", "data · privacy",
        "terms · advertising", "ad choices", "cookies", "more", "information about page",
        "socialdemokratiets officielle facebookside", "page description", "about this page"
    ]
    
    for elem in all_post_elements:
        text_content = elem.get_text(strip=True)
        text_lower = text_content.lower()
        
        # Skip if it's too short (likely not a post)
        if len(text_content) < 20:
            continue
        
        # Check if this element matches the page/group name - if so, be more lenient
        has_page_match = False
        if page_name_from_url:
            page_name_normalized = page_name_from_url.replace("-", "").replace("_", "")
            text_normalized = text_lower.replace("-", "").replace("_", "").replace(" ", "")
            has_page_match = page_name_normalized in text_normalized or (len(page_name_normalized) > 5 and text_normalized.startswith(page_name_normalized[:5]))
        
        # Skip if it contains UI exclusion patterns - but be more lenient for page-matching elements
        if any(pattern in text_lower for pattern in ui_exclusion_patterns):
            # Skip "See more from [page]" - this is definitely UI
            if "see more from" in text_lower:
                print(f"[INFO] Skipping 'See more' UI element: {text_content[:60]}...", flush=True)
                continue
            
            # If it matches the page AND has substantial text, it might be a real post with UI mixed in
            if has_page_match and len(text_content) > 80:
                # Check if it has actual post content (not just UI)
                # Look for sentence-like content (has periods, question marks, or multiple sentences)
                has_sentences = any(marker in text_content for marker in [".", "!", "?", "?"])
                if has_sentences or len(text_content) > 100:
                    # It might be a real post, don't skip it yet
                    print(f"[INFO] Keeping page-matching element with UI (might be post): {text_content[:60]}...", flush=True)
                else:
                    print(f"[INFO] Skipping UI element: {text_content[:60]}...", flush=True)
                    continue
            else:
                print(f"[INFO] Skipping UI element: {text_content[:60]}...", flush=True)
                continue
        
        # Skip if it's mostly just navigation/UI words - but be more lenient for page-matching elements
        ui_words = ["log", "in", "forgot", "account", "followers", "following", "verified", "reels", "more", "about", "photos", "page", "intro"]
        words = text_content.lower().split()
        ui_word_count = sum(1 for word in words if word in ui_words)
        if len(words) > 0:
            ui_ratio = ui_word_count / len(words)
            # For page-matching elements, allow up to 60% UI words (they might have UI mixed in)
            # For others, stick to 50%
            threshold = 0.6 if has_page_match else 0.5
            if ui_ratio > threshold:
                print(f"[INFO] Skipping mostly UI text ({ui_ratio:.1%} UI words): {text_content[:60]}...", flush=True)
                continue
        
        # Skip comments - comments usually have "Reply" or are in nested structures
        # Check if this element is inside a comment structure or has comment indicators
        has_comment_indicators = False
        
        # Check for comment-specific attributes
        elem_attrs = elem.attrs
        if "data-commentid" in elem_attrs or "comment" in str(elem_attrs).lower():
            has_comment_indicators = True
        
        # Check parent elements for comment structure
        parent = elem.find_parent()
        depth = 0
        while parent and depth < 5:
            parent_classes = parent.get("class", [])
            parent_attrs = parent.attrs
            
            # Comments often have specific classes or attributes
            if any("comment" in str(c).lower() for c in parent_classes):
                has_comment_indicators = True
                break
            if "data-commentid" in parent_attrs or "comment" in str(parent_attrs).lower():
                has_comment_indicators = True
                break
            # Check for reply buttons or comment actions
            if parent.find("a", string=lambda x: x and "reply" in str(x).lower()):
                has_comment_indicators = True
                break
            parent = parent.find_parent()
            depth += 1
        
        # Also check the element itself for comment indicators
        elem_classes = elem.get("class", [])
        if any("comment" in str(c).lower() for c in elem_classes):
            has_comment_indicators = True
        
        # Check if text contains comment-like patterns (short responses, replies)
        # Comments are often shorter and may contain reply indicators
        # Also check for comment-like patterns in the text itself
        comment_patterns = [
            "reply", "svar", "kommentar", "den største fejl", "nedeMette", "helle castor"
        ]
        text_contains_comment_pattern = any(pattern in text_lower for pattern in comment_patterns)
        
        # Check if it looks like a comment (very short response to another post)
        # Comments often start with a name followed by a short statement
        if len(text_content) < 120 and "den største fejl" in text_lower:
            print(f"[INFO] Skipping comment pattern: {text_content[:50]}...", flush=True)
            continue
        
        # More aggressive comment detection: if it starts with a name and is short, likely a comment
        words = text_content.split()
        if len(words) > 0 and len(words) < 15:
            first_word = words[0].strip()
            # If first word looks like a name (capitalized, short) and text is short, might be comment
            if first_word[0].isupper() and len(first_word) < 20 and len(text_content) < 100:
                # Check if second word is also capitalized (like "Helle Castor")
                if len(words) > 1 and words[1][0].isupper() and len(words[1]) < 20:
                    print(f"[INFO] Skipping likely comment (name pattern): {text_content[:50]}...", flush=True)
                    continue
        
        # More lenient filtering: only skip if we're very sure it's a comment
        # Only skip if it has clear comment indicators AND is short
        if has_comment_indicators and len(text_content) < 100:
            # Check for comment patterns in text
            if text_contains_comment_pattern:
                print(f"[INFO] Skipping comment (pattern match): {text_content[:50]}...", flush=True)
                continue
            # Only skip if it's very short AND has comment indicators
            if len(text_content) < 80:
                print(f"[INFO] Skipping comment (short with indicators): {text_content[:50]}...", flush=True)
                continue
        
        # Skip obvious UI-only elements - but be more lenient
        ui_only_patterns = [
            "see all photos",
            "see all videos", 
            "write a comment",
            "add a comment",
        ]
        
        # Only skip if text is very short AND clearly UI
        is_ui_only = False
        for pattern in ui_only_patterns:
            if pattern in text_lower:
                # Only skip if it's very short and mostly just this pattern
                if len(text_content) < 40 and text_lower.count(" ") < 3:
                    is_ui_only = True
                    break
        
        if is_ui_only:
            continue
        
        # Skip if it's just a single word or very short - be more lenient
        if text_content.count(" ") < 2 and len(text_content) < 30:
            continue
        
        # Check for duplicate content (normalize text for comparison)
        # Extract the actual post text (skip author name and UI text at the start)
        # For page-matching posts, look for text after page name or after common UI patterns
        post_text_start = text_content
        if has_page_match and page_name_from_url:
            # Try to find where the actual post text starts (after author name and UI)
            # Look for patterns like "Socialdemokratiet... [time] ...actual post text"
            # Or just take text after first sentence if it's very long
            parts = text_content.split(".", 1)
            if len(parts) > 1 and len(parts[1]) > 30:
                post_text_start = parts[1].strip()
            # Also try to skip common UI prefixes
            for ui_prefix in ["verified account", "128k", "followers", "following"]:
                if text_lower.startswith(ui_prefix):
                    # Find where actual content starts
                    idx = text_content.lower().find(ui_prefix)
                    if idx >= 0:
                        remaining = text_content[idx + len(ui_prefix):].strip()
                        if len(remaining) > 30:
                            post_text_start = remaining
                            break
        
        # Use first 25 words of actual post text for comparison
        check_words = 25
        normalized_text = " ".join(post_text_start.split()[:check_words]).lower()  # First N words
        
        # Skip if we've seen this exact normalized text before
        if normalized_text in seen_texts:
            print(f"[INFO] Skipping duplicate post: {normalized_text[:50]}...", flush=True)
            continue
        
        seen_texts.add(normalized_text)
            
        filtered_elements.append(elem)
    
    all_post_elements = filtered_elements
    print(f"[INFO] After filtering, {len(all_post_elements)} posts remain", flush=True)
    
    # If we don't have enough posts or found very few, try to be less strict with filtering
    if len(filtered_elements) < max_posts or len(filtered_elements) < 3:
        print(f"[INFO] Only found {len(filtered_elements)} posts after filtering, trying less strict filtering...", flush=True)
        # Re-filter with less strict criteria - prioritize page-matching elements
        less_strict_elements = list(filtered_elements)  # Keep already filtered ones
        
        # Also try to get more elements from the original soup, focusing on page-matching elements
        additional_selectors = [
            {"role": "article"},
            {"class": lambda x: x and isinstance(x, list) and len(x) > 0},
        ]
        
        found_ids_set = {id(elem) for elem in less_strict_elements}
        
        for selector in additional_selectors:
            found = soup.find_all("div", selector, limit=100)
            for elem in found:
                elem_id = id(elem)
                if elem_id in found_ids_set:
                    continue
                    
                text_content = elem.get_text(strip=True)
                text_lower = text_content.lower()
                
                # Prioritize elements matching the page
                has_page_match = False
                if page_name_from_url:
                    page_name_normalized = page_name_from_url.replace("-", "").replace("_", "")
                    text_normalized = text_lower.replace("-", "").replace("_", "").replace(" ", "")
                    has_page_match = page_name_normalized in text_normalized
                
                if len(text_content) > 20:
                    # Quick check - skip obvious UI
                    if not any(ui in text_lower for ui in ["write a comment", "add a comment", "see all photos", "log in", "forgot account"]):
                        less_strict_elements.append(elem)
                        found_ids_set.add(elem_id)
                        # If it matches the page, prioritize it
                        if has_page_match:
                            # Move to front
                            less_strict_elements.remove(elem)
                            less_strict_elements.insert(0, elem)
                        
                        if len(less_strict_elements) >= max_posts * 3:  # Get extra to filter later
                            break
            if len(less_strict_elements) >= max_posts * 3:
                break
        
        all_post_elements = less_strict_elements[:max_posts * 3]  # Take more for processing
        print(f"[INFO] After less strict filtering, {len(all_post_elements)} elements to process", flush=True)
    
    print(f"[INFO] Processing up to {len(all_post_elements)} elements to find {max_posts} posts...", flush=True)
    
    if len(all_post_elements) == 0:
        print(f"[WARNING] No post elements found! This might indicate:", flush=True)
        print(f"[WARNING]   1. Facebook changed their HTML structure", flush=True)
        print(f"[WARNING]   2. Page requires login to see posts", flush=True)
        print(f"[WARNING]   3. Page has no posts or posts haven't loaded yet", flush=True)
        print(f"[WARNING]   4. Page URL might be incorrect", flush=True)
        return []
    
    # Track seen posts by unique key (text + time + video)
    seen_post_keys = set()

    for idx, post_element in enumerate(all_post_elements):
        # Process all elements - duplicates will be filtered during processing
        print(f"[INFO] Processing element {idx + 1}/{len(all_post_elements)} (found {len(posts)}/{max_posts} posts so far)", flush=True)

        post_data = {
            "post_id": f"post_{len(posts) + 1}",
            "author_name": "",
            "post_text": "",
            "post_time": "",
            "post_link": "",
            "video_url": "",
            "video_thumbnail": "",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Try to find author name - multiple strategies
            author_selectors = [
                {"class": "oajrlxb2 g5ia77u1 qu0x051f esr5mh6w e9989ue4 r7d6kgcz"},
                {"class": "x1i10hfl xjbqb8w x1ejq31n xd10rxx x1sy0etr x17r0tee"},
                {"class": "x1heor9g x1qlqyl8 x1pd3egz x1a2a7pz"},
                {"class": "x1i10hfl"},
                {"href": lambda x: x and "/user/" in x or "/groups/" in x or "/pages/" in x},
            ]

            # Try link-based selectors first
            for author_selector in author_selectors[:3]:
                author_link = post_element.find("a", author_selector)
                if author_link:
                    author_text = author_link.get_text(strip=True)
                    if author_text:
                        post_data["author_name"] = author_text
                        post_data["post_link"] = author_link.get("href", "")
                        break
            
            # If no author found via links, try finding by href pattern
            if not post_data["author_name"]:
                author_links = post_element.find_all("a", href=lambda x: x and ("/user/" in x or "/groups/" in x or "/pages/" in x or "/profile.php" in x))
                for link in author_links[:3]:  # Check first few links
                    author_text = link.get_text(strip=True)
                    # Filter out UI elements
                    if author_text and len(author_text) > 2 and len(author_text) < 50:
                        skip_ui = ["posts", "photos", "videos", "about", "intro", "see all", "more"]
                        if not any(ui_word in author_text.lower() for ui_word in skip_ui):
                            post_data["author_name"] = author_text
                            post_data["post_link"] = link.get("href", "")
                            break
            
            # If still no author, try finding strong/bold text near the top (often author name)
            if not post_data["author_name"]:
                strong_tags = post_element.find_all(["strong", "b", "span", "h2", "h3"], limit=15)
                for tag in strong_tags:
                    text = tag.get_text(strip=True)
                    # Better filtering for author names
                    if text and 3 < len(text) < 50 and not text.startswith("http"):
                        skip_ui = ["posts", "photos", "videos", "about", "intro", "see all", "more", "verified"]
                        if not any(ui_word in text.lower() for ui_word in skip_ui):
                            # Check if it looks like a name (has letters, not just UI text)
                            if len([c for c in text if c.isalpha()]) > 2:
                                # Prioritize page name if found
                                if page_name_from_url and page_name_from_url.replace("-", "").replace("_", "") in text.lower().replace("-", "").replace("_", "").replace(" ", ""):
                                    post_data["author_name"] = page_name_from_url.replace("-", " ").title()
                                    break
                                post_data["author_name"] = text
                                break
            
            # Also check the first text in the element - sometimes author is just plain text at the start
            if not post_data["author_name"]:
                all_text = post_element.get_text(separator=" ", strip=True)
                words = all_text.split()
                # Check if first word or first two words match page name
                if len(words) > 0 and page_name_from_url:
                    first_word = words[0].strip()
                    page_name_normalized = page_name_from_url.replace("-", "").replace("_", "")
                    first_word_normalized = first_word.lower().replace("-", "").replace("_", "").replace(" ", "")
                    
                    if page_name_normalized in first_word_normalized or first_word_normalized in page_name_normalized:
                        post_data["author_name"] = page_name_from_url.replace("-", " ").title()
                        print(f"[INFO] Found page name from first word: {first_word}", flush=True)
                    elif len(words) > 1:
                        first_two = " ".join(words[:2]).strip()
                        first_two_normalized = first_two.lower().replace("-", "").replace("_", "").replace(" ", "")
                        if page_name_normalized in first_two_normalized or first_two_normalized in page_name_normalized:
                            post_data["author_name"] = page_name_from_url.replace("-", " ").title()
                            print(f"[INFO] Found page name from first two words: {first_two}", flush=True)

            # Try to find post text - multiple strategies
            # Prioritize elements that are NOT comments
            text_selectors = [
                {"data-ad-preview": "message"},  # This is usually the main post text
                {"class": "x193iq5w xeuugli x13faqbe x1vvkbs x1xmvt09"},
                {"class": "x1y1aw1k x1n2onr6 xymvj8b"},
                {"class": "x1lliihq"},
                {"dir": "auto"},  # Many post texts have dir="auto"
            ]

            # First, try to find the main post text (not comments)
            for text_selector in text_selectors:
                text_elements = post_element.find_all("div", text_selector, limit=10)
                for text_element in text_elements:
                    # Skip if this element is inside a button or link
                    if text_element.find_parent("button") or (text_element.find_parent("a") and text_element.find_parent("a").get("href", "").startswith("#")):
                        continue
                    
                    # Check if this is inside a comment structure - skip comments
                    is_comment = False
                    parent = text_element.find_parent()
                    depth = 0
                    while parent and depth < 5:
                        parent_classes = parent.get("class", [])
                        parent_attrs = parent.attrs
                        
                        # Comments often have specific classes or attributes
                        if any("comment" in str(c).lower() for c in parent_classes):
                            is_comment = True
                            break
                        if "data-commentid" in parent_attrs or "comment" in str(parent_attrs).lower():
                            is_comment = True
                            break
                        # Check for reply buttons or comment actions
                        if parent.find("a", string=lambda x: x and ("reply" in str(x).lower() or "svar" in str(x).lower())):
                            is_comment = True
                            break
                        parent = parent.find_parent()
                        depth += 1
                    
                    if is_comment:
                        continue
                    
                    post_text = text_element.get_text(strip=True)
                    # Accept text if it's longer than 10 chars and doesn't look like UI
                    if post_text and len(post_text) > 10:
                        # Skip if it looks like a link, button text, or UI element
                        skip_indicators = [
                            "http", "www.", "See more", "See less", "Like", "Comment", 
                            "Share", "Follow", "React", "Send", "Write a comment",
                            "people reacted", "people commented", "shares", "hr ago",
                            "min ago", "Just now", "Yesterday", "Reply", "Svar"
                        ]
                        if not any(indicator.lower() in post_text.lower() for indicator in skip_indicators):
                            # Check if it's mostly text (not just numbers/symbols)
                            if len([c for c in post_text if c.isalpha() or c.isspace()]) > len(post_text) * 0.5:
                                # Try to extract actual post text by removing metadata
                                # Look for patterns like "Author Name Verified account · Time · Actual post text"
                                # Split by common separators and take the longest part (likely the actual post)
                                import re
                                # Split by common metadata separators
                                parts = re.split(r'\s*·\s*|\s*with\s+Public\s*|\s*Verified account\s*', post_text, flags=re.IGNORECASE)
                                # Find the part that looks most like actual post content
                                best_part = post_text
                                for part in parts:
                                    part = part.strip()
                                    # Prefer parts that:
                                    # - Are longer
                                    # - Contain sentence markers (., !, ?)
                                    # - Don't start with numbers/metadata
                                    if len(part) > len(best_part) * 0.7:
                                        has_sentences = any(marker in part for marker in [".", "!", "?"])
                                        starts_with_metadata = re.match(r'^\d+[km]?\s*(followers|following|hr|min)', part, re.IGNORECASE)
                                        if has_sentences or not starts_with_metadata:
                                            if len(part) > 30:  # Must be substantial
                                                best_part = part
                                
                                # Prefer longer text (more likely to be the main post)
                                if not post_data["post_text"] or len(best_part) > len(post_data["post_text"]):
                                    post_data["post_text"] = best_part
                if post_data["post_text"]:
                    break
            
            # If still no text, try getting all text from the post element and cleaning it
            # But exclude comments
            if not post_data["post_text"]:
                # Create a copy to avoid modifying the original
                from copy import deepcopy
                post_element_copy = deepcopy(post_element)
                
                # Remove comment sections first
                comment_elements = post_element_copy.find_all(["div", "span"], class_=lambda x: x and "comment" in str(x).lower())
                for comment_elem in comment_elements:
                    comment_elem.decompose()
                
                # Also remove elements with comment IDs
                comment_by_id = post_element_copy.find_all(attrs={"data-commentid": True})
                for comment_elem in comment_by_id:
                    comment_elem.decompose()
                
                # Get text but exclude common UI elements
                ui_elements = post_element_copy.find_all(["button", "a"], class_=lambda x: x and ("like" in str(x).lower() or "comment" in str(x).lower() or "share" in str(x).lower() or "reply" in str(x).lower() or "svar" in str(x).lower()))
                for ui_elem in ui_elements:
                    ui_elem.decompose()  # Remove UI elements
                
                all_text = post_element_copy.get_text(separator=" ", strip=True)
                # Remove author name if found
                if post_data["author_name"]:
                    all_text = all_text.replace(post_data["author_name"], "", 1).strip()
                # Clean up common Facebook UI text
                skip_patterns = [
                    "See more", "See less", "Like", "Comment", "Share", "Follow",
                    "React", "Send", "Write a comment", "Add a comment", "Reply", "Svar",
                    "people reacted", "people commented", "shares",
                    "hr", "min", "ago", "Just now", "Yesterday"
                ]
                for skip_text in skip_patterns:
                    all_text = all_text.replace(skip_text, "").strip()
                # Remove extra whitespace
                import re
                all_text = re.sub(r'\s+', ' ', all_text).strip()
                
                # Try to extract just the main post text (before first comment-like pattern)
                # Look for patterns that indicate comments start
                comment_start_patterns = [
                    r'\d+\s*(person|people|personer)\s*(har|has)\s*(kommenteret|commented)',
                    r'(Reply|Svar|Kommenter)',
                    r'^\w+\s+\w+\s*:',  # Name pattern like "John Doe:"
                ]
                for pattern in comment_start_patterns:
                    match = re.search(pattern, all_text, re.IGNORECASE)
                    if match:
                        all_text = all_text[:match.start()].strip()
                        break
                
                if len(all_text) > 10:
                    post_data["post_text"] = all_text[:500]  # Limit length

            # Try to find the actual post link (not just author link)
            # Look for links that contain /posts/, /permalink/, or /story.php
            # Also check time links - they often link to the post
            if not post_data["post_link"] or ("/posts/" not in post_data["post_link"] and "/permalink/" not in post_data["post_link"] and "/story.php" not in post_data["post_link"]):
                # First, try finding links with post patterns
                post_links = post_element.find_all("a", href=lambda x: x and ("/posts/" in str(x) or "/permalink/" in str(x) or "/story.php" in str(x)))
                for link in post_links[:10]:  # Check more links
                    href = link.get("href", "")
                    # Make sure it's a full URL or relative path to a post
                    if href and ("/posts/" in href or "/permalink/" in href or "/story.php" in href):
                        # Prepend facebook.com if it's a relative URL
                        if href.startswith("/"):
                            href = "https://www.facebook.com" + href
                        post_data["post_link"] = href
                        print(f"[INFO] Found post link: {href[:80]}...", flush=True)
                        break
                
                # If still no post link, try finding any link that might be a post link
                # Look for links with timestamps or "ago" text - these often link to posts
                if not post_data["post_link"]:
                    all_links = post_element.find_all("a", href=True)
                    for link in all_links[:15]:  # Check more links
                        href = link.get("href", "")
                        link_text = link.get_text(strip=True).lower()
                        
                        # Check if link text contains time indicators (likely a post link)
                        time_indicators = ["hr", "min", "ago", "d.", "m.", "y.", "kl.", "for", "timer", "minutter"]
                        if any(indicator in link_text for indicator in time_indicators) and href:
                            # Make sure it's not just a profile link
                            if "/user/" not in href and "/profile.php" not in href:
                                if href.startswith("/"):
                                    href = "https://www.facebook.com" + href
                                post_data["post_link"] = href
                                print(f"[INFO] Found post link via time link: {href[:80]}...", flush=True)
                                break

            # Try to find post time/date - multiple strategies
            time_selectors = [
                {"class": "x1i10hfl xjbqb8w x1ejq31n xd10rxx x1sy0etr x17r0tee x1ypdohk"},
                {"class": "x1p4m5qa"},
                {"aria-label": True},
            ]

            # Try link-based selectors first
            for time_selector in time_selectors:
                time_elements = post_element.find_all("a", time_selector, limit=5)
                for time_element in time_elements:
                    time_text = time_element.get_text(strip=True)
                    # Look for time patterns (hr, min, ago, dates, etc.)
                    if time_text and (any(indicator in time_text.lower() for indicator in ["hr", "min", "ago", "now", "yesterday", "day", "week", "month", "year", "d.", "m.", "y.", "kl.", "kl"]) or 
                                     any(char.isdigit() for char in time_text)):
                        # Skip if it's clearly not a time (too long, contains UI words)
                        if len(time_text) < 50 and not any(ui in time_text.lower() for ui in ["see", "more", "comment", "like", "share"]):
                            post_data["post_time"] = time_text
                            print(f"[INFO] Found post time: {time_text}", flush=True)
                            # Also check if this link is the post link
                            href = time_element.get("href", "")
                            if href and ("/posts/" in href or "/permalink/" in href or "/story.php" in href):
                                if href.startswith("/"):
                                    href = "https://www.facebook.com" + href
                                post_data["post_link"] = href
                                print(f"[INFO] Found post link from time element: {href[:80]}...", flush=True)
                            break
                if post_data["post_time"]:
                    break
            
            # If no time found via links, try span/div elements with aria-label or title
            if not post_data["post_time"]:
                # Look for elements with aria-label containing time info
                time_elements = post_element.find_all(["a", "span", "div"], {"aria-label": True}, limit=10)
                for time_element in time_elements:
                    aria_label = time_element.get("aria-label", "").lower()
                    if any(indicator in aria_label for indicator in ["hr", "min", "ago", "now", "yesterday", "day", "week", "month", "year", "time", "posted"]):
                        time_text = time_element.get_text(strip=True) or aria_label
                        if time_text and len(time_text) < 50:
                            post_data["post_time"] = time_text
                            print(f"[INFO] Found post time via aria-label: {time_text}", flush=True)
                            break
                
                # Try title attribute
                if not post_data["post_time"]:
                    time_elements = post_element.find_all(["a", "span"], {"title": True}, limit=10)
                    for time_element in time_elements:
                        title = time_element.get("title", "").lower()
                        if any(indicator in title for indicator in ["hr", "min", "ago", "now", "yesterday", "day", "week", "month", "year", "posted"]):
                            time_text = time_element.get_text(strip=True) or title
                            if time_text and len(time_text) < 50:
                                post_data["post_time"] = time_text
                                print(f"[INFO] Found post time via title: {time_text}", flush=True)
                                break
                
                # Try to find text that looks like a timestamp (contains numbers and time words)
                if not post_data["post_time"]:
                    all_text = post_element.get_text(separator=" ", strip=True)
                    # Look for patterns like "2 hr ago", "3 min ago", "Yesterday", dates, etc.
                    import re
                    time_patterns = [
                        r'\d+\s*(hr|hour|hours|min|minute|minutes|sec|second|seconds)\s*ago',
                        r'(yesterday|today|just now|for nylig|i dag|i går)',
                        r'\d{1,2}\s*(day|days|week|weeks|month|months|year|years|dag|dage|uge|uger|måned|måneder|år)\s*ago',
                        r'\d{1,2}[./]\d{1,2}[./]\d{2,4}',  # Date patterns like 11/11/2024
                        r'\d{1,2}\.\s*(januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december)',  # Danish dates
                    ]
                    for pattern in time_patterns:
                        matches = re.findall(pattern, all_text, re.IGNORECASE)
                        if matches:
                            # Find the context around the match
                            match_obj = re.search(pattern, all_text, re.IGNORECASE)
                            if match_obj:
                                start = max(0, match_obj.start() - 10)
                                end = min(len(all_text), match_obj.end() + 10)
                                time_text = all_text[start:end].strip()
                                # Clean up the extracted text
                                time_text = re.sub(r'^\W+|\W+$', '', time_text)  # Remove leading/trailing non-word chars
                                if len(time_text) < 50 and len(time_text) > 3:
                                    post_data["post_time"] = time_text
                                    print(f"[INFO] Found post time via pattern match: {time_text}", flush=True)
                                    break
                        if post_data["post_time"]:
                            break

            # Try to find video URL
            if not post_data["video_url"]:
                # Look for video elements
                video_elements = post_element.find_all("video", limit=3)
                for video_elem in video_elements:
                    video_src = video_elem.get("src") or video_elem.get("data-src")
                    if video_src:
                        post_data["video_url"] = video_src
                        print(f"[INFO] Found video URL: {video_src[:80]}...", flush=True)
                        break
                
                # Look for video links
                if not post_data["video_url"]:
                    video_links = post_element.find_all("a", href=lambda x: x and ("video" in str(x).lower() or "/videos/" in str(x)))
                    for link in video_links[:3]:
                        href = link.get("href", "")
                        if href and ("video" in href.lower() or "/videos/" in href):
                            # Make absolute URL if relative
                            if href.startswith("/"):
                                href = "https://www.facebook.com" + href
                            post_data["video_url"] = href
                            print(f"[INFO] Found video link: {href[:80]}...", flush=True)
                            break
                
                # Look for video thumbnail images
                if not post_data["video_thumbnail"]:
                    img_elements = post_element.find_all("img", limit=10)
                    for img_elem in img_elements:
                        img_src = img_elem.get("src") or img_elem.get("data-src")
                        # Check if it's a video thumbnail (often contains "video" or "scontent" in URL)
                        if img_src and ("video" in img_src.lower() or "scontent" in img_src.lower()):
                            # Skip very small images (likely icons)
                            width = img_elem.get("width")
                            height = img_elem.get("height")
                            if width and height:
                                try:
                                    if int(width) > 100 and int(height) > 100:
                                        post_data["video_thumbnail"] = img_src
                                        print(f"[INFO] Found video thumbnail: {img_src[:80]}...", flush=True)
                                        break
                                except (ValueError, TypeError):
                                    pass
                            elif len(img_src) > 50:  # If no dimensions, check URL length
                                post_data["video_thumbnail"] = img_src
                                print(f"[INFO] Found video thumbnail (no dimensions): {img_src[:80]}...", flush=True)
                                break

            # Add post if we found text (author is optional for page posts)
            # If no author found but this is a page/group post, use group/page name
            if not post_data["author_name"] and group_url:
                if "/groups/" in group_url:
                    # Extract group name from URL
                    group_name = group_url.split("/groups/")[-1].split("/")[0].split("?")[0]
                    post_data["author_name"] = group_name.replace("-", " ").title()
                elif "/pages/" in group_url:
                    page_name = group_url.split("/pages/")[-1].split("/")[0].split("?")[0]
                    post_data["author_name"] = page_name.replace("-", " ").title()
                elif "facebook.com/" in group_url and "/groups/" not in group_url:
                    # It's a page URL like facebook.com/socialdemokratiet
                    page_name = group_url.split("facebook.com/")[-1].split("/")[0].split("?")[0]
                    post_data["author_name"] = page_name.replace("-", " ").title()
            
            # Normalize author name for comparison
            author_normalized = post_data["author_name"].lower().strip() if post_data["author_name"] else ""
            
            # Check if this is a page/post from the target URL (not a comment from someone else)
            is_page_post = False
            if group_url:
                # Extract page/group name from URL
                page_name_from_url = None
                if "/groups/" in group_url:
                    page_name_from_url = group_url.split("/groups/")[-1].split("/")[0].split("?")[0].lower()
                elif "/pages/" in group_url:
                    page_name_from_url = group_url.split("/pages/")[-1].split("/")[0].split("?")[0].lower()
                elif "facebook.com/" in group_url:
                    page_name_from_url = group_url.split("facebook.com/")[-1].split("/")[0].split("?")[0].lower()
                
                # Check if author matches page name or if author is empty (likely a page post)
                if page_name_from_url:
                    # Normalize page name (remove dashes, etc.)
                    page_name_normalized = page_name_from_url.replace("-", "").replace("_", "")
                    author_normalized_clean = author_normalized.replace("-", "").replace("_", "").replace(" ", "")
                    
                    if page_name_normalized in author_normalized_clean or author_normalized_clean in page_name_normalized:
                        is_page_post = True
                    elif not post_data["author_name"] or len(post_data["author_name"]) < 3:
                        # No author or very short author - likely a page post
                        is_page_post = True
            
            # Only add if we have substantial text (not just UI or comments)
            # Be lenient with all page posts, not just specific parties
            # Also accept posts without author if they have good text (might be page posts)
            if post_data["post_text"] and len(post_data["post_text"]) > 15:
                # For page posts or posts without author (likely page posts), be more lenient
                if is_page_post or not post_data["author_name"]:
                    print(f"[INFO] Processing page post: author='{post_data['author_name'] or 'None'}', text_length={len(post_data['post_text'])}", flush=True)
                else:
                    # For non-page posts (likely comments), be more strict
                    if len(post_data["post_text"]) < 40:
                        print(f"[INFO] Skipping short non-page post (likely comment): {post_data['post_text'][:50]}...", flush=True)
                        continue
                # Filter out non-post content
                # But be smart - posts often contain metadata mixed with content
                # Only skip if the ENTIRE text is just metadata/UI
                text_lower = post_data["post_text"].lower()
                skip_patterns = [
                    "reels",
                    "officielle facebookside",
                    "page description",
                    "about this page",
                    "log in",
                    "forgot account",
                    "facebook log in",
                    "more posts",
                    "see all photos",
                    "page insights",
                    "data · privacy",
                    "terms · advertising",
                    "ad choices",
                    "cookies",
                    "information about page",
                    "socialdemokratiets officielle facebookside",
                ]
                
                # Check if text contains skip patterns, but only skip if:
                # 1. Text is very short (< 60 chars) AND contains skip pattern
                # 2. OR text is mostly just skip patterns (no actual content)
                should_skip = False
                if len(post_data["post_text"]) < 60:
                    # Short text - skip if it contains skip patterns
                    if any(pattern in text_lower for pattern in skip_patterns):
                        should_skip = True
                else:
                    # Longer text - only skip if it's ALL skip patterns (no real content)
                    # Check if text starts with skip patterns and has no sentence-like content
                    starts_with_skip = any(text_lower.startswith(pattern) for pattern in skip_patterns)
                    has_sentences = any(marker in post_data["post_text"] for marker in [".", "!", "?", "?"])
                    
                    # Skip only if it starts with skip pattern AND has no sentences
                    if starts_with_skip and not has_sentences:
                        should_skip = True
                
                if should_skip:
                    print(f"[INFO] Skipping non-post content: {post_data['post_text'][:50]}...", flush=True)
                    continue
                
                # Clean up metadata from post text (but keep the actual post content)
                # Remove common metadata prefixes like "Verified account", "Page ·", etc.
                cleaned_text = post_data["post_text"]
                metadata_prefixes = [
                    r"^[^·]*verified account[^·]*·",
                    r"^[^·]*page[^·]*·",
                    r"^[^·]*political party[^·]*·",
                    r"^\d+k\s*followers[^·]*·",
                    r"^\d+\s*following[^·]*·",
                ]
                for pattern in metadata_prefixes:
                    cleaned_text = re.sub(pattern, "", cleaned_text, flags=re.IGNORECASE).strip()
                
                # If cleaning removed significant content, use cleaned version
                if len(cleaned_text) > len(post_data["post_text"]) * 0.5 and len(cleaned_text) > 20:
                    post_data["post_text"] = cleaned_text
                    print(f"[INFO] Cleaned metadata from post text, new length: {len(cleaned_text)}", flush=True)
                
                # Skip intro/about page content - be more aggressive
                text_lower = post_data["post_text"].lower()
                intro_indicators = [
                    "intro page",
                    "page political party",
                    "see all photos",
                    "information about page",
                    "insights data privacy",
                    "advertising ad choices",
                    "cookies more",
                    "page · political party",
                    "intro " + page_name_from_url.lower() if page_name_from_url else "",
                    "velkommen til",  # "Welcome to" - common intro text
                    "et borgerligt parti",  # "A civic party" - from the example
                    "gør op med blokpolitiken",  # Common intro text
                ]
                # Check if text starts with or contains intro patterns
                if any(indicator in text_lower for indicator in intro_indicators if indicator):
                    # Skip if it's short or starts with intro patterns
                    if len(post_data["post_text"]) < 200 or text_lower.startswith("intro") or "page · political party" in text_lower or text_lower.startswith("velkommen"):
                        print(f"[INFO] Skipping intro/about page content: {post_data['post_text'][:80]}...", flush=True)
                        continue
                
                # Skip welcome/introductory messages
                if text_lower.startswith("velkommen") or "velkommen til" in text_lower:
                    if len(post_data["post_text"]) < 150:  # Short welcome messages
                        print(f"[INFO] Skipping welcome message: {post_data['post_text'][:80]}...", flush=True)
                        continue
                
                # Skip if it's mostly UI words (but be lenient for page posts)
                words = post_data["post_text"].lower().split()
                # Removed "followers", "following", "verified", "page", "about", "photos", "intro" from UI words
                # These can appear in legitimate posts as metadata
                ui_words = ["log", "in", "forgot", "account", "reels", "more", "see", "all"]
                ui_word_count = sum(1 for word in words if word in ui_words)
                if len(words) > 0:
                    ui_ratio = ui_word_count / len(words)
                    # For page posts, allow up to 50% UI words (they might have metadata)
                    # For others, stick to 40%
                    threshold = 0.5 if (is_page_post or not post_data["author_name"]) else 0.4
                    if ui_ratio > threshold:
                        print(f"[INFO] Skipping mostly UI words ({ui_ratio:.1%}): {post_data['post_text'][:50]}...", flush=True)
                        continue
                
                # Final check: filter out comments that slipped through
                if len(post_data["post_text"]) < 100:
                    # Check for comment indicators in the post text itself
                    comment_indicators = ["den største fejl", "helle castor", "nedeMette"]
                    if any(indicator in text_lower for indicator in comment_indicators):
                        print(f"[INFO] Skipping comment in final check: {post_data['post_text'][:50]}...", flush=True)
                        continue
                    
                    # Check if it starts with a name pattern (likely comment)
                    post_words = post_data["post_text"].split()
                    if len(post_words) >= 2:
                        # If first two words are both capitalized and short, might be a name (comment)
                        if (post_words[0][0].isupper() and len(post_words[0]) < 20 and 
                            post_words[1][0].isupper() and len(post_words[1]) < 20 and
                            len(post_data["post_text"]) < 80):
                            # Check if author is different from page name (likely a comment)
                            if post_data["author_name"] and page_name_from_url:
                                author_normalized = post_data["author_name"].lower().replace("-", "").replace("_", "").replace(" ", "")
                                page_normalized = page_name_from_url.replace("-", "").replace("_", "")
                                if page_normalized not in author_normalized and author_normalized not in page_normalized:
                                    print(f"[INFO] Skipping likely comment (name pattern + different author): {post_data['post_text'][:50]}...", flush=True)
                                    continue
                
                # Check for duplicates - use post link as primary identifier, then text + time
                # Facebook posts have unique links, so this is the most reliable way to identify duplicates
                seen_post_key = None
                
                # First, try to use post_link as the unique identifier
                if post_data["post_link"]:
                    # Extract post ID from link (e.g., /posts/123456789 or /permalink/123456789)
                    post_link = post_data["post_link"]
                    # Extract ID from various Facebook URL formats
                    post_id_match = None
                    if "/posts/" in post_link:
                        post_id_match = post_link.split("/posts/")[-1].split("/")[0].split("?")[0]
                    elif "/permalink/" in post_link:
                        post_id_match = post_link.split("/permalink/")[-1].split("/")[0].split("?")[0]
                    elif "/story.php" in post_link:
                        # Extract story ID from query params
                        import urllib.parse
                        parsed = urllib.parse.urlparse(post_link)
                        params = urllib.parse.parse_qs(parsed.query)
                        if "story_fbid" in params:
                            post_id_match = params["story_fbid"][0]
                    
                    if post_id_match:
                        seen_post_key = f"post_id:{post_id_match}"
                        print(f"[INFO] Using post ID for duplicate check: {post_id_match}", flush=True)
                
                # If no post link, fall back to text-based duplicate detection
                # For duplicate detection, ignore images/videos - same text = same post
                # But be smart: if text is very similar (>95% match), it's a duplicate
                # If text is different, it's a different post even if from same author
                if not seen_post_key and post_data["post_text"]:
                    # Create a unique key from first 50 words of text
                    # Normalize text to catch same posts with minor differences
                    import re
                    text_words = post_data["post_text"].split()[:50]
                    text_key = " ".join(text_words).lower().strip()
                    
                    # Normalize text key:
                    # - Remove extra spaces
                    text_key = re.sub(r'\s+', ' ', text_key).strip()
                    # - Remove all punctuation (posts can have different punctuation)
                    text_key = re.sub(r'[^\w\s]', '', text_key)
                    # - Remove extra spaces again after removing punctuation
                    text_key = re.sub(r'\s+', ' ', text_key).strip()
                    
                    # Check if this text is very similar to any existing post (>95% word match)
                    # Also check against already added posts directly
                    is_duplicate_by_text = False
                    
                    # Normalize and compare against all existing posts
                    for existing_post in posts:
                        existing_text = existing_post.get("post_text", "")
                        if not existing_text:
                            continue
                        
                        # Normalize existing text the same way
                        existing_words = existing_text.split()[:50]
                        existing_key = " ".join(existing_words).lower().strip()
                        existing_key = re.sub(r'\s+', ' ', existing_key).strip()
                        existing_key = re.sub(r'[^\w\s]', '', existing_key)
                        existing_key = re.sub(r'\s+', ' ', existing_key).strip()
                        
                        # Calculate similarity (word overlap)
                        text_words_set = set(text_key.split())
                        existing_words_set = set(existing_key.split())
                        
                        if len(text_words_set) > 0 and len(existing_words_set) > 0:
                            # Calculate Jaccard similarity (intersection over union)
                            intersection = len(text_words_set & existing_words_set)
                            union = len(text_words_set | existing_words_set)
                            similarity = intersection / union if union > 0 else 0
                            
                            # If >95% similar, it's a duplicate
                            if similarity > 0.95:
                                is_duplicate_by_text = True
                                print(f"[INFO] Skipping duplicate post (text similarity {similarity:.1%}): {post_data['post_text'][:50]}...", flush=True)
                                break
                    
                    # First check against seen keys
                    if seen_post_keys:
                        for existing_key in seen_post_keys:
                            if existing_key.startswith("text:"):
                                existing_text = existing_key.replace("text:", "").strip()
                                # Compare word sets
                                words1 = set(text_key.split())
                                words2 = set(existing_text.split())
                                if len(words1) > 0 and len(words2) > 0:
                                    # Calculate similarity using Jaccard similarity
                                    intersection = len(words1 & words2)
                                    union = len(words1 | words2)
                                    similarity = intersection / union if union > 0 else 0
                                    # If >98% similar, it's a duplicate (very strict - only exact duplicates)
                                    if similarity > 0.98:
                                        is_duplicate_by_text = True
                                        print(f"[INFO] Text is {similarity:.1%} similar to existing post (key match), marking as duplicate", flush=True)
                                        break
                    
                    # Also check against already added posts (double-check)
                    if not is_duplicate_by_text and posts:
                        for existing_post in posts:
                            if existing_post.get("post_text"):
                                existing_text_words = existing_post["post_text"].split()[:50]
                                existing_text_normalized = " ".join(existing_text_words).lower().strip()
                                existing_text_normalized = re.sub(r'\s+', ' ', existing_text_normalized).strip()
                                existing_text_normalized = re.sub(r'[^\w\s]', '', existing_text_normalized)
                                existing_text_normalized = re.sub(r'\s+', ' ', existing_text_normalized).strip()
                                
                                words1 = set(text_key.split())
                                words2 = set(existing_text_normalized.split())
                                if len(words1) > 0 and len(words2) > 0:
                                    intersection = len(words1 & words2)
                                    union = len(words1 | words2)
                                    similarity = intersection / union if union > 0 else 0
                                    # If >98% similar, it's a duplicate (very strict - only exact duplicates)
                                    if similarity > 0.98:
                                        is_duplicate_by_text = True
                                        print(f"[INFO] Text is {similarity:.1%} similar to existing post (direct check), marking as duplicate", flush=True)
                                        break
                    
                    if is_duplicate_by_text:
                        seen_post_key = "DUPLICATE_SKIP"
                    else:
                        # Use text as unique key
                        seen_post_key = f"text:{text_key[:200]}"
                        print(f"[INFO] Using text-only for duplicate check (no post link): {seen_post_key[:80]}...", flush=True)
                
                # Final check: if seen_post_key is DUPLICATE_SKIP, skip it
                if seen_post_key == "DUPLICATE_SKIP":
                    print(f"[INFO] Skipping duplicate post {idx + 1}: marked as duplicate by similarity check", flush=True)
                    continue
                
                # Check if key already exists
                if seen_post_key and seen_post_key in seen_post_keys:
                    print(f"[INFO] Skipping duplicate post {idx + 1}: already seen (key: {seen_post_key[:80]}...)", flush=True)
                    continue
                
                # Final double-check against posts list (in case similarity check missed something)
                is_duplicate_final = False
                if posts and post_data.get("post_text"):
                    text_normalized = re.sub(r'[^\w\s]', '', post_data["post_text"].lower().strip())
                    text_normalized = re.sub(r'\s+', ' ', text_normalized).strip()
                    text_words = set(text_normalized.split()[:50])
                    
                    for existing_post in posts:
                        if existing_post.get("post_text"):
                            existing_normalized = re.sub(r'[^\w\s]', '', existing_post["post_text"].lower().strip())
                            existing_normalized = re.sub(r'\s+', ' ', existing_normalized).strip()
                            existing_words = set(existing_normalized.split()[:50])
                            
                            if len(text_words) > 0 and len(existing_words) > 0:
                                intersection = len(text_words & existing_words)
                                union = len(text_words | existing_words)
                                similarity = intersection / union if union > 0 else 0
                                if similarity > 0.98:
                                    is_duplicate_final = True
                                    print(f"[INFO] Skipping duplicate post {idx + 1}: {similarity:.1%} similar to existing post", flush=True)
                                    break
                
                if is_duplicate_final:
                    continue
                
                if seen_post_key:
                    seen_post_keys.add(seen_post_key)
                
                posts.append(post_data)
                print(f"[INFO] Added post {len(posts)}: author='{post_data['author_name']}', text_length={len(post_data['post_text'])}", flush=True)
                print(f"[INFO]   Text preview: {post_data['post_text'][:80]}...", flush=True)
            else:
                print(f"[INFO] Skipped post {idx + 1}: no substantial text found (text_length={len(post_data.get('post_text', ''))})", flush=True)

        except Exception as e:
            print(f"[WARNING] Error processing post {idx + 1}: {e}", file=sys.stderr)
            continue

    return posts


def write_payload(
    items: list[dict[str, str]],
    group_url: str,
    output_path: Path,
) -> None:
    """Write scraped posts to a JSON file atomically."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": group_url,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "totalPosts": len(items),
        "posts": items,
    }

    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(output_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape posts from a public Facebook group and persist to JSON."
    )
    parser.add_argument(
        "--group-url",
        required=True,
        help="URL of the Facebook group to scrape (e.g., https://www.facebook.com/groups/groupname)",
    )
    parser.add_argument(
        "--email",
        help="Facebook email for login (optional for public groups)",
    )
    parser.add_argument(
        "--password",
        help="Facebook password for login (optional for public groups)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=DEFAULT_MAX_POSTS,
        help=f"Maximum number of posts to scrape (default: {DEFAULT_MAX_POSTS})",
    )
    parser.add_argument(
        "--scroll-pause",
        type=float,
        default=DEFAULT_SCROLL_PAUSE,
        help=f"Pause time between scrolls in seconds (default: {DEFAULT_SCROLL_PAUSE})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--output",
        help="Optional output file path. Defaults to data/facebook_group_<group_id>.json",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    group_url = args.group_url.strip()

    # Extract group ID or name for default output filename
    group_id = "unknown"
    if "/groups/" in group_url:
        group_id = group_url.split("/groups/")[-1].split("/")[0].split("?")[0]
    elif "profile.php?id=" in group_url:
        # Extract ID from profile.php?id=XXXXX
        match = re.search(r'profile\.php\?id=(\d+)', group_url)
        if match:
            group_id = match.group(1)
        else:
            group_id = group_url.rstrip("/").split("/")[-1]
    elif "/" in group_url:
        group_id = group_url.rstrip("/").split("/")[-1].split("?")[0]

    output_path = (
        Path(args.output) if args.output
        else DATA_DIR / f"facebook_group_{group_id}.json"
    )

    driver = None
    try:
        print("[INFO] Setting up Chrome driver...", flush=True)
        driver = setup_chrome_driver(headless=args.headless)
        print("[INFO] Chrome driver ready", flush=True)

        # Attempt login if credentials provided
        if args.email and args.password:
            print(f"[INFO] Attempting login with email: {args.email[:3]}***", flush=True)
            login_success = login_to_facebook(driver, args.email, args.password)
            print(f"[INFO] Login result: {login_success}", flush=True)
        else:
            print("[INFO] No credentials provided, accessing as guest", flush=True)

        # Scrape posts
        print(f"[INFO] Starting to scrape posts (max: {args.max_posts})", flush=True)
        posts = scrape_facebook_group_posts(
            driver,
            group_url,
            max_posts=args.max_posts,
            scroll_pause=args.scroll_pause,
        )
        print(f"[INFO] Scraped {len(posts)} posts", flush=True)

        # Write results (even if empty, so we know the script ran)
        write_payload(posts, group_url, output_path)
        
        if not posts:
            print("[WARNING] No posts were scraped. Facebook may have changed their HTML structure.", file=sys.stderr)
            print("[INFO] You may need to update the CSS selectors in the script.", file=sys.stderr)
            print(f"[INFO] Wrote empty result file to {output_path}", file=sys.stderr)
            return 0  # Return 0 so we can see the empty result
        
        print(f"[SUCCESS] Wrote {len(posts)} posts to {output_path}")

        return 0

    except KeyboardInterrupt:
        print("\n[INFO] Scraping interrupted by user", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[ERROR] Scraping failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if driver:
            driver.quit()
            print("[INFO] Browser closed")


if __name__ == "__main__":
    raise SystemExit(main())


