"""外部新闻 / 情绪源（可配置，失败绝不阻塞主流程）。

FR-1.1 要求至少支持一个外部新闻源（默认 CryptoPanic）。未配置 token 或请求
失败时返回空列表，标签服务继续按行情数据生成标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from aiquant.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class NewsItem:
    title: str
    published_at: str
    url: str | None = None
    source: str = "cryptopanic"
    currencies: tuple[str, ...] = ()

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "title": self.title[:200],
            "published_at": self.published_at,
            "url": self.url,
            "currencies": list(self.currencies),
        }


class NewsClient:
    def __init__(
        self,
        enabled: bool = False,
        url: str = "https://cryptopanic.com/api/v1/posts/",
        token: str = "",
        timeout_sec: float = 10.0,
        max_items: int = 20,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.enabled = enabled
        self.url = url
        self.token = token
        self.timeout_sec = timeout_sec
        self.max_items = max_items
        self._transport = transport

    # ---------------------------------------------------------------- 公开
    def fetch(self, pair: str) -> list[NewsItem]:
        """失败返回空列表——新闻是可选输入，不得阻塞标签生成。"""
        if not self.enabled:
            return []
        if not self.token:
            log.warning("news: enabled 但 NEWS_API_TOKEN 为空，跳过")
            return []
        currency = pair.split("/")[0]
        params = {
            "auth_token": self.token,
            "currencies": currency,
            "public": "true",
            "kind": "news",
        }
        try:
            with httpx.Client(timeout=self.timeout_sec, transport=self._transport) as client:
                resp = client.get(self.url, params=params)
            if resp.status_code >= 400:
                log.warning("news: HTTP %s", resp.status_code)
                return []
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("news: 请求失败 %s", type(exc).__name__)
            return []
        return self._parse(payload)

    # ---------------------------------------------------------------- 解析
    def _parse(self, payload: Any) -> list[NewsItem]:
        if not isinstance(payload, dict):
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        items: list[NewsItem] = []
        for entry in results[: self.max_items]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            published = str(entry.get("published_at") or entry.get("created_at") or "")
            if not published:
                published = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            currencies = tuple(
                str(c.get("code", "")).upper()
                for c in entry.get("currencies") or []
                if isinstance(c, dict) and c.get("code")
            )
            items.append(
                NewsItem(
                    title=title[:200],
                    published_at=published,
                    url=str(entry.get("url")) if entry.get("url") else None,
                    currencies=currencies,
                )
            )
        return items
