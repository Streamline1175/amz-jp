"""
Proxy rotation manager.

Supported proxy formats in proxies.txt (one per line):
  host:port
  user:pass@host:port
  http://host:port
  http://user:pass@host:port
  socks5://user:pass@host:port

Lines starting with # are treated as comments.
"""

import logging
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ProxyManager:
    def __init__(
        self,
        proxies: Optional[list[str]] = None,
        proxy_file: Optional[str] = None,
    ):
        self._proxies: list[str] = []
        self._bad: set[str] = set()

        if proxies:
            self._proxies = [self._normalise(p) for p in proxies]
        elif proxy_file:
            self._load_file(proxy_file)

    # ------------------------------------------------------------------

    def get_proxy(self) -> Optional[str]:
        """Return a random proxy that hasn't been flagged as bad."""
        available = [p for p in self._proxies if p not in self._bad]
        if not available:
            # All proxies are bad — reset the bad set and try again
            if self._bad:
                logger.warning("All proxies were marked bad; resetting bad list")
                self._bad.clear()
                available = self._proxies
        return random.choice(available) if available else None

    def mark_bad(self, proxy: Optional[str]) -> None:
        if proxy:
            self._bad.add(proxy)
            logger.warning("Proxy marked bad: %s", proxy)

    @property
    def count(self) -> int:
        return len(self._proxies)

    # ------------------------------------------------------------------

    def _load_file(self, filepath: str) -> None:
        path = Path(filepath)
        if not path.exists():
            logger.warning("Proxy file not found: %s", filepath)
            return
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self._proxies.append(self._normalise(line))
        logger.info("Loaded %d proxies from %s", len(self._proxies), filepath)

    @staticmethod
    def _normalise(proxy: str) -> str:
        """Ensure the proxy string has a scheme prefix."""
        if "://" not in proxy:
            return f"http://{proxy}"
        return proxy
