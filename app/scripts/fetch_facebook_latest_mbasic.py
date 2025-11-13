#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

try:
    import requests
    from bs4 import BeautifulSoup  # type: ignore
except Exception as e:
    print(f"[ERROR] Missing dependency: {e}", file=sys.stderr)
    print("[INFO] Install with: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
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


def normalize_page_slug(page_url: str) -> str:
    from urllib.parse import urlparse
    raw = (page_url or "").strip()
    parsed = urlparse(raw)
    if parsed.netloc and "facebook.com" in parsed.netloc:
        path = parsed.path.strip("/")
    else:
        # Fallback: try regex stripping
        path = re.sub(r"^https?://(www\\.)?facebook\\.com/", "", raw, flags=re.I).split("?")[0].strip("/")
    if path.startswith("profile.php"):
        # profile.php?id=XXXX
        m = re.search(r"id=(\\d+)", raw)
        return m.group(1) if m else "profile"
    if path.startswith("groups/"):
        return path.split("/", 1)[1] or "group"
    return path or "page"


def to_absolute(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"https://mbasic.facebook.com{url}"
    return f"https://mbasic.facebook.com/{url}"


def login_session(email: Optional[str], password: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
        }
    )
    if not email or not password:
        print("[INFO] No credentials provided; scraping as guest (mbasic)", flush=True)
        return session

    try:
        # Start at mbasic login
        login_page = session.get("https://mbasic.facebook.com/login")
        login_soup = BeautifulSoup(login_page.text, "html.parser")
        form = login_soup.find("form")
        action = to_absolute(form.get("action") if form else "/login")

        payload = {}
        if form:
            for inp in form.find_all("input"):
                name = inp.get("name")
                value = inp.get("value", "")
                if name:
                    payload[name] = value
        payload.update({"email": email, "pass": password})

        resp = session.post(action, data=payload, allow_redirects=True)
        # If login succeeded, homepage won't be login again
        if "login" not in resp.url.lower():
            print("[SUCCESS] Logged in to Facebook (mbasic)", flush=True)
        else:
            print("[WARNING] Login may have failed; continuing as guest", flush=True)
    except Exception as e:
        print(f"[WARNING] Login failed: {e}; continuing as guest", flush=True)
    return session


def parse_first_post(html: str, page_slug: str) -> Optional[dict]:
    """
    Very lightweight mbasic parser: find first story link and surrounding text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Heuristic 1: first story.php link (mbasic story)
    story_link = soup.find("a", href=re.compile(r"(story\\.php|/posts/)"))
    if not story_link:
        return None

    # Walk up to container
    container = story_link
    for _ in range(6):
        if container.parent:
            container = container.parent
        else:
            break

    # Extract text content (trim UI labels)
    def clean_text(s: str) -> str:
        s = re.sub(r"\\s+", " ", s).strip()
        # Remove common UI phrases
        s = re.sub(r"(Like|Comment|Share|See more|Vis mere)", "", s, flags=re.I)
        return s.strip()

    text_blocks = []
    # Collect paragraphs and div text within the container, but avoid links/buttons lists
    for node in container.find_all(["p", "div"], recursive=True):
        if node.find("a", href=re.compile(r"(like|comment|share|privacy|help)", re.I)):
            continue
        t = clean_text(node.get_text(" ", strip=True))
        if t and len(t) > 10:
            text_blocks.append(t)
        if len(" ".join(text_blocks)) > 500:
            break

    post_text = clean_text(" ".join(text_blocks))[:1500] if text_blocks else ""

    # Time (abbr or small)
    time_str = ""
    abbr = container.find("abbr")
    if abbr and abbr.get_text(strip=True):
        time_str = abbr.get_text(strip=True)
    else:
        small = container.find("small")
        if small:
            time_str = small.get_text(" ", strip=True)
    time_str = time_str[:50]

    # Author: try to find heading/link closest above story_link
    author_name = page_slug
    try:
        header_link = container.find("a")
        if header_link and header_link.get_text(strip=True):
            author_name = header_link.get_text(strip=True)
    except Exception:
        pass

    # Normalize link to full facebook desktop URL if possible
    href = story_link.get("href", "")
    abs_href = to_absolute(href)
    desktop_link = re.sub(r"^https://mbasic\\.facebook\\.com", "https://www.facebook.com", abs_href)

    # Video/image thumbnail (best-effort)
    video_url = ""
    video_thumb = ""
    img = container.find("img")
    if img and img.get("src"):
        video_thumb = img.get("src")

    return {
        "post_id": "post_1",
        "author_name": author_name,
        "post_text": post_text,
        "post_time": time_str,
        "post_link": desktop_link,
        "video_url": video_url,
        "video_thumbnail": video_thumb,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def scrape_latest(page_url: str, email: Optional[str], password: Optional[str]) -> Tuple[str, dict]:
    page_slug = normalize_page_slug(page_url)
    base = f"https://mbasic.facebook.com/{page_slug}"

    session = login_session(email, password)
    # Try timeline view first, then root
    for path in (f"{base}?v=timeline", base):
        try:
            r = session.get(path, timeout=20)
            if r.status_code == 200 and r.text:
                post = parse_first_post(r.text, page_slug)
                if post and post.get("post_text") or post.get("post_link"):
                    data = {
                        "source": page_url,
                        "scrapedAt": datetime.now(timezone.utc).isoformat(),
                        "totalPosts": 1,
                        "posts": [post],
                    }
                    return page_slug, data
        except Exception as e:
            print(f"[WARNING] Fetch failed for {path}: {e}", flush=True)
            continue

    # No post
    data = {
        "source": page_url,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "totalPosts": 0,
        "posts": [],
    }
    return page_slug, data


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch latest Facebook post quickly via mbasic")
    parser.add_argument("--page-url", required=True, help="Facebook page URL")
    parser.add_argument("--email", default=os.environ.get("FACEBOOK_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("FACEBOOK_PASSWORD", ""))
    args = parser.parse_args()

    slug, data = scrape_latest(args.page_url, args.email or None, args.password or None)
    out_path = DATA_DIR / f"facebook_group_{slug}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SUCCESS] Wrote latest post to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


