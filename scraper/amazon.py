"""
Amazon JP All-Offers Display (AOD) scraper.

Hits the undocumented AOD AJAX endpoint that powers the "See all buying options"
panel on product pages:

  GET https://www.amazon.co.jp/gp/aod/ajax/ref=auto_load_aod
      ?asin=<ASIN>&pc=dp&qty=1&pageno=<N>

Returns an HTML fragment containing one `div#aod-offer` block per seller.

TLS fingerprinting note
-----------------------
Amazon fingerprints the TLS handshake (JA3/JA4). Plain `requests` or
`httpx` expose a Python TLS signature that gets flagged regardless of
User-Agent. We use `curl_cffi` with impersonate="chrome124" to emit
a real Chrome TLS hello, bypassing that check.
"""

import logging
import random
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Correct AOD endpoint — ref= is part of the path, not a query param
AOD_AJAX_URL = "https://www.amazon.co.jp/gp/aod/ajax/ref=auto_load_aod"
PRODUCT_URL  = "https://www.amazon.co.jp/dp/{asin}/"

# curl_cffi impersonation profile — must match the UA we advertise
_IMPERSONATE = "chrome124"

# Each profile keeps the UA and its matching Sec-CH-UA-Platform together so
# they never produce an inconsistent (e.g. Linux UA + Windows hint) pair.
_UA_PROFILES = [
    {
        "ua":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": '"Windows"',
    },
    {
        "ua":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": '"macOS"',
    },
    {
        "ua":       "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": '"Linux"',
    },
]

# Static Chrome client-hint headers (platform is injected per-request from the profile)
_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Cookie that ensures prices come back in yen, not USD
_JP_COOKIES = {"i18n-prefs": "JPY"}


