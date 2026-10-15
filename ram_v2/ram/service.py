"""Логика менеджера без Telegram: загрузка данных WB и расчёт отчётов."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable


from .config import Cabinet, Settings
from .db import Storage
from .reports import summary_text, summary_xlsx
from .unit_economics import Summary, calculate
from .wb_client import WBClient, WBError

MSK = timezone(timedelta(hours=3))
CACHE_TTL = 20 * 60


@dataclass
class UnitReport:
    text: str
    xlsx: bytes
    filename: str
    summary: Summary


class Manager:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        *,
        client_factory: Callable[[str], WBClient] | None = None,
        today: Callable[[], date] | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self._client_factory = client_factory or (lambda token: WBClient(token))
        self._today = today or (lambda: datetime.now(MSK).date())
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[tuple, tuple[float, list[dict], dict[int, float] | None]] = {}

    def cabinet(self, key: str) -> Cabinet:
        return self.settings.cabinets[key]

    def is_busy(self, key: str) -> bool:
        lock = self._locks.get(key)
        return bool(lock and lock.locked())

    def period(self, days: int) -> tuple[date, date]:
        d2 = self._today() - timedelta(days=1)  # отчёт WB готов на следующий день
        return d2 - timedelta(days=days - 1), d2

    async def _load(self, cab: Cabinet, d1: date, d2: date) -> tuple[list[dict], dict[int, float] | None, str | None]:
        key = (cab.key, d1, d2)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1], hit[2], None
        client = self._client_factory(cab.token)
        ads_note = None
        try:
            rows = await client.realization_report(d1, d2)
            try:
                ads = await client.ad_spend_by_article(d1, d2)
            except WBError as exc:
                ads, ads_note = None, str(exc)
        finally:
            await client.aclose()
        self._cache[key] = (time.monotonic(), rows, ads)
        return rows, ads, ads_note

    async def unit_report(self, cab_key: str, days: int) -> UnitReport:
        cab = self.cabinet(cab_key)
        if not cab.configured:
            raise WBError(f"Для «{cab.name}» не задан токен WB (переменная RAM_WB_{cab.key}_TOKEN).")
        lock = self._locks.setdefault(cab.key, asyncio.Lock())
        async with lock:
            d1, d2 = self.period(days)
            rows, ads, ads_note = await self._load(cab, d1, d2)
            s = calculate(
                rows,
                costs=self.storage.get_costs(cab.key),
                ad_spend=ads,
                tax_percent=self.storage.tax_rate(cab.key),
            )
            self.storage.remember_articles(
                cab.key, [(a.nm_id, a.vendor_code, a.title) for a in s.articles.values()]
            )
            self.storage.log(cab.key, f"Юнит-экономика {d1}–{d2}: {len(rows)} строк, {len(s.articles)} арт.")
            if not s.articles:
                text = (
                    f"<b>{cab.name}</b>: за {d1:%d.%m} – {d2:%d.%m.%Y} в отчёте WB нет продаж и расходов по артикулам."
                )
                if s.shared_costs:
                    text += f"\nОбщие расходы кабинета за период: {s.shared_costs:,.0f} ₽".replace(",", " ")
                return UnitReport(text, b"", "", s)
            text = summary_text(s, cab.name, d1, d2)
            if ads_note:
                text += f"\n\n(Реклама: {ads_note})"
            return UnitReport(
                text,
                summary_xlsx(s, cab.name, d1, d2),
                f"unit_{cab.key}_{d1:%Y%m%d}_{d2:%Y%m%d}.xlsx",
                s,
            )
