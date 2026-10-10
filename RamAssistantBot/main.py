import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ForceReply, ReplyKeyboardMarkup, KeyboardButton
from openai import AsyncOpenAI, OpenAIError

from config import BOT_TOKEN, BOT_PROXY, OPENAI_API_KEY
from handlers import (
    handle_youtube_summary, handle_google_form_submission, is_youtube_url,
)

logger = logging.getLogger(__name__)
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
)


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
        "Ас-саляму алейкум! Я RamAssistantBot. Выбери опцию:",
        reply_markup=main_menu,
    )


async def youtube_summary_prompt(message: types.Message):
    await message.reply("Вставь ссылку на YouTube-видео:", reply_markup=ForceReply(selective=True))


async def youtube_summary_handler(message: types.Message):
    summary = handle_youtube_summary(message.text)
    await reply_text(message, summary or "Не удалось получить саммари.")


async def ask_ai_prompt(message: types.Message):
    await message.reply(AI_PROMPT, reply_markup=ForceReply(selective=True))


async def ask_ai_handler(message: types.Message):
    if not message.text:
        await message.reply("Напиши вопрос текстом.")
        return
    if not OPENAI_API_KEY:
        await message.reply("AI пока не настроен. Обратитесь к администратору.")
        return
    try:
        async with AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=30, max_retries=1) as client:
            response = await client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": message.text}],
            )
        text = response.choices[0].message.content if response.choices else None
    except OpenAIError as exc:
        # Request URLs and exception messages can contain sensitive data.
        logger.warning("OpenAI request failed: %s", type(exc).__name__)
        await message.reply("Не удалось получить ответ AI. Попробуй позже.")
        return
    if not text or not text.strip():
        await message.reply("AI вернул пустой ответ. Попробуй переформулировать вопрос.")
        return
    await reply_text(message, text)


async def google_form_prompt(message: types.Message):
    await message.reply(FORM_PROMPT, reply_markup=ForceReply(selective=True))


async def google_form_handler(message: types.Message):
    result = await asyncio.to_thread(handle_google_form_submission, message.text)
    await message.reply("✅ Заявка отправлена!" if result else "❌ Ошибка при отправке.")


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
    return dp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    executor.start_polling(create_dispatcher(), skip_updates=True)