class AmazonJPScraper:
    def __init__(
        self,
        proxy_manager=None,
        request_delay: tuple[float, float] = (3.0, 7.0),
        max_retries: int = 3,
    ):
        self.proxy_manager   = proxy_manager
        self.request_delay   = request_delay
        self.max_retries     = max_retries
        # curl_cffi session — impersonate spoofs TLS cipher suite + extensions
        self.session = curl_requests.Session(impersonate=_IMPERSONATE)
        self.session.headers.update(_BASE_HEADERS)
        self.session.cookies.update(_JP_COOKIES)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_offers(self, asin: str) -> list[dict]:
        """Return all active offers for *asin* as a list of dicts."""
        # Seed session cookies by visiting the product page first
        self._get(PRODUCT_URL.format(asin=asin))
        time.sleep(random.uniform(*self.request_delay))

        offers: list[dict] = []
        page = 1

        while True:
            params = {
                "asin":    asin,
                "pc":      "dp",
                "qty":     "1",
                "pageno":  page,
            }
            resp = self._get(
                AOD_AJAX_URL,
                params=params,
                referer=PRODUCT_URL.format(asin=asin),
                is_ajax=True,
            )
            if resp is None:
                break

            page_offers = self._parse_aod_html(resp.text, asin)
            if not page_offers:
                break

            offers.extend(page_offers)

            # Check for a next-page link in the pagination widget
            soup = BeautifulSoup(resp.text, "html.parser")
            if not soup.select_one("ul.a-pagination li.a-last:not(.a-disabled)"):
                break

            page += 1
            time.sleep(random.uniform(*self.request_delay))

        return offers

    def get_product_name(self, asin: str) -> Optional[str]:
        """Fetch the product title from the detail page."""
        resp = self._get(PRODUCT_URL.format(asin=asin))
        if resp is None:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ("#productTitle", "h1#title span", "span#productTitle"):
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_aod_html(self, html: str, asin: str) -> list[dict]:
        soup  = BeautifulSoup(html, "html.parser")
        offers: list[dict] = []

        # The pinned / featured (Buy Box) offer sits in its own container
        pinned = soup.select_one("#aod-pinned-offer")
        if pinned:
            offer = self._extract_offer(pinned, asin)
            if offer:
                offers.append(offer)

        # Non-pinned offers — each is a direct child div with id="aod-offer"
        for div in soup.select("div#aod-offer"):
            offer = self._extract_offer(div, asin)
            if offer:
                offers.append(offer)

        return offers

    def _extract_offer(self, div, asin: str) -> Optional[dict]:
        # ── Seller OID ──────────────────────────────────────────────────
        # Priority: data attribute > soldBy link href > hidden input > Amazon fallback
        seller_id: Optional[str] = (
            div.get("data-csa-c-seller-id")
            or div.get("data-seller-id")
        )
        seller_name: Optional[str] = None

        # The confirmed structure from the AOD response:
        #   <div id="aod-offer-soldBy">
        #     <a href="/gp/aag/main?seller=AXXXXXXXX">Seller Name</a>
        #   </div>
        sold_by_div = div.select_one("#aod-offer-soldBy")
        if sold_by_div:
            link = sold_by_div.select_one("a[href]")
            if link:
                seller_id   = seller_id or _extract_seller_param(link["href"])
                seller_name = link.get_text(strip=True) or None

        # Broader fallback selectors
        if not seller_id:
            for sel in ("a[href*='seller=']", "a[href*='gp/aag/main']", "a[href*='/sp?']"):
                link = div.select_one(sel)
                if link:
                    seller_id   = _extract_seller_param(link.get("href", ""))
                    seller_name = seller_name or link.get_text(strip=True) or None
                    break

        # Hidden input inside add-to-cart form
        if not seller_id:
            inp = div.select_one("input[name='seller']")
            if inp:
                seller_id = inp.get("value")

        # Amazon itself as seller — no anchor link, just text
        if not seller_id:
            text = (sold_by_div or div).get_text()
            if "Amazon" in text or "アマゾン" in text:
                seller_id   = "ATVPDKIKX0DER"  # Amazon JP's own OID
                seller_name = "Amazon.co.jp"

        if not seller_id:
            return None

        # ── Price ────────────────────────────────────────────────────────
        price_jpy: Optional[int] = None
        for sel in (".a-price .a-offscreen", "span.a-price-whole", ".a-color-price"):
            el = div.select_one(sel)
            if el:
                price_jpy = _parse_jpy(el.get_text(strip=True))
                if price_jpy:
                    break

        # ── Condition ────────────────────────────────────────────────────
        condition = "new"
        cond_el = div.select_one("#aod-offer-heading h5, .a-section h5")
        if cond_el:
            condition = cond_el.get_text(strip=True)

        # ── Fulfillment type ─────────────────────────────────────────────
        full_text = div.get_text()
        if seller_id == "ATVPDKIKX0DER":
            fulfillment = "AMAZON"
        elif "Amazon" in full_text and any(w in full_text for w in ("発送", "出荷", "配送")):
            fulfillment = "FBA"
        else:
            fulfillment = "FBM"

        return {
            "asin":        asin,
            "seller_id":   seller_id,
            "seller_name": seller_name or "Unknown",
            "price_jpy":   price_jpy,
            "condition":   condition,
            "fulfillment": fulfillment,
            "in_stock":    True,
        }

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _get(
        self,
        url: str,
        params: Optional[dict]  = None,
        referer: Optional[str]  = None,
        is_ajax: bool           = False,
    ):
        profile = random.choice(_UA_PROFILES)
        headers: dict[str, str] = {
            "User-Agent":        profile["ua"],
            "Sec-CH-UA-Platform": profile["platform"],
        }
        if referer:
            headers["Referer"] = referer
        if is_ajax:
            # Required by Amazon's AOD endpoint to return the HTML fragment
            headers["X-Requested-With"] = "XMLHttpRequest"

        for attempt in range(1, self.max_retries + 1):
            # Select (and potentially rotate) proxy on every attempt so that
            # marking a proxy bad on a 503 actually takes effect next retry.
            proxy_url: Optional[str] = self.proxy_manager.get_proxy() if self.proxy_manager else None
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    proxies=proxies,
                    timeout=20,
                )
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 503:
                    logger.warning("503 (attempt %d/%d) — backing off", attempt, self.max_retries)
                    if self.proxy_manager and proxy_url:
                        self.proxy_manager.mark_bad(proxy_url)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("HTTP %s for %s", resp.status_code, url)
                    return None
            except Exception as exc:
                logger.warning("Request error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                if self.proxy_manager and proxy_url:
                    self.proxy_manager.mark_bad(proxy_url)
                time.sleep(2 ** attempt)

        logger.error("All retries exhausted for %s", url)
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_seller_param(href: str) -> Optional[str]:
    """Pull the seller= query parameter out of an Amazon URL."""
    try:
        qs = parse_qs(urlparse(href).query)
        values = qs.get("seller") or qs.get("smid") or qs.get("me")
        return values[0] if values else None
    except Exception:
        return None


def _parse_jpy(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None
