import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import openai

from config import BOT_TOKEN, OPENAI_API_KEY
from handlers import handle_youtube_summary, handle_google_form_submission

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Главное меню
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(
    KeyboardButton("📹 YouTube-саммари"),
    KeyboardButton("💬 Задать вопрос AI")
).add(
    KeyboardButton("📊 Отправить заявку"),
    KeyboardButton("📎 Получить PDF")
)

@dp.message_handler(commands=["start"])
async def send_welcome(message: types.Message):
    await message.answer("Ас-саляму алейкум! Я RamAssistantBot. Выбери опцию:", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "📹 YouTube-саммари")
async def youtube_summary_prompt(message: types.Message):
    await message.reply("Вставь ссылку на YouTube-видео:")

@dp.message_handler(lambda m: m.text and ("https://www.youtube.com" in m.text or "youtu.be" in m.text))
async def youtube_summary_handler(message: types.Message):
    summary = handle_youtube_summary(message.text)
    await message.reply(summary or "Не удалось получить саммари.")

@dp.message_handler(lambda m: m.text == "💬 Задать вопрос AI")
async def ask_ai_prompt(message: types.Message):
    await message.reply("Напиши свой вопрос:")

@dp.message_handler(lambda m: m.reply_to_message and "вопрос" in m.reply_to_message.text.lower())
async def ask_ai_handler(message: types.Message):
    openai.api_key = OPENAI_API_KEY
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": message.text}]
    )
    await message.reply(response.choices[0].message.content)

@dp.message_handler(lambda m: m.text == "📊 Отправить заявку")
async def google_form_prompt(message: types.Message):
    await message.reply("Напиши заявку в формате: Имя / Телефон / Комментарий")

@dp.message_handler(lambda m: m.reply_to_message and "заявку" in m.reply_to_message.text.lower())
async def google_form_handler(message: types.Message):
    result = handle_google_form_submission(message.text)
    await message.reply("✅ Заявка отправлена!" if result else "❌ Ошибка при отправке.")

@dp.message_handler(lambda m: m.text == "📎 Получить PDF")
async def send_pdf(message: types.Message):
    try:
        with open("media/info.pdf", "rb") as f:
            await message.reply_document(f)
    except FileNotFoundError:
        await message.reply("PDF пока не загружен.")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
