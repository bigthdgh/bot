# 🔍 ПОЛНЫЙ АУДИТ БОТА "УЗДЕЧКА"

## 📊 ОБЩАЯ ОЦЕНКА: **7.5/10**

---

## ✅ ЧТО СДЕЛАНО ОТЛИЧНО

### 1. **Архитектура (9/10)**
- ✅ Модульная структура с разделением логики
- ✅ FSM (Finite State Machine) для админ-панели
- ✅ Middleware для XP системы
- ✅ Callback handlers для inline кнопок

### 2. **AI Интеграция (8/10)**
- ✅ Поддержка двух SDK: `google-genai` и `google-generativeai`
- ✅ Fallback на Qwen через OpenRouter
- ✅ Ротация API ключей при 429 ошибке
- ✅ Кастомные фразы при недоступности AI
- ✅ Контекст бота в промптах (BOT_KNOWLEDGE)

### 3. **База данных (7/10)**
- ✅ SQLite с WAL mode (Write-Ahead Logging)
- ✅ Автоматические миграции колонок
- ✅ Функция `_remember_user` для кеширования
- ⚠️ Нет индексов на `username` (медленный поиск)

### 4. **Функционал (9/10)**
- ✅ XP система с рангами
- ✅ Мем-теги (#имя)
- ✅ Игра "Тузик-Ножницы-Бумага" на XP
- ✅ Магазин за Telegram Stars
- ✅ Рулетка /roll
- ✅ Админ-панель с inline кнопками
- ✅ Модерация (бан, мут, очистка)

---

## 🔴 КРИТИЧЕСКИЕ ПРОБЛЕМЫ

### 1. **Удаление сообщений (ИСПРАВЛЕНО частично)**
**Статус:** ⚠️ Улучшено, но не оптимально

**Текущая реализация:**
```python
# Проблема: пробует ID подряд, но в Telegram есть пропуски
attempts = count * 2
for offset in range(1, attempts + 1):
    msg_id = start_msg_id - offset
    msg_ids_to_delete.append(msg_id)
```

**Почему не работает идеально:**
- Telegram может иметь большие пропуски в ID (удалённые, системные сообщения)
- Множитель `count * 2` может быть недостаточен

**Оптимальное решение:**
```python
# Используй bot.iter_history для получения реальных ID
deleted = 0
batch = []
try:
    # Получаем последние N сообщений из истории чата
    messages = []
    async for msg in bot.iter_history(group_chat_id, limit=count):
        messages.append(msg.message_id)
    
    # Удаляем батчами по 100
    for i in range(0, len(messages), 100):
        batch = messages[i:i+100]
        try:
            await bot.delete_messages(group_chat_id, batch)
            deleted += len(batch)
        except:
            # Fallback: по одному
            for msg_id in batch:
                try:
                    await bot.delete_message(group_chat_id, msg_id)
                    deleted += 1
                except:
                    pass
except Exception as e:
    print(f"Ошибка удаления: {e}")
```

**Проблема:** `bot.iter_history` НЕ СУЩЕСТВУЕТ в aiogram 3.x!

**ПРАВИЛЬНОЕ РЕШЕНИЕ для aiogram 3.x:**
```python
# Используй get_updates или храни ID последних сообщений
# Либо используй диапазон шире:
attempts = count * 5  # увеличь множитель
```

---

### 2. **Поиск участников (ИСПРАВЛЕНО ✅)**
**Статус:** ✅ Уже реализовано в коде

Функция `resolve_target` (строки 502-546) уже:
- ✅ Ищет через reply
- ✅ Ищет через text_mention
- ✅ Ищет в БД по username
- ✅ Ищет через `bot.get_chat(@username)`
- ✅ Проверяет членство через `bot.get_chat_member`

**Отлично!** Это решение покрывает 95% случаев.

---

### 3. **Админ-панель: множественные сообщения (ЧАСТИЧНО ИСПРАВЛЕНО)**
**Статус:** ⚠️ Используется `edit_text`, но не везде

**Где уже исправлено:**
- ✅ `admin_panel_callback` (строка 1383) — `call.message.edit_text`
- ✅ `admin_waiting_input` (строка 1459) — `edit_admin_panel_message`

**Где НЕ исправлено:**
- ❌ Некоторые ответы всё ещё используют `message.reply()` вместо редактирования

**Рекомендация:**
Добавь в `state.data` поле `panel_msg_id` и всегда редактируй это сообщение:

```python
async def edit_admin_panel_message(state: FSMContext, text: str, **kwargs):
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    chat_id = data.get("admin_chat_id")  # ID чата админа (ЛС)
    
    if panel_msg_id and chat_id:
        try:
            await bot.edit_message_text(
                text, chat_id, panel_msg_id, **kwargs
            )
        except Exception as e:
            # Если не удалось отредактировать, отправляем новое
            msg = await bot.send_message(chat_id, text, **kwargs)
            await state.update_data(panel_msg_id=msg.message_id)
    else:
        # Первое сообщение панели
        msg = await bot.send_message(chat_id, text, **kwargs)
        await state.update_data(panel_msg_id=msg.message_id)
```

---

## ⚠️ СЕРЬЁЗНЫЕ ПРОБЛЕМЫ

### 4. **Gemini API: лимит 20 запросов/день**
**Из логов:**
```
429 RESOURCE_EXHAUSTED
Quota exceeded: 20 requests/day for gemini-2.5-flash
```

**Проблема:** Бесплатный tier Gemini = 20 запросов/день (очень мало!)

**Решения:**
1. **Используй кеширование ответов:**
```python
# Добавь таблицу в БД
cursor.execute('''CREATE TABLE IF NOT EXISTS ai_cache 
    (prompt_hash TEXT PRIMARY KEY, response TEXT, timestamp REAL)''')

async def get_cached_ai_response(prompt: str) -> str | None:
    import hashlib
    h = hashlib.md5(prompt.encode()).hexdigest()
    cursor.execute("SELECT response FROM ai_cache WHERE prompt_hash = ?", (h,))
    res = cursor.fetchone()
    return res[0] if res else None

async def cache_ai_response(prompt: str, response: str):
    import hashlib
    h = hashlib.md5(prompt.encode()).hexdigest()
    cursor.execute("INSERT OR REPLACE INTO ai_cache VALUES (?, ?, ?)", 
                   (h, response, time.time()))
    conn.commit()
```

2. **Увеличь шанс fallback-фраз:**
```python
AI_PHRASE_CHANCE_SHORT = 0.50  # было 0.08 → 50% фраз вместо AI
```

3. **Переключись на Qwen по умолчанию** (у OpenRouter лимиты выше)

4. **Получи платный Gemini API** ($0.075 за 1M токенов)

---

### 5. **Безопасность: хардкод токенов**
**Проблема:** Токены и ключи в коде (строки 45-61)

```python
TOKEN = os.getenv('BOT_TOKEN', '8562246018:AAEc8_JPt4JgG_dqyfLyFhCncgNAzVSXNUk')  # ❌
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', 'AIzaSyDFJN_lKPoOaiKZNnIIMreZeD3vHHEcc7Y')  # ❌
```

**Решение:**
1. Создай `.env` файл:
```env
BOT_TOKEN=8562246018:AAEc8_JPt4JgG_dqyfLyFhCncgNAzVSXNUk
GEMINI_API_KEY=AIzaSyDFJN_lKPoOaiKZNnIIMreZeD3vHHEcc7Y
QWEN_API_KEY=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
```

2. Убери дефолты из кода:
```python
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env!")
```

3. Добавь `.env` в `.gitignore`

---

### 6. **SQLite: race conditions**
**Проблема:** `check_same_thread=False` без блокировок

```python
conn = sqlite3.connect('uzdechka_bot.db', check_same_thread=False)  # ⚠️
```

**Почему опасно:**
- Несколько хендлеров могут писать в БД одновременно
- Возможна потеря данных или `database is locked`

**Решение:**
```python
import asyncio
from contextlib import asynccontextmanager

db_lock = asyncio.Lock()

@asynccontextmanager
async def db_transaction():
    async with db_lock:
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e

# Использование:
async with db_transaction() as cur:
    cur.execute("UPDATE users SET xp = ? WHERE id = ?", (new_xp, user_id))
```

**Или используй aiosqlite:**
```python
import aiosqlite

conn = await aiosqlite.connect('uzdechka_bot.db')
# Все операции через await
await conn.execute("INSERT ...")
await conn.commit()
```

---

### 7. **Производительность: N+1 запросы**
**Проблема:** В `XPMiddleware` каждый запрос делает 2-3 SQL запроса

```python
cursor.execute("SELECT xp, lvl, ... FROM users WHERE id = ?", (user_id,))
cursor.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
cursor.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
```

**Решение:** Объедини в один `INSERT ... ON CONFLICT`:
```python
cursor.execute("""
    INSERT INTO users (id, username, xp, lvl, last_t) 
    VALUES (?, ?, 0, 0, 0)
    ON CONFLICT(id) DO UPDATE SET username = excluded.username
    RETURNING xp, lvl, last_t, is_banned, mute_until, custom_rank
""", (user_id, username))
res = cursor.fetchone()
```

---

## 🟡 СРЕДНИЕ ПРОБЛЕМЫ

### 8. **Отсутствие логирования**
**Проблема:** Только `print()`, нет структурированных логов

**Решение:**
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Использование:
logger.info(f"User {user_id} leveled up to {new_lvl}")
logger.error(f"AI request failed: {e}")
```

---

### 9. **Нет обработки отключения бота в группе**
**Проблема:** Если бот кикнут из группы, он продолжает хранить её в БД

**Решение:**
```python
@dp.my_chat_member()
async def on_chat_member_update(update: types.ChatMemberUpdated):
    if update.new_chat_member.status == "kicked":
        cursor.execute("UPDATE groups SET enabled = 0 WHERE id = ?", (update.chat.id,))
        conn.commit()
    elif update.new_chat_member.status == "member":
        cursor.execute("UPDATE groups SET enabled = 1 WHERE id = ?", (update.chat.id,))
        conn.commit()
```

---

### 10. **Нет rate limiting для команд**
**Проблема:** Пользователь может спамить `/play`, `/roll`, AI запросы

**Решение:**
```python
from collections import defaultdict
import time

user_cooldowns = defaultdict(lambda: {"play": 0, "roll": 0, "ai": 0})

async def check_cooldown(user_id: int, action: str, seconds: int) -> bool:
    now = time.time()
    if now - user_cooldowns[user_id][action] < seconds:
        return False
    user_cooldowns[user_id][action] = now
    return True

# В хендлере:
if not await check_cooldown(user_id, "play", 5):
    await message.reply("⏳ Подожди 5 секунд перед следующей игрой!")
    return
```

---

## 🟢 МЕЛКИЕ УЛУЧШЕНИЯ

### 11. **Добавь индексы в БД**
```python
cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_owner ON tags(owner_id)")
```

### 12. **Валидация входных данных**
```python
def sanitize_tag_name(name: str) -> str:
    # Только буквы, цифры, _
    return re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9_]', '', name)[:32]

