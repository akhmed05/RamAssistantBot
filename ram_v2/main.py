"""Запуск: python main.py (Railway: Root Directory = /ram_v2)."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from ram.bot import build_router
from ram.config import load_settings
from ram.db import Storage
from ram.service import Manager


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.bot_token:
        sys.exit("Не задан BOT_TOKEN")
    if not settings.owner_ids:
        sys.exit("Не задан RAM_OWNER_IDS (ваш Telegram ID) — без него бот никого не пустит")
    storage = Storage(settings.db_path)
    mgr = Manager(settings, storage)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(mgr))
    await bot.set_my_commands([
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="tax", description="Ставка налога, например /tax 6"),
    ])
    logging.info("RAM v2 запущен. Кабинеты: %s",
                 ", ".join(f"{c.name}={'ok' if c.configured else 'нет токена'}" for c in settings.cabinets.values()))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
