"""
Amazon JP All-Offers Display (AOD) scraper.

Hits the undocumented AOD AJAX endpoint that powers the "See all buying options"
panel on product pages:
  GET https://www.amazon.co.jp/gp/aod/ajax?asin=<ASIN>&pc=dp&isonlyrenderofferlistingpage=1&pageno=<N>

Returns an HTML fragment containing offer cards. Each card carries:
  - data-csa-c-seller-id  →  seller OID
  - Seller name in a link with href containing ?seller=<OID>
  - Price, condition, fulfillment type
"""

import logging
import random
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AOD_AJAX_URL = "https://www.amazon.co.jp/gp/aod/ajax"
PRODUCT_URL = "https://www.amazon.co.jp/dp/{asin}/"

# Rotate UAs to avoid trivial bot detection
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Seller OID regex: Amazon seller IDs are uppercase alphanumeric, ~14 chars
_SELLER_ID_RE = re.compile(r"[?&]seller=([A-Z0-9]{10,20})")


class AmazonJPScraper:
    def __init__(
        self,
        proxy_manager=None,
        request_delay: tuple[float, float] = (3.0, 7.0),
        max_retries: int = 3,
    ):
        self.proxy_manager = proxy_manager
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(_BASE_HEADERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_offers(self, asin: str) -> list[dict]:
        """Return all active offers for *asin* as a list of dicts."""
        # Seed the session with real cookies by visiting the product page first
        self._get(PRODUCT_URL.format(asin=asin))
        time.sleep(random.uniform(*self.request_delay))

        offers: list[dict] = []
        page = 1

        while True:
            params = {
                "asin": asin,
                "pc": "dp",
                "isonlyrenderofferlistingpage": "1",
                "pageno": page,
            }
            resp = self._get(
                AOD_AJAX_URL,
                params=params,
                referer=PRODUCT_URL.format(asin=asin),
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
        soup = BeautifulSoup(html, "html.parser")
        offers: list[dict] = []

        # The pinned / featured offer sits in a dedicated container
        pinned = soup.select_one("#aod-pinned-offer")
        if pinned:
            offer = self._extract_offer(pinned, asin)
            if offer:
                offers.append(offer)

        # Remaining non-pinned offers
        for div in soup.select("div#aod-offer, div[id^='aod-offer-']:not(#aod-offer-heading)"):
            # Skip child divs that are not top-level offer cards
            if div.parent and div.parent.get("id", "").startswith("aod-offer"):
                continue
            offer = self._extract_offer(div, asin)
            if offer:
                offers.append(offer)

        return offers

    def _extract_offer(self, div, asin: str) -> Optional[dict]:
        # ── Seller OID ──────────────────────────────────────────────────
        seller_id: Optional[str] = (
            div.get("data-csa-c-seller-id")
            or div.get("data-seller-id")
        )

        seller_name: Optional[str] = None

        # Seller link carries the OID in the URL and the name as text
        for sel in (
            "#aod-offer-soldBy a",
            "a[href*='seller=']",
            "a[href*='gp/aawrs']",
            ".mbcMerchantName a",
            "span.a-size-small a",
        ):
            link = div.select_one(sel)
            if link:
                href = link.get("href", "")
                m = _SELLER_ID_RE.search(href)
                if m:
                    seller_id = seller_id or m.group(1)
                text = link.get_text(strip=True)
                if text:
                    seller_name = text
                break

        # Hidden input fallback (add-to-cart forms)
        if not seller_id:
            inp = div.select_one("input[name='seller']")
            if inp:
                seller_id = inp.get("value")

        # Amazon itself as seller (no seller link present)
        sold_by_text = ""
        sold_by_div = div.select_one("#aod-offer-soldBy, .a-size-small")
        if sold_by_div:
            sold_by_text = sold_by_div.get_text(strip=True)

        if not seller_id and ("Amazon" in sold_by_text or "アマゾン" in sold_by_text):
            seller_id = "ATVPDKIKX0DER"  # Amazon JP's own seller ID
            seller_name = "Amazon.co.jp"

        if not seller_id:
            return None

        # ── Price ────────────────────────────────────────────────────────
        price_jpy: Optional[int] = None
        for sel in (
            ".a-price .a-offscreen",
            "span.a-price-whole",
            ".a-color-price",
            "#aod-price-1",
        ):
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
        elif "Amazon" in full_text and ("発送" in full_text or "出荷" in full_text or "配送" in full_text):
            fulfillment = "FBA"
        else:
            fulfillment = "FBM"

        return {
            "asin": asin,
            "seller_id": seller_id,
            "seller_name": seller_name or "Unknown",
            "price_jpy": price_jpy,
            "condition": condition,
            "fulfillment": fulfillment,
            "in_stock": True,
        }

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _get(
        self,
        url: str,
        params: Optional[dict] = None,
        referer: Optional[str] = None,
    ) -> Optional[requests.Response]:
        headers = {"User-Agent": random.choice(_USER_AGENTS)}
        if referer:
            headers["Referer"] = referer

        proxy_url: Optional[str] = None
        if self.proxy_manager:
            proxy_url = self.proxy_manager.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        for attempt in range(1, self.max_retries + 1):
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
                    logger.warning("503 from Amazon (attempt %d/%d) — backing off", attempt, self.max_retries)
                    if self.proxy_manager and proxy_url:
                        self.proxy_manager.mark_bad(proxy_url)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("HTTP %s for %s", resp.status_code, url)
                    return None
            except requests.RequestException as exc:
                logger.warning("Request error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                time.sleep(2 ** attempt)

        logger.error("All retries exhausted for %s", url)
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_jpy(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None
