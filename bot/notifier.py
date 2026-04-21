"""
Discord webhook notifier.

Sends a rich embed for every restock event containing:
  • Product name + ASIN / SKU
  • Seller OID (seller_id)
  • Seller display name
  • Price (¥)
  • Condition  (新品 / 中古 …)
  • Fulfillment type  (FBA / FBM / AMAZON)
  • Direct link to the All Offers page for the product
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_AMAZON_JP_OFFER_URL = "https://www.amazon.co.jp/gp/offer-listing/{asin}/"
_SELLER_PAGE_URL = "https://www.amazon.co.jp/s?me={seller_id}&marketplaceID=A1VC38T7YXB528"

# Embed colour palette
_COLOUR_NEW_SELLER = 0x00FF7F   # spring green  – never seen this seller before
_COLOUR_RESTOCK    = 0x1DA0F2   # twitter blue  – seller returned after being out-of-stock


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.strip()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def send_restock(self, event: dict) -> bool:
        """
        Post a restock embed to the Discord channel.

        *event* is a row from restock_events joined with products + sellers:
          asin, seller_id, seller_name, product_name,
          price_jpy, condition, fulfillment, detected_at
        """
        asin        = event.get("asin", "")
        seller_id   = event.get("seller_id", "")
        seller_name = event.get("seller_name") or "Unknown"
        product_name = event.get("product_name") or f"ASIN {asin}"
        price_jpy   = event.get("price_jpy")
        condition   = event.get("condition") or "new"
        fulfillment = event.get("fulfillment") or "FBM"
        detected_at = event.get("detected_at") or datetime.now(timezone.utc).isoformat()
        is_new      = event.get("is_new_seller", False)

        price_str = f"¥{price_jpy:,}" if price_jpy else "N/A"
        colour    = _COLOUR_NEW_SELLER if is_new else _COLOUR_RESTOCK

        offer_url  = _AMAZON_JP_OFFER_URL.format(asin=asin)
        seller_url = _SELLER_PAGE_URL.format(seller_id=seller_id)

        embed = {
            "title": "🔔 Restock Detected — Amazon JP",
            "color": colour,
            "url": offer_url,
            "description": (
                f"**[{_truncate(product_name, 120)}]({offer_url})**\n"
                f"A seller is now listing stock for this product."
            ),
            "fields": [
                {
                    "name": "ASIN / SKU",
                    "value": f"[`{asin}`](https://www.amazon.co.jp/dp/{asin}/)",
                    "inline": True,
                },
                {
                    "name": "Seller OID",
                    "value": f"[`{seller_id}`]({seller_url})",
                    "inline": True,
                },
                {
                    "name": "Seller Name",
                    "value": _truncate(seller_name, 60),
                    "inline": True,
                },
                {
                    "name": "Price",
                    "value": price_str,
                    "inline": True,
                },
                {
                    "name": "Condition",
                    "value": condition.title(),
                    "inline": True,
                },
                {
                    "name": "Fulfillment",
                    "value": fulfillment,
                    "inline": True,
                },
            ],
            "footer": {
                "text": "Amazon JP Stock Monitor",
            },
            "timestamp": _to_iso_utc(detected_at),
        }

        payload = {
            "username": "Amazon JP Stock Bot",
            "embeds": [embed],
        }

        return self._post(payload)

    def send_startup(self, asins: list[str]) -> None:
        """Optional startup ping so you know the bot is alive."""
        embed = {
            "title": "✅ Amazon JP Monitor Started",
            "color": 0xFFD700,
            "description": f"Watching **{len(asins)}** ASIN(s):\n" + "\n".join(f"• `{a}`" for a in asins),
            "footer": {"text": "Amazon JP Stock Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post({"username": "Amazon JP Stock Bot", "embeds": [embed]})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> bool:
        if not self.webhook_url:
            logger.warning("Discord webhook URL not configured — skipping notification")
            return False
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return True
            logger.error("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("Discord webhook request failed: %s", exc)
            return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _to_iso_utc(ts: str) -> str:
    """
    Normalise a local datetime string (from SQLite) to a UTC ISO-8601 string
    that Discord's embed timestamp field accepts.
    """
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            # Assume local time (JST = UTC+9); just mark as UTC for embed display
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()
