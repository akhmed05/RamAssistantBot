"""Настройки берутся только из переменных окружения (Railway → Variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cabinet:
    key: str          # "1" или "2"
    name: str         # как показывать в боте
    token: str        # токен WB API (категории: Статистика, Продвижение)

    @property
    def configured(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    cabinets: dict[str, Cabinet] = field(default_factory=dict)
    db_path: str = "data/ram.sqlite3"


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    cabinets = {}
    for key, default_name in (("1", "Кабинет 1"), ("2", "Кабинет 2")):
        cabinets[key] = Cabinet(
            key=key,
            name=env.get(f"RAM_WB_{key}_NAME", "").strip() or default_name,
            token=env.get(f"RAM_WB_{key}_TOKEN", "").strip(),
        )
    return Settings(
        bot_token=env.get("BOT_TOKEN", "").strip(),
        owner_ids=_parse_ids(env.get("RAM_OWNER_IDS", "")),
        cabinets=cabinets,
        db_path=env.get("RAM_DB_PATH", "").strip() or "data/ram.sqlite3",
    )
