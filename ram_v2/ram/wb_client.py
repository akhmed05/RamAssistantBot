"""Клиент официального API Wildberries. Сейчас — только чтение.

Методы (проверены по dev.wildberries.ru, октябрь 2026):
  * Отчёт о реализации: GET statistics-api /api/v5/supplier/reportDetailByPeriod
    лимит 1 запрос в минуту, постранично через rrdid, 204 = нет данных.
  * Список кампаний:   GET advert-api /adv/v1/promotion/count
  * Статистика рекламы: GET advert-api /adv/v3/fullstats (до 50 кампаний,
    период до 31 дня, 3 запроса в минуту, кампании в статусах 7, 9, 11).
"""
from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any, Awaitable, Callable

import httpx

STAT_HOST = "https://statistics-api.wildberries.ru"
ADV_HOST = "https://advert-api.wildberries.ru"

REPORT_PATH = "/api/v5/supplier/reportDetailByPeriod"
PROMO_COUNT_PATH = "/adv/v1/promotion/count"
FULLSTATS_PATH = "/adv/v3/fullstats"

# Разрешены только эти адреса и только GET. Записи в WB этот клиент не делает.
ALLOWED = {
    STAT_HOST + REPORT_PATH: 61.0,       # минимальный интервал между запросами, сек
    ADV_HOST + PROMO_COUNT_PATH: 0.3,
    ADV_HOST + FULLSTATS_PATH: 21.0,
}

REPORT_PAGE = 100_000
STATS_STATUSES = {7, 9, 11}  # завершена, активна, на паузе


class WBError(Exception):
    """Понятная пользователю ошибка WB API (без тела ответа и токена)."""


class WBClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not token:
            raise WBError("Не задан токен WB для этого кабинета.")
        self._token = token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._clock = clock
        self._last_call: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        if url not in ALLOWED:
            raise WBError("Адрес не разрешён.")
        interval = ALLOWED[url]
        for attempt in range(3):
            last = self._last_call.get(url)
            if last is not None:
                wait = interval - (self._clock() - last)
                if wait > 0:
                    await self._sleep(wait)
            self._last_call[url] = self._clock()
            try:
                resp = await self._client.get(
                    url, params=params, headers={"Authorization": self._token}
                )
            except httpx.HTTPError as exc:
                if attempt < 2:
                    await self._sleep(5)
                    continue
                raise WBError(f"WB не отвечает ({type(exc).__name__}). Попробуйте позже.") from None
            if resp.status_code == 204:
                return []
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt < 2:
                retry = resp.headers.get("X-Ratelimit-Retry") or resp.headers.get("Retry-After") or "60"
                try:
                    delay = min(float(retry), 120.0)
                except ValueError:
                    delay = 60.0
                await self._sleep(delay)
                continue
            if resp.status_code in (401, 403):
                raise WBError(
                    "WB отклонил токен. Проверьте, что токен действующий и в нём "
                    "отмечены категории «Статистика» и «Продвижение»."
                )
            if resp.status_code >= 500 and attempt < 2:
                await self._sleep(10)
                continue
            raise WBError(f"WB вернул ошибку {resp.status_code}.")
        raise WBError("WB ограничил частоту запросов. Попробуйте через пару минут.")

    # --- Отчёт о реализации ---
    async def realization_report(self, date_from: date, date_to: date) -> list[dict]:
        rows: list[dict] = []
        rrdid = 0
        while True:
            page = await self._get(
                STAT_HOST + REPORT_PATH,
                {
                    "dateFrom": date_from.isoformat(),
                    "dateTo": date_to.isoformat(),
                    "limit": REPORT_PAGE,
                    "rrdid": rrdid,
                    "period": "daily",
                },
            )
            if not page:
                break
            rows.extend(page)
            if len(page) < REPORT_PAGE:
                break
            rrdid = int(page[-1].get("rrd_id") or 0)
            if not rrdid:
                break
        return rows

    # --- Реклама: расходы по артикулам ---
    async def campaign_ids(self) -> list[int]:
        data = await self._get(ADV_HOST + PROMO_COUNT_PATH, {})
        ids: list[int] = []
        for group in (data or {}).get("adverts") or []:
            if int(group.get("status", 0)) not in STATS_STATUSES:
                continue
            for adv in group.get("advert_list") or []:
                if adv.get("advertId"):
                    ids.append(int(adv["advertId"]))
        return sorted(set(ids))

    async def ad_spend_by_article(self, date_from: date, date_to: date) -> dict[int, float]:
        if (date_to - date_from).days > 30:
            raise WBError("Статистика рекламы доступна максимум за 31 день.")
        spend: dict[int, float] = {}
        ids = await self.campaign_ids()
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            try:
                data = await self._get(
                    ADV_HOST + FULLSTATS_PATH,
                    {
                        "ids": ",".join(map(str, chunk)),
                        "beginDate": date_from.isoformat(),
                        "endDate": date_to.isoformat(),
                    },
                )
            except WBError as exc:
                # 400 бывает, если у кампаний нет статистики за период
                if "ошибку 400" in str(exc):
                    continue
                raise
            for camp in data or []:
                for day in camp.get("days") or []:
                    for app in day.get("apps") or []:
                        for nm in app.get("nms") or []:
                            nm_id = int(nm.get("nmId") or 0)
                            if nm_id:
                                spend[nm_id] = spend.get(nm_id, 0.0) + float(nm.get("sum") or 0)
        return spend
