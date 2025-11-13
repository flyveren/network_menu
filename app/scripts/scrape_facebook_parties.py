#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

PARTY_URLS = {
    "A": "https://www.facebook.com/socialdemokratiet",
    "B": "https://www.facebook.com/radikalevenstre",
    "C": "https://www.facebook.com/Konservative/",
    "F": "https://www.facebook.com/sfparti",
    "H": "https://www.facebook.com/profile.php?id=61561610215954",
    "I": "https://www.facebook.com/LiberalAlliance",
    "M": "https://www.facebook.com/moderaterne",
    "O": "https://www.facebook.com/danskfolkeparti",
    "V": "https://www.facebook.com/venstre.dk",
    "Æ": "https://www.facebook.com/PartietDD",
    "Ø": "https://www.facebook.com/enhedslisten",
    "Å": "https://www.facebook.com/alternativet.dk",
}

SCRIPT_OPTIMIZED = os.path.join(os.path.dirname(__file__), "fetch_facebook_first_post.py")
TIMEOUT_SECONDS = int(os.environ.get("FACEBOOK_TIMEOUT", "90"))

def run_party_scrape(url: str) -> int:
    """Run optimized scraper for a party URL."""
    args = ["python3", SCRIPT_OPTIMIZED, "--group-url", url, "--headless"]
    workdir = os.path.dirname(SCRIPT_OPTIMIZED)

    print(f"[CRON] {datetime.now(timezone.utc).isoformat()} Running: {' '.join(args)}", flush=True)
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=workdir, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"[CRON] Timeout ({TIMEOUT_SECONDS}s) for scraper: {url}", flush=True)
        return 124
    
    sys.stdout.write(proc.stdout[-4000:])  # tail output
    return proc.returncode

def main() -> int:
    failures = 0
    for code, url in PARTY_URLS.items():
        try:
            rc = run_party_scrape(url)
            if rc != 0:
                print(f"[CRON] Scrape failed for {code} {url} rc={rc}", flush=True)
                failures += 1
        except Exception as e:
            print(f"[CRON] Exception scraping {code} {url}: {e}", flush=True)
            failures += 1
    return 0 if failures == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())