def sanitize_text(text: str) -> str:
    # Убираем HTML/markdown инъекции
    return text.replace('<', '&lt;').replace('>', '&gt;')[:4000]
```

### 13. **Graceful shutdown**
```python
async def on_shutdown():
    logger.info("Shutting down...")
    # Сохраняем незавершённые игры
    # Закрываем соединения
    await conn.close()
    logger.info("Bot stopped.")
```

### 14. **Мониторинг здоровья бота**
```python
@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await message.reply(f"🏓 Pong! Uptime: {time.time() - start_time:.0f}s")
```

---

## 📈 РЕКОМЕНДАЦИИ ПО ПРИОРИТЕТАМ

### 🔴 КРИТИЧНО (сделать СЕЙЧАС):
1. **Переместить токены в .env** (безопасность)
2. **Увеличить AI_PHRASE_CHANCE до 50%** (экономия API лимита)
3. **Добавить кеширование AI ответов** (снизить нагрузку)

### 🟠 ВАЖНО (сделать на этой неделе):
4. **Исправить удаление сообщений** (увеличить множитель до `count * 5`)
5. **Добавить rate limiting** (защита от спама)
6. **Настроить логирование** (отладка проблем)

### 🟡 ЖЕЛАТЕЛЬНО (сделать в течение месяца):
7. **Перейти на aiosqlite** (безопасность БД)
8. **Добавить индексы в БД** (производительность)
9. **Обработка кика из группы** (чистота БД)

### 🟢 ОПЦИОНАЛЬНО (когда будет время):
10. **Мониторинг и метрики** (Prometheus/Grafana)
11. **Юнит-тесты** (pytest)
12. **Docker-контейнер** (деплой)

---

## 🎯 ИТОГОВАЯ ОЦЕНКА ПО КАТЕГОРИЯМ

| Категория | Оценка | Комментарий |
|-----------|--------|-------------|
| **Архитектура** | 9/10 | Отличная структура, FSM, middleware |
| **Безопасность** | 4/10 | ❌ Хардкод токенов, race conditions в БД |
| **Производительность** | 6/10 | ⚠️ N+1 запросы, нет кеширования AI |
| **Надёжность** | 7/10 | ✅ Fallback для AI, но нет rate limiting |
| **Функционал** | 9/10 | Богатый набор фич |
| **Код-стиль** | 8/10 | Читаемый, но местами избыточный |

**ОБЩАЯ ОЦЕНКА: 7.5/10** — хороший бот с потенциалом стать отличным после исправления критических проблем.

---

## 🚀 QUICK WINS (быстрые улучшения за 10 минут):

```python
# 1. Увеличь шанс fallback-фраз (строка 153)
AI_PHRASE_CHANCE_SHORT = 0.50  # было 0.08

# 2. Увеличь множитель для удаления сообщений (строка 2645)
attempts = count * 5  # было count * 2

# 3. Добавь индексы (после строки 131)
cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_owner ON tags(owner_id)")
conn.commit()

# 4. Добавь rate limiting для AI (перед строкой 2300)
if not await check_cooldown(user_id, "ai", 10):
    await message.reply("⏳ Подожди 10 секунд перед следующим AI запросом!")
    return
```

---

## 📚 ПОЛЕЗНЫЕ ССЫЛКИ

- [aiogram 3.x документация](https://docs.aiogram.dev/en/latest/)
- [SQLite best practices](https://www.sqlite.org/bestpractice.html)
- [Gemini API pricing](https://ai.google.dev/pricing)
- [OpenRouter API](https://openrouter.ai/docs)
- [Python asyncio patterns](https://docs.python.org/3/library/asyncio.html)

---

**Автор аудита:** AI Code Reviewer  
**Дата:** 2026-05-21  
**Версия бота:** Уздечка v2.0 (с играми и магазином)
