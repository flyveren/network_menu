#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DATA_DIR = (Path(__file__).resolve().parent.parent / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

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


def setup_firefox(headless: bool = True) -> webdriver.Firefox:
    options = FirefoxOptions()
    if headless:
        options.add_argument("--headless")
    # Prefer system binaries if present
    geckodriver = None
    for p in ["/usr/bin/geckodriver", "/usr/local/bin/geckodriver"]:
        if Path(p).exists():
            geckodriver = p
            break
    if geckodriver:
        service = FirefoxService(geckodriver)
    else:
        service = FirefoxService()
    driver = webdriver.Firefox(service=service, options=options)
    driver.maximize_window()
    return driver


def normalize_slug(raw_url: str) -> str:
    from urllib.parse import urlparse
    raw = (raw_url or "").strip()
    try:
        parsed = urlparse(raw)
        if parsed.netloc and "facebook.com" in parsed.netloc:
            path = parsed.path.strip("/")
        else:
            path = re.sub(r"^https?://(www\\.)?facebook\\.com/", "", raw, flags=re.I)
            path = path.split("?")[0].strip("/")
    except Exception:
        path = re.sub(r"^https?://(www\\.)?facebook\\.com/", "", raw, flags=re.I)
        path = path.split("?")[0].strip("/")
    if not path:
        return "page"
    if path.startswith("profile.php"):
        return "profile"
    return path


def climb_to_article(element):
    node = element
    for _ in range(8):
        if node is None:
            break
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


def extract_text(element) -> str:
    try:
        text = element.text or ""
    except Exception:
        text = ""
    # Trim common UI phrases
    text = re.sub(r"(Like|Comment|Share|See more|Vis mere)", "", text, flags=re.I)
    text = re.sub(r"\\s+", " ", text).strip()
    return text[:1500]


def find_post_link(element) -> str:
    # Look for a descendant link that resembles a post/story link
    try:
        links = element.find_elements(By.TAG_NAME, "a")
    except Exception:
        links = []
    for a in links:
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            href = ""
        if "/posts/" in href or "story.php" in href or "/videos/" in href:
            return href
    return ""


def find_time_text(element) -> str:
    # Try abbr/time-like link text
    try:
        abbr = element.find_elements(By.TAG_NAME, "abbr")
        if abbr:
            t = abbr[0].text.strip()
            if t:
                return t[:50]
    except Exception:
        pass
    try:
        smalls = element.find_elements(By.TAG_NAME, "small")
        if smalls:
            t = smalls[0].text.strip()
            if t:
                return t[:50]
    except Exception:
        pass
    return ""


def find_author(element, default_author: str) -> str:
    # Try to find a prominent link or heading near top
    try:
        heads = element.find_elements(By.XPATH, ".//h2|.//h3")
        for h in heads:
            t = (h.text or "").strip()
            if len(t) > 0:
                return t
    except Exception:
        pass
    try:
        alinks = element.find_elements(By.XPATH, ".//a")
        if alinks:
            t = (alinks[0].text or "").strip()
            if len(t) > 0:
                return t
    except Exception:
        pass
    return default_author


def scrape_latest_selenium(group_url: str, headless: bool = True) -> dict:
    driver = setup_firefox(headless=headless)
    slug = normalize_slug(group_url)
    try:
        # Try multiple variants; avoid scrolling; only pick the first visible article
        base = group_url.rstrip("/")
        variants = [f"{base}/", f"{base}/posts", f"{base}?sk=posts", base]
        for url in variants:
            try:
                driver.get(url)
                WebDriverWait(driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                continue
            sleep(2)

            # Candidate strategies (no scrolling):
            candidates = []
            # 1) Any top article first
            try:
                arts = driver.find_elements(By.CSS_SELECTOR, "article[role='article']")
                candidates.extend(arts[:5])
            except Exception:
                pass
            # 2) Suggested CSS seed
            try:
                els = driver.find_elements(By.CSS_SELECTOR, "[aria-posinset='1'] .x1jx94hy > div > div > div > div")
                candidates.extend(els[:2])
            except Exception:
                pass
            # 3) Fallback data-pagelet FeedUnit
            try:
                feed = driver.find_elements(By.CSS_SELECTOR, "[data-pagelet*='FeedUnit']")
                candidates.extend(feed[:2])
            except Exception:
                pass

            # Deduplicate
            seen = set()
            uniq = []
            for el in candidates:
                try:
                    key = el.id
                except Exception:
                    key = id(el)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(el)

            # Process first viable candidate
            for el in uniq:
                art = climb_to_article(el)
                text = extract_text(art)
                if not text or len(text) < 10:
                    continue
                link = find_post_link(art)
                time_text = find_time_text(art)
                author = find_author(art, slug)

                thumb = ""
                try:
                    imgs = art.find_elements(By.TAG_NAME, "img")
                    if imgs:
                        thumb = imgs[0].get_attribute("src") or ""
                except Exception:
                    pass

                post = {
                    "post_id": "post_1",
                    "author_name": author,
                    "post_text": text,
                    "post_time": time_text,
                    "post_link": link,
                    "video_url": "",
                    "video_thumbnail": thumb,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
                data = {
                    "source": group_url,
                    "scrapedAt": datetime.now(timezone.utc).isoformat(),
                    "totalPosts": 1,
                    "posts": [post],
                }
                return {"slug": slug, "data": data}

        # If nothing matched
        return {
            "slug": slug,
            "data": {
                "source": group_url,
                "scrapedAt": datetime.now(timezone.utc).isoformat(),
                "totalPosts": 0,
                "posts": [],
            },
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch latest post with Selenium (no scrolling)")
    parser.add_argument("--group-url", required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    result = scrape_latest_selenium(args.group_url, headless=args.headless)
    out_path = DATA_DIR / f"facebook_group_{result['slug']}.json"
    out_path.write_text(json.dumps(result["data"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SUCCESS] Wrote latest post (no-scroll) to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


