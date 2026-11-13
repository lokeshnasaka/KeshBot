"""Fetch pages that sit behind a JavaScript bot challenge (Bunny Shield on Gocomics).

A single headless Chromium is started lazily and kept open. It loads the site once to
pass the challenge, then every page is retrieved with an in-page ``fetch()`` so the
request goes through the real browser network stack (cookies and fingerprint included)
without rendering anything. Copying the cookies into aiohttp does not work: the shield
still answers 403.
"""

import asyncio
import logging
import os

from playwright.async_api import Browser, Page, Playwright, async_playwright

# Path to a Chromium binary to use instead of the one installed by `playwright install chromium`
BROWSER_PATH_ENV = "BDBOT_BROWSER_PATH"

_FETCH_JS = """async url => {
    const response = await fetch(url, {credentials: "include"});
    return [response.status, await response.text()];
}"""


class ShieldedFetcher:
    def __init__(self, origin: str):
        self.origin = origin
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    async def _start(self):
        logger = logging.getLogger("discord")
        logger.info(f"Starting headless browser for {self.origin}...")
        self._playwright = await async_playwright().start()
        executable_path = os.getenv(BROWSER_PATH_ENV) or None
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            # The full Chromium build in new headless mode; the stripped headless shell gets blocked
            channel=None if executable_path else "chromium",
            executable_path=executable_path,
            args=["--headless=new"],
        )
        # The default user agent advertises "HeadlessChrome", which the shield blocks outright
        probe = await self._browser.new_page()
        user_agent = (await probe.evaluate("navigator.userAgent")).replace(
            "HeadlessChrome", "Chrome"
        )
        await probe.close()
        context = await self._browser.new_context(user_agent=user_agent)
        self._page = await context.new_page()
        response = await self._page.goto(self.origin, wait_until="domcontentloaded")
        logger.info(
            f"Headless browser ready for {self.origin} (status {response.status if response else '?'})"
        )

    async def close(self):
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._page = None

    async def fetch(self, url: str) -> tuple[int, str]:
        """Return the status code and body of ``url``, restarting the browser once on failure."""
        logger = logging.getLogger("discord")
        async with self._lock:
            for attempt in range(2):
                try:
                    if self._page is None or self._page.is_closed():
                        await self.close()
                        await self._start()
                    status, text = await self._page.evaluate(_FETCH_JS, url)
                except Exception as e:
                    logger.warning(f"Headless browser fetch of {url} failed: {e!r}")
                    status, text = 0, ""
                if status != 403 and status != 0:
                    return status, text
                if attempt == 0:
                    logger.info(
                        f"Got status {status} for {url}, restarting headless browser..."
                    )
                    await self.close()
            return status, text


_fetchers: dict[str, ShieldedFetcher] = {}


def get_fetcher(origin: str) -> ShieldedFetcher:
    if origin not in _fetchers:
        _fetchers[origin] = ShieldedFetcher(origin)
    return _fetchers[origin]


async def close_all():
    for fetcher in _fetchers.values():
        await fetcher.close()
