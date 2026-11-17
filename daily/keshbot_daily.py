#!/usr/bin/env python3
"""
KeshBot daily comic poster (standalone).

Fetches today's Peanuts and Garfield strips from GoComics and posts them to a
Discord channel via a webhook. The comic image is downloaded here and uploaded
directly to Discord as a file attachment, so Discord never has to reach GoComics
itself.

Why this runs on your Mac and not a free cloud cron: GoComics blocks datacenter
IPs (returns 403). A residential IP -- like your Mac -- is accepted. See
daily/README.md for the launchd + self-wake setup that makes this run daily even
while the Mac is asleep.

Run manually to test:
    ./.venv/bin/python daily/keshbot_daily.py
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
WEBHOOK_ENV_FILE = HERE / "webhook.env"
STATE_FILE = HERE / ".last_posted.json"

# Comics to post: (gocomics slug, display name, author)
COMICS = [
    ("peanuts", "Peanuts", "Charles Schulz"),
    ("garfield", "Garfield", "Jim Davis"),
]

# Browser-style headers. GoComics serves 403 to obvious bots / datacenter IPs;
# these headers plus a residential IP get a normal 200.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Real strip images live here. The generic "GC_Social_FB_*" banners live on a
# different host; we reject those so we never post a placeholder banner.
REAL_IMAGE_MARKER = "featureassets.gocomics.com/assets/"

# How many days back to look for the most recent available strip.
LOOKBACK_DAYS = 5

# Headless browser retries: a run during a brief background wake can hit a slow network
FETCH_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 20
PAGE_TIMEOUT_MS = 30_000

EMBED_COLOR = 0xF6C700  # comic-strip yellow


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def get_webhook_url() -> str:
    """Read the Discord webhook URL from the env var or daily/webhook.env."""
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if url:
        return url
    if WEBHOOK_ENV_FILE.exists():
        for line in WEBHOOK_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DISCORD_WEBHOOK_URL":
                return value.strip().strip('"').strip("'")
    return ""


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        log(f"WARN: could not write state file: {exc}")


class GoComicsBrowser:
    """Headless Chromium that gets past GoComics' Bunny Shield bot challenge.

    Since 2026-11-12 every GoComics HTML page answers plain HTTP clients with a
    403 JavaScript challenge. A real browser passes it, so the browser loads the
    home page once, then each page is read with an in-page fetch() (no rendering).
    The full Chromium build is needed (the headless shell is blocked), and the
    "HeadlessChrome" user agent is rewritten to "Chrome".
    Requires: ./.venv/bin/playwright install chromium
    """

    def __enter__(self) -> "GoComicsBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = None
        try:
            self._browser = self._pw.chromium.launch(
                headless=True, channel="chromium", args=["--headless=new"]
            )
            probe = self._browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()
            self._page = self._browser.new_context(user_agent=user_agent).new_page()
            self._page.goto(
                "https://www.gocomics.com/", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
            )
        except Exception:
            self.__exit__()
            raise
        return self

    def get(self, url: str) -> tuple[int, str]:
        return self._page.evaluate(
            """async url => {
                const r = await fetch(url, {credentials: "include"});
                return [r.status, await r.text()];
            }""",
            url,
        )

    def __exit__(self, *exc) -> None:
        if self._browser is not None:
            self._browser.close()
        self._pw.stop()


def fetch_strips() -> list[tuple[str, str, str, tuple[str, str, datetime] | None]]:
    """Find the latest strip for every comic, retrying the whole browser session
    when it errors or finds nothing (e.g. flaky network during a background wake)."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with GoComicsBrowser() as browser:
                strips = []
                for slug, name, author in COMICS:
                    log(f"{name}: looking for latest strip...")
                    strips.append((slug, name, author, find_todays_strip(browser, slug)))
            if any(found for *_, found in strips):
                return strips
            log(f"Attempt {attempt}/{FETCH_ATTEMPTS}: no strips found.")
        except Exception as exc:
            strips = [(slug, name, author, None) for slug, name, author in COMICS]
            log(f"Attempt {attempt}/{FETCH_ATTEMPTS}: headless browser failed: {exc}")
        if attempt < FETCH_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
    return strips


