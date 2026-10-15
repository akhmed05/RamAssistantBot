"""Простое хранилище SQLite: выбранный кабинет, себестоимость, налог, журнал."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    cabinet TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cost_price (
    cabinet TEXT NOT NULL,
    nm_id INTEGER NOT NULL,
    vendor_code TEXT NOT NULL DEFAULT '',
    cost REAL NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (cabinet, nm_id)
);
CREATE TABLE IF NOT EXISTS cabinet_setting (
    cabinet TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (cabinet, name)
);
CREATE TABLE IF NOT EXISTS known_article (
    cabinet TEXT NOT NULL,
    nm_id INTEGER NOT NULL,
    vendor_code TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cabinet, nm_id)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    cabinet TEXT NOT NULL,
    event TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- выбранный кабинет ---
    def get_cabinet(self, user_id: int, default: str = "1") -> str:
        row = self.conn.execute(
            "SELECT cabinet FROM user_state WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else default

    def set_cabinet(self, user_id: int, cabinet: str) -> None:
        self.conn.execute(
            "INSERT INTO user_state(user_id, cabinet) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET cabinet=excluded.cabinet",
            (user_id, cabinet),
        )
        self.conn.commit()

    # --- себестоимость ---
    def save_costs(self, cabinet: str, items: list[tuple[int, str, float]]) -> int:
        now = int(time.time())
        with self.conn:
            self.conn.executemany(
                "INSERT INTO cost_price(cabinet, nm_id, vendor_code, cost, updated_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(cabinet, nm_id) DO UPDATE SET "
                "vendor_code=excluded.vendor_code, cost=excluded.cost, updated_at=excluded.updated_at",
                [(cabinet, nm, vc, cost, now) for nm, vc, cost in items],
            )
        return len(items)

    def get_costs(self, cabinet: str) -> dict[int, float]:
        rows = self.conn.execute(
            "SELECT nm_id, cost FROM cost_price WHERE cabinet=?", (cabinet,)
        ).fetchall()
        return {int(nm): float(cost) for nm, cost in rows}

    def get_cost_rows(self, cabinet: str) -> dict[int, tuple[str, float]]:
        rows = self.conn.execute(
            "SELECT nm_id, vendor_code, cost FROM cost_price WHERE cabinet=?", (cabinet,)
        ).fetchall()
        return {int(nm): (vc, float(c)) for nm, vc, c in rows}

    # --- известные артикулы (для шаблона себестоимости) ---
    def remember_articles(self, cabinet: str, items: list[tuple[int, str, str]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO known_article(cabinet, nm_id, vendor_code, title) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(cabinet, nm_id) DO UPDATE SET "
                "vendor_code=CASE WHEN excluded.vendor_code<>'' THEN excluded.vendor_code ELSE vendor_code END, "
                "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE title END",
                [(cabinet, nm, vc, t) for nm, vc, t in items if nm],
            )

    def known_articles(self, cabinet: str) -> list[tuple[int, str, str]]:
        return [
            (int(nm), vc, t)
            for nm, vc, t in self.conn.execute(
                "SELECT nm_id, vendor_code, title FROM known_article WHERE cabinet=? ORDER BY vendor_code, nm_id",
                (cabinet,),
            ).fetchall()
        ]

    # --- настройки кабинета (налог) ---
    def get_setting(self, cabinet: str, name: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM cabinet_setting WHERE cabinet=? AND name=?", (cabinet, name)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, cabinet: str, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO cabinet_setting(cabinet, name, value) VALUES(?, ?, ?) "
            "ON CONFLICT(cabinet, name) DO UPDATE SET value=excluded.value",
            (cabinet, name, value),
        )
        self.conn.commit()

    def tax_rate(self, cabinet: str) -> float | None:
        raw = self.get_setting(cabinet, "tax_percent")
        return float(raw) if raw else None

    # --- журнал ---
    def log(self, cabinet: str, event: str) -> None:
        self.conn.execute(
            "INSERT INTO journal(ts, cabinet, event) VALUES(?, ?, ?)",
            (int(time.time()), cabinet, event[:500]),
        )
        self.conn.commit()

    def recent_log(self, limit: int = 10) -> list[tuple[int, str, str]]:
        return self.conn.execute(
            "SELECT ts, cabinet, event FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        with closing(self.conn):
            pass
