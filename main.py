"""
Amazon JP stock monitor — entry point.

Usage:
  python main.py                        # run the monitor loop
  python main.py --test-asin B0XXXXXXX  # one-shot scrape + print, no DB writes
  python main.py --list-sellers         # dump all known sellers from the DB

Environment is configured via .env (see .env.example).
"""

import argparse
import json
import logging
import random
import signal
import sys
import time
from datetime import datetime

from config import Config
from scraper.amazon import AmazonJPScraper
from scraper.db import Database
from scraper.proxy import ProxyManager
from bot.notifier import DiscordNotifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # Quieten noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Core monitoring logic
# ---------------------------------------------------------------------------

def _process_offers(
    db: Database,
    offers: list[dict],
    asin: str,
) -> list[dict]:
    """
    Diff *offers* against the last known state stored in *db*.
    Upserts sellers, saves offer snapshots, and returns a list of
    restock-event dicts ready to be queued for Discord notification.
    """
    restocks: list[dict] = []

    for offer in offers:
        seller_id   = offer["seller_id"]
        seller_name = offer.get("seller_name")
        price_jpy   = offer.get("price_jpy")
        condition   = offer.get("condition", "new")
        fulfillment = offer.get("fulfillment", "FBM")
        in_stock    = bool(offer.get("in_stock", True))

        db.upsert_seller(seller_id, seller_name)

        last = db.get_last_offer(asin, seller_id)
        is_new_seller   = last is None
        was_out_of_stock = last is not None and not last["in_stock"]

        db.save_offer(asin, seller_id, price_jpy, condition, fulfillment, in_stock)

        if in_stock and (is_new_seller or was_out_of_stock):
            logger.info(
                "RESTOCK  asin=%-14s  seller=%-20s  name=%s  price=%s",
                asin, seller_id, seller_name, f"¥{price_jpy:,}" if price_jpy else "N/A",
            )
            restock_id = db.record_restock(asin, seller_id, price_jpy, condition, fulfillment)
            restocks.append({
                "id":           restock_id,
                "asin":         asin,
                "seller_id":    seller_id,
                "seller_name":  seller_name or "Unknown",
                "product_name": (db.get_product(asin) or {}).get("name") or f"ASIN {asin}",
                "price_jpy":    price_jpy,
                "condition":    condition,
                "fulfillment":  fulfillment,
                "is_new_seller": is_new_seller,
                "detected_at":  datetime.now().isoformat(),
            })

    return restocks


def _notify_pending(db: Database, notifier: DiscordNotifier) -> None:
    for event in db.pending_notifications():
        success = notifier.send_restock(event)
        if success:
            db.mark_notified(event["id"])
        time.sleep(1.0)  # respect Discord rate-limit (5 req / 2 s per webhook)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def run_monitor(config: Config) -> None:
    if not config.asins:
        logger.error("No ASINs configured. Add ASINS=B0XXX,B0YYY to your .env")
        sys.exit(1)

    proxy_mgr  = ProxyManager(proxy_file=config.proxy_file) if config.proxy_file else None
    scraper    = AmazonJPScraper(
        proxy_manager=proxy_mgr,
        request_delay=(config.request_delay_min, config.request_delay_max),
    )
    db         = Database(db_path=config.db_path)
    notifier   = DiscordNotifier(webhook_url=config.discord_webhook_url)

    if proxy_mgr:
        logger.info("Loaded %d proxies", proxy_mgr.count)

    # Seed product names
    for asin in config.asins:
        logger.info("Resolving product name for ASIN %s …", asin)
        name = scraper.get_product_name(asin)
        db.upsert_product(asin, name, url=f"https://www.amazon.co.jp/dp/{asin}/")
        logger.info("  → %s", name or "(not found)")
        time.sleep(random.uniform(config.request_delay_min, config.request_delay_max))

    if config.discord_startup_ping and config.discord_webhook_url:
        notifier.send_startup(config.asins)

    logger.info(
        "Monitor running — %d ASIN(s), poll interval %d–%d s",
        len(config.asins), config.interval_min, config.interval_max,
    )

    # Graceful shutdown on SIGTERM (systemd stop / kill)
    _running = [True]
    def _handle_sigterm(sig, frame):   # noqa: ANN001
        logger.info("Received SIGTERM — shutting down cleanly")
        _running[0] = False
    signal.signal(signal.SIGTERM, _handle_sigterm)

    while _running[0]:
        cycle_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("── Scrape cycle started at %s ──", cycle_ts)

        for asin in config.asins:
            if not _running[0]:
                break
            try:
                logger.info("Fetching offers  asin=%s", asin)
                offers = scraper.fetch_offers(asin)
                logger.info("  %d offer(s) returned", len(offers))

                _process_offers(db, offers, asin)

                if len(config.asins) > 1:
                    time.sleep(random.uniform(5.0, 12.0))

            except Exception:
                logger.exception("Unhandled error for ASIN %s", asin)

        if config.discord_webhook_url:
            _notify_pending(db, notifier)

        sleep_secs = random.randint(config.interval_min, config.interval_max)
        wake_at    = datetime.fromtimestamp(time.time() + sleep_secs).strftime("%H:%M:%S")
        logger.info("Sleeping %d s — next cycle at %s", sleep_secs, wake_at)

        # Sleep in small increments so SIGTERM is handled promptly
        deadline = time.time() + sleep_secs
        while _running[0] and time.time() < deadline:
            time.sleep(min(5.0, deadline - time.time()))

    logger.info("Monitor stopped.")


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------

def cmd_test_asin(asin: str, config: Config) -> None:
    proxy_mgr = ProxyManager(proxy_file=config.proxy_file) if config.proxy_file else None
    scraper   = AmazonJPScraper(proxy_manager=proxy_mgr)
    logger.info("One-shot test for ASIN %s", asin)
    name   = scraper.get_product_name(asin)
    offers = scraper.fetch_offers(asin)
    print(f"\nProduct : {name}")
    print(f"Offers  : {len(offers)}\n")
    for o in offers:
        price = f"¥{o['price_jpy']:,}" if o.get("price_jpy") else "N/A"
        print(
            f"  OID={o['seller_id']:<20}  name={o['seller_name']:<30}  "
            f"price={price:<10}  cond={o['condition']:<12}  fulfil={o['fulfillment']}"
        )


def cmd_list_sellers(config: Config) -> None:
    db = Database(db_path=config.db_path)
    sellers = db.all_sellers()
    if not sellers:
        print("No sellers in database yet.")
        return
    print(f"{'OID':<22} {'Name':<35} {'First seen'}")
    print("-" * 75)
    for s in sellers:
        print(f"{s['seller_id']:<22} {(s['seller_name'] or ''):<35} {s['first_seen']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Amazon JP stock monitor")
    parser.add_argument("--test-asin",    metavar="ASIN", help="One-shot scrape for a single ASIN")
    parser.add_argument("--list-sellers", action="store_true", help="Print known sellers from DB")
    args = parser.parse_args()

    config = Config.from_env()
    _setup_logging(config.log_level, config.log_file)

    if args.test_asin:
        cmd_test_asin(args.test_asin, config)
    elif args.list_sellers:
        cmd_list_sellers(config)
    else:
        try:
            run_monitor(config)
        except KeyboardInterrupt:
            logger.info("Interrupted — bye")


if __name__ == "__main__":
    main()