def find_todays_strip(browser: GoComicsBrowser, slug: str) -> tuple[str, str, datetime] | None:
    """Return (image_url, page_url, date) for the most recent available strip,
    or None if nothing usable was found in the lookback window."""
    for days_ago in range(LOOKBACK_DAYS):
        date = datetime.now() - timedelta(days=days_ago)
        page_url = f"https://www.gocomics.com/{slug}/{date.strftime('%Y/%m/%d')}"
        try:
            status, html = browser.get(page_url)
        except Exception as exc:
            log(f"  {slug}: browser fetch error for {page_url}: {exc}")
            continue
        if status == 403:
            log(f"  {slug}: 403 Forbidden -- the GoComics bot shield rejected the headless browser.")
            return None
        if status != 200:
            log(f"  {slug}: HTTP {status} for {page_url}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("meta", attrs={"property": "og:image"})
        image_url = tag.get("content") if tag else None
        if image_url and REAL_IMAGE_MARKER in image_url:
            return image_url, page_url, date
        # Otherwise it's the generic banner (strip not posted for that day yet)
    return None


def download_image(image_url: str) -> tuple[bytes, str] | None:
    """Download the image; return (bytes, filename) or None on failure."""
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"  image download failed: {exc}")
        return None
    content_type = resp.headers.get("Content-Type", "")
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".gif"
    if ext == ".jpe":
        ext = ".jpg"
    return resp.content, f"comic{ext}"


def post_to_discord(
    webhook_url: str,
    name: str,
    author: str,
    page_url: str,
    date: datetime,
    image_bytes: bytes,
    filename: str,
) -> bool:
    """Post one comic to Discord as an embed with the image attached."""
    embed = {
        "title": f"{name} — {date.strftime('%A, %B %-d, %Y')}",
        "url": page_url,
        "color": EMBED_COLOR,
        "description": f"by {author}",
        "image": {"url": f"attachment://{filename}"},
        "footer": {"text": "Source: GoComics"},
    }
    payload = {"embeds": [embed]}
    content_type = mimetypes.guess_type(filename)[0] or "image/gif"
    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
        "files[0]": (filename, image_bytes, content_type),
    }
    try:
        resp = requests.post(webhook_url, files=files, timeout=60)
    except requests.RequestException as exc:
        log(f"  Discord post failed: {exc}")
        return False
    if resp.status_code in (200, 204):
        return True
    log(f"  Discord returned HTTP {resp.status_code}: {resp.text[:300]}")
    return False


def main() -> int:
    # --dry-run: find and download today's strips, but don't post or update state
    dry_run = "--dry-run" in sys.argv[1:]
    webhook_url = get_webhook_url()
    if not webhook_url and not dry_run:
        log(
            "ERROR: No webhook URL. Set DISCORD_WEBHOOK_URL or create "
            f"{WEBHOOK_ENV_FILE} with a line: DISCORD_WEBHOOK_URL=https://..."
        )
        return 1

    state = load_state()
    posted = 0
    failed = 0

    for slug, name, author, found in fetch_strips():
        if not found:
            log(f"  {name}: no strip found (skipping).")
            failed += 1
            continue
        image_url, page_url, date = found

        if state.get(slug) == image_url:
            log(f"  {name}: already posted this strip ({date.strftime('%Y-%m-%d')}); skipping.")
            continue

        dl = download_image(image_url)
        if not dl:
            failed += 1
            continue
        image_bytes, filename = dl
        # Give the attachment a per-comic name so multiple embeds don't clash.
        filename = f"{slug}{Path(filename).suffix}"

        if dry_run:
            log(f"  {name}: [dry run] would post {filename} ({len(image_bytes)} bytes) for "
                f"{date.strftime('%Y-%m-%d')}: {image_url}")
            posted += 1
            continue

        if post_to_discord(webhook_url, name, author, page_url, date, image_bytes, filename):
            log(f"  {name}: posted strip for {date.strftime('%Y-%m-%d')}.")
            state[slug] = image_url
            posted += 1
        else:
            failed += 1

    if not dry_run:
        save_state(state)
    log(f"Done. {'Would post' if dry_run else 'Posted'} {posted}, failed {failed}.")
    # Exit non-zero only if everything failed, so launchd logs a clear signal.
    return 1 if (failed and not posted) else 0


if __name__ == "__main__":
    sys.exit(main())
