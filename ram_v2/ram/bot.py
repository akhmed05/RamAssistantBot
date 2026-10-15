"""Telegram-интерфейс менеджера (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .reports import CostFileError, cost_template_xlsx, parse_cost_file
from .service import MSK, Manager
from .wb_client import WBError

log = logging.getLogger("ram.bot")
MAX_FILE = 5 * 1024 * 1024


class OwnerOnly(BaseMiddleware):
    """Пропускает только владельцев из RAM_OWNER_IDS и только в личных чатах."""

    def __init__(self, owner_ids: frozenset[int]):
        self.owner_ids = owner_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if user is None or user.id not in self.owner_ids:
            if isinstance(event, Message) and chat is not None and chat.type == "private":
                await event.answer("Это закрытый бот. Доступ только для владельца.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None
        if chat is not None and chat.type != "private":
            return None
        return await handler(event, data)


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu(mgr: Manager, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cab = mgr.cabinet(mgr.storage.get_cabinet(user_id))
    tax = mgr.storage.tax_rate(cab.key)
    costs = len(mgr.storage.get_costs(cab.key))
    text = (
        "<b>RAM — менеджер Wildberries</b>\n\n"
        f"Кабинет: <b>{cab.name}</b>{'' if cab.configured else ' (токен не задан)'}\n"
        f"Себестоимость: {costs} арт.\n"
        f"Налог: {'не указан' if tax is None else f'{tax:g}%'}"
    )
    kb = _kb([
        [("📊 Юнит-экономика", "ue")],
        [("💰 Себестоимость", "cost"), ("🔁 Сменить кабинет", "cab")],
        [("ℹ️ Статус", "status")],
    ])
    return text, kb


def build_router(mgr: Manager) -> Router:
    r = Router()
    r.message.outer_middleware(OwnerOnly(mgr.settings.owner_ids))
    r.callback_query.outer_middleware(OwnerOnly(mgr.settings.owner_ids))
    tasks: set[asyncio.Task] = set()

    @r.message(CommandStart())
    @r.message(Command("menu"))
    async def start(m: Message) -> None:
        text, kb = main_menu(mgr, m.from_user.id)
        await m.answer(text, reply_markup=kb)

    @r.callback_query(F.data == "menu")
    async def menu_cb(c: CallbackQuery) -> None:
        text, kb = main_menu(mgr, c.from_user.id)
        await c.message.edit_text(text, reply_markup=kb)
        await c.answer()

    # --- кабинеты ---
    @r.callback_query(F.data == "cab")
    async def choose_cab(c: CallbackQuery) -> None:
        rows = [[(f"{'✅ ' if k == mgr.storage.get_cabinet(c.from_user.id) else ''}{cab.name}", f"cab:{k}")]
                for k, cab in mgr.settings.cabinets.items()]
        rows.append([("← Назад", "menu")])
        await c.message.edit_text("Выберите кабинет:", reply_markup=_kb(rows))
        await c.answer()

    @r.callback_query(F.data.startswith("cab:"))
    async def set_cab(c: CallbackQuery) -> None:
        key = c.data.split(":", 1)[1]
        if key in mgr.settings.cabinets:
            mgr.storage.set_cabinet(c.from_user.id, key)
        text, kb = main_menu(mgr, c.from_user.id)
        await c.message.edit_text(text, reply_markup=kb)
        await c.answer(f"Кабинет: {mgr.cabinet(key).name}" if key in mgr.settings.cabinets else "")

    # --- юнит-экономика ---
    @r.callback_query(F.data == "ue")
    async def ue_period(c: CallbackQuery) -> None:
        await c.message.edit_text(
            "За какой период посчитать? (по вчерашний день включительно)",
            reply_markup=_kb([[("7 дней", "ue:7"), ("14 дней", "ue:14"), ("30 дней", "ue:30")], [("← Назад", "menu")]]),
        )
        await c.answer()

    @r.callback_query(F.data.startswith("ue:"))
    async def ue_run(c: CallbackQuery, bot: Bot) -> None:
        days = int(c.data.split(":", 1)[1])
        if days not in (7, 14, 30):
            await c.answer()
            return
        cab_key = mgr.storage.get_cabinet(c.from_user.id)
        if mgr.is_busy(cab_key):
            await c.answer("Уже считаю по этому кабинету, подождите", show_alert=True)
            return
        await c.answer()
        await c.message.edit_text(
            f"⏳ Загружаю отчёт WB по «{mgr.cabinet(cab_key).name}» за {days} дн. "
            "Это может занять 1–3 минуты (WB разрешает 1 запрос в минуту)."
        )
        chat_id = c.message.chat.id

        async def job() -> None:
            try:
                rep = await mgr.unit_report(cab_key, days)
                await bot.send_message(chat_id, rep.text)
                if rep.xlsx:
                    await bot.send_document(chat_id, BufferedInputFile(rep.xlsx, rep.filename))
            except WBError as exc:
                await bot.send_message(chat_id, f"❌ {exc}")
            except Exception:
                log.exception("unit report failed")
                await bot.send_message(chat_id, "❌ Не удалось посчитать отчёт. Ошибка записана в журнал сервера.")
            text, kb = main_menu(mgr, c.from_user.id)
            await bot.send_message(chat_id, text, reply_markup=kb)

        t = asyncio.create_task(job())
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    # --- себестоимость ---
    @r.callback_query(F.data == "cost")
    async def cost_menu(c: CallbackQuery) -> None:
        cab = mgr.cabinet(mgr.storage.get_cabinet(c.from_user.id))
        n = len(mgr.storage.get_costs(cab.key))
        await c.message.edit_text(
            f"<b>Себестоимость · {cab.name}</b>\nЗаполнено: {n} арт.\n\n"
            "1. Скачайте шаблон (в нём артикулы из последних отчётов).\n"
            "2. Заполните колонку «Себестоимость, ₽ за 1 шт» — полная стоимость товара "
            "до склада WB: закупка/пошив, упаковка, доставка, фулфилмент.\n"
            "3. Пришлите файл сюда в чат.\n\n"
            "Совет: сначала постройте юнит-экономику за 30 дней — тогда шаблон заполнится артикулами.",
            reply_markup=_kb([[("📥 Скачать шаблон", "cost:tpl")], [("← Назад", "menu")]]),
        )
        await c.answer()

    @r.callback_query(F.data == "cost:tpl")
    async def cost_tpl(c: CallbackQuery) -> None:
        cab = mgr.cabinet(mgr.storage.get_cabinet(c.from_user.id))
        data = cost_template_xlsx(mgr.storage.known_articles(cab.key), mgr.storage.get_cost_rows(cab.key))
        await c.message.answer_document(
            BufferedInputFile(data, f"sebestoimost_{cab.key}.xlsx"),
            caption=f"Шаблон себестоимости · {cab.name}. Заполните и пришлите обратно.",
        )
        await c.answer()

    @r.message(F.document)
    async def cost_upload(m: Message, bot: Bot) -> None:
        doc = m.document
        cab = mgr.cabinet(mgr.storage.get_cabinet(m.from_user.id))
        if (doc.file_size or 0) > MAX_FILE:
            await m.answer("Файл слишком большой (больше 5 МБ).")
            return
        buf = await bot.download(doc)
        try:
            items, warnings = parse_cost_file(buf.read(), doc.file_name or "")
        except CostFileError as exc:
            await m.answer(f"❌ {exc}")
            return
        n = mgr.storage.save_costs(cab.key, items)
        mgr.storage.log(cab.key, f"Загружена себестоимость: {n} арт.")
        text = f"✅ Себестоимость сохранена для «{cab.name}»: {n} арт."
        if warnings:
            text += "\n\nПропущено:\n" + "\n".join(warnings[:10])
            if len(warnings) > 10:
                text += f"\n…и ещё {len(warnings) - 10}"
        await m.answer(text)

    # --- налог ---
    @r.message(Command("tax"))
    async def tax(m: Message, command: CommandObject) -> None:
        cab = mgr.cabinet(mgr.storage.get_cabinet(m.from_user.id))
        arg = (command.args or "").strip().replace(",", ".").rstrip("%")
        try:
            value = float(arg)
            if not 0 <= value <= 50:
                raise ValueError
        except ValueError:
            await m.answer("Укажите ставку налога от выручки в процентах, например: /tax 6\n(0 — если налог не нужен)")
            return
        mgr.storage.set_setting(cab.key, "tax_percent", f"{value:g}")
        await m.answer(f"✅ Налог для «{cab.name}»: {value:g}% от выручки.")

    # --- статус ---
    @r.callback_query(F.data == "status")
    async def status(c: CallbackQuery) -> None:
        lines = ["<b>Статус</b>"]
        for cab in mgr.settings.cabinets.values():
            lines.append(f"• {cab.name}: {'токен задан' if cab.configured else 'токен НЕ задан'}")
        recent = mgr.storage.recent_log(5)
        if recent:
            lines += ["", "<b>Последние действия:</b>"]
            for ts, cab, event in recent:
                name = mgr.cabinet(cab).name if cab in mgr.settings.cabinets else cab
                lines.append(f"{datetime.fromtimestamp(ts, MSK):%d.%m %H:%M} · {name}: {event}")
        await c.message.edit_text("\n".join(lines), reply_markup=_kb([[("← Назад", "menu")]]))
        await c.answer()

    return r
