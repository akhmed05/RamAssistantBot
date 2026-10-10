import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ForceReply, ReplyKeyboardMarkup, KeyboardButton
from openai import AsyncOpenAI, OpenAIError, NotFoundError

from config import BOT_TOKEN, BOT_PROXY, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MAX_TOKENS, RAM_DB_PATH
from ram_ai import Store, SafeError, reserve_ai
from ram_telegram import RamTelegram, authorized
from handlers import (
    handle_youtube_summary, handle_google_form_submission, is_youtube_url,
)

logger = logging.getLogger(__name__)
_store = None


def get_store():
    global _store
    if _store is None:
        _store = Store(RAM_DB_PATH)
    return _store


AI_PROMPT = "Напиши свой вопрос:"
FORM_PROMPT = "Напиши заявку в формате: Имя / Телефон / Комментарий"
PDF_PATH = Path(__file__).resolve().parent / "media" / "info.pdf"

main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(
    KeyboardButton("📹 YouTube-саммари"),
    KeyboardButton("💬 Задать вопрос AI"),
).add(
    KeyboardButton("📊 Отправить заявку"),
    KeyboardButton("📎 Получить PDF"),
).add(KeyboardButton("🤖 RAM AI"))


def is_prompt_reply(message: types.Message, prompt: str, bot_id: Optional[int] = None) -> bool:
    reply = message.reply_to_message
    return bool(
        message.text and reply and reply.text == prompt
        and reply.from_user and reply.from_user.is_bot
        and (bot_id is None or reply.from_user.id == bot_id)
    )


async def reply_text(message: types.Message, text: str):
    # Stay within Telegram's limit even when text contains UTF-16 surrogate pairs.
    chunk, length = [], 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if length + width > 4096:
            await message.reply("".join(chunk))
            chunk, length = [], 0
        chunk.append(character)
        length += width
    if chunk:
        await message.reply("".join(chunk))


async def send_welcome(message: types.Message):
    await message.answer(
        "Ас-саляму алейкум! Я RAM AI. Выбери опцию:",
        reply_markup=main_menu,
    )


async def youtube_summary_prompt(message: types.Message):
    await message.reply("Вставь ссылку на YouTube-видео:", reply_markup=ForceReply(selective=True))


async def youtube_summary_handler(message: types.Message):
    summary = handle_youtube_summary(message.text)
    await reply_text(message, summary or "Не удалось получить саммари.")


async def ask_ai_prompt(message: types.Message):
    await message.reply(AI_PROMPT, reply_markup=ForceReply(selective=True))


async def _ask_ai_handler(message: types.Message):
    if not message.text:
        await message.reply("Напиши вопрос текстом.")
        return "not_sent"
    if not OPENAI_API_KEY:
        await message.reply("AI пока не настроен. Обратитесь к администратору.")
        return "not_sent"
    if not authorized(message):
        await message.reply("Доступ закрыт.")
        return "not_sent"
    if not OPENAI_MODEL:
        await message.reply("Задайте OPENAI_MODEL в Railway: модель, доступную вашему проекту OpenAI.")
        return "not_sent"
    if len(message.text.encode('utf-8')) > 16000:
        await message.reply("Вопрос слишком длинный. Сократите до 16000 байт UTF-8.")
        return "not_sent"
    try:
        reserve_ai(get_store(), message.text, OPENAI_MAX_TOKENS)
    except SafeError as exc:
        await message.reply(str(exc))
        return "budget_blocked"
    try:
        async with AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=30, max_retries=0) as client:
            response = await client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=OPENAI_MAX_TOKENS,
                messages=[{"role": "user", "content": message.text}],
            )
        text = response.choices[0].message.content if response.choices else None
    except NotFoundError:
        logger.warning("OpenAI request failed: NotFoundError")
        await message.reply("OpenAI 404: проверьте OPENAI_MODEL и доступ к модели в вашем проекте. Автоподмена модели отключена.")
        return "api_404"
    except OpenAIError as exc:
        # Request URLs and exception messages can contain sensitive data.
        logger.warning("OpenAI request failed: %s", type(exc).__name__)
        await message.reply("Не удалось получить ответ AI. Попробуй позже.")
        return "api_failed"
    if not text or not text.strip():
        await message.reply("AI вернул пустой ответ. Попробуй переформулировать вопрос.")
        return "empty_answer"
    await reply_text(message, text)

    return "done"


