#!/usr/bin/env python3
"""Scrape posts from a public Facebook group and persist them as JSON."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Optional

try:
    from bs4 import BeautifulSoup
    from selenium import webdriver  # Fallback if selenium-wire not present
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.firefox import GeckoDriverManager
except ImportError as e:
    print(f"[ERROR] Missing required dependency: {e}", file=sys.stderr)
    print("[INFO] Install with: pip install beautifulsoup4 selenium webdriver-manager", file=sys.stderr)
    sys.exit(1)

# Try to enable Selenium Wire for network interception
WIRE_AVAILABLE = False
try:
    from seleniumwire import webdriver as wire_webdriver  # type: ignore
    WIRE_AVAILABLE = True
except Exception:
    wire_webdriver = None  # type: ignore
    WIRE_AVAILABLE = False

# Optional: requests for HTTP fallback (will gracefully degrade if missing)
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SCROLL_PAUSE = 2
DEFAULT_MAX_POSTS = 50
PARTY_POSTS_FILE = DATA_DIR / "facebook_party_posts.json"

# Best-effort: load FACEBOOK_EMAIL and FACEBOOK_PASSWORD from app/.env if not set
def _load_env_from_dotenv() -> None:
    try:
        app_dir = Path(__file__).resolve().parent.parent
        env_path = app_dir / ".env"
        if not env_path.exists():
            return
        text = env_path.read_text(encoding="utf-8")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key in ("FACEBOOK_EMAIL", "FACEBOOK_PASSWORD") and not os.environ.get(key):
                os.environ[key] = value
    except Exception:
        pass

_load_env_from_dotenv()

# Minimal mapping from Facebook page slug to Danish party letter code
PARTY_NAME_TO_CODE = {
    "socialdemokratiet": "A",
    "radikalevenstre": "B",
    "detkonservativefolkeparti": "C",
    "socialistiskfolkeparti": "F",
    "borgernesparti": "H",
    "liberalalliance": "I",
    "moderaterne": "M",
    "danskfolkeparti": "O",
    "venstre": "V",
    "danmarksdemokraterne": "Æ",
    "enhedslisten": "Ø",
    "alternativet": "Å",
}

def _get_page_slug(group_url: str) -> str:
    try:
        import re as _re
        from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
        parsed = _urlparse(group_url)
        path = (parsed.path or "").strip("/")
        if path.startswith("profile.php"):
            q = _parse_qs(parsed.query or "")
            if "id" in q and q["id"]:
                return q["id"][0]
            return "profile"
        if path.startswith("groups/"):
            parts = path.split("/", 1)
            return parts[1] if len(parts) > 1 else "group"
        if path:
            return path.split("/")[0]
    except Exception:
        pass
    return "page"

def _ascend_to_article(element):
    """Walk up a few levels to reach the containing article-like node."""
    node = element
    for _ in range(8):
        try:
            role = node.get_attribute("role") or ""
        except Exception:
            role = ""
        if role == "article":
            return node
        try:
            node = node.find_element(By.XPATH, "./..")
        except Exception:
            break
    return element

def _extract_text_from_container(container) -> str:
    try:
        raw = container.text or ""
    except Exception:
        raw = ""
    raw = re.sub(r"(Like|Comment|Share|See more|Vis mere)", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:1500]

def _extract_time_from_container(container) -> str:
    # abbr, time, or small
    for tag in ["abbr", "time", "small"]:
        try:
            els = container.find_elements(By.TAG_NAME, tag)
            if els:
                t = (els[0].text or "").strip()
                if t:
                    return t[:50]
        except Exception:
            continue
    return ""

def _extract_thumb_from_container(container) -> str:
    try:
        imgs = container.find_elements(By.TAG_NAME, "img")
        if imgs:
            src = imgs[0].get_attribute("src") or ""
            return src
    except Exception:
        pass
    return ""

def quick_scrape_first_post(driver, group_url: str, page_name_from_url: str | None) -> list[dict]:
    """
    Minimal, no-scroll attempt against m.facebook.com to fetch the very first post.
    Returns a list with at most one post dict on success, or empty list on failure.
    """
    try:
        slug = _get_page_slug(group_url)
        # Try desktop first (user sees a dismissible modal there), then mobile/mbasic
        candidates = [
            f"https://www.facebook.com/{slug}",
            f"https://www.facebook.com/{slug}/posts",
            f"https://www.facebook.com/{slug}?sk=posts",
            f"https://m.facebook.com/{slug}?v=timeline",
            f"https://m.facebook.com/{slug}/posts",
            f"https://m.facebook.com/{slug}",
            f"https://mbasic.facebook.com/{slug}?v=timeline",
            f"https://mbasic.facebook.com/{slug}",
        ]
        for url in candidates:
            try:
                driver.get(url)
            except Exception:
                continue
            # Try to close login/signup modal if it appears
            try:
                # Close buttons or Not now/Luk
                for by, sel in [
                    (By.CSS_SELECTOR, "[aria-label='Close'][role='button']"),
                    (By.CSS_SELECTOR, "div[role='dialog'] [aria-label='Close']"),
                    (By.XPATH, "//div[@role='dialog']//div[@aria-label='Close']"),
                    (By.XPATH, "//button[contains(., 'Not now') or contains(., 'Ikke nu') or contains(., 'Luk')]"),
                    (By.XPATH, "//span[text()='Close' or text()='Luk']/ancestor::div[@role='button']"),
                ]:
                    try:
                        el = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((by, sel)))
                        driver.execute_script("arguments[0].click();", el)
                        sleep(0.4)
                        break
                    except Exception:
                        continue
            except Exception:
                pass
            # Try to accept cookie banner on mobile/mbasic
            try:
                mobile_cookie_selectors = [
                    (By.XPATH, '//button[contains(.,"Allow essential and optional cookies")]'),
                    (By.XPATH, '//button[contains(.,"Allow All")]'),
                    (By.XPATH, '//button[contains(.,"Accept All")]'),
                    (By.XPATH, '//button[contains(.,"Accept")]'),
                    (By.XPATH, '//a[contains(.,"Allow All Cookies")]'),
                ]
                for by, selector in mobile_cookie_selectors:
                    try:
                        el = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, selector)))
                        el.click()
                        sleep(1)
                        break
                    except Exception:
                        continue
                # Also attempt to click common mbasic cookie links
                try:
                    el2 = driver.find_element(By.PARTIAL_LINK_TEXT, "Allow All")
                    driver.execute_script("arguments[0].click();", el2)
                    sleep(0.6)
                except Exception:
                    pass
            except Exception:
                pass
            # Wait for DOM settle briefly
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            sleep(2)
            # Find first story link
            try:
                anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='story.php'], a[href*='/posts/']")
            except Exception:
                anchors = []
            if not anchors:
                # Try finding any article
                try:
                    articles = driver.find_elements(By.CSS_SELECTOR, "article, div[role='article']")
                    anchors = articles[:1]
                except Exception:
                    anchors = []
            if not anchors:
                continue
            target = anchors[0]
            container = _ascend_to_article(target)
            text = _extract_text_from_container(container)
            if not text or len(text) < 10:
                # Try second candidate if available
                if len(anchors) > 1:
                    container = _ascend_to_article(anchors[1])
                    text = _extract_text_from_container(container)
            if not text or len(text) < 10:
                continue
            # Link and time
            try:
                post_link = target.get_attribute("href") or ""
            except Exception:
                post_link = ""
            post_time = _extract_time_from_container(container)
            thumb = _extract_thumb_from_container(container)
            author = page_name_from_url or slug
            post = {
                "post_id": "post_1",
                "author_name": author,
                "post_text": text,
                "post_time": post_time,
                "post_link": post_link,
                "video_url": "",
                "video_thumbnail": thumb,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            return [post]
    except Exception as e:
        print(f"[WARNING] quick_scrape_first_post failed: {e}", flush=True)
    return []

def _requests_session_from_driver(driver):
    try:
        import requests as _requests  # type: ignore
    except Exception:
        return None
    try:
        sess = _requests.Session()
        # Copy selenium cookies into requests session
        for c in driver.get_cookies():
            try:
                cookie_args = {k: c.get(k) for k in ["name", "value", "domain", "path"]}
                if not cookie_args.get("name"):
                    continue
                sess.cookies.set(cookie_args["name"], cookie_args["value"], domain=cookie_args.get("domain") or ".facebook.com", path=cookie_args.get("path") or "/")
            except Exception:
                continue
        # Headers for mbasic
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
        })
        return sess
    except Exception:
        return None

def http_mbasic_first_post_with_cookies(driver, group_url: str, page_name_from_url: str | None) -> list[dict]:
    if requests is None:
        return []
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return []
    try:
        slug = _get_page_slug(group_url)
        # Try without cookies first (guest), then with selenium cookies
        bases = [f"https://mbasic.facebook.com/{slug}?v=timeline", f"https://mbasic.facebook.com/{slug}", f"https://mbasic.facebook.com/{slug}/posts"]
        html = ""
        # Guest fetch
        try:
            import requests as _requests  # type: ignore
            guest = _requests.Session()
            guest.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
            })
            for url in bases:
                try:
                    r = guest.get(url, timeout=15)
                    if r.status_code == 200 and r.text and "login" not in r.url.lower():
                        html = r.text
                        break
                except Exception:
                    continue
        except Exception:
            pass
        # Cookie fetch if guest failed
        if not html:
            sess = _requests_session_from_driver(driver)
            if sess:
                for url in bases:
                    try:
                        r = sess.get(url, timeout=20)
                        if r.status_code == 200 and r.text:
                            html = r.text
                            break
                    except Exception:
                        continue
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        # pick first story link
        a = soup.select_one("a[href*='story.php'], a[href*='/posts/']")
        if not a:
            return []
        # climb up few parents
        container = a
        for _ in range(6):
            if container.parent:
                container = container.parent
        def clean(s): 
            import re as _re
            s = _re.sub(r"\\s+", " ", (s or "").strip())
            s = _re.sub(r"(Like|Comment|Share|See more|Vis mere)", "", s, flags=_re.I)
            return s.strip()
        text = clean(container.get_text(" ", strip=True))[:1500]
        if not text or len(text) < 10:
            return []
        time_str = ""
        ab = container.find("abbr")
        if ab and ab.get_text(strip=True):
            time_str = ab.get_text(strip=True)[:50]
        else:
            sm = container.find("small")
            if sm:
                time_str = sm.get_text(" ", strip=True)[:50]
        img = container.find("img")
        thumb = img.get("src") if img and img.get("src") else ""
        href = a.get("href") or ""
        link = href if href.startswith("http") else f"https://mbasic.facebook.com{href}"
        link = link.replace("https://mbasic.facebook.com", "https://www.facebook.com")
        author = page_name_from_url or slug
        post = {
            "post_id": "post_1",
            "author_name": author,
            "post_text": text,
            "post_time": time_str,
            "post_link": link,
            "video_url": "",
            "video_thumbnail": thumb,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        return [post]
    except Exception as e:
        print(f"[WARNING] http mbasic cookie fetch failed: {e}", flush=True)
        return []

def setup_firefox_driver(headless: bool = False) -> webdriver.Firefox:
    """Set up and return a Firefox WebDriver instance."""
    from pathlib import Path
    
    firefox_options = FirefoxOptions()
    if headless:
        firefox_options.add_argument("--headless")
    
    firefox_options.set_preference("general.useragent.override", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0")
    
    # Use system geckodriver if available
    geckodriver_path = None
    firefox_binary_path = None
    
    # Check for system geckodriver
    for path in ["/usr/bin/geckodriver", "/usr/local/bin/geckodriver"]:
        if Path(path).exists():
            geckodriver_path = path
            break
    
    # Check for system firefox binary
    for path in ["/usr/bin/firefox", "/usr/bin/firefox-esr", "/usr/local/bin/firefox"]:
        if Path(path).exists():
            firefox_binary_path = path
            firefox_options.binary_location = path
            break
    
    if geckodriver_path:
        service = FirefoxService(geckodriver_path)
    else:
        # Fallback to GeckoDriverManager
        service = FirefoxService(GeckoDriverManager().install())
    
    if WIRE_AVAILABLE and wire_webdriver is not None:
        seleniumwire_options = {
            "verify_ssl": True,
            "disable_encoding": True,  # Let Selenium Wire decode bodies
        }
        driver = wire_webdriver.Firefox(service=service, options=firefox_options, seleniumwire_options=seleniumwire_options)  # type: ignore
    else:
        driver = webdriver.Firefox(service=service, options=firefox_options)
    driver.maximize_window()
    return driver


def setup_chrome_driver(headless: bool = False) -> webdriver.Chrome:
    """Set up and return a Chrome WebDriver instance."""
    import os
    from pathlib import Path
    
    chrome_options = ChromeOptions()
    if headless:
        chrome_options.add_argument("--headless=new")  # Use new headless mode
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
        service = ChromeService(chromedriver_path)
    else:
        # Fallback to ChromeDriverManager
        service = ChromeService(ChromeDriverManager().install())
    
    if WIRE_AVAILABLE and wire_webdriver is not None:
        seleniumwire_options = {
            "verify_ssl": True,
            "disable_encoding": True,  # Let Selenium Wire decode bodies
        }
        driver = wire_webdriver.Chrome(service=service, options=chrome_options, seleniumwire_options=seleniumwire_options)  # type: ignore
    else:
        driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.maximize_window()
    return driver


def setup_driver(browser: str = "firefox", headless: bool = False):
    """Set up and return a WebDriver instance for the specified browser."""
    browser_lower = browser.lower()
    
    if browser_lower == "firefox":
        try:
            print(f"[INFO] Attempting to use Firefox...", flush=True)
            return setup_firefox_driver(headless=headless)
        except Exception as e:
            print(f"[WARNING] Failed to setup Firefox: {e}, falling back to Chrome", flush=True)
            return setup_chrome_driver(headless=headless)
    else:
        # Default to Chrome/Chromium
        return setup_chrome_driver(headless=headless)


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
        # Prefer mobile login which is usually simpler and more reliable in headless
        login_urls = [
            "https://m.facebook.com/login",
            "https://www.facebook.com/login",
        ]
        driver.get(login_urls[0])
        sleep(2)

        # Accept cookies if present
        try:
            # Try various common cookie consent buttons/selectors
            possible_cookie_selectors = [
                (By.XPATH, '//button[contains(.,"Allow essential and optional cookies")]'),
                (By.XPATH, '//button[contains(.,"Allow all cookies")]'),
                (By.XPATH, '//button[contains(.,"Accept")]'),
                (By.XPATH, '//button[contains(.,"Tillad")]'),
                (By.XPATH, '//*[@data-cookiebanner="accept_button"]'),
            ]
            for by, selector in possible_cookie_selectors:
                try:
                    el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
                    el.click()
                    sleep(1)
                    break
                except Exception:
                    continue
        except Exception:
            pass  # Cookies button might not be present

        # Fill in login form with human-like typing
        email_field = None
        try:
            email_field = WebDriverWait(driver, wait_timeout).until(
                EC.presence_of_element_located((By.NAME, "email"))
            )
        except Exception:
            # Fallback to typical mobile selector
            try:
                email_field = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.ID, "m_login_email"))
                )
            except Exception:
                pass
        if not email_field:
            # Retry with desktop login page
            driver.get(login_urls[1])
            sleep(2)
            email_field = WebDriverWait(driver, wait_timeout).until(
                EC.presence_of_element_located((By.NAME, "email"))
            )
        simulate_human_typing(email_field, email)

        # Try multiple possible password fields
        password_field = None
        for by, selector in [
            (By.NAME, "pass"),
            (By.ID, "m_login_password"),
        ]:
            try:
                password_field = driver.find_element(by, selector)
                break
            except Exception:
                continue
        if not password_field:
            password_field = driver.find_element(By.NAME, "pass")
        simulate_human_typing(password_field, password)

        sleep(random.uniform(0.5, 1.5))

        # Click login button with mouse movement simulation
        # Try multiple possible login buttons
        login_button = None
        for by, selector in [
            (By.XPATH, "//button[@type='submit']"),
            (By.NAME, "login"),
            (By.ID, "loginbutton"),
        ]:
            try:
                login_button = driver.find_element(by, selector)
                break
            except Exception:
                continue
        if not login_button:
            login_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver)\
            .move_to_element(login_button)\
            .pause(random.uniform(0.2, 0.4))\
            .click()\
            .perform()

        # Wait longer, then try to navigate to a lightweight page to verify login
        sleep(8)
        try:
            # Navigate to a lightweight endpoint that redirects if logged in
            driver.get("https://m.facebook.com/home.php")
            sleep(5)
        except Exception:
            pass

        # Check if login was successful (not on login page anymore)
        current_url = driver.current_url.lower()
        if "login" not in current_url and "checkpoint" not in current_url:
            # Also check for common post-login elements
            try:
                # Look for feed or home page indicators
                page_source = driver.page_source.lower()
                if any(indicator in page_source for indicator in ["feed", "home", "watch", "marketplace", "menu_bookmarks"]):
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


def author_matches_page(author_name: str | None, page_name_from_url: str | None) -> bool:
    """Return True if the post author matches the current page/group name."""
    if not author_name or not page_name_from_url:
        return False
    try:
        import re
        # Normalize: remove non-letters/digits and lower
        an = re.sub(r"\W+", "", str(author_name).lower())
        pn = re.sub(r"\W+", "", str(page_name_from_url).lower())
        if not pn:
            return False
        # Direct containment or equality
        if pn in an or an == pn:
            return True
        # Also check title-cased display name containment
        display = page_name_from_url.replace("-", " ").replace("_", " ").title()
        dn = re.sub(r"\W+", "", display.lower())
        return dn in an or an == dn
    except Exception:
        return False


def scrape_facebook_group_posts(
    driver: webdriver.Chrome,
    group_url: str,
    max_posts: int = DEFAULT_MAX_POSTS,
    scroll_pause: float = DEFAULT_SCROLL_PAUSE,
    search_query: str | None = None,
) -> list[dict[str, str]]:
    """Scrape posts from a Facebook group."""
    print(f"[INFO] Navigating directly to page: {group_url}", flush=True)
    driver.get(group_url)
    sleep(3)  # Wait for initial load
    
    # Wait for content to load
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except:
        pass
    
    print(f"[INFO] Page loaded, current URL: {driver.current_url}", flush=True)
    
    # STEP 1: Accept cookies FIRST (required before posts become visible) - FAST with timeout
    print("[INFO] Step 1: Accepting cookies...", flush=True)
    sleep(0.5)  # Minimal wait
    try:
        # Quick attempt to find and click cookie button
        cookie_accepted = False
        quick_selectors = [
            (By.XPATH, '//button[contains(.,"Tillad alle cookies")]'),
            (By.XPATH, '//button[contains(.,"Allow all cookies")]'),
            (By.XPATH, '//button[contains(.,"Tillad")]'),
            (By.XPATH, '//button[contains(.,"Accept")]'),
        ]
        for by, selector in quick_selectors:
            try:
                el = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((by, selector)))
                el.click()
                print("[INFO] Accepted cookies", flush=True)
                cookie_accepted = True
                sleep(0.5)
                break
            except:
                continue
        if not cookie_accepted:
            print("[INFO] No cookie button found (may already be accepted) - continuing", flush=True)
    except Exception as e:
        print(f"[INFO] Cookie step skipped: {e}", flush=True)
    
    # STEP 2: If login modal appears, log in with credentials from .env
    print("[INFO] Step 2: Checking for login modal...", flush=True)
    sleep(2)  # Wait for modal to appear
    
    # Check if login modal/dialog is present
    login_modal_present = False
    try:
        login_modal = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="dialog"], [data-testid*="dialog"], [aria-modal="true"]'))
        )
        login_modal_present = True
        print("[INFO] Login modal detected, attempting to log in...", flush=True)
    except:
        print("[INFO] No login modal detected, continuing...", flush=True)
    
    # If login modal is present, log in
    if login_modal_present:
        try:
            # Get credentials from environment
            email = os.getenv("FACEBOOK_EMAIL")
            password = os.getenv("FACEBOOK_PASSWORD")
            
            if email and password:
                print("[INFO] Found credentials in .env, logging in...", flush=True)
                
                # Find email/phone input
                try:
                    email_input = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="text"][name="email"], input[type="email"], input[placeholder*="email" i], input[placeholder*="phone" i], input[aria-label*="email" i], input[aria-label*="phone" i]'))
                    )
                    email_input.clear()
                    email_input.send_keys(email)
                    print("[INFO] Entered email", flush=True)
                    sleep(0.5)
                except Exception as e:
                    print(f"[WARNING] Could not find email input: {e}", flush=True)
                
                # Find password input
                try:
                    password_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"], input[name="pass"]')
                    password_input.clear()
                    password_input.send_keys(password)
                    print("[INFO] Entered password", flush=True)
                    sleep(0.5)
                except Exception as e:
                    print(f"[WARNING] Could not find password input: {e}", flush=True)
                
                # Find and click login button
                try:
                    # Try multiple selectors for login button
                    login_selectors = [
                        (By.CSS_SELECTOR, 'button[type="submit"]'),
                        (By.CSS_SELECTOR, 'button[name="login"]'),
                        (By.XPATH, '//button[contains(., "Log")]'),
                        (By.XPATH, '//button[contains(@aria-label, "Log")]'),
                        (By.XPATH, '//button[contains(text(), "Log")]'),
                    ]
                    login_btn = None
                    for by, selector in login_selectors:
                        try:
                            login_btn = driver.find_element(by, selector)
                            break
                        except:
                            continue
                    
                    if login_btn:
                        # Use JavaScript click to avoid interception
                        try:
                            driver.execute_script("arguments[0].click();", login_btn)
                            print("[INFO] Clicked login button (JavaScript)", flush=True)
                        except:
                            login_btn.click()
                            print("[INFO] Clicked login button (regular)", flush=True)
                        sleep(5)  # Wait for login to complete
                        print("[INFO] Login completed", flush=True)
                    else:
                        print("[WARNING] Could not find login button", flush=True)
                except Exception as e:
                    print(f"[WARNING] Could not click login button: {e}", flush=True)
            else:
                print("[WARNING] No credentials found in .env (FACEBOOK_EMAIL and FACEBOOK_PASSWORD)", flush=True)
        except Exception as e:
            print(f"[WARNING] Login attempt failed: {e}", flush=True)
    
    # Wait 5 seconds after login (or if no login was needed)
    print("[INFO] Waiting 5 seconds before scraping...", flush=True)
    sleep(5)
    
    # Ensure we're on the correct page (not login redirect)
    if "/login" in driver.current_url.lower():
        print("[INFO] Still on login page, navigating directly...", flush=True)
        driver.get(group_url)
        sleep(3)

    # Extract page/group name from URL first (needed for filtering and login page parsing)
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
    
    # STEP 3: Extract first post from page (after login and 5 second wait)
    try:
        print("[INFO] Step 3: Extracting first post...", flush=True)
        print(f"[DEBUG] Current URL: {driver.current_url}", flush=True)
        
        # Wait for posts container (x19h7ccj class) or articles to appear
        posts_container = None
        try:
            # First try to find the posts container with class x19h7ccj
            posts_container = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".x19h7ccj, div.x19h7ccj"))
            )
            print("[DEBUG] Found posts container with class x19h7ccj", flush=True)
        except:
            print("[DEBUG] Posts container x19h7ccj not found, trying articles directly...", flush=True)
        
        # Wait for articles to appear
        try:
            if posts_container:
                # Look for articles within the posts container
                WebDriverWait(posts_container, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "article, div[role='article']"))
                )
            else:
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "article, div[role='article']")))
            print("[DEBUG] Articles detected on page", flush=True)
        except:
            print("[DEBUG] No articles detected yet, continuing anyway", flush=True)
        sleep(2)  # Additional wait for content to stabilize
        
        # Look for article elements - prioritize within posts container
        if posts_container:
            articles = posts_container.find_elements(By.CSS_SELECTOR, "article")
            print(f"[DEBUG] Found {len(articles)} article elements in posts container", flush=True)
            if not articles:
                articles = posts_container.find_elements(By.XPATH, ".//div[@role='article']")
                print(f"[DEBUG] Found {len(articles)} div[@role='article'] elements in posts container", flush=True)
            # Wait a bit more for content to load within articles
            # Also do a small scroll to trigger lazy loading
            if articles:
                try:
                    driver.execute_script("window.scrollTo(0, 300);")
                    sleep(1)
                    driver.execute_script("window.scrollTo(0, 0);")
                    sleep(1)
                except:
                    pass
                sleep(2)
        else:
            # Fallback: search entire page
            articles = driver.find_elements(By.CSS_SELECTOR, "article")
            print(f"[DEBUG] Found {len(articles)} article elements", flush=True)
            if not articles:
                articles = driver.find_elements(By.XPATH, "//div[@role='article']")
                print(f"[DEBUG] Found {len(articles)} div[@role='article'] elements", flush=True)
            if not articles:
                articles = driver.find_elements(By.CSS_SELECTOR, "[data-pagelet*='FeedUnit']")
                print(f"[DEBUG] Found {len(articles)} FeedUnit elements", flush=True)
        
        print(f"[DEBUG] Checking {len(articles)} articles for posts with both text and video...", flush=True)
        
        # First pass: prioritize articles with video/reel links
        articles_with_video = []
        articles_without_video = []
        for article in articles[:10]:
            try:
                video_links = article.find_elements(By.XPATH, ".//a[contains(@href, '/reel/')] | .//a[contains(@href, '/video/')] | .//a[contains(@href, '/watch/')] | .//video")
                if video_links:
                    articles_with_video.append(article)
                else:
                    articles_without_video.append(article)
            except:
                articles_without_video.append(article)
        
        # Check video articles first, then non-video articles
        articles_to_check = articles_with_video + articles_without_video
        if not articles_to_check:
            articles_to_check = articles[:10]
        
        print(f"[DEBUG] Found {len(articles_with_video)} articles with video, {len(articles_without_video)} without video", flush=True)
        
        for idx, article in enumerate(articles_to_check[:10]):  # Check up to 10 articles to find one with both text and video
            print(f"[DEBUG] Checking article {idx+1}/{min(len(articles_to_check), 10)}...", flush=True)
            try:
                # FIRST: Extract reel/video URL and post link (needed for text extraction logic)
                time_elem = article.find_elements(By.XPATH, ".//a[contains(@href, '/reel/')] | .//a[contains(@href, '/posts/')] | .//abbr")
                post_time = ""
                post_link = ""
                reel_url_found = ""
                if time_elem:
                    try:
                        for elem in time_elem:
                            href = elem.get_attribute("href") or ""
                            text = elem.text.strip()[:50]
                            # Check if this is a reel link
                            if "/reel/" in href:
                                if not reel_url_found:
                                    reel_url_found = href
                                    if not reel_url_found.startswith("http"):
                                        reel_url_found = "https://www.facebook.com" + reel_url_found
                                    # Extract clean reel ID
                                    if "/reel/" in reel_url_found:
                                        reel_id = reel_url_found.split("/reel/")[-1].split("/")[0].split("?")[0]
                                        reel_url_found = f"https://www.facebook.com/reel/{reel_id}"
                                if not post_time and text:
                                    post_time = text
                            elif "/posts/" in href or not post_link:
                                if not post_link:
                                    post_link = href
                                    if post_link and not post_link.startswith("http"):
                                        post_link = "https://www.facebook.com" + post_link
                                if not post_time and text:
                                    post_time = text
                    except Exception as e:
                        print(f"[DEBUG] Error extracting time/link: {e}", flush=True)
                        pass
                
                # SECOND: Extract text - try multiple strategies to get the actual post content
                text = ""
                time_patterns = ["m", "h", "d", "min", "hour", "day", "minute", "minutter", "timer", "dage"]
                
                # Strategy 1: Try to get text from main post content containers first
                main_text_selectors = [
                    ".//div[contains(@class, 'userContent')]",
                    ".//div[contains(@data-testid, 'post_message')]",
                    ".//div[@data-ad-preview='message']",
                    ".//div[contains(@class, 'text')]",
                    ".//div[@role='article']//div[contains(@class, 'x1y1aw1k')]//span[@dir='auto']",
                    ".//div[contains(@class, 'x1y1aw1k')]//span[@dir='auto']",
                    ".//div[contains(@class, 'x19h7ccj')]//span[@dir='auto']",  # Within posts container
                    ".//div[contains(@data-pagelet, 'FeedUnit')]//span[@dir='auto']",
                    ".//p[@dir='auto']",
                    ".//div[@dir='auto' and string-length(text()) > 20]",  # Direct text content > 20 chars
                ]
                for selector in main_text_selectors:
                    try:
                        main_text_elems = article.find_elements(By.XPATH, selector)
                        for elem in main_text_elems[:3]:
                            content = elem.text.strip()
                            # Skip if it's just a time or UI element
                            if content and len(content) > 10 and not any(pattern in content.lower() for pattern in ["synes godt om", "kommenter", "alle reaktioner", "all reactions", "0:00"]):
                                # Check if it's not just a time pattern (like "1m", "2h", etc.)
                                if not (len(content) <= 5 and (any(p in content.lower() for p in time_patterns) or re.match(r'^\d+[mhd]$', content.lower()))):
                                    if len(content) > len(text):
                                        text = content[:1000]
                                    break
                        if text and len(text) > 20:
                            break
                    except:
                        continue
                
                # Strategy 1.5: Use JavaScript to extract all text nodes (in case Selenium text is empty)
                if not text or len(text) < 20:
                    try:
                        js_text = driver.execute_script("""
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
                                if (text && text.length > 20) {
                                    // Skip if it's just UI elements
                                    if (!text.match(/^(Synes godt om|Kommenter|Del|Like|Comment|Share|Alle reaktioner|All reactions)/i)) {
                                        textNodes.push(text);
                                    }
                                }
                            }
                            // Return the longest text node (likely the post content)
                            if (textNodes.length > 0) {
                                return textNodes.sort((a, b) => b.length - a.length)[0];
                            }
                            return '';
                        """, article)
                        if js_text and len(js_text) > len(text):
                            text = js_text[:1000]
                            print(f"[DEBUG] JavaScript extracted text: {text[:100]}...", flush=True)
                    except Exception as e:
                        print(f"[DEBUG] JavaScript text extraction failed: {e}", flush=True)
                
                # Strategy 2: If no good text found, collect from all text elements and filter
                if not text or len(text) < 20:
                    text_elems = article.find_elements(By.XPATH, ".//span[@dir='auto'] | .//div[@dir='auto'] | .//p | .//div[contains(@class, 'x1y1aw1k')]")
                    text_parts = []
                    seen_texts = set()
                    
                    for te in text_elems[:50]:
                        t = te.text.strip()
                        # Skip if it's just a time (like "1m", "2h", "18m", "3m")
                        # Check for patterns like "1m", "2h", "18m", "3m" etc.
                        if t and (len(t) <= 5 and (any(pattern in t.lower() for pattern in time_patterns) or re.match(r'^\d+[mhd]$', t.lower()))):
                            continue
                        # Skip single characters or very short text that looks like time
                        if t and len(t) <= 3:
                            continue
                        # Skip UI elements and time patterns
                        if t and len(t) > 5 and not t.startswith(("Synes godt om", "Kommenter", "Del", "Like", "Comment", "Share", "Alle reaktioner", "All reactions", "All reactions:", "0:00", "/", "·")):
                            # Deduplicate: skip if we've seen this exact text
                            t_normalized = t.lower().strip()
                            # Skip if it's just a number + time unit
                            if not re.match(r'^\d+[mhd]$', t_normalized) and t_normalized not in seen_texts and len(t_normalized) > 2:
                                # Prefer longer text chunks (at least 10 chars)
                                if len(t) > 10:
                                    text_parts.append(t)
                                    seen_texts.add(t_normalized)
                    
                    combined_text = " ".join(text_parts).strip()[:1000]
                    # Only use combined text if it's longer than what we have
                    if len(combined_text) > len(text) and len(combined_text) > 20:
                        text = combined_text
                        print(f"[DEBUG] Strategy 2 found text: {text[:100]}...", flush=True)
                
                # Strategy 3: Get all text from article and extract the longest meaningful chunk
                if not text or len(text) < 20:
                    try:
                        # Try JavaScript to get text content (in case article.text is empty due to shadow DOM)
                        try:
                            full_text_js = driver.execute_script("""
                                var article = arguments[0];
                                return article.innerText || article.textContent || '';
                            """, article)
                            if full_text_js and len(full_text_js) > len(article.text):
                                full_text = full_text_js
                            else:
                                full_text = article.text
                        except:
                            full_text = article.text
                        print(f"[DEBUG] Full article text length: {len(full_text)}, preview: {full_text[:300]}...", flush=True)
                        # Split by common separators and find the longest meaningful part
                        parts = full_text.split("\n")
                        for part in parts:
                            part = part.strip()
                            # Skip time patterns like "1m", "2h", "18m"
                            if part and re.match(r'^\d+[mhd]$', part.lower()):
                                continue
                            # Skip very short parts
                            if part and len(part) <= 10:
                                continue
                            # Skip UI elements but allow post content
                            skip_patterns = ["synes godt om", "kommenter", "alle reaktioner", "log in", "followers", "following", "verified account", "is with"]
                            if any(skip in part.lower() for skip in skip_patterns):
                                # But if it's long and contains actual content, keep it
                                if len(part) < 50:
                                    continue
                            # Look for actual post content - prefer parts with punctuation, emojis, or keywords
                            has_content_indicators = any(char in part for char in [".", ",", "!", "?", ":", "👇", "👆", "Mette", "familie", "Holbæk", "besøgte", "hverdagen", "travlhed", "udfordringer"])
                            if has_content_indicators or len(part) > 40:
                                if len(part) > len(text):
                                    text = part[:1000]
                                    print(f"[DEBUG] Found text candidate: {text[:100]}...", flush=True)
                    except Exception as e:
                        print(f"[DEBUG] Strategy 3 error: {e}", flush=True)
                        pass
                
                # Filter out author names (typically 2-3 words, capitalized, no punctuation)
                if text:
                    text_clean = text.strip()
                    # Check if text looks like just an author name (2-3 words, all capitalized or title case, no punctuation)
                    words = text_clean.split()
                    if len(words) <= 3 and all(word[0].isupper() if word else False for word in words) and not any(c in text_clean for c in [".", ",", "!", "?", ":", "👇", "👆"]):
                        print(f"[DEBUG] Text '{text_clean}' looks like author name, clearing...", flush=True)
                        text = ""
                
                # Debug output
                if text:
                    print(f"[DEBUG] Extracted text length: {len(text)}, preview: {text[:100]}...", flush=True)
                
                # Extract author name
                author_xpath = ".//h2//a | .//strong//a"
                if page_name_from_url:
                    author_xpath += f" | .//a[contains(@href, '{page_name_from_url}')]"
                author_elem = article.find_elements(By.XPATH, author_xpath)
                author = page_name_from_url.replace("-", " ").title() if page_name_from_url else "Unknown"
                if author_elem:
                    try:
                        author_text = author_elem[0].text.strip()
                        if author_text and len(author_text) > 2:
                            author = author_text
                    except:
                        pass
                
                # Extract image/video thumbnail - skip emoji/small images, look for actual media
                img_elem = article.find_elements(By.CSS_SELECTOR, "img")
                thumb = ""
                video_url = reel_url_found  # Start with reel URL if we found one
                for img in img_elem:
                    try:
                        src = img.get_attribute("src") or ""
                        # Skip emoji URLs, small icons, and profile pics
                        if src and "fbcdn.net" in src and len(src) > 100:
                            # Skip emoji and small icon URLs
                            if "emoji" not in src.lower() and "icon" not in src.lower() and "profile" not in src.lower():
                                # Prefer images that look like media (scontent, video thumbnails)
                                if "scontent" in src or "video" in src.lower() or "thumb" in src.lower():
                                    thumb = src
                                    break
                                elif not thumb:  # Fallback to any large image
                                    thumb = src
                    except:
                        pass
                
                # Check for video/reel link - try multiple strategies
                video_link = None
                # Strategy 1: Look for links with /reel/ in href
                video_links = article.find_elements(By.XPATH, ".//a[contains(@href, '/reel/')] | .//a[contains(@href, '/video/')] | .//a[contains(@href, '/watch/')]")
                print(f"[DEBUG] Found {len(video_links)} video links in article", flush=True)
                if video_links:
                    for link in video_links:
                        try:
                            href = link.get_attribute("href") or ""
                            print(f"[DEBUG] Video link href: {href[:100] if href else 'empty'}...", flush=True)
                            # Skip blob URLs, look for actual Facebook URLs
                            if href and not href.startswith("blob:") and ("/reel/" in href or "/video/" in href or "/watch/" in href):
                                if href.startswith("http"):
                                    video_url = href.split("?")[0]  # Remove query params for cleaner URL
                                elif href.startswith("/"):
                                    video_url = "https://www.facebook.com" + href.split("?")[0]
                                else:
                                    continue
                                # Extract reel ID if it's a reel URL
                                if "/reel/" in video_url:
                                    reel_id = video_url.split("/reel/")[-1].split("/")[0].split("?")[0]
                                    video_url = f"https://www.facebook.com/reel/{reel_id}"
                                video_link = link
                                break
                        except:
                            continue
                
                # Strategy 2: If we found a link but got blob URL, try to get URL from data attributes or parent
                if video_link and not video_url:
                    try:
                        # Check parent elements for data attributes
                        parent = video_link.find_element(By.XPATH, "./..")
                        data_url = parent.get_attribute("data-href") or parent.get_attribute("href") or ""
                        if data_url and "/reel/" in data_url:
                            if data_url.startswith("http"):
                                video_url = data_url.split("?")[0]
                            elif data_url.startswith("/"):
                                video_url = "https://www.facebook.com" + data_url.split("?")[0]
                    except:
                        pass
                
                # Strategy 3: Extract from post_link if it contains /reel/ (fallback)
                if not video_url and post_link and "/reel/" in post_link:
                    video_url = post_link.split("?")[0]
                    reel_id = video_url.split("/reel/")[-1].split("/")[0].split("?")[0]
                    video_url = f"https://www.facebook.com/reel/{reel_id}"
                
                # Strategy 4: If we have reel_url_found but video_url is still empty, use it
                if not video_url and reel_url_found:
                    video_url = reel_url_found
                
                # Quick check: if we have both text and video, return immediately
                if text and len(text) > 20 and video_url:
                    # Final validation: ensure text is not a time pattern
                    if not (re.match(r'^\d+[mhd]$', text.strip().lower()) or len(text.strip()) <= 5):
                        print(f"[DEBUG] Found both text and video in same article, returning immediately", flush=True)
                        # Extract author name quickly
                        author_xpath = ".//h2//a | .//strong//a"
                        if page_name_from_url:
                            author_xpath += f" | .//a[contains(@href, '{page_name_from_url}')]"
                        author_elem = article.find_elements(By.XPATH, author_xpath)
                        author = page_name_from_url.replace("-", " ").title() if page_name_from_url else "Unknown"
                        if author_elem:
                            try:
                                author_text = author_elem[0].text.strip()
                                if author_text and len(author_text) > 2:
                                    author = author_text
                            except:
                                pass
                        post = {
                            "post_id": "post_1",
                            "author_name": author,
                            "post_text": text,
                            "post_time": post_time,
                            "post_link": post_link or f"https://www.facebook.com/{page_name_from_url}",
                            "video_url": video_url,
                            "video_thumbnail": thumb,
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                        }
                        print(f"[SUCCESS] Extracted first post with BOTH text and video: author='{author}', text='{text[:80]}...', text_length={len(text)}, video={video_url[:50]}...", flush=True)
                        return [post]
                
                # Final check: if text looks like a time pattern, try to get better text from article
                if text and (re.match(r'^\d+[mhd]$', text.lower()) or len(text) <= 5):
                    print(f"[DEBUG] Text '{text}' looks like time, trying to get better text...", flush=True)
                    # Try one more time with article.text and find longest meaningful part
                    try:
                        all_text = article.text
                        lines = [l.strip() for l in all_text.split("\n") if l.strip()]
                        for line in lines:
                            line = line.strip()
                            # Skip time patterns and UI
                            if line and len(line) > 20 and not re.match(r'^\d+[mhd]$', line.lower()):
                                skip_ui = ["synes godt om", "kommenter", "alle reaktioner", "log in", "followers"]
                                if not any(skip in line.lower() for skip in skip_ui):
                                    # Check if it contains actual content (has punctuation, emojis, or is long)
                                    if any(c in line for c in [".", ",", "!", "?", ":", "👇", "👆", "Mette", "familie", "Holbæk"]) or len(line) > 40:
                                        text = line[:1000]
                                        print(f"[DEBUG] Found better text: {text[:80]}...", flush=True)
                                        break
                    except:
                        pass
                
                # Final check: ensure we have BOTH text AND video before creating post
                # Also ensure text is not a time pattern
                print(f"[DEBUG] Final check: text_length={len(text) if text else 0}, video_url={video_url[:50] if video_url else 'None'}...", flush=True)
                if not text or len(text) < 20:
                    print(f"[DEBUG] Final check: Text too short ({len(text) if text else 0} chars), skipping...", flush=True)
                    continue
                if text and (re.match(r'^\d+[mhd]$', text.strip().lower()) or len(text.strip()) <= 5):
                    print(f"[DEBUG] Final check: Text '{text}' is time pattern, skipping...", flush=True)
                    continue
                if not video_url:
                    print(f"[DEBUG] Final check: No video URL, skipping...", flush=True)
                    continue
                
                # Create post only if we have BOTH text and video
                post = {
                    "post_id": "post_1",
                    "author_name": author,
                    "post_text": text,
                    "post_time": post_time,
                    "post_link": post_link or f"https://www.facebook.com/{page_name_from_url}",
                    "video_url": video_url,
                    "video_thumbnail": thumb,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
                print(f"[SUCCESS] Extracted first post with BOTH text and video: author='{author}', text='{text[:80]}...', text_length={len(text)}, video={video_url[:50]}...", flush=True)
                return [post]
            except Exception as e:
                print(f"[DEBUG] Article extraction error: {e}", flush=True)
                continue
    except Exception as e:
        print(f"[WARNING] First post extraction failed: {e}", flush=True)
    
    # Limit scraping to a single solid post
    max_posts = 1
    print(f"[INFO] Extracted page name from URL: '{page_name_from_url}'", flush=True)
    
    # Fast path: if only first post needed, try mobile no-scroll quick scrape first
    try:
        if (max_posts or 0) <= 1:
            quick = quick_scrape_first_post(driver, group_url, page_name_from_url)
            if quick:
                print(f"[INFO] Quick path (selenium mobile) succeeded with 1 post", flush=True)
                return quick[:1]
            # Fallback: HTTP mbasic using Selenium cookies
            http_quick = http_mbasic_first_post_with_cookies(driver, group_url, page_name_from_url)
            if http_quick:
                print(f"[INFO] Quick path (http mbasic) succeeded with 1 post", flush=True)
                return http_quick[:1]
    except Exception as e:
        print(f"[WARNING] Quick path failed: {e}", flush=True)
    
    # If a search query is provided, navigate to the group's/page's internal search first
    if search_query:
        try:
            from urllib.parse import quote
            base_url = driver.current_url.split("?")[0].rstrip("/")
            candidate_search_urls = [
                f"{base_url}/search/?q={quote(search_query)}",
                f"https://www.facebook.com/search/posts/?q={quote(search_query)}",
                f"https://m.facebook.com/search/posts/?q={quote(search_query)}",
            ]
            for su in candidate_search_urls:
                try:
                    driver.get(su)
                    sleep(4)
                    print(f"[INFO] Navigated to search URL: {driver.current_url}", flush=True)
                    # If we see some results, keep this page
                    _articles = driver.find_elements(By.XPATH, "//div[@role='article']")
                    if _articles is not None:
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"[WARNING] Could not navigate to search page: {e}", flush=True)
    
    # Try to navigate explicitly to the Posts tab to ensure feed is visible
    try:
        posts_selectors = [
            (By.XPATH, "//a[contains(., 'Posts') and not(contains(., 'About'))]"),
            (By.XPATH, "//a[contains(., 'Opslag')]"),
            (By.XPATH, "//a[contains(@href, '/posts')]"),
            (By.XPATH, "//a[@role='tab' and (contains(., 'Posts') or contains(., 'Opslag'))]"),
        ]
        clicked_posts = False
        for by, selector in posts_selectors:
            try:
                el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                sleep(0.5)
                el.click()
                clicked_posts = True
                print("[INFO] Clicked Posts tab", flush=True)
                break
            except Exception:
                continue
        if not clicked_posts and page_name_from_url:
            # Try direct navigation to posts route
            candidate_urls = [
                f"https://www.facebook.com/{page_name_from_url}/posts",
                f"https://m.facebook.com/{page_name_from_url}/posts",
                f"https://www.facebook.com/{page_name_from_url}",
            ]
            for url in candidate_urls:
                try:
                    driver.get(url)
                    sleep(3)
                    if "/posts" in driver.current_url or page_name_from_url in driver.current_url:
                        print(f"[INFO] Navigated directly to: {driver.current_url}", flush=True)
                        break
                except Exception:
                    continue
        # Wait a bit for the feed to stabilize
        sleep(3)
    except Exception:
        pass
    
    # Scroll to load more posts - use more aggressive scrolling strategy
    # Facebook uses lazy loading, so we need to scroll slowly and wait for content
    last_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
    scrolls = 0
    max_scrolls = 100
    # If we only need the first post, skip scrolling entirely
    try:
        if (max_posts or 0) <= 1 or os.environ.get("FACEBOOK_NO_SCROLL") == "1":
            max_scrolls = 0
            print("[INFO] Skipping scroll (first-post mode)", flush=True)
    except Exception:
        pass
    consecutive_no_change = 0
    last_post_count = 0

    print(f"[INFO] Starting to scroll (initial height: {last_height}, max scrolls: {max_scrolls})", flush=True)
    
    # First, wait a bit for initial content to load
    sleep(3)
    
    # Try to capture a reference to the feed container if present
    feed_container = None
    try:
        for by, selector in [
            (By.XPATH, "//*[@role='feed']"),
            (By.XPATH, "//*[@aria-label='Timeline']"),
            (By.XPATH, "//*[@data-pagelet='ProfileTimeline']"),
        ]:
            try:
                feed_container = driver.find_element(by, selector)
                print("[INFO] Found feed container", flush=True)
                break
            except Exception:
                continue
    except Exception:
        pass
    
    while scrolls < max_scrolls:
        # Strategy 1: Get current scroll position using multiple methods
        current_scroll = driver.execute_script("""
            return Math.max(
                window.pageYOffset || 0,
                window.scrollY || 0,
                document.documentElement.scrollTop || 0,
                document.body.scrollTop || 0,
                document.documentElement.scrollTop || 0
            );
        """)
        
        viewport_height = driver.execute_script("return window.innerHeight || document.documentElement.clientHeight || 800;")
        scroll_amount = viewport_height * 0.8  # Scroll 80% of viewport
        
        # Strategy 2: Try multiple scrolling methods to ensure it works
        # Method 1: window.scrollBy
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        sleep(0.5)
        
        # Method 2: window.scrollTo with calculated position
        target_scroll = current_scroll + scroll_amount
        driver.execute_script(f"window.scrollTo(0, {target_scroll});")
        sleep(0.5)
        
        # Method 3: document.documentElement.scrollTop
        driver.execute_script(f"document.documentElement.scrollTop = {target_scroll};")
        sleep(0.5)
        
        # Method 4: document.body.scrollTop
        driver.execute_script(f"document.body.scrollTop = {target_scroll};")
        sleep(0.5)
        
        # Method 5: Try scrolling the main content container (Facebook often uses a specific div)
        driver.execute_script("""
            // Try to find and scroll the main content container
            var containers = document.querySelectorAll('[role="main"], [data-pagelet="FeedUnit"], .x1n2onr6');
            for (var i = 0; i < containers.length; i++) {
                if (containers[i].scrollHeight > containers[i].clientHeight) {
                    containers[i].scrollTop += arguments[0];
                }
            }
        """, scroll_amount)
        sleep(0.5)
        
        # Method 5b: Explicitly scroll the feed container if we captured it
        if feed_container:
            try:
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + arguments[1];", feed_container, scroll_amount)
                sleep(0.5)
            except Exception:
                pass
        
        # Method 6: Use keyboard Page Down (simulates real user)
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            # Try multiple keyboard methods
            body.send_keys(Keys.PAGE_DOWN)
            sleep(0.3)
            body.send_keys(Keys.PAGE_DOWN)
            sleep(0.3)
            # Try sending multiple arrow downs
            for _ in range(10):
                body.send_keys(Keys.ARROW_DOWN)
            sleep(0.3)
        except:
            pass
        
        # Method 7: Use ActionChains to scroll (more reliable)
        try:
            actions = ActionChains(driver)
            # Scroll using mouse wheel simulation
            actions.move_by_offset(0, 0).perform()  # Move to center
            actions.scroll_by_amount(0, scroll_amount).perform()
            sleep(0.5)
            # Scroll down using mouse wheel simulation
            actions.send_keys(Keys.PAGE_DOWN).perform()
            sleep(0.3)
            actions.send_keys(Keys.PAGE_DOWN).perform()
            sleep(0.3)
        except:
            pass
        
        # Method 8: Try to find and scroll the actual scrollable container
        try:
            scrollable_containers = driver.execute_script("""
                var containers = [];
                var allElements = document.querySelectorAll('*');
                for (var i = 0; i < allElements.length; i++) {
                    var el = allElements[i];
                    var style = window.getComputedStyle(el);
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll' || 
                        style.overflow === 'auto' || style.overflow === 'scroll') {
                        if (el.scrollHeight > el.clientHeight) {
                            containers.push(el);
                        }
                    }
                }
                return containers;
            """)
            if scrollable_containers:
                # Scroll the first scrollable container
                driver.execute_script("arguments[0].scrollTop += arguments[1];", scrollable_containers[0], scroll_amount)
                sleep(0.5)
        except:
            pass
        
        # Wait for content to load - Facebook needs time to lazy load
        sleep(2.5)  # Increased wait time for lazy loading
        
        # Also wait for any loading indicators to disappear
        try:
            WebDriverWait(driver, 3).until_not(
                EC.presence_of_element_located((By.XPATH, "//*[contains(@class, 'loading') or contains(@aria-label, 'Loading')]"))
            )
        except:
            pass  # No loading indicator or timeout is fine
        
        # Strategy 3: Scroll to bottom every few scrolls
        if scrolls % 3 == 0:  # Every 3rd scroll
            # Get max scroll height
            max_scroll = driver.execute_script("""
                return Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight,
                    document.body.offsetHeight,
                    document.documentElement.offsetHeight
                );
            """)
            # Scroll to bottom using multiple methods
            driver.execute_script(f"window.scrollTo(0, {max_scroll});")
            driver.execute_script(f"document.documentElement.scrollTop = {max_scroll};")
            driver.execute_script(f"document.body.scrollTop = {max_scroll};")
            sleep(2.5)  # Wait longer for bottom content
            # Also try to scroll feed container to bottom
            if feed_container:
                try:
                    driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", feed_container)
                    sleep(1.5)
                except Exception:
                    pass
        
        # Strategy 4: Try scrolling to trigger intersection observer
        # Facebook uses intersection observer for lazy loading
        driver.execute_script("""
            // Trigger scroll event manually
            window.dispatchEvent(new Event('scroll'));
            // Also try to trigger resize
            window.dispatchEvent(new Event('resize'));
        """)
        sleep(1)
        
        # Strategy 5: Try clicking/activating elements that might trigger loading
        if scrolls % 5 == 0 and scrolls > 0:
            try:
                # Look for "See more" or "Load more" buttons
                see_more_buttons = driver.find_elements(By.XPATH, "//span[contains(text(), 'See more') or contains(text(), 'Se mere')]")
                for btn in see_more_buttons[:3]:  # Try first 3 buttons
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", btn)
                        sleep(1)
                        btn.click()
                        sleep(2)
                        print(f"[INFO] Clicked 'See more' button", flush=True)
                    except:
                        pass
            except:
                pass
        
        # Check if page height changed (try multiple methods)
        new_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, document.body.offsetHeight, document.documentElement.offsetHeight);")
        current_scroll_pos = driver.execute_script("""
            return Math.max(
                window.pageYOffset || 0,
                window.scrollY || 0,
                document.documentElement.scrollTop || 0,
                document.body.scrollTop || 0
            );
        """)
        
        # Debug: Print scroll position to verify scrolling is working
        if scrolls % 5 == 0:
            print(f"[DEBUG] Scroll {scrolls}: current_scroll={current_scroll_pos}, target_scroll={target_scroll}, height={new_height}", flush=True)
        
        # Also check if we can see more posts by counting article elements
        # This is more reliable than checking scroll position in headless mode
        try:
            current_articles = driver.find_elements(By.XPATH, "//div[@role='article']")
            current_post_count = len(current_articles)
            if current_post_count > last_post_count:
                print(f"[INFO] Found more posts! Count increased from {last_post_count} to {current_post_count}", flush=True)
                last_post_count = current_post_count
                consecutive_no_change = 0  # Reset counter if we found more posts
                # If we found more posts, scrolling is working even if position doesn't change
                print(f"[INFO] Scrolling is working - found {current_post_count} posts so far", flush=True)
        except:
            pass
        
        # Also check if we have enough posts already
        if last_post_count >= max_posts * 2:  # Get extra posts for filtering
            print(f"[INFO] Found {last_post_count} posts, which should be enough. Stopping scroll.", flush=True)
            break
        
        # Check if we got more posts (more reliable than height check in headless mode)
        if current_post_count == last_post_count and current_post_count > 0:
            consecutive_no_change += 1
            
            # Try more aggressive scrolling if no new posts
            if consecutive_no_change % 3 == 0:
                # Scroll way down and back up to trigger loading
                max_scroll = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);")
                driver.execute_script(f"window.scrollTo(0, {max_scroll});")
                sleep(2)
                driver.execute_script("window.scrollTo(0, 0);")
                sleep(1)
                driver.execute_script(f"window.scrollTo(0, {max_scroll});")
                sleep(2)
                # Also try keyboard scrolling
                try:
                    body = driver.find_element(By.TAG_NAME, "body")
                    for _ in range(5):
                        body.send_keys(Keys.END)
                        sleep(0.5)
                    for _ in range(3):
                        body.send_keys(Keys.PAGE_DOWN)
                        sleep(0.5)
                except:
                    pass
                # Re-check post count
                try:
                    current_articles = driver.find_elements(By.XPATH, "//div[@role='article']")
                    current_post_count = len(current_articles)
                    if current_post_count > last_post_count:
                        print(f"[INFO] Aggressive scroll worked! Found {current_post_count} posts (was {last_post_count})", flush=True)
                        last_post_count = current_post_count
                        consecutive_no_change = 0
                except:
                    pass
            
            # Only break if we've had no new posts for many consecutive scrolls
            if consecutive_no_change >= 20:  # Increased patience
                print(f"[INFO] No more posts loading after {consecutive_no_change} consecutive scrolls (found {last_post_count} posts total)", flush=True)
                # Try one last aggressive scroll
                max_scroll = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);")
                driver.execute_script(f"window.scrollTo(0, {max_scroll * 2});")
                sleep(3)
                # Final check
                try:
                    current_articles = driver.find_elements(By.XPATH, "//div[@role='article']")
                    final_count = len(current_articles)
                    if final_count > last_post_count:
                        print(f"[INFO] Final scroll found {final_count} posts!", flush=True)
                        last_post_count = final_count
                    else:
                        break
                except:
                    break
        
        # Check if height changed or we found more posts
        if new_height == last_height and current_post_count == last_post_count:
            consecutive_no_change += 1
        else:
            consecutive_no_change = 0  # Reset counter if we got new content
            if new_height != last_height:
                print(f"[INFO] Content loaded! Height changed from {last_height} to {new_height}", flush=True)
            
        last_height = new_height
        scrolls += 1
        
        # Print progress every 5 scrolls
        if scrolls % 5 == 0:
            print(f"[INFO] Scroll {scrolls}/{max_scrolls}, height: {new_height}, scroll pos: {current_scroll_pos}, consecutive no-change: {consecutive_no_change}", flush=True)

    print(f"[INFO] Finished scrolling: {scrolls} scrolls completed, final height: {last_height}", flush=True)
    
    # Final aggressive scrolls to ensure all content is loaded
    print(f"[INFO] Doing final scrolls to ensure all posts are loaded...", flush=True)
    for final_scroll in range(10):  # Increased from 5 to 10
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        sleep(2)
        # Try scrolling back up a bit and down again
        driver.execute_script("window.scrollBy(0, -500);")
        sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        sleep(2)
        # Trigger events
        driver.execute_script("window.dispatchEvent(new Event('scroll')); window.dispatchEvent(new Event('resize'));")
        sleep(1)
    
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
    
    # Prefer the very first feed post using aria-posinset='1'
    try:
        first_article = None
        try:
            candidates = driver.find_elements(By.XPATH, "//div[@role='article' and @aria-posinset='1']")
            if candidates:
                first_article = candidates[0]
        except Exception:
            first_article = None
        if not first_article:
            try:
                pos_elem = driver.find_element(By.XPATH, "//*[@aria-posinset='1']")
                try:
                    first_article = pos_elem.find_element(By.XPATH, "./ancestor::div[@role='article'][1]")
                except Exception:
                    first_article = pos_elem
            except Exception:
                first_article = None
        if first_article:
            outer_html = first_article.get_attribute("outerHTML")
            if outer_html:
                temp_soup = BeautifulSoup(outer_html, "html.parser")
                node = temp_soup.find()
                if node:
                    all_post_elements.append(node)
                    found_ids.add(id(node))
                    print("[INFO] Seeded first candidate from aria-posinset=1 (top feed post)", flush=True)
    except Exception as e:
        print(f"[DEBUG] Could not seed first post via aria-posinset=1: {e}", flush=True)
    
    # Author-targeted collection: explicitly find posts by the page name
    try:
        if page_name_from_url:
            # Build common display variants of the page name
            name_variants = {
                page_name_from_url,
                page_name_from_url.replace("-", " ").replace("_", " ").title(),
                page_name_from_url.replace("-", " ").replace("_", " "),
            }
            # Selenium XPaths targeting role=article that contain the author name in header/link
            xpaths = []
            for nv in name_variants:
                if not nv:
                    continue
                # Header span or link containing the name
                xpaths.extend([
                    f"//div[@role='article'][.//h2//*[contains(normalize-space(.), '{nv}')]]",
                    f"//div[@role='article'][.//a[contains(normalize-space(.), '{nv}')]]",
                    f"//div[contains(@class,'x1n2onr6')][.//a[contains(normalize-space(.), '{nv}')]]",
                ])
            for xp in xpaths[:10]:  # limit attempts
                try:
                    elems = driver.find_elements(By.XPATH, xp)
                except Exception:
                    elems = []
                if not elems:
                    continue
                for el in elems:
                    try:
                        outer_html = el.get_attribute("outerHTML")
                        if not outer_html:
                            continue
                        temp_soup = BeautifulSoup(outer_html, "html.parser")
                        root = temp_soup.find()
                        if not root:
                            continue
                        elem_id = id(root)
                        if elem_id in found_ids:
                            continue
                        found_ids.add(elem_id)
                        all_post_elements.append(root)
                    except Exception:
                        continue
            if all_post_elements:
                print(f"[INFO] Author-targeted pass found {len(all_post_elements)} elements for '{page_name_from_url}'", flush=True)
    except Exception as e:
        print(f"[DEBUG] Author-targeted collection failed: {e}", flush=True)
    
    # First, try the specific CSS selector provided by user for better accuracy
    # This targets posts more precisely: [aria-posinset='1'] .x1jx94hy > div > div > div > div
    try:
        # Use Selenium to find elements with this selector (more reliable than BeautifulSoup for complex selectors)
        # First try with specific aria-posinset='1', then try any aria-posinset
        specific_selectors = [
            "[aria-posinset='1'] .x1jx94hy > div > div > div > div",
            "[aria-posinset] .x1jx94hy > div > div > div > div"
        ]
        
        for css_selector in specific_selectors:
            try:
                specific_post_elements = driver.find_elements(By.CSS_SELECTOR, css_selector)
                if specific_post_elements:
                    print(f"[INFO] Found {len(specific_post_elements)} elements using specific CSS selector: {css_selector}", flush=True)
                    # For each found element, find its parent post container
                    for sel_elem in specific_post_elements:
                        try:
                            # Find the parent article or post container
                            # Try multiple strategies to find the actual post container
                            parent = None
                            
                            # Strategy 1: Find ancestor with role="article"
                            try:
                                parent = sel_elem.find_element(By.XPATH, "./ancestor::div[@role='article'][1]")
                            except:
                                pass
                            
                            # Strategy 2: Find ancestor with class x1n2onr6 (common post container class)
                            if not parent:
                                try:
                                    parent = sel_elem.find_element(By.XPATH, "./ancestor::div[contains(@class, 'x1n2onr6')][1]")
                                except:
                                    pass
                            
                            # Strategy 3: Find ancestor with aria-posinset attribute
                            if not parent:
                                try:
                                    parent = sel_elem.find_element(By.XPATH, "./ancestor::div[@aria-posinset][1]")
                                except:
                                    pass
                            
                            # Strategy 4: Use the element itself if no parent found
                            if not parent:
                                parent = sel_elem
                            
                            # Get the outer HTML and parse it
                            outer_html = parent.get_attribute("outerHTML")
                            if outer_html:
                                temp_soup = BeautifulSoup(outer_html, "html.parser")
                                if temp_soup.find():
                                    elem = temp_soup.find()
                                    elem_id = id(elem)
                                    if elem_id not in found_ids:
                                        found_ids.add(elem_id)
                                        all_post_elements.append(elem)
                        except Exception as e:
                            print(f"[DEBUG] Error processing specific selector element: {e}", flush=True)
                            continue
                    
                    # If we found elements with this selector, break (don't try next selector)
                    if specific_post_elements:
                        break
            except Exception as e:
                print(f"[DEBUG] Could not use CSS selector {css_selector}: {e}", flush=True)
                continue
    except Exception as e:
        print(f"[DEBUG] Could not use specific CSS selector: {e}", flush=True)
    
    # Also try finding by aria-posinset attribute directly in BeautifulSoup
    try:
        posinset_elements = soup.find_all("div", attrs={"aria-posinset": True})
        for elem in posinset_elements:
            # Find nested .x1jx94hy elements
            nested = elem.select(".x1jx94hy > div > div > div > div")
            for nested_elem in nested:
                elem_id = id(nested_elem)
                if elem_id not in found_ids:
                    found_ids.add(elem_id)
                    all_post_elements.append(nested_elem)
        if posinset_elements:
            print(f"[INFO] Found {len(posinset_elements)} elements with aria-posinset attribute", flush=True)
    except Exception as e:
        print(f"[DEBUG] Could not find aria-posinset elements: {e}", flush=True)
    
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
                # Search any tag, not only divs; FB often uses spans for message
                text_elements = post_element.find_all(True, text_selector, limit=10)
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
            # Look for links that contain /posts/, /permalink/, /story.php, /videos/, /watch/, or photo links
            # Also check time links - they often link to the post
            if not post_data["post_link"] or ("/posts/" not in post_data["post_link"] and "/permalink/" not in post_data["post_link"] and "/story.php" not in post_data["post_link"]):
                # First, try finding links with post patterns
                post_links = post_element.find_all("a", href=lambda x: x and ("/posts/" in str(x) or "/permalink/" in str(x) or "/story.php" in str(x) or "/videos/" in str(x) or "/watch/" in str(x) or "photo.php" in str(x)))
                for link in post_links[:10]:  # Check more links
                    href = link.get("href", "")
                    # Make sure it's a full URL or relative path to a post
                    if href and ("/posts/" in href or "/permalink/" in href or "/story.php" in href or "/videos/" in href or "/watch/" in href or "photo.php" in href):
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
                            allowed = ("/posts/" in href or "/permalink/" in href or "/story.php" in href or "/videos/" in href or "/watch/" in href or "photo.php" in href)
                            if "/user/" not in href and "/profile.php" not in href and allowed:
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
                # Stop after the first valid post
                if len(posts) >= 1:
                    break
            else:
                print(f"[INFO] Skipped post {idx + 1}: no substantial text found (text_length={len(post_data.get('post_text', ''))})", flush=True)

        except Exception as e:
            print(f"[WARNING] Error processing post {idx + 1}: {e}", file=sys.stderr)
            continue
        if len(posts) >= 1:
            break

    # Filter to only keep posts authored by the current page/group
    if page_name_from_url:
        before = len(posts)
        posts = [p for p in posts if author_matches_page(p.get("author_name"), page_name_from_url)]
        after = len(posts)
        if after != before:
            print(f"[INFO] Author filter applied: kept {after}/{before} posts for page '{page_name_from_url}'", flush=True)

    # Try GraphQL interception via selenium-wire to fill remaining posts
    if WIRE_AVAILABLE and hasattr(driver, "requests"):
        try:
            print("[INFO] Attempting to collect posts from GraphQL network traffic...", flush=True)
            graphql_posts = collect_graphql_posts_from_requests(
                driver,
                existing_posts=posts,
                max_posts=max_posts,
                default_author_name=page_name_from_url or ""
            )
            # Deduplicate and merge
            existing_keys = {create_post_key(p) for p in posts}
            for p in graphql_posts:
                # Enforce author filter from network capture as well
                if page_name_from_url and not author_matches_page(p.get("author_name"), page_name_from_url):
                    continue
                try:
                    k = create_post_key(p)
                except Exception:
                    k = None
                if k and k not in existing_keys and p.get("post_text"):
                    posts.append(p)
                    existing_keys.add(k)
                    print(f"[INFO] Added GraphQL post {len(posts)} via network interception", flush=True)
                if len(posts) >= max_posts:
                    break
        except Exception as e:
            print(f"[WARNING] GraphQL interception failed: {e}", file=sys.stderr)

    # If we didn't get enough posts, try mbasic fallback pagination (more reliable in headless)
    if len(posts) < max_posts:
        try:
            print(f"[INFO] Only scraped {len(posts)} posts, trying mbasic fallback to reach {max_posts}...", flush=True)
            # Derive page name from URL again for mbasic
            page_name = None
            try:
                if "/groups/" in group_url:
                    page_name = group_url.split("/groups/")[-1].split("/")[0].split("?")[0]
                elif "facebook.com/" in group_url:
                    page_name = group_url.split("facebook.com/")[-1].split("/")[0].split("?")[0]
            except Exception:
                page_name = None
            if page_name:
                extra_posts = scrape_mbasic_posts(driver, page_name, max_posts, posts)
                if len(posts) + len(extra_posts) < max_posts:
                    # Try HTTP mbasic fallback if still short
                    http_extra = scrape_mbasic_posts_http(driver, page_name, max_posts, posts + extra_posts)
                    extra_posts.extend(http_extra)
                # Deduplicate and extend
                existing_keys = set()
                for p in posts:
                    try:
                        existing_keys.add(create_post_key(p))
                    except Exception:
                        continue
                for p in extra_posts:
                    try:
                        k = create_post_key(p)
                        if k not in existing_keys:
                            posts.append(p)
                            existing_keys.add(k)
                            print(f"[INFO] Added mbasic post {len(posts)} (fallback)", flush=True)
                        if len(posts) >= max_posts:
                            break
                    except Exception:
                        continue
            else:
                print("[INFO] Could not determine page name for mbasic fallback", flush=True)
        except Exception as e:
            print(f"[WARNING] mbasic fallback failed: {e}", file=sys.stderr)
    
    return posts


def scrape_mbasic_posts(
    driver: webdriver.Chrome,
    page_name: str,
    max_posts: int,
    existing_posts: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Fallback: use mbasic.facebook.com with pagination to extract more posts."""
    collected: list[dict[str, str]] = []
    visited_links: set[str] = set()
    seen_texts: set[str] = set()
    
    # Seed seen_texts from existing posts to avoid duplicates
    for p in existing_posts or []:
        txt = (p.get("post_text") or "").strip()
        if txt:
            seen_texts.add(re.sub(r"\s+", " ", txt.lower())[:240])
    
    def to_abs(url: str) -> str:
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"https://mbasic.facebook.com{url}"
        return f"https://mbasic.facebook.com/{url}"
    
    # Start at mbasic page
    # Try timeline first (more posts), then fallback to root if needed
    url_candidates = [
        f"https://mbasic.facebook.com/{page_name}?v=timeline",
        f"https://mbasic.facebook.com/{page_name}/posts",
        f"https://mbasic.facebook.com/{page_name}",
    ]
    url = url_candidates[0]
    try:
        driver.get(url)
        sleep(3)
    except Exception:
        pass
    
    pages_traversed = 0
    tried_candidates = 0
    while len(collected) + len(existing_posts) < max_posts and pages_traversed < 20:
        pages_traversed += 1
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Find candidate post links
    anchors = soup.find_all("a", href=True)
    # Process each candidate link by extracting nearby text from listing (faster) first
    for a in anchors:
        href = a["href"]
        href_lower = href.lower()
        if not any(s in href_lower for s in ["/story.php", "/permalink.php", "/posts/"]):
            continue
            if len(collected) + len(existing_posts) >= max_posts:
                break
            abs_url = to_abs(href)
            if abs_url in visited_links:
                continue
            visited_links.add(abs_url)
            
            # Try to extract text from the anchor's parent block on the listing page
            post_block_text = ""
            try:
                parent = a.find_parent("div")
                depth = 0
                while parent and len(parent.get_text(strip=True)) < 40 and depth < 3:
                    parent = parent.find_parent("div")
                    depth += 1
                if parent:
                    text = parent.get_text(" ", strip=True)
                    post_block_text = text
            except Exception:
                post_block_text = ""
            
            if not post_block_text or len(post_block_text) < 40:
                # Open the post to get full text
                try:
                    driver.get(abs_url)
                    sleep(2)
                    psoup = BeautifulSoup(driver.page_source, "html.parser")
                    # Common mbasic containers that hold story content
                    content = None
                    for sel in [
                        {"id": "m_story_permalink_view"},
                        {"id": "inline_share"},
                        {"class": lambda x: x},
                    ]:
                        content = psoup.find("div", sel)
                        if content:
                            break
                    text = content.get_text(" ", strip=True) if content else psoup.get_text(" ", strip=True)
                    post_block_text = text
                except Exception:
                    continue
            
            # Clean and check text
            text_clean = re.sub(r"\s+", " ", (post_block_text or "").strip())
            # Skip obvious UI noise
            ui_terms = ["like", "comment", "share", "se mere", "see more", "log in", "forgot account"]
            if any(t in text_clean.lower() for t in ui_terms) and len(text_clean) < 80:
                continue
            if len(text_clean) < 40:
                continue
            
            normalized = text_clean.lower()[:240]
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            
            # Build post
            post_link_www = abs_url.replace("mbasic.facebook.com", "www.facebook.com")
            post = {
                "post_id": f"mbasic_{len(existing_posts) + len(collected) + 1}",
                "author_name": page_name.replace("-", " ").title(),
                "post_text": text_clean,
                "post_time": "",
                "post_link": post_link_www,
                "video_url": "",
                "video_thumbnail": "",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            collected.append(post)
            print(f"[INFO] Collected mbasic post {len(collected)}: {text_clean[:80]}...", flush=True)
            
            if len(collected) + len(existing_posts) >= max_posts:
                break
        
        if len(collected) + len(existing_posts) >= max_posts:
            break
        
        # Find pagination link ("See more posts", etc.)
        next_link = None
        # 1) Specific container Facebook uses on mbasic
        try:
            more_div = soup.find("div", id="m_more_item")
            if more_div:
                a_more = more_div.find("a", href=True)
                if a_more:
                    next_link = to_abs(a_more["href"])
        except Exception:
            pass
        # 2) Common texts in English/Danish
        if not next_link:
            for text in ["See more posts", "Older posts", "See more", "Se flere opslag", "Ældre opslag", "Se mere"]:
                link = soup.find("a", string=lambda x: x and text.lower() in x.lower())
                if link and link.get("href"):
                    next_link = to_abs(link["href"])
                    break
        # 3) Heuristics on href
        if not next_link:
            for a in anchors:
                href_raw = a.get("href", "")
                href = href_raw.lower()
                if any(k in href for k in ["more", "older", "sectionloading", "unit_cursor", "timeline/stream"]):
                    if "/story.php" not in href and "/permalink.php" not in href:
                        next_link = to_abs(href_raw)
                        break
        # 4) If still nothing and we haven't tried other candidates, try next url candidate
        if not next_link and tried_candidates < len(url_candidates) - 1 and len(collected) == 0 and pages_traversed <= 2:
            tried_candidates += 1
            url = url_candidates[tried_candidates]
            print(f"[INFO] Switching mbasic URL candidate: {url}", flush=True)
            try:
                driver.get(url)
                sleep(2)
            except Exception:
                pass
            continue
        
        if not next_link:
            print("[INFO] No more pagination links on mbasic", flush=True)
            break
        
        try:
            driver.get(next_link)
            sleep(2)
        except Exception:
            break
    
    return collected


def scrape_mbasic_posts_http(
    driver: webdriver.Chrome,
    page_name: str,
    max_posts: int,
    existing_posts: list[dict[str, str]],
) -> list[dict[str, str]]:
    """HTTP-only mbasic fallback using requests with transferred Selenium cookies."""
    if requests is None:
        print("[INFO] requests not available, skipping HTTP mbasic fallback", flush=True)
        return []
    
    session = requests.Session()
    # Transfer cookies from Selenium
    try:
        for c in driver.get_cookies():
            try:
                domain = c.get("domain") or ".facebook.com"
                session.cookies.set(c.get("name"), c.get("value"), domain=domain)
            except Exception:
                continue
    except Exception:
        pass
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15A372 Safari/604.1",
        "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
    }
    
    def abs_url(u: str) -> str:
        if u.startswith("http"):
            return u
        if u.startswith("/"):
            return f"https://mbasic.facebook.com{u}"
        return f"https://mbasic.facebook.com/{u}"
    
    collected: list[dict[str, str]] = []
    seen_texts = {re.sub(r"\\s+", " ", (p.get("post_text") or "").strip().lower())[:240] for p in (existing_posts or []) if p.get("post_text")}
    visited_story_urls: set[str] = set()
    
    url_candidates = [
        f"https://mbasic.facebook.com/{page_name}?v=timeline",
        f"https://mbasic.facebook.com/{page_name}/posts",
        f"https://mbasic.facebook.com/{page_name}",
    ]
    next_url: Optional[str] = url_candidates[0]
    tried_idx = 0
    
    pages = 0
    while next_url and len(collected) + len(existing_posts) < max_posts and pages < 30:
        pages += 1
        try:
            resp = session.get(next_url, headers=headers, timeout=20)
            html = resp.text
        except Exception as e:
            print(f"[DEBUG] HTTP get failed for {next_url}: {e}", flush=True)
            # Try next candidate base URL once if initial fails
            if tried_idx < len(url_candidates) - 1:
                tried_idx += 1
                next_url = url_candidates[tried_idx]
                continue
            break
        
        soup = BeautifulSoup(html, "htmlparser") if "htmlparser" in dir(BeautifulSoup) else BeautifulSoup(html, "html.parser")
        
        # Find candidate story links on listing page
        anchors = soup.find_all("a", href=True)
        story_links: list[str] = []
        for a in anchors:
            href = a["href"]
            href_l = href.lower()
            if any(s in href_l for s in ["/story.php", "/permalink.php", "/posts/"]):
                story_links.append(abs_url(href))
        
        # Visit each story link to extract full text
        for story_url in story_links:
            if len(collected) + len(existing_posts) >= max_posts:
                break
            if story_url in visited_story_urls:
                continue
            visited_story_urls.add(story_url)
            try:
                r = session.get(story_url, headers=headers, timeout=20)
                psoup = BeautifulSoup(r.text, "html.parser")
                content = None
                # Typical mbasic containers
                for sel in [
                    {"id": "m_story_permalink_view"},
                    {"id": "root"},
                    {"class": lambda x: x},
                ]:
                    content = psoup.find("div", sel)
                    if content:
                        break
                text = content.get_text(" ", strip=True) if content else psoup.get_text(" ", strip=True)
                text = re.sub(r"\\s+", " ", text or "").strip()
                # Filter out obvious UI noise and too-short texts
                if len(text) < 40:
                    continue
                ui_terms = ["like", "comment", "share", "see more", "se mere", "log in", "forgot account"]
                if any(t in text.lower() for t in ui_terms) and len(text) < 80:
                    continue
                normalized = text.lower()[:240]
                if normalized in seen_texts:
                    continue
                seen_texts.add(normalized)
                
                post = {
                    "post_id": f"http_{len(existing_posts) + len(collected) + 1}",
                    "author_name": page_name.replace("-", " ").title(),
                    "post_text": text,
                    "post_time": "",
                    "post_link": story_url.replace("mbasic.facebook.com", "www.facebook.com"),
                    "video_url": "",
                    "video_thumbnail": "",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
                collected.append(post)
                print(f"[INFO] HTTP mbasic collected {len(collected)}: {text[:80]}...", flush=True)
            except Exception as e:
                print(f"[DEBUG] HTTP story fetch failed {story_url}: {e}", flush=True)
                continue
        
        if len(collected) + len(existing_posts) >= max_posts:
            break
        
        # Find pagination link
        next_link = None
        try:
            more_div = soup.find("div", id="m_more_item")
            if more_div:
                a_more = more_div.find("a", href=True)
                if a_more:
                    next_link = abs_url(a_more["href"])
        except Exception:
            pass
        if not next_link:
            texts = ["See more posts", "Older posts", "See more", "Se flere opslag", "Ældre opslag", "Se mere"]
            for t in texts:
                a = soup.find("a", string=lambda x: x and t.lower() in x.lower())
                if a and a.get("href"):
                    next_link = abs_url(a["href"])
                    break
        if not next_link:
            # Heuristic: find links containing cursor params
            for a in anchors:
                href = a.get("href", "").lower()
                raw = a.get("href", "")
                if any(k in href for k in ["more", "older", "sectionloading", "unit_cursor", "timeline/stream"]):
                    if "/story.php" not in href and "/permalink.php" not in href:
                        next_link = abs_url(raw)
                        break
        if not next_link:
            # Try switching to next base candidate once if no progress yet
            if tried_idx < len(url_candidates) - 1 and len(collected) == 0 and pages <= 2:
                tried_idx += 1
                next_url = url_candidates[tried_idx]
                print(f"[INFO] HTTP mbasic switching base URL to {next_url}", flush=True)
                continue
            print("[INFO] HTTP mbasic: no more pagination", flush=True)
            break
        next_url = next_link
    
    return collected


def collect_graphql_posts_from_requests(
    driver,
    existing_posts: list[dict[str, str]],
    max_posts: int,
    default_author_name: str = "",
) -> list[dict[str, str]]:
    """Scan selenium-wire captured requests for GraphQL responses and extract posts."""
    collected: list[dict[str, str]] = []
    if not hasattr(driver, "requests"):
        return collected
    
    # Build set of normalized existing texts to avoid duplicates
    import re
    seen_texts = set()
    for p in existing_posts or []:
        text = (p.get("post_text") or "").strip()
        if text:
            norm = re.sub(r"\s+", " ", text.lower())[:240]
            seen_texts.add(norm)
    
    # Helper: recursively search dicts/lists for post-like objects
    def extract_from_obj(obj) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        if isinstance(obj, dict):
            # Direct post-like node
            post_id = None
            post_text = None
            author_name = None
            # Try common shapes
            try:
                # Comet story locations
                # obj.get('comet_sections',{}).get('content',{}).get('story' ... )
                story = None
                cs = obj.get("comet_sections")
                if isinstance(cs, dict):
                    content = cs.get("content")
                    if isinstance(content, dict):
                        story = content.get("story")
                    if story is None:
                        # Sometimes under context_layout.story
                        cl = cs.get("context_layout")
                        if isinstance(cl, dict):
                            s2 = cl.get("story")
                            if isinstance(s2, dict):
                                story = s2
                if story is None and "story" in obj and isinstance(obj["story"], dict):
                    story = obj["story"]
                if story:
                    # message text
                    msg = story.get("message") or {}
                    if isinstance(msg, dict) and isinstance(msg.get("text"), str):
                        post_text = msg.get("text")
                    # feedback/story for post_id
                    feedback = story.get("feedback") or {}
                    if isinstance(feedback, dict):
                        st = feedback.get("story") or {}
                        if isinstance(st, dict) and isinstance(st.get("post_id"), str):
                            post_id = st.get("post_id")
                    # author
                    actor_section = story.get("comet_sections", {}).get("actor_photo", {}).get("story")
                    if isinstance(actor_section, dict):
                        actors = actor_section.get("actors")
                        if isinstance(actors, list) and actors and isinstance(actors[0], dict):
                            author_name = actors[0].get("name")
                # Alternative shapes: node->comet_sections...
            except Exception:
                pass
            
            # Fallbacks: generic keys
            if not post_text:
                if isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("text"), str):
                    post_text = obj["message"]["text"]
            
            if not post_id:
                # Sometimes 'post_id' floats in nested dicts
                if "post_id" in obj and isinstance(obj["post_id"], str):
                    post_id = obj["post_id"]
            
            if not author_name:
                # Try 'actor' or 'author'
                if isinstance(obj.get("actor"), dict) and isinstance(obj["actor"].get("name"), str):
                    author_name = obj["actor"]["name"]
                elif isinstance(obj.get("author"), dict) and isinstance(obj["author"].get("name"), str):
                    author_name = obj["author"]["name"]
            
            if post_text and (post_id or len(post_text) > 40):
                # Build post
                norm = re.sub(r"\s+", " ", post_text.lower())[:240]
                if norm not in seen_texts:
                    seen_texts.add(norm)
                    collected_post = {
                        "post_id": post_id or f"graphql_{len(existing_posts) + len(collected) + 1}",
                        "author_name": author_name or (default_author_name.replace("-", " ").title() if default_author_name else ""),
                        "post_text": post_text,
                        "post_time": "",
                        "post_link": "",
                        "video_url": "",
                        "video_thumbnail": "",
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                    }
                    results.append(collected_post)
            
            # Recurse into children
            for v in obj.values():
                results.extend(extract_from_obj(v))
        elif isinstance(obj, list):
            for item in obj:
                results.extend(extract_from_obj(item))
        return results
    
    # Iterate recorded requests; newest last
    for req in getattr(driver, "requests", []):
        try:
            if not req.response:
                continue
            url = (req.url or "").lower()
            if "graphql" not in url:
                continue
            # Only POST responses with body
            body_bytes = getattr(req.response, "body", b"")
            if not body_bytes:
                continue
            try:
                text = body_bytes.decode("utf-8", errors="ignore")
            except Exception:
                continue
            
            # Some GraphQL endpoints return NDJSON (newline-delimited)
            lines = [line for line in text.splitlines() if line.strip()]
            for line in lines:
                try:
                    data = json.loads(line)
                except Exception:
                    # Some are standard JSON (not NDJSON)
                    try:
                        data = json.loads(text)
                        # Consume whole body once
                        lines = []
                    except Exception:
                        continue
                posts_from_line = extract_from_obj(data)
                for p in posts_from_line:
                    if len(collected) + len(existing_posts) >= max_posts:
                        break
                    # Avoid adding trivially short UI text
                    if isinstance(p.get("post_text"), str) and len(p["post_text"]) >= 40:
                        collected.append(p)
                if len(collected) + len(existing_posts) >= max_posts:
                    break
        except Exception:
            continue
        if len(collected) + len(existing_posts) >= max_posts:
            break
    
    # Clear captured requests to avoid growth across runs
    try:
        driver.requests.clear()  # type: ignore
    except Exception:
        pass
    
    return collected


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
        "--search-query",
        help="Optional search query to run inside the group/page (e.g., 'socialdemokratiet')",
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
        print("[INFO] Setting up browser driver...", flush=True)
        # Try Firefox first, fall back to Chrome if Firefox is not available
        try:
            driver = setup_driver(browser="firefox", headless=args.headless)
            print("[INFO] Firefox driver ready", flush=True)
        except Exception as e:
            print(f"[WARNING] Firefox not available: {e}, using Chrome instead", flush=True)
            driver = setup_driver(browser="chrome", headless=args.headless)
            print("[INFO] Chrome driver ready", flush=True)

        # Attempt login using CLI flags or environment variables as fallback
        email = (args.email or os.environ.get("FACEBOOK_EMAIL") or "").strip()
        password = (args.password or os.environ.get("FACEBOOK_PASSWORD") or "").strip()
        if email and password:
            print(f"[INFO] Attempting login with email: {email[:3]}***", flush=True)
            login_success = login_to_facebook(driver, email, password)
            print(f"[INFO] Login result: {login_success}", flush=True)
            if not login_success:
                print("[WARNING] Proceeding without login (login failed)", flush=True)
        else:
            print("[INFO] No credentials provided (flags/env), accessing as guest", flush=True)

        # Scrape posts
        print(f"[INFO] Starting to scrape posts (max: {args.max_posts})", flush=True)
        posts = scrape_facebook_group_posts(
            driver,
            group_url,
            max_posts=args.max_posts,
            scroll_pause=args.scroll_pause,
            search_query=(args.search_query or None),
        )
        print(f"[INFO] Scraped {len(posts)} posts", flush=True)

        # Write results (even if empty, so we know the script ran)
        write_payload(posts, group_url, output_path)
        
        # Persist first post to per-party list with timestamp (dedup by link/text)
        try:
            # Determine page slug and party code
            page_slug = None
            if "/groups/" in group_url:
                page_slug = group_url.split("/groups/")[-1].split("/")[0].split("?")[0].lower()
            elif "facebook.com/" in group_url:
                page_slug = group_url.split("facebook.com/")[-1].split("/")[0].split("?")[0].lower()
            party_code = None
            if page_slug:
                normalized = page_slug.replace("-", "").replace("_", "")
                party_code = PARTY_NAME_TO_CODE.get(normalized)
            if posts and party_code:
                # Build minimal record with scrapedAt timestamp
                post = dict(posts[0])
                post["scrapedAt"] = datetime.now(timezone.utc).isoformat()
                
                # Load or init store
                try:
                    PARTY_POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    if PARTY_POSTS_FILE.exists():
                        store = json.loads(PARTY_POSTS_FILE.read_text(encoding="utf-8") or "{}")
                    else:
                        store = {}
                except Exception:
                    store = {}
                
                posts_list = store.get(party_code) or []
                new_link = (post.get("post_link") or "").strip().lower()
                new_text_norm = re.sub(r"\s+", " ", (post.get("post_text") or "").strip().lower())[:240]
                exists = False
                for existing in posts_list:
                    link = (existing.get("post_link") or "").strip().lower()
                    txt = re.sub(r"\s+", " ", (existing.get("post_text") or "").strip().lower())[:240]
                    if (new_link and link and new_link == link) or (new_text_norm and txt and new_text_norm == txt):
                        exists = True
                        break
                if not exists:
                    posts_list.append(post)
                    store[party_code] = posts_list
                    # Also append to ALL list
                    all_list = store.get("ALL") or []
                    store["ALL"] = all_list
                    all_exists = False
                    for ex in all_list:
                        link = (ex.get("post_link") or "").strip().lower()
                        txt = re.sub(r"\s+", " ", (ex.get("post_text") or "").strip().lower())[:240]
                        if (new_link and link and new_link == link) or (new_text_norm and txt and new_text_norm == txt):
                            all_exists = True
                            break
                    if not all_exists:
                        all_list.append(post)
                    tmp = PARTY_POSTS_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp.replace(PARTY_POSTS_FILE)
                    print(f"[SUCCESS] Appended first post to party list '{party_code}'", flush=True)
        except Exception as persist_err:
            print(f"[WARNING] Failed to persist party post list: {persist_err}", flush=True)

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


