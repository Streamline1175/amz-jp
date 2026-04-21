"""
Configuration loaded from environment variables (via a .env file).
See .env.example for all available settings.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Comma-separated list of ASINs to monitor
    asins: list[str] = field(default_factory=list)

    # Random poll interval range (seconds).  Defaults: 5–15 minutes.
    interval_min: int = 300
    interval_max: int = 900

    # Per-request delay between consecutive HTTP calls (seconds)
    request_delay_min: float = 3.0
    request_delay_max: float = 7.0

    # SQLite database path
    db_path: str = "amz_jp.db"

    # Discord incoming-webhook URL
    discord_webhook_url: str = ""

    # Send a startup ping when the monitor first launches
    discord_startup_ping: bool = True

    # Path to proxy list file (one proxy per line)
    proxy_file: str = "proxies.txt"

    # Logging level: DEBUG | INFO | WARNING | ERROR
    log_level: str = "INFO"

    # Log file path (empty = stdout only)
    log_file: str = "amz_monitor.log"

    @classmethod
    def from_env(cls) -> "Config":
        raw_asins = os.getenv("ASINS", "")
        asins = [a.strip() for a in raw_asins.split(",") if a.strip()]

        return cls(
            asins=asins,
            interval_min=int(os.getenv("INTERVAL_MIN", 300)),
            interval_max=int(os.getenv("INTERVAL_MAX", 900)),
            request_delay_min=float(os.getenv("REQUEST_DELAY_MIN", 3.0)),
            request_delay_max=float(os.getenv("REQUEST_DELAY_MAX", 7.0)),
            db_path=os.getenv("DB_PATH", "amz_jp.db"),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            discord_startup_ping=os.getenv("DISCORD_STARTUP_PING", "true").lower() == "true",
            proxy_file=os.getenv("PROXY_FILE", "proxies.txt"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE", "amz_monitor.log"),
        )
