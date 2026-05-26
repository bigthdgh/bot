import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command

# Вставь сюда свой токен из BotFather
TOKEN = "8562246018:AAEc8_JPt4JgG_dqyfLyFhCncgNAzVSXNUk"

# Настройка логирования, чтобы видеть ошибки в терминале
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Хендлер на команду /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Здорово! Скидывай сюда ГС, и я вытащу его ID для нашей игры.")

# Хендлер, который ловит ГС и выдает file_id
@dp.message(F.voice)
async def get_voice_id(message: types.Message):
    voice_id = message.voice.file_id
    # Отправляем в чат
    await message.answer(f"✅ ID получен!\n\n{voice_id}", parse_mode="Markdown")
    # Дублируем в консоль (терминал), чтобы удобно было копировать на HP ProBook
    print(f"\n--- СКОПИРУЙ ЭТОТ ID ---\n{voice_id}\n-----------------------\n")

async def main():
    print("Бот запущен и ждет ГС...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен")