async def ask_ai_handler(message: types.Message):
    # Do not persist raw prompts or answers: they can contain secrets/business data.
    if not authorized(message):
        await message.reply("Доступ закрыт.")
        return
    store = get_store()
    key = f"ai:{message.chat.id}:{message.message_id}"
    previous = store.claim(key, message.from_user.id, "ai", "openai")
    if previous is not None:
        await message.reply(previous)
        return
    try:
        status = await _ask_ai_handler(message)
    except Exception:
        store.finish(key, message.from_user.id, "ai", "openai", "failed",
                     "AI запрос завершился ошибкой. Повторное выполнение заблокировано.")
        await message.reply("Внутренняя ошибка AI. Автоповтора нет.")
        return
    store.finish(key, message.from_user.id, "ai", "openai", status,
                 "AI запрос уже обработан. Повторное выполнение заблокировано.")


async def google_form_prompt(message: types.Message):
    await message.reply(FORM_PROMPT, reply_markup=ForceReply(selective=True))


async def google_form_handler(message: types.Message):
    store = get_store()
    owner = message.from_user.id
    key = f"form:{message.chat.id}:{message.message_id}"
    previous = store.claim(key, owner, "legacy", "google_form")
    if previous is not None:
        await message.reply(previous)
        return
    try:
        result = await asyncio.to_thread(handle_google_form_submission, message.text)
    except Exception:
        result = False
    response = "✅ Заявка отправлена!" if result else "❌ Ошибка при отправке."
    store.finish(key, owner, "legacy", "google_form", "done" if result else "failed_or_unknown", response)
    await message.reply(response)


async def send_pdf(message: types.Message):
    try:
        document = PDF_PATH.open("rb")
    except OSError:
        await message.reply("PDF пока не загружен.")
        return
    with document:
        await message.reply_document(document)


def create_dispatcher(token: Optional[str] = None) -> Dispatcher:
    token = token if token is not None else BOT_TOKEN
    if not token:
        raise ValueError("Задайте BOT_TOKEN в переменных окружения.")
    if not 1 <= OPENAI_MAX_TOKENS <= 4096:
        raise ValueError("OPENAI_MAX_TOKENS должен быть от 1 до 4096.")
    bot = Bot(token=token, proxy=BOT_PROXY)
    dp = Dispatcher(bot)
    dp.register_message_handler(send_welcome, commands=["start"])
    dp.register_message_handler(youtube_summary_prompt, lambda m: m.text == "📹 YouTube-саммари")
    dp.register_message_handler(youtube_summary_handler, lambda m: is_youtube_url(m.text))
    dp.register_message_handler(ask_ai_prompt, lambda m: m.text == "💬 Задать вопрос AI")
    dp.register_message_handler(ask_ai_handler, lambda m: is_prompt_reply(m, AI_PROMPT, bot.id))
    dp.register_message_handler(google_form_prompt, lambda m: m.text == "📊 Отправить заявку")
    dp.register_message_handler(google_form_handler, lambda m: is_prompt_reply(m, FORM_PROMPT, bot.id))
    dp.register_message_handler(send_pdf, lambda m: m.text == "📎 Получить PDF")
    RamTelegram(get_store()).register(dp)
    return dp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # httpx INFO contains full Telegram URLs (and potentially bot tokens).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    executor.start_polling(create_dispatcher(), skip_updates=False)
