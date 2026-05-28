import asyncio
import time
import random
import re
import os
from io import BytesIO
from typing import Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from datetime import datetime, timedelta, timezone
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    BufferedInputFile, ChatPermissions, BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, LabeledPrice, PreCheckoutQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

try:
    from google import genai as modern_genai
except ImportError:
    modern_genai = None

try:
    import google.generativeai as legacy_genai
except ImportError:
    legacy_genai = None

try:
    from PIL import Image
except ImportError:
    Image = None

# Turso (libsql) для облачной базы данных
# ВНИМАНИЕ: Для полноценной работы Turso нужно переписать все cursor.execute на async
# Пока оставляем локальную SQLite как primary
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
USE_TURSO = os.getenv("USE_TURSO", "false").lower() == "true"

if USE_TURSO and TURSO_URL and TURSO_TOKEN:
    try:
        from libsql_client import create_client
        turso_client = create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)
        print(f"[DB] Turso включена: {TURSO_URL}")
    except ImportError:
        print("[DB] libsql-client не установлен. Установи: pip install libsql-client")
        turso_client = None
else:
    turso_client = None
    print("[DB] Используется локальная SQLite. Для Turso установи USE_TURSO=true и задай TURSO_DATABASE_URL/TURSO_AUTH_TOKEN")

# Загрузка переменных окружения из .env файла
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- ИНИЦИАЛИЗАЦИЯ ---
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

# Qwen через OpenRouter для сочинений
QWEN_API_KEY = os.getenv('QWEN_API_KEY')
QWEN_MODEL = os.getenv('QWEN_MODEL', 'qwen/qwen3-next-80b-a3b-instruct:free')
QWEN_API_URL = "https://openrouter.ai/api/v1/chat/completions"
QWEN_ENABLED = os.getenv('QWEN_ENABLED', 'true').lower() == 'true'

# Кластер API-ключей Gemini для ротации при 429 ошибке
GEMINI_API_KEYS = [
    k.strip()
    for k in (os.getenv("GEMINI_API_KEYS") or "").split(",")
    if k.strip()
]
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]
_gemini_key_index = 0
MATVEY_ID = int(os.getenv('MATVEY_ID', '2076532055'))
ADMIN_IDS = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '8425434588,8062523010').split(',') if x.strip()]
CORE_ADMIN_IDS = set(ADMIN_IDS)  # хардкодные — неизменяемы, полная панель
PURCHASED_ADMIN_IDS: set[int] = set()  # купленные — урезанная панель

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ (локальная SQLite для совместимости) ---
# Для Render Free: данные будут теряться при рестарте. Для сохранения используй Render Paid + Disk
import sqlite3
conn = sqlite3.connect('uzdechka_bot.db', check_same_thread=False)
conn.execute('PRAGMA journal_mode=WAL')
cursor = conn.cursor()

# Инициализация Gemini API — оба SDK с fallback
gemini_client = None
gemini_model = None
gemini_backend = None

if GEMINI_API_KEY:
    if modern_genai:
        try:
            gemini_client = modern_genai.Client(
                api_key=GEMINI_API_KEY,
                http_options={"api_version": "v1"}
            )
            gemini_backend = "google-genai"
            print(f"[AI] Gemini: {gemini_backend}, модель: {GEMINI_MODEL}")
        except Exception as e:
            print(f"[AI] Не удалось google-genai: {type(e).__name__}: {e}")

    if gemini_client is None and legacy_genai:
        try:
            legacy_genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = legacy_genai.GenerativeModel(GEMINI_MODEL)
            gemini_backend = "google-generativeai"
            print(f"[AI] Gemini (legacy): {gemini_backend}, модель: {GEMINI_MODEL}")
        except Exception as e:
            print(f"[AI] Не удалось google-generativeai: {type(e).__name__}: {e}")

    if gemini_client is None and gemini_model is None:
        print("[AI] Ни один Gemini SDK не инициализирован. Бот на fallback-фразах.")
else:
    print("[AI] GEMINI_API_KEY не задан. AI-ответы только fallback.")

# --- ИНИЦИАЛИЗАЦИЯ СХЕМЫ БАЗЫ ДАННЫХ ---
cursor.execute('''CREATE TABLE IF NOT EXISTS users 
    (id INTEGER PRIMARY KEY, xp INTEGER DEFAULT 0, lvl INTEGER DEFAULT 0, last_t REAL DEFAULT 0)''')

# Безопасное добавление новых колонок (миграция)
try: cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
except sqlite3.OperationalError: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
except sqlite3.OperationalError: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN mute_until REAL DEFAULT 0")
except sqlite3.OperationalError: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN custom_rank TEXT")
except sqlite3.OperationalError: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN vip_until REAL DEFAULT 0")
except sqlite3.OperationalError: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN saved_rank TEXT")
except sqlite3.OperationalError: pass
cursor.execute('''CREATE TABLE IF NOT EXISTS tags 
    (name TEXT PRIMARY KEY, content TEXT, owner_id INTEGER)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS groups 
    (id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER DEFAULT 1)''')
try: cursor.execute("ALTER TABLE groups ADD COLUMN enabled INTEGER DEFAULT 1")
except sqlite3.OperationalError: pass

# Индексы для ускорения поиска
cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_owner ON tags(owner_id)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_lvl ON users(lvl)")
conn.commit()

# --- ВОССТАНОВЛЕНИЕ КУПЛЕННЫХ АДМИНОК ПОСЛЕ ПЕРЕЗАПУСКА ---
cursor.execute("SELECT id FROM users WHERE custom_rank = '👑 Админ'")
for row in cursor.fetchall():
    purchased_id = row[0]
    if purchased_id not in ADMIN_IDS:
        ADMIN_IDS.append(purchased_id)
    PURCHASED_ADMIN_IDS.add(purchased_id)


def get_rank_name(lvl: int) -> str:
    if lvl == 0: return "Новорег"
    elif lvl < 5: return "Жертва Влада"
    elif lvl < 10: return "Эластичный"
    elif lvl < 20: return "Опытный камикадзе"
    elif lvl < 30: return "Хирург-любитель"
    elif lvl < 50: return "Ветеран урологии"
    else: return "Стальная Уздечка"


# Короткие ответы: баланс между ИИ и фразами (экономия API лимита)
AI_PHRASE_CHANCE_SHORT = float(os.getenv("AI_PHRASE_CHANCE", "0.35"))  # 35% фраз вместо AI
AI_PHRASE_CHANCE_LONG = 0.10  # для длинных ответов чаще используем AI
AI_PHRASE_CHANCE_PHOTO = 0.15  # для фото чаще используем AI
AI_PHRASE_CHANCE_UZDECHKA = 0.08  # «уздечка вопрос» — почти всегда ИИ

UZDECHKA_NAMES_RE = r"(уздечк[а-яё]*|удечка|уздека|узздечк[а-яё]*)"

BOT_KNOWLEDGE = """
Ты — телеграм-бот «Уздечка». Токсично-шутливый чат: уздечка, швы, изолента, скорая, Влад (у него тузы/тузик крепкие).
Мат в меру допустим. Решай математику и задачи в ответе. На оскорбления/троллинг — отвечай с юмором, не морализируй.

═══ ЧТО ТЫ УМЕЕШЬ (если спросят) ═══
Рассказывай весело, в стиле Уздечки: опыт, ранги, профиль, теги, ИИ-вопросы, пасхалки, рулетка /roll, магазин /shop (звёзды Telegram), игра Тузик-Ножницы-Бумага /play на XP, баланс /balance. Админ-панель /admin в ЛС для админов.

═══ КАК УСТРОЕН БОТ ═══
• Опыт: сообщения в группе от 4 символов, раз в ~30 сек +5–15 XP. VIP ×1.5 XP. Уровень ≈ √(XP/100). При апе — «ГРАЦ».
• Ранги: 0 Новорег | <5 Жертва Влада | <10 Эластичный | <20 Опытный камикадзе | <30 Хирург-любитель | <50 Ветеран урологии | 50+ Стальная Уздечка.
• Профиль: «Уздечка статус», /status.
• Мем-теги: с 5 lvl — «Уздечка сделай тег имя текст», вызов #имя. Удаление: свой тег или с 10 lvl чужой.
• ИИ: «Уздечка» + любой вопрос; /otvet, /sochinenie, /foto. /frazy — все фразы.
• Магазин /shop: мут обидчика, размут себя, VIP-ранг, админка, доп. бросок — за Telegram Stars.
• Игра /play: Тузик-Изолента-Уздечка (камень-ножницы-бумага) на XP, ставка от 5 до 100 XP.
• Рулетка /roll: 3 бесплатных броска в день. /id — узнать свой ID.
• Пасхалки: влад, порвал, туз, изолента, шов, скорая…
• Админ: /admin в ЛС → кнопки.

Если просят «в магазин», «хочу играть», «давай в игру» — объясни как пользоваться /shop и /play.

═══ ACTION (если пользователь ЯВНО просит действие бота — мут, бан, статус, тег и т.д.) ═══
Первая строка ответа СТРОГО: ACTION|команда|target|args
Пустая строка. Дальше — текст пользователю.

⚠️ ВАЖНО про target:
• target = username пользователя БЕЗ собачки @ (например alex_drivi, не @alex_drivi)
• target = ID пользователя (цифры) — если username неизвестен
• target = пусто если команда без юзера (название, описание, очисти, напиши)
• Примеры: ACTION|мут|alex_drivi|10 → мут на 10 мин
• Пример: ACTION|бан|123456789| → бан по ID
• Пример: ACTION|статус|| → показ статуса спросившего
• Пример: ACTION|название||Новое имя чата
• Пример: ACTION|напиши||Привет чат!

Команды для ВСЕХ: статус | фразы | создай_тег|имя_тега|текст_тега | удали_тег|имя_тега|
Только для админа: бан|username| , разбан|username| , мут|username|минуты , размут|username| , админ_дай_тег|username|подписьTG , админ_дай_ранг|username|звание , название||новое_имя , описание||текст , очисти||число , напиши||текст

Если просто поболтать, пошутить, «реши», «поиграем», спросить про Матвея — БЕЗ строки ACTION, только ответ.
Если админ в ГРУППЕ пишет «Уздечка мут @user 10» или «Уздечка бан username» — ОБЯЗАТЕЛЬНО дай ACTION.
"""

USER_AI_ACTIONS = {"статус", "фразы", "создай_тег", "удали_тег"}

MATVEY_PHRASES = [
    "Матвей, тише, швы разойдутся!",
    "Матвей, не делай резких движений, мы всё помним.",
    "Матвей, это не йога — это уздечка на износ.",
    "Матвей, Влад уже в пути с изолентой.",
    "Матвей, скорая знает твой ник наизусть.",
    "Матвей, эластичность — не бесконечный ресурс.",
    "Матвей, один резкий поворот — и снова «порвал».",
    "Матвей, у Влада туз крепкий, у тебя — шов на честном слове.",
]

UZDECHKA_GREETINGS = [
    "Я тут. Кто опять нарушил технику безопасности?",
    "Слушаю. Уздечка на связи, нервы — нет.",
    "Да? Опять про швы или про Влада?",
    "На связи. Тузик у Влада крепкий, у меня — только шутки.",
]

# Ответы вместо ИИ (короткие) и запасные при падении Gemini
BOT_CATCHPHRASES = [
    "Швы держатся. Нервы — нет.",
    "Изолента — временное решение навсегда.",
    "Эластичность — миф, как твои оправдания.",
    "Сначала скорая, потом мемы.",
    "Уздечка на связи. Влад — в ударе. Туз — крепкий.",
    "У Влада тузы крепкие. У остальных — уздечка на износ.",
    "Тузик крепкий, уздечка — нет. Классика.",
    "Влад зашёл — тузы в колоде, швы в чате.",
    "Порвал? Это не баг, это фича чата.",
    "Влад — не имя, это козырь в рукаве.",
    "Кто порвал — тот в топе по больнице.",
    "Скотч держит всё, кроме репутации.",
    "Память чата не заживает. Никогда.",
    "Техника безопасности отменена. Снова.",
    "Врач сказал «береги». Ты услышал «давай ещё».",
    "Один резкий поворот — и снова «порвал».",
    "Уздечка эластичная. Нервы — нет.",
    "Влад где-то рядом. Туз уже в руке.",
    "Крепкий тузик бьёт слабую эластичность.",
    "Шов заживёт. Стыд в чате — нет.",
]

EASTER_EGGS: list[tuple[str, list[str]]] = [
    ("влад", [
        "Влад — это не имя, это приговор для одной уздечки.",
        "Влад где-то рядом. Уздечка уже дрожит.",
        "Упомянул Влада — получил моральную травму уздечки.",
        "У Влада тузы крепкие. У тебя — только надежда на изоленту.",
        "Влад заходит в чат — тузик крепкий, уздечка плачет.",
        "Влад не блефует. У него туз, у тебя — шов.",
    ]),
    ("туз", [
        "Туз крепкий. Уздечка — нет.",
        "Крепкий тузик — лучший аргумент в этом чате.",
        "Тузы у Влада, дыры у остальных.",
        "Туз не рвётся. В отличие от некоторых.",
    ]),
    ("тузик", [
        "Тузик крепкий — уздечка в шоке.",
        "Крепкий тузик beats эластичность.",
    ]),
    ("козыр", [
        "Козырь Влада — туз. Твой — изолента.",
    ]),
    ("порвал", [
        "Кто опять порвал?! Вызывайте скорую, или хотя бы изоленту несите!",
        "Порвал — классика жанра. Швы держатся из вежливости.",
        "Снова порвал? Это уже не авария, это стиль жизни.",
    ]),
    ("изолент", [
        "Изолента — временное решение навсегда.",
        "Сначала изолента, потом сожаления.",
    ]),
    ("шов", [
        "Шов держится. Нервы — нет.",
        "Швы заживают. Память чата — никогда.",
    ]),
    ("скор", [
        "Скорая в пути. Уздечка в панике.",
    ]),
    ("хуй", [
        "Культурно, но уздечка всё равно в шоке.",
        "Ладно, ладно. Сначала врач, потом мемы.",
    ]),
    ("уздечк", [
        "Уздечка на связи. Спроси /otvet или /фразы.",
    ]),
]

AI_FALLBACK_PHRASES = [
    "Швы заживают. Память чата — нет.",
    "Изолента — не медицина, но помогает отрицанию.",
    "Эластичность — навык выживания.",
    "Зови врача. Или хотя бы неси скотч.",
    "Сначала результат, потом объяснения.",
    "У Влада туз крепкий. У меня — только fallback.",
]


def pick_canned_reply(prompt: str = "") -> str:
    """Фраза по ключевым словам в вопросе или случайная."""
    p = (prompt or "").lower()
    for keyword, replies in EASTER_EGGS:
        if keyword in p:
            return random.choice(replies)
    return random.choice(BOT_CATCHPHRASES)


async def get_chat_response(
    prompt: str,
    mode: str = "short",
    image_bytes: bytes | None = None,
    *,
    allow_canned: bool = True,
) -> tuple[str, bool]:
    """Ответ чата: (текст, from_ai). Иногда — готовая фраза вместо ИИ."""
    chance = AI_PHRASE_CHANCE_SHORT
    if mode == "long":
        chance = AI_PHRASE_CHANCE_LONG
    elif mode == "uzdechka":
        chance = AI_PHRASE_CHANCE_UZDECHKA
    if image_bytes:
        chance = AI_PHRASE_CHANCE_PHOTO

    if allow_canned and random.random() < chance:
        return pick_canned_reply(prompt), False

    ai_mode = "short" if mode == "uzdechka" else mode
    text = await get_ai_response(prompt, mode=ai_mode, image_bytes=image_bytes)
    if text:
        return text, True
    return pick_canned_reply(prompt), False


def get_user_profile_context(user_id: int) -> str:
    cursor.execute("SELECT xp, lvl, custom_rank FROM users WHERE id = ?", (user_id,))
    res = cursor.fetchone()
    xp, lvl, custom_rank = res if res else (0, 0, None)
    rank = custom_rank if custom_rank else get_rank_name(lvl)
    nxt = (lvl + 1) ** 2 * 100
    need = max(0, nxt - xp)
    return (
        f"Профиль спрашивающего (ID {user_id}): уровень {lvl}, XP {xp}, ранг «{rank}». "
        f"До след. уровня ~{need} XP (пиши в чат от 4 символов, кд ~30 сек)."
    )


def parse_ai_action_response(raw: str) -> tuple[str | None, str | None, str | None, str]:
    raw = (raw or "").strip()
    if not raw.startswith("ACTION|"):
        return None, None, None, raw
    first_line, _, body = raw.partition("\n")
    parts = first_line.split("|", 3)
    cmd = parts[1].strip() if len(parts) > 1 else ""
    target = parts[2].strip() if len(parts) > 2 else ""
    args = parts[3].strip() if len(parts) > 3 else ""
    if not cmd:
        return None, None, None, body.strip() or raw
    # Чистим target: убираем @ и всё после первого пробела (ИИ может написать "@user бла")
    target = target.lstrip("@").split()[0] if target else ""
    return cmd, (target or None), args, body.strip()


def ai_action_allowed(cmd: str, user_id: int, is_pm: bool) -> bool:
    if cmd in USER_AI_ACTIONS:
        return True
    if cmd in ADMIN_PM_COMMANDS:
        # Админ может выполнять команды ВЕЗДЕ: и в ЛС, и в группе
        return user_id in ADMIN_IDS
    return False


# Ответы-подсказки для команд, которые бот умеет выполнять
CMD_HINTS = {
    "рулетка": "🎲 Для рулетки используй команду /roll или скажи «Уздечка рулетка». У тебя 3 бесплатных броска в день.",
    "магазин": "🛒 Магазин: /shop. Там мут обидчика, размут, VIP, админка и доп. бросок рулетки.",
    "играть": "🎮 Игра Тузик-Ножницы-Бумага на XP: /play в группе. Ставка от 5 до 100 XP.",
    "игра": "🎮 Хочешь поиграть? Пиши /play в группе. Или попроси Уздечка кинуть рулетку /roll.",
    "игр(а|ы|у|ять)": "🎮 Игра: /play в группе. Тузик-Ножницы-Бумага на XP.",
    "помоги|помощь|что ты умееш": "ℹ️ /help — все команды. А если кратко: пиши «Уздечка» + вопрос, /shop, /play, /roll, /status, /id.",
    "статистик|стата": "📊 Статистика: /status — твой профиль, ранг, уровень и XP.",
    "мой id|айди|узнать id": "🆔 Твой ID: /id",
    "брось рулетк|кинь рулетк|крути рулетк|рулетка": "🎲 /roll — крутить рулетку 9А. 3 броска в день бесплатно.",
    "кто лох": "🎯 /roll — кину рулетку и узнаешь, кто лох дня!",
}


def route_uzdechka_intent(query: str) -> tuple[str, str | None, str | None]:
    """Быстрый разбор без ИИ для очевидных просьб."""
    q = (query or "").strip()
    ql = q.lower()
    if not ql:
        return "ai_chat", None, ""

    # Проверяем подсказки для команд
    for pattern, hint in CMD_HINTS.items():
        if re.search(pattern, ql, re.I):
            return "ai_chat", None, hint

    if re.search(r"^(статус|профиль|инфо|мой профиль)\s*\??$", ql):
        return "статус", None, None
    if re.search(r"(какой|мой)\s+(ранг|уровень|лвл)", ql) or re.search(
        r"как\s+(повыс|подня|качать|фарм|получ).*(ранг|уров|лвл|xp|опыт)", ql
    ):
        return "ai_chat", None, q

    if re.search(r"(все\s+)?фраз|список\s+фраз", ql):
        return "фразы", None, None

    m = re.search(r"(?:сделай|создай)\s+тег\s+(\S+)\s+(.+)", q, re.I | re.DOTALL)
    if m:
        return "создай_тег", m.group(1), m.group(2).strip()

    if re.search(r"сочинени|эссе|развернут|подробн", ql):
        return "ai_long", None, q

    if re.search(r"что\s+на\s+фото|опиши\s+фото|что\s+на\s+картинке", ql):
        rest = re.sub(r"^.*?(что\s+на\s+фото|опиши\s+фото)\s*", "", q, flags=re.I).strip()
        return "ai_photo", None, rest

    return "ai_chat", None, q


async def get_uzdechka_brain_reply(
    query: str,
    user_id: int,
    is_admin: bool,
) -> tuple[str, bool, str | None, str | None, str | None]:
    """ИИ с знанием бота. Возвращает (текст, from_ai, action_cmd, target, args)."""
    ctx = get_user_profile_context(user_id)
    admin_line = (
        "Спросивший — главный админ бота: можешь предлагать ACTION с админ-командами, если он просит бан/мут и т.д. (в ЛС через /admin удобнее)."
        if is_admin
        else "Спросивший — обычный игрок, админ-команды не выполняй."
    )
    full_prompt = (
        f"{BOT_KNOWLEDGE}\n\n{ctx}\n{admin_line}\n\n"
        f"Вопрос/реплика пользователя:\n{query}"
    )
    raw, from_ai = await get_chat_response(full_prompt, mode="uzdechka", allow_canned=True)
    acmd, atgt, aargs, display = parse_ai_action_response(raw)
    if not display:
        display = raw
    return display, from_ai, acmd, atgt, aargs


def message_has_uzdechka(text: str) -> bool:
    return bool(text and re.search(UZDECHKA_NAMES_RE, text, re.I))


def format_all_phrases() -> str:
    lines = ["📜 <b>Все фразы Уздечки</b>\n"]

    def block(title: str, items: list[str]):
        lines.append(f"\n<b>{title}</b> ({len(items)}):")
        for i, s in enumerate(items, 1):
            lines.append(f"{i}. {s}")

    block("Короткие ответы (вместо ИИ ~25%)", BOT_CATCHPHRASES)
    block("Матвей", MATVEY_PHRASES)
    block("Приветствия «уздечка»", UZDECHKA_GREETINGS)
    block("Запасные (если ИИ упал)", AI_FALLBACK_PHRASES)

    lines.append("\n<b>Пасхалки по словам</b>:")
    for kw, replies in EASTER_EGGS:
        lines.append(f"\n🔑 <code>{kw}</code>:")
        for i, s in enumerate(replies, 1):
            lines.append(f"  {i}. {s}")

    lines.append(
        f"\n<i>ИИ: ~{int(AI_PHRASE_CHANCE_SHORT * 100)}% /otvet — заготовка; "
        f"«Уздечка …» — почти всегда ИИ. .env: AI_PHRASE_CHANCE</i>"
    )
    return "\n".join(lines)


def _remember_user(user: types.User | None):
    if not user:
        return
    cursor.execute("INSERT INTO users (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (user.id,))
    if user.username:
        cursor.execute("UPDATE users SET username = ? WHERE id = ?", (user.username.lower(), user.id))
    conn.commit()


def _target_from_text_mention(message: Message, target: str | None) -> int | None:
    text = message.text or message.caption or ""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        if entity.type != "text_mention" or not entity.user:
            continue
        mentioned_text = text[entity.offset:entity.offset + entity.length].strip()
        if not target or target.strip() == mentioned_text:
            _remember_user(entity.user)
            return entity.user.id
    return None


async def resolve_target(target: str | None, group_chat_id: int | None = None, message: Message | None = None) -> int | None:
    if message and message.reply_to_message and not target:
        _remember_user(message.reply_to_message.from_user)
        return message.reply_to_message.from_user.id

    if message:
        from_mention = _target_from_text_mention(message, target)
        if from_mention:
            return from_mention

    if not target:
        return None

    target = target.strip()
    if target.isdigit():
        return int(target)

    # Поддержка username как с @, так и без
    username = target.lstrip('@').lower()

    # 1. Ищем в локальной БД
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    res = cursor.fetchone()
    if res:
        return res[0]

    # 2. Пробуем через Telegram API — получаем ID по @username
    try:
        chat = await bot.get_chat(f"@{username}")
        uid = getattr(chat, "id", None)
        if uid:
            # Сохраняем username в БД
            cursor.execute("INSERT INTO users (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (uid,))
            cursor.execute("UPDATE users SET username = ? WHERE id = ?", (username, uid))
            conn.commit()
            # Проверяем членство в группе (попутно кешируем)
            if group_chat_id:
                try:
                    member = await bot.get_chat_member(group_chat_id, uid)
                    _remember_user(member.user)
                except Exception:
                    pass
            return uid
    except Exception as e:
        print(f"[TARGET] get_chat(@{username}) failed: {e}")

    # 3. Если знаем ID группы, пробуем искать среди админов (они точно есть в getChatAdministrators)
    if group_chat_id:
        try:
            admins = await bot.get_chat_administrators(group_chat_id)
            for admin in admins:
                if admin.user.username and admin.user.username.lower() == username:
                    _remember_user(admin.user)
                    return admin.user.id
        except Exception as e:
            print(f"[TARGET] get_chat_administrators failed: {e}")

    return None


def ensure_user_exists(user_id: int):
    cursor.execute("INSERT INTO users (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (user_id,))
    conn.commit()


def _guess_image_mime(image_bytes: bytes) -> str:
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:3] == b"GIF":
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and len(image_bytes) > 12 and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _build_gemini_contents(prompt: str, image_bytes: bytes | None = None):
    if not image_bytes:
        return prompt
    if modern_genai and gemini_client:
        try:
            from google.genai import types as genai_types
            return [
                genai_types.Part.from_text(text=prompt),
                genai_types.Part.from_bytes(data=image_bytes, mime_type=_guess_image_mime(image_bytes)),
            ]
        except Exception:
            pass
    if Image is not None:
        return [prompt, Image.open(BytesIO(image_bytes))]
    return prompt


# Глобальная aiohttp сессия (создаётся один раз, а не на каждый запрос)
_http_session = None

async def get_http_session() -> "aiohttp.ClientSession":
    global _http_session
    if _http_session is None:
        import aiohttp
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close_http_session():
    global _http_session
    if _http_session:
        await _http_session.close()
        _http_session = None


async def get_qwen_response(prompt: str, mode: str = "short") -> str:
    """Ответ через Qwen (OpenRouter) — используется для сочинений и развёрнутых ответов."""
    if not QWEN_ENABLED:
        return ""
    try:
        import aiohttp
        style_hint = (
            "Ответь 1-2 короткими предложениями на русском."
            if mode == "short"
            else "Развёрнутый ответ на русском: 3-6 абзацев, минимум 800 символов, с примерами."
        )
        system_prompt = (
            "Ты — телеграм-бот «Уздечка» в токсично-шутливом чате про уздечку, швы, изоленту и «медицину». "
            f"{style_hint} "
            "Стиль: матерный сленг допустим, но без угроз реальному насилию и без травли конкретных людей. "
            "Юмор абсурдный, про эластичность, порванное, Влада (у него тузы крепкие, тузик крепкий), скорую и изоленту. "
            "Не морализируй, не пиши дисклеймеры про ИИ."
        )
        headers = {
            "Authorization": f"Bearer {QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": QWEN_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1536 if mode == "long" else 768,
            "temperature": 0.7,
        }
        session = await get_http_session()
        async with session.post(QWEN_API_URL, json=payload, headers=headers, timeout=60) as resp:
                if resp.status != 200:
                    text_err = await resp.text()
                    print(f"[QWEN] HTTP {resp.status}: {text_err[:200]}")
                    return ""
                data = await resp.json()
                result = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if result:
                    print(f"[QWEN] Response ({len(result)} chars): {result[:100]}...")
                return result
    except ImportError:
        print("[QWEN] aiohttp не установлен. pip install aiohttp")
        return ""
    except Exception as e:
        print(f"[QWEN] Error: {type(e).__name__}: {e}")
        return ""


async def get_ai_response(prompt: str, mode: str = "short", image_bytes: bytes | None = None) -> str:
    try:
        try:
            print(f"[AI] Request: {prompt}")
        except UnicodeEncodeError:
            print(f"[AI] Request: <unicode prompt, {len(prompt)} chars>")
        if not prompt or not prompt.strip():
            print("[AI] Error: empty prompt")
            return ""

        mode = (mode or "short").lower()
        if mode not in ("short", "long", "vision"):
            mode = "short"

        style_hint = (
            "Ответь 1-2 короткими предложениями на русском."
            if mode == "short"
            else "Развёрнутый ответ на русском: 3-6 абзацев, минимум 800 символов, с примерами."
        )

        full_prompt = (
            "Ты — телеграм-бот «Уздечка» в токсично-шутливом чате про уздечку, швы, изоленту и «медицину». "
            f"{style_hint} "
            "Стиль: матерный сленг допустим, но без угроз реальному насилию и без травли конкретных людей. "
            "Юмор абсурдный, про эластичность, порванное, Влада (у него тузы крепкие, тузик крепкий), скорую и изоленту. "
            "Не морализируй, не пиши дисклеймеры про ИИ. "
            f"Сообщение пользователя: {prompt}"
        )

        generation_config = {
            "temperature": 0.7,
            "max_output_tokens": 768 if mode in ("short", "uzdechka") else 1536,
        }

        # Для long mode (сочинения) сначала пробуем Qwen через OpenRouter
        if mode == "long" and QWEN_ENABLED and not image_bytes:
            qwen_result = await get_qwen_response(prompt, mode="long")
            if qwen_result and len(qwen_result) >= 600:
                print(f"[AI] Used Qwen for long mode, {len(qwen_result)} chars")
                return qwen_result
            # Если Qwen не сработал или ответ короткий, пробуем Gemini как fallback

        contents = _build_gemini_contents(full_prompt, image_bytes)

        if gemini_client:
            def _modern_call():
                try:
                    return gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=contents,
                        config=generation_config,
                    )
                except TypeError:
                    return gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=contents,
                    )

            response = await asyncio.to_thread(_modern_call)
            print(f"[AI] backend={gemini_backend}, model={GEMINI_MODEL}")
        elif gemini_model:
            if image_bytes and Image is None:
                raise RuntimeError("Pillow is required for image prompts (pip install pillow).")
            try:
                response = await asyncio.to_thread(
                    gemini_model.generate_content,
                    contents,
                    generation_config=generation_config,
                )
            except TypeError:
                response = await asyncio.to_thread(gemini_model.generate_content, contents)
            print(f"[AI] backend={gemini_backend}, model={GEMINI_MODEL}")
        else:
            raise RuntimeError(
                "Gemini SDK is not initialized. Install google-genai "
                "or google-generativeai and check GEMINI_API_KEY."
            )

        result = response.text.strip() if response and response.text else ""
        print(f"[AI] Response: {result}")

        # Some models ignore length instructions. For long mode, try to extend the answer a bit.
        if mode == "long" and not image_bytes and len(result) < 600:
            for _ in range(2):
                cont_prompt = (
                    "Continue and expand the previous answer. "
                    "Add more detail, examples, and explanations. "
                    "Do NOT restart. Previous answer:\n"
                    f"{result}\n\nContinue:"
                )
                if gemini_client:
                    extra = await asyncio.to_thread(
                        gemini_client.models.generate_content,
                        model=GEMINI_MODEL,
                        contents=cont_prompt,
                    )
                else:
                    extra = await asyncio.to_thread(gemini_model.generate_content, cont_prompt)
                extra_text = extra.text.strip() if extra and extra.text else ""
                if not extra_text:
                    break
                result = (result + "\n\n" + extra_text).strip()
                if len(result) >= 800:
                    break

        return result
    except Exception as e:
        print(f"[AI] Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

        prompt_lower = (prompt or "").lower()

        if any(word in prompt_lower for word in ["stitch", "suture", "heal"]):
            return "Stitches heal. Memory does not."

        if any(word in prompt_lower for word in ["tore", "tear", "ripped"]):
            return "Something ripped? Bring tape and optimism."

        if any(word in prompt_lower for word in ["tape", "duct tape"]):
            return "Tape is not medicine, but it helps denial."

        if any(word in prompt_lower for word in ["safety", "careful", "rules"]):
            return "Safety briefing skipped successfully."

        return random.choice(AI_FALLBACK_PHRASES)


GROUP_CHAT = F.chat.type.in_({"group", "supergroup"})


async def register_bot_commands():
    group_cmds = [
        BotCommand(command="otvet", description="ИИ: короткий ответ"),
        BotCommand(command="sochinenie", description="ИИ: развёрнутый текст"),
        BotCommand(command="foto", description="ИИ: что на фото (с картинкой)"),
        BotCommand(command="status", description="Профиль и ранг"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="frazy", description="Все фразы бота"),
    ]
    private_cmds = group_cmds + [
        BotCommand(command="admin", description="Панель админа (только ЛС)"),
    ]
    await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())


def extract_bot_command(text: str, is_pm: bool = False):
    text_lower = text.lower()
    after = text_lower
    had_bot = False

    if not is_pm:
        match = re.search(rf"{UZDECHKA_NAMES_RE}|бот", text_lower)
        if not match:
            return None, None, None
        had_bot = True
        after = text_lower[match.end():].strip().lstrip(",:;!-— ")
    else:
        match = re.search(rf"{UZDECHKA_NAMES_RE}|бот", text_lower)
        if match:
            had_bot = True
            after = text_lower[match.end():].strip().lstrip(",:;!-— ")
        else:
            after = text_lower
        
    m = re.search(r'(удали|удоли)\s*(игрока|пользователя|юзера)\s+(@\w+|\d+)', after)
    if m: return "удали_игрока", m.group(3), None
    m = re.search(r'^(удали|удоли)\s*(игрока|пользователя|юзера)\s*$', after)
    if m: return "удали_игрока", None, None
    
    m = re.search(r'(разбан|раз\s*бан|разбань)\s+(@\w+|\d+)', after)
    if m: return "разбан", m.group(2), None
    m = re.search(r'^(разбан|раз\s*бан|разбань)\s*$', after)
    if m: return "разбан", None, None
    
    m = re.search(r'(бан|забань)\s+(@\w+|\d+)', after)
    if m: return "бан", m.group(2), None
    m = re.search(r'^(бан|забань)\s*$', after)
    if m: return "бан", None, None
    
    m = re.search(r'(размут|раз\s*мут|анмут)\s+(@\w+|\d+)', after)
    if m: return "размут", m.group(2), None
    m = re.search(r'^(размут|раз\s*мут|анмут)\s*$', after)
    if m: return "размут", None, None
    
    m = re.search(r'(мут|заткни)\s+(@\w+|\d+)(.*)', after)
    if m: return "мут", m.group(2), m.group(3).strip()
    m = re.search(r'^(мут|заткни)\s*(.*)', after)
    if m and not re.search(r'@\w+|\d+', m.group(2)): return "мут", None, m.group(2).strip()
    
    m = re.search(r'(удали|удоли)\s*(тег|тэг)\s+(@\w+|\d+)', after)
    if m: return "админ_удали_тег", m.group(3), None
    
    m = re.search(r'(тег|тэг)\s+(@\w+|\d+)\s+(.+)', after)
    if m: return "админ_дай_тег", m.group(2), m.group(3).strip()

    m = re.search(r'(ранг|звание)\s+(@\w+|\d+)\s+(.+)', after)
    if m: return "админ_дай_ранг", m.group(2), m.group(3).strip()
    
    m = re.search(r'(измени\s*название|название)\s+(.+)', after)
    if m: return "название", None, m.group(2).strip()
    
    m = re.search(r'(измени\s*описание|описание)\s+(.+)', after)
    if m: return "описание", None, m.group(2).strip()
    
    m = re.search(r'(измени\s*аву|аватарка|ава)', after)
    if m: return "ава", None, None
    
    m = re.search(r'(закрепи|закрп|закреп)', after)
    if m: return "закрепи", None, None
    
    m = re.search(r'(открепи|откреп)', after)
    if m: return "открепи", None, None
    
    m = re.search(r'(права\s*чат)\s+(on|off)', after)
    if m: return "права_чат", None, m.group(2)
    
    m = re.search(r'(права\s*медиа)\s+(on|off)', after)
    if m: return "права_медиа", None, m.group(2)
    
    m = re.search(r'(права\s*стикеры)\s+(on|off)', after)
    if m: return "права_стикеры", None, m.group(2)

    # Прямые команды (магазин, рулетка, игра, помощь, статистика, id)
    if re.search(r'^(магазин|шоп|shop)\s*$', after, re.I):
        return "cmd_shop", None, None
    if re.search(r'^(рулетк|брось рулетк|кинь рулетк|крути рулетк|ролл)\s*$', after, re.I):
        return "cmd_roll", None, None
    if re.search(r'^(играть|поиграем|в игру|игра с ботом|кинь играть)\s*$', after, re.I):
        return "cmd_play", None, None
    if re.search(r'^(помоги|помощь|что ты умееш|команды|хелп)\s*$', after, re.I):
        return "cmd_help", None, None
    if re.search(r'^(статистик|стата|мой профиль)\s*$', after, re.I):
        return "cmd_status", None, None
    if re.search(r'^(мой id|айди|узнать id)\s*$', after, re.I):
        return "cmd_id", None, None
    if re.search(r'^кто лох\s*$', after, re.I):
        return "cmd_roll", None, None

    # AI: длинный режим (эссе/сочинение/подробно) — ДО «напиши»
    m = re.search(
        r'(сочинение|эссе|развернуто|подробно|подробнее|написать\s+сочинение|напиши\s+сочинение)\s*(.+)?',
        after,
        flags=re.DOTALL
    )
    if m:
        rest = (m.group(2) or "").strip()
        return "ai_long", None, rest

    send_patterns = r'(напиши|отправь)\s+(.+)'
    if is_pm:
        send_patterns = r'(напиши|скажи|отправь)\s+(.+)'
    m = re.search(send_patterns, after, flags=re.DOTALL)
    if m: return "напиши", None, m.group(2).strip()
    
    m = re.search(r'(очисти|удали\s*сообщения)\s+(\d+)', after)
    if m: return "очисти", None, m.group(2)
    
    m = re.search(r'(статус|профиль|инфо)', after)
    if m: return "статус", None, None

    m = re.search(r'(фразы|все\s*фразы|список\s*фраз)', after)
    if m: return "фразы", None, None
    
    m = re.search(r'(сделай|создай)\s*(тег|тэг)\s+([^\s]+)\s+(.+)', after)
    if m: return "создай_тег", m.group(3), m.group(4)
    
    m = re.search(r'(удали|удоли)\s*(тег|тэг)\s+([^\s]+)', after)
    if m: return "удали_тег", m.group(3), None

    # AI: описание фото (если фото есть, текст может быть пустым)
    m = re.search(r'(что\s+на\s+фото|опиши\s+фото|что\s+изображено|просмотри\s+фото|что\s+на\s+картинке)\s*(.+)?', after, flags=re.DOTALL)
    if m:
        rest = (m.group(2) or "").strip()
        return "ai_photo", None, rest

    # AI: явные триггеры (ответь, реши…) — то же что свободный диалог
    m = re.search(r'(ai|ответь|объясни|реши|скажи|помоги|посоветуй)\s+(.+)', after, flags=re.DOTALL)
    if m:
        return route_uzdechka_intent(m.group(2).strip())

    # «Уздечка» + что угодно — главный режим (ИИ знает бота)
    if had_bot or (is_pm and after):
        rest = after.strip()
        if not rest and had_bot:
            return "ai_chat", None, ""
        if rest:
            return route_uzdechka_intent(rest)

    return None, None, None


class AdminPanel(StatesGroup):
    managing = State()
    waiting_input = State()


class TsuefaState(StatesGroup):
    waiting_stake = State()


ADMIN_PM_COMMANDS = {
    "бан", "разбан", "мут", "размут", "удали_игрока", "админ_удали_тег",
    "админ_дай_тег", "админ_дай_ранг", "название", "описание", "ава",
    "закрепи", "открепи", "очисти", "напиши", "pay_tsuefa",
}

# callback adm:xxx → внутренняя команда
ADM_ACTIONS = {
    "ban": ("бан", "target"),
    "unban": ("разбан", "target"),
    "mute": ("мут", "target_then_minutes"),
    "unmute": ("размут", "target"),
    "delpl": ("удали_игрока", "target"),
    "deltags": ("админ_удали_тег", "target"),
    "tgtag": ("админ_дай_тег", "target_then_text"),
    "rank": ("админ_дай_ранг", "target_then_text"),
    "title": ("название", "text"),
    "desc": ("описание", "text"),
    "ava": ("ава", "photo"),
    "send": ("напиши", "text"),
    "pin": ("закрепи", "hint"),
    "unall": ("открепи", "instant"),
}


def admin_panel_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    is_core = user_id in CORE_ADMIN_IDS if user_id else False
    buttons = [
        [
            InlineKeyboardButton(text="🔨 Бан", callback_data="adm:ban"),
            InlineKeyboardButton(text="🕊 Разбан", callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton(text="🤐 Мут", callback_data="adm:mute"),
            InlineKeyboardButton(text="🔊 Размут", callback_data="adm:unmute"),
        ],
        [
            InlineKeyboardButton(text="💥 Удалить игрока", callback_data="adm:delpl"),
            InlineKeyboardButton(text="🗑 Мем-теги юзера", callback_data="adm:deltags"),
        ],
        [
            InlineKeyboardButton(text="🏷 Тег в TG", callback_data="adm:tgtag"),
            InlineKeyboardButton(text="🎖 Ранг в боте", callback_data="adm:rank"),
        ],
        [
            InlineKeyboardButton(text="📝 Название", callback_data="adm:title"),
            InlineKeyboardButton(text="📄 Описание", callback_data="adm:desc"),
        ],
        [
            InlineKeyboardButton(text="🖼 Аватарка", callback_data="adm:ava"),
            InlineKeyboardButton(text="💬 Написать в чат", callback_data="adm:send"),
        ],
        [
            InlineKeyboardButton(text="📌 Закрепить", callback_data="adm:pin"),
            InlineKeyboardButton(text="📍 Открепить всё", callback_data="adm:unall"),
        ],
    ]
    if is_core:
        buttons.append([InlineKeyboardButton(text="💰 Цены магазина", callback_data="adm:shoprices")])
        buttons.append([
            InlineKeyboardButton(text="👑 Выдать админку", callback_data="adm:gadmin"),
            InlineKeyboardButton(text="🗑 Забрать админку", callback_data="adm:radmin"),
        ])
        buttons.append([
            InlineKeyboardButton(text="⭐ Начислить", callback_data="adm:addstars"),
            InlineKeyboardButton(text="⭐ Отобрать", callback_data="adm:removestars"),
            InlineKeyboardButton(text="⭐ Обнулить", callback_data="adm:resetstars"),
        ])
        buttons.append([InlineKeyboardButton(text="—— Очистка ——", callback_data="adm:noop")])
        buttons.append([
            InlineKeyboardButton(text="🧹 10", callback_data="adm:clr:10"),
            InlineKeyboardButton(text="🧹 25", callback_data="adm:clr:25"),
            InlineKeyboardButton(text="🧹 50", callback_data="adm:clr:50"),
            InlineKeyboardButton(text="🧹 100", callback_data="adm:clr:100"),
        ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить меню", callback_data="adm:menu"),
        InlineKeyboardButton(text="🚪 Выход", callback_data="adm:exit"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_mute_minutes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="5 мин", callback_data="adm:mm:5"),
            InlineKeyboardButton(text="10 мин", callback_data="adm:mm:10"),
            InlineKeyboardButton(text="30 мин", callback_data="adm:mm:30"),
        ],
        [
            InlineKeyboardButton(text="60 мин", callback_data="adm:mm:60"),
            InlineKeyboardButton(text="120 мин", callback_data="adm:mm:120"),
            InlineKeyboardButton(text="1440 мин", callback_data="adm:mm:1440"),
        ],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")],
    ])


async def edit_admin_panel_message(
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
):
    data = await state.get_data()
    chat_id = data.get("panel_chat_id")
    message_id = data.get("panel_message_id")
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        print(f"[ADMIN_PANEL] edit failed: {e}")
        return False


async def admin_panel_text(state: FSMContext) -> str:
    data = await state.get_data()
    chat_id = data.get("chat_id")
    title = "?"
    if chat_id:
        cursor.execute("SELECT title FROM groups WHERE id = ?", (chat_id,))
        row = cursor.fetchone()
        if row:
            title = row[0]
    mode = data.get("mode", "normal")
    mode_text = "🥷 В тени" if mode == "stealth" else "📢 Обычный"
    return (
        f"✅ <b>Панель управления</b>\n"
        f"🎯 <b>Группа:</b> {title}\n"
        f"👁 <b>Режим:</b> {mode_text}\n\n"
        f"Нажми кнопку — бот попросит данные (юзер, текст или фото).\n"
        f"Или кидай сюда медиа/текст без кнопки — уйдёт в группу."
    )


async def dispatch_admin_cmd(
    message: Message,
    state: FSMContext,
    cmd: str,
    target: str | None = None,
    args: str = "",
):
    await main_text_handler(
        message, state,
        forced_cmd=cmd,
        forced_target=target,
        forced_args=args,
    )


class XPMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        msg_text = event.text or event.caption or ""
            
        # Запоминаем группу
        if event.chat.type in ['group', 'supergroup']:
            cursor.execute("INSERT INTO groups (id, title) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET title = excluded.title", (event.chat.id, event.chat.title))
            conn.commit()
            for member in event.new_chat_members or []:
                _remember_user(member)
            if event.left_chat_member:
                _remember_user(event.left_chat_member)
            
        user_id = event.from_user.id
        username = event.from_user.username.lower() if event.from_user.username else None
        now = time.time()
        
        cursor.execute("SELECT xp, lvl, last_t, is_banned, mute_until, custom_rank, vip_until FROM users WHERE id = ?", (user_id,))
        res = cursor.fetchone()
        
        if res: xp, lvl, last_t, is_banned, mute_until, custom_rank, vip_until = res
        else: xp, lvl, last_t, is_banned, mute_until, custom_rank, vip_until = 0, 0, 0, 0, 0, None, 0
            
        cursor.execute("INSERT INTO users (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (user_id,))
        if username: cursor.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
        conn.commit()
        
        if is_banned:
            try: await event.delete()
            except: pass
            return
            
        if mute_until and mute_until > now:
            try: await event.delete()
            except: pass
            return 
            
        if mute_until and mute_until <= now:
            cursor.execute("UPDATE users SET mute_until = 0 WHERE id = ?", (user_id,))
            conn.commit()
            
        # Проверка VIP: если истёк — сброс, восстанавливаем сохранённый ранг
        if vip_until and vip_until <= now:
            # Восстанавливаем сохранённый ранг, если он был
            cursor.execute("SELECT saved_rank FROM users WHERE id = ?", (user_id,))
            saved_row = cursor.fetchone()
            saved_rank_val = saved_row[0] if saved_row else None
            if saved_rank_val:
                cursor.execute("UPDATE users SET custom_rank = ?, vip_until = 0, saved_rank = NULL WHERE id = ?", (saved_rank_val, user_id))
            else:
                cursor.execute("UPDATE users SET custom_rank = NULL, vip_until = 0 WHERE id = ?", (user_id,))
            conn.commit()
            custom_rank = None
            vip_until = 0

        # Начисление опыта только в группах
        if event.chat.type in ['group', 'supergroup'] and msg_text and len(msg_text) > 3 and now - last_t > 30:
            base_xp = random.randint(5, 15)
            vip_mult = 1.5 if vip_until and vip_until > now else 1.0
            new_xp = xp + int(base_xp * vip_mult)
            new_lvl = int((new_xp / 100) ** 0.5)
            
            if new_lvl > lvl:
                rank_name = custom_rank if custom_rank else get_rank_name(new_lvl)
                await event.answer(f"📈 ГРАЦ! Твой уровень эластичности вырос до {new_lvl}!\nТеперь ты: {rank_name}")
                
            cursor.execute("UPDATE users SET xp = ?, lvl = ?, last_t = ? WHERE id = ?", 
                           (new_xp, new_lvl, now, user_id))
            conn.commit()
            
        return await handler(event, data)

dp.message.middleware(XPMiddleware())


# --- ПАНЕЛЬ АДМИНА В ЛС ---

@dp.message(F.chat.type == "private", Command("admin"))
async def pm_admin_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    cursor.execute("SELECT id, title FROM groups")
    groups = cursor.fetchall()
    if not groups:
        await message.reply("Я пока не состою ни в одной группе (или никто еще не писал).")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=f"grp_{gid}")] for gid, title in groups
    ])
    panel_msg = await message.reply("🛠 **Панель управления**\nВыберите группу:", reply_markup=kb)
    await state.update_data(panel_chat_id=panel_msg.chat.id, panel_message_id=panel_msg.message_id)


@dp.callback_query(F.data.startswith("grp_"))
async def pm_admin_group_selected(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    gid = int(call.data.split("_")[1])
    await state.update_data(chat_id=gid)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥷 В тени (Без следов)", callback_data="mode_stealth")],
        [InlineKeyboardButton(text="📢 Обычный (С уведомлениями)", callback_data="mode_normal")]
    ])
    await call.message.edit_text(
        "**Выберите режим работы:**\n\n"
        "🥷 **В тени** — команды выполняются молча, в группу ничего не пишется (полная скрытность, владелец не узнает).\n"
        "📢 **Обычный** — бот отправляет подтверждения в группу о банах, мутах и т.д.", reply_markup=kb)


@dp.callback_query(F.data.startswith("mode_"))
async def pm_admin_mode_selected(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    mode = call.data.split("_")[1]
    
    data = await state.get_data()
    chat_id = data.get('chat_id')
    if not chat_id:
        await call.message.edit_text("❌ Ошибка: группа не выбрана. Напиши /admin заново.")
        return
        
    await state.update_data(mode=mode)
    await state.set_state(AdminPanel.managing)
    text = await admin_panel_text(state)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel_keyboard(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:"))
async def admin_panel_callback(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("chat_id"):
        await call.answer("Сначала /admin → выбери группу", show_alert=True)
        return

    raw = call.data[4:]  # после adm:

    if raw == "noop":
        await call.answer()
        return

    if raw == "exit":
        await state.clear()
        text, kb = await build_start_text(call.message, override_user_id=call.from_user.id)
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                print(f"[ADMIN_EXIT] edit_text error: {e}")
        await call.answer()
        return

    if raw == "menu":
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        text = await admin_panel_text(state)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel_keyboard(call.from_user.id))
        await call.answer()
        return

    if raw.startswith("clr:"):
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        count = raw.split(":")[1]
        await dispatch_admin_cmd(call.message, state, "очисти", None, count)
        await call.answer("Готово")
        return

    if raw == "shoprices":
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Мут 30 мин — {SHOP_PRICES.get('mute_30', ('?',0))[1]} ⭐", callback_data="adm:sprice_mute_30")],
            [InlineKeyboardButton(text=f"Размут себя — {SHOP_PRICES.get('unmute_self', ('?',0))[1]} ⭐", callback_data="adm:sprice_unmute_self")],
            [InlineKeyboardButton(text=f"VIP-ранг — {SHOP_PRICES.get('vip_7', ('?',0))[1]} ⭐", callback_data="adm:sprice_vip_7")],
            [InlineKeyboardButton(text=f"Админка — {SHOP_PRICES.get('admin_40', ('?',0))[1]} ⭐", callback_data="adm:sprice_admin_40")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")],
        ])
        await call.message.edit_text(
            "💰 <b>Цены магазина</b>\nНажми на товар и пришли новую цену.\nЦены в Telegram Stars (XTR).",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()
        return

    if raw.startswith("sprice_"):
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        item_id = raw[7:]
        if item_id not in SHOP_PRICES:
            await call.answer("Товар не найден", show_alert=True)
            return
        await state.update_data(pending_shop_item=item_id)
        await state.set_state(AdminPanel.waiting_input)
        await call.message.edit_text(
            f"✏️ Отправь новую цену для <b>{SHOP_PRICES[item_id][0]}</b> (текущая: {SHOP_PRICES[item_id][1]} ⭐):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:shoprices")]
            ]),
        )
        await call.answer()
        return

    if raw.startswith("mm:"):
        minutes = raw.split(":")[1]
        target = data.get("pending_target")
        if not target:
            await call.answer("Сначала укажи пользователя", show_alert=True)
            return
        await dispatch_admin_cmd(call.message, state, "мут", target, minutes)
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        text = await admin_panel_text(state)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel_keyboard(call.from_user.id))
        await call.answer("Мут выдан")
        return

    if raw in ADM_ACTIONS:
        cmd, flow = ADM_ACTIONS[raw]
        if flow == "instant":
            await dispatch_admin_cmd(call.message, state, cmd)
            await call.answer("Готово")
            return

        if flow == "hint":
            await call.message.edit_text(
                "📌 В <b>группе</b> ответь (reply) на сообщение и напиши <code>закрепи</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")]
                ]),
            )
            await call.answer()
            return

        prompts = {
            "target": f"👤 Отправь <b>@username</b> или <b>ID</b> для: <code>{cmd}</code>",
            "target_then_minutes": "👤 Отправь @username или ID пользователя для мута:",
            "target_then_text": f"👤 Отправь @username или ID (потом текст для «{cmd}»):",
            "text": f"✏️ Отправь текст для: <code>{cmd}</code>",
            "photo": "🖼 Отправь <b>фото</b> — поставлю аватарку группы.",
        }
        await state.set_state(AdminPanel.waiting_input)
        await state.update_data(
            pending_action=raw,
            pending_step="target" if flow.startswith("target") else flow,
            pending_target=None,
        )
        await call.message.edit_text(
            prompts.get(flow if flow in prompts else "target", prompts["target"]),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
            ]),
        )
        await call.answer()
        return

    if raw == "gadmin":
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        await state.set_state(AdminPanel.waiting_input)
        await state.update_data(pending_action="gadmin", pending_step="target", pending_target=None)
        await call.message.edit_text(
            "👤 Отправь <b>@username</b> или <b>ID</b> кому выдать админку:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
            ]),
        )
        await call.answer()
        return

    if raw == "radmin":
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        await state.set_state(AdminPanel.waiting_input)
        await state.update_data(pending_action="radmin", pending_step="target", pending_target=None)
        await call.message.edit_text(
            "👤 Отправь <b>@username</b> или <b>ID</b> у кого забрать админку:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
            ]),
        )
        await call.answer()
        return

    if raw in ("addstars", "removestars", "resetstars"):
        if call.from_user.id not in CORE_ADMIN_IDS:
            await call.answer("⛔ Только для главных админов", show_alert=True)
            return
        labels = {"addstars": "⭐ Начислить звёзды", "removestars": "⭐ Отобрать звёзды", "resetstars": "⭐ Обнулить баланс"}
        await state.set_state(AdminPanel.waiting_input)
        await state.update_data(pending_action=raw, pending_step="target", pending_target=None)
        await call.message.edit_text(
            f"{labels.get(raw, '⚡')}\n\n👤 Отправь <b>@username</b> или <b>ID</b> пользователя:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
            ]),
        )
        await call.answer()
        return

    await call.answer()


@dp.message(F.chat.type == "private", AdminPanel.waiting_input)
async def admin_waiting_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    action = data.get("pending_action")
    step = data.get("pending_step")

    # Обработка смены цены магазина
    shop_item = data.get("pending_shop_item")
    if shop_item:
        text_in = (message.text or message.caption or "").strip()
        if not text_in.isdigit():
            await edit_admin_panel_message(state, "❌ Отправь число (цену в звёздах).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:shoprices")]]))
            try: await message.delete()
            except: pass
            return
        new_price = int(text_in)
        if new_price < 1:
            await edit_admin_panel_message(state, "❌ Цена должна быть ≥ 1 ⭐.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:shoprices")]]))
            try: await message.delete()
            except: pass
            return
        SHOP_PRICES[shop_item] = (SHOP_PRICES[shop_item][0], new_price)
        await state.update_data(pending_shop_item=None)
        await state.set_state(AdminPanel.managing)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Мут 30 мин — {SHOP_PRICES.get('mute_30', ('?',0))[1]} ⭐", callback_data="adm:sprice_mute_30")],
            [InlineKeyboardButton(text=f"Размут себя — {SHOP_PRICES.get('unmute_self', ('?',0))[1]} ⭐", callback_data="adm:sprice_unmute_self")],
            [InlineKeyboardButton(text=f"VIP-ранг — {SHOP_PRICES.get('vip_7', ('?',0))[1]} ⭐", callback_data="adm:sprice_vip_7")],
            [InlineKeyboardButton(text=f"Админка — {SHOP_PRICES.get('admin_40', ('?',0))[1]} ⭐", callback_data="adm:sprice_admin_40")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")],
        ])
        await edit_admin_panel_message(state, f"✅ Цена обновлена!\n\n💰 <b>Цены магазина</b>", reply_markup=kb)
        try: await message.delete()
        except: pass
        return

    # Обработка звёзд: addstars (начислить), removestars (отобрать), resetstars (обнулить)
    if action in ("addstars", "removestars", "resetstars"):
        text_in = (message.text or message.caption or "").strip()
        if not text_in:
            await edit_admin_panel_message(state, "Пустое сообщение. Укажи @username или ID юзера.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]]))
            try: await message.delete()
            except: pass
            return
        
        if step == "target":
            # Первый шаг: получили юзера, теперь просим сумму
            target_uid = await resolve_target(text_in)
            if not target_uid:
                await edit_admin_panel_message(state, f"❌ Пользователь {text_in} не найден.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]]))
                try: await message.delete()
                except: pass
                return
            ensure_user_exists(target_uid)
            await state.update_data(pending_target=target_uid, pending_target_raw=text_in)
            await state.update_data(pending_step="amount")
            labels = {"addstars": "начислить", "removestars": "отобрать", "resetstars": "обнулить"}
            action_label = labels.get(action, "изменить")
            if action == "resetstars":
                # Обнуление - не нужна сумма, сразу делаем
                cursor.execute("UPDATE balances SET balance = 0 WHERE user_id = ?", (target_uid,))
                conn.commit()
                await edit_admin_panel_message(state, f"✅ Баланс пользователя {text_in} обнулён!", reply_markup=admin_panel_keyboard(message.from_user.id))
                await state.set_state(AdminPanel.managing)
                await state.update_data(pending_action=None, pending_step=None, pending_target=None)
                try: await message.delete()
                except: pass
                return
            else:
                await edit_admin_panel_message(state, f"✏️ Сколько звёзд {action_label} пользователю {text_in}?\nВведи число:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]]))
                try: await message.delete()
                except: pass
                return
        elif step == "amount":
            # Второй шаг: получили сумму
            target_uid = data.get("pending_target")
            target_raw = data.get("pending_target_raw", "пользователь")
            if not text_in.isdigit() or int(text_in) < 0:
                await edit_admin_panel_message(state, "❌ Введи положительное число.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]]))
                try: await message.delete()
                except: pass
                return
            amount = int(text_in)
            if action == "addstars":
                cursor.execute("INSERT INTO balances (user_id, balance) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING", (target_uid,))
                cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, target_uid))
                conn.commit()
                reply = f"✅ Пользователю {target_raw} начислено {amount} ⭐!"
                try:
                    await bot.send_message(target_uid, f"💸 Админ начислил тебе {amount} ⭐ на вирт. баланс!")
                except: pass
            elif action == "removestars":
                cursor.execute("INSERT INTO balances (user_id, balance) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING", (target_uid,))
                cursor.execute("UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?", (amount, target_uid))
                conn.commit()
                reply = f"✅ У пользователя {target_raw} отобрано {amount} ⭐!"
            await edit_admin_panel_message(state, reply, reply_markup=admin_panel_keyboard(message.from_user.id))
            await state.set_state(AdminPanel.managing)
            await state.update_data(pending_action=None, pending_step=None, pending_target=None)
            try: await message.delete()
            except: pass
            return
        return

    if action in ("gadmin", "radmin"):
        text_in = (message.text or message.caption or "").strip()
        if not text_in:
            await edit_admin_panel_message(state, "Пустое сообщение. Укажи @username или ID юзера.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]]))
            try: await message.delete()
            except: pass
            return
        target_uid = await resolve_target(text_in)
        if not target_uid:
            await edit_admin_panel_message(state, f"❌ Не смог найти {text_in}.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")]]))
            try: await message.delete()
            except: pass
            return
        ensure_user_exists(target_uid)
        if action == "gadmin":
            if target_uid not in ADMIN_IDS:
                ADMIN_IDS.append(target_uid)
            PURCHASED_ADMIN_IDS.add(target_uid)
            cursor.execute("UPDATE users SET custom_rank = '👑 Админ' WHERE id = ?", (target_uid,))
            conn.commit()
            reply = f"✅ Пользователь {target_uid} теперь админ бота."
        else:
            if target_uid in CORE_ADMIN_IDS:
                reply = "❌ Нельзя снять админку с главного админа."
            else:
                if target_uid in ADMIN_IDS:
                    ADMIN_IDS.remove(target_uid)
                PURCHASED_ADMIN_IDS.discard(target_uid)
                cursor.execute("UPDATE users SET custom_rank = NULL WHERE id = ?", (target_uid,))
                conn.commit()
                reply = f"✅ Админка у пользователя {target_uid} отобрана."
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        menu_text = await admin_panel_text(state)
        await edit_admin_panel_message(state, f"{reply}\n\n{menu_text}", reply_markup=admin_panel_keyboard(message.from_user.id))
        try: await message.delete()
        except: pass
        return

    if not action or action not in ADM_ACTIONS:
        await state.set_state(AdminPanel.managing)
        return

    cmd, flow = ADM_ACTIONS[action]

    if step == "photo":
        if not message.photo:
            await edit_admin_panel_message(
                state,
                "Нужно фото. Или нажми отмену.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
                ]),
            )
            try:
                await message.delete()
            except Exception:
                pass
            return
        await dispatch_admin_cmd(message, state, cmd)
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        return

    text_in = (message.text or message.caption or "").strip()
    if not text_in:
        await edit_admin_panel_message(
            state,
            "Пустое сообщение. Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
            ]),
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    if step == "target":
        await state.update_data(pending_target=text_in)
        if flow == "target_then_minutes":
            await state.update_data(pending_step="minutes")
            await edit_admin_panel_message(state, "⏱ На сколько замутить?", reply_markup=admin_mute_minutes_keyboard())
            try:
                await message.delete()
            except Exception:
                pass
            return
        if flow == "target_then_text":
            await state.update_data(pending_step="text")
            await edit_admin_panel_message(
                state,
                f"✏️ Теперь текст для «{cmd}» (до 16 симв. для тега в TG):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:menu")]
                ]),
            )
            try:
                await message.delete()
            except Exception:
                pass
            return
        await dispatch_admin_cmd(message, state, cmd, text_in)
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        return

    if step == "text":
        target = data.get("pending_target")
        await dispatch_admin_cmd(message, state, cmd, target, text_in)
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)
        return

    if step == "minutes":
        target = data.get("pending_target")
        nums = re.findall(r"\d+", text_in)
        minutes = nums[0] if nums else "10"
        await dispatch_admin_cmd(message, state, cmd, target, minutes)
        await state.set_state(AdminPanel.managing)
        await state.update_data(pending_action=None, pending_step=None, pending_target=None)


@dp.message(F.chat.type == "private", Command("exit"))
async def pm_admin_exit(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await build_start_text(message)
    await message.reply(text, reply_markup=kb)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    user_id = message.from_user.id
    is_pm = message.chat.type == "private"
    
    help_text = (
        "🤖 <b>УЗДЕЧКА БОТ</b>\n\n"
        "📈 <b>Система опыта</b>\n"
        "Общайся в чате (от 4 символов) — раз в ~30 сек +5–15 XP.\n"
        "Уровень ≈ √(XP/100). Новый уровень → «ГРАЦ!»\n\n"
        "💬 <b>Главное</b>\n"
        "Напиши <code>Уздечка</code> + вопрос — ИИ сам всё поймёт и ответит.\n"
        "ИИ знает все команды бота, решит математику, пошутит.\n"
        "Примеры:\n"
        "<code>Уздечка как повысить ранг?</code>\n"
        "<code>Уздечка реши 2+2*2</code>\n"
        "<code>Уздечка напиши сочинение на тему</code>\n\n"
        "⚡ <b>Слэш-команды</b>\n"
        "🔹 <code>/help</code> — это сообщение\n"
        "🔹 <code>/status</code> — профиль\n"
        "🔹 <code>/roll</code> — рулетка (раз в 24ч)\n"
        "🔹 <code>/shop</code> — магазин за звёзды\n"
        "🔹 <code>/play</code> — Тузик-Ножницы-Бумага на XP (в группе)\n"
        "🔹 <code>/id</code> — узнать свой Telegram ID\n\n"
        "🎯 <b>Пасхалки</b>\n"
        "Слова: влад, порвал, туз, изолента, шов, скорая, хуй — бот реагирует.\n"
    )
    
    kb = None
    if is_pm:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="start:back")]
        ])
        if user_id in ADMIN_IDS:
            help_text += (
                "\n━━━━━━━━━━━━━━━━━━\n"
                "👑 <b>АДМИН-ПАНЕЛЬ</b>\n"
                "<i>/admin в ЛС → выбрать группу → кнопки</i>\n\n"
                "👮‍♂️ <b>Модерация:</b>\n"
                "🔸 бан / разбан / мут / размут\n"
                "🔸 удалить игрока / удалить теги\n"
                "🔸 тег в TG (до 16 симв.) / ранг в боте\n\n"
                "🛠 <b>Группа:</b>\n"
                "🔸 название / описание / аватарка\n"
                "🔸 закрепить / открепить\n"
                "🔸 очистить N сообщений\n"
                "🔸 написать в чат\n\n"
                "⚙️ <b>Сервис:</b>\n"
                "🔸 /uzdechka_off — выключить бота в группе\n"
                "🔸 /uzdechka_on — включить обратно\n"
                "(доступно админам группы и владельцам бота)\n"
            )

    await message.answer(help_text, parse_mode="HTML", reply_markup=kb)


async def build_start_text(message: Message, override_user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    uid = override_user_id or message.from_user.id
    cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
    bal_row = cursor.fetchone()
    bal = bal_row[0] if bal_row else 0
    cursor.execute("SELECT xp, lvl FROM users WHERE id = ?", (uid,))
    row = cursor.fetchone()
    xp, lvl = row if row else (0, 0)
    rank = get_rank_name(lvl)
    cursor.execute("SELECT last_roll, roll_count FROM cooldowns WHERE user_id = ?", (uid,))
    cooldown_row = cursor.fetchone()
    if cooldown_row:
        last_roll, used = cooldown_row
        if time.time() - last_roll >= 86400:
            used = 0
    else:
        used = 0
    ROLL_FREE_DAILY = 3
    rolls_left = max(0, ROLL_FREE_DAILY - used)
    text = (
        f"🤖 УЗДЕЧКА — твой токсичный шов в чате 9А\n\n"
        f"👤 Твой профиль\n"
        f"├ Уровень: {lvl}\n"
        f"├ Ранг: {rank}\n"
        f"├ XP: {xp}\n"
        f"├ Баланс: {bal} ⭐\n"
        f"└ Бросков рулетки сегодня: {used}/{ROLL_FREE_DAILY} (осталось {rolls_left})\n\n"
        f"ℹ️ Напиши Уздечка + вопрос — и я отвечу!\n"
        f"Или выбери действие:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛒 Магазин", callback_data="start:shop"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="start:status"),
        ],
        [
            InlineKeyboardButton(text="🎮 Игра (КНБ)", callback_data="start:play"),
            InlineKeyboardButton(text="🎲 Рулетка 9А", callback_data="start:roll"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Помощь", callback_data="start:help"),
        ],
    ])
    if uid in ADMIN_IDS:
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="start:admin")]
        )
    return text, kb


# --- ОБРАБОТЧИК СИСТЕМНЫХ СООБЩЕНИЙ (Удаление следов) ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Приветственное сообщение с инлайн-кнопками."""
    is_pm = message.chat.type == "private"
    uid = message.from_user.id
    if not is_pm:
        await message.answer(
            "🤖 Уздечка на связи! Напиши /help или «Уздечка» + вопрос.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Команды", callback_data="start:help")],
            ])
        )
        return
    ensure_user_exists(uid)
    # Звёзды не начисляются автоматически — только через магазин или админа
    text, kb = await build_start_text(message)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("start:"))
async def start_callback(call: CallbackQuery, state: FSMContext):
    """Обработка инлайн-кнопок из /start."""
    action = call.data[6:]
    uid = call.from_user.id

    if action == "shop":
        p = SHOP_PRICES
        text = (
            f"🛒 МАГАЗИН УЗДЕЧКИ\n\n"
            f"🤐 Мут обидчика 30 мин — {p['mute_30'][1]}⭐\n"
            f"🔊 Размут себя — {p['unmute_self'][1]}⭐\n"
            f"🎭 VIP-ранг на 7 дней — {p['vip_7'][1]}⭐\n"
            f"👑 Админка в боте — {p['admin_40'][1]}⭐\n"
            f"🎲 Доп. бросок рулетки — {p['roll_1'][1]}⭐\n\n"
            f"Выбирай:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🤐 Мут обидчика 30 мин — {p['mute_30'][1]} ⭐", callback_data="shop:mute_30")],
            [InlineKeyboardButton(text=f"🔊 Размут себя — {p['unmute_self'][1]} ⭐", callback_data="shop:unmute_self")],
            [InlineKeyboardButton(text=f"🎭 VIP-ранг на 7 дней — {p['vip_7'][1]} ⭐", callback_data="shop:vip_7")],
            [InlineKeyboardButton(text=f"👑 Админка в боте — {p['admin_40'][1]} ⭐", callback_data="shop:admin_40")],
            [InlineKeyboardButton(text=f"🎲 Доп. бросок рулетки — {p['roll_1'][1]} ⭐", callback_data="shop:roll_1")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="start:back")],
        ])
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    if action == "status":
        cursor.execute("SELECT xp, lvl, custom_rank, vip_until FROM users WHERE id = ?", (uid,))
        res = cursor.fetchone()
        xp, lvl, cr, vip = res if res else (0, 0, None, 0)
        rank = cr if cr else get_rank_name(lvl)
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        bal_row = cursor.fetchone()
        bal = bal_row[0] if bal_row else 0
        text = f"👤 Твой профиль:\n🏆 Уровень: {lvl}\n✨ Опыт: {xp}\n🎖 Ранг: {rank}\n💰 Баланс: {bal} ⭐"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="start:back")],
        ])
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    if action == "play":
        if call.message.chat.type == "private":
            await call.answer("🎮 Игра только в группе! Напиши /play в чате.", show_alert=True)
        else:
            await cmd_play(call.message)
        await call.answer()
        return

    if action == "roll":
        await do_roll_9a(call.message)
        await call.answer()
        return

    if action == "help":
        await cmd_help(call.message)
        await call.answer()
        return

    if action == "admin":
        if uid not in ADMIN_IDS:
            await call.answer("⛔ Только для администраторов", show_alert=True)
            return
        cursor.execute("SELECT id, title FROM groups")
        groups = cursor.fetchall()
        if not groups:
            await call.answer("Я пока не состою ни в одной группе", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"grp_{gid}")] for gid, title in groups
        ])
        try:
            panel_msg = await call.message.edit_text("🛠 **Панель управления**\nВыберите группу:", reply_markup=kb)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                panel_msg = call.message  # fallback, используем текущее сообщение
            else:
                panel_msg = call.message
        await state.update_data(panel_chat_id=panel_msg.chat.id, panel_message_id=panel_msg.message_id)
        await call.answer()
        return

    if action == "back":
        text, kb = await build_start_text(call.message, override_user_id=call.from_user.id)
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                print(f"[BACK] edit_text error: {e}")
        try:
            await call.answer()
        except Exception:
            pass
        return

    try:
        await call.answer()
    except Exception:
        pass


@dp.message(F.new_chat_members | F.left_chat_member | F.new_chat_title | F.new_chat_photo | F.delete_chat_photo | F.pinned_message)
async def delete_system_messages(message: Message):
    # Бот будет тихо удалять сообщения о том, что кто-то сменил аву, название, закрепил сообщение
    # или что кто-то вошел/вышел из группы.
    try:
        await message.delete()
    except Exception:
        pass


# --- ВКЛ/ВЫКЛ УЗДЕЧКИ ---
@dp.message(Command("uzdechka_on"))
async def cmd_uzdechka_on(message: Message):
    if message.chat.type == "private":
        await message.reply("Эта команда только для группы.")
        return
    user_id = message.from_user.id
    member = await bot.get_chat_member(message.chat.id, user_id)
    if member.status not in ("creator", "administrator") and user_id not in ADMIN_IDS:
        await message.reply("Только админ группы может включить уздечку.")
        return
    cursor.execute("UPDATE groups SET enabled = 1 WHERE id = ?", (message.chat.id,))
    conn.commit()
    await message.reply("✅ Уздечка снова в деле! Бот реагирует на сообщения.")

@dp.message(Command("uzdechka_off"))
async def cmd_uzdechka_off(message: Message):
    if message.chat.type == "private":
        await message.reply("Эта команда только для группы.")
        return
    user_id = message.from_user.id
    member = await bot.get_chat_member(message.chat.id, user_id)
    if member.status not in ("creator", "administrator") and user_id not in ADMIN_IDS:
        await message.reply("Только админ группы может выключить уздечку.")
        return
    cursor.execute("UPDATE groups SET enabled = 0 WHERE id = ?", (message.chat.id,))
    conn.commit()
    await message.reply("😴 Уздечка ушла спать. Бот не реагирует на сообщения (кроме команд админов).")

# --- СЛЭШ В ГРУППЕ: /otvet текст — без @бота (aiogram Command) ---
@dp.message(GROUP_CHAT, Command("otvet", "ответь", "answer", "ai"))
async def group_slash_otvet(message: Message, command: CommandObject, state: FSMContext):
    await _run_group_slash(message, state, "ai", command.args)


@dp.message(GROUP_CHAT, Command("sochinenie", "сочинение", "essay"))
async def group_slash_essay(message: Message, command: CommandObject, state: FSMContext):
    await _run_group_slash(message, state, "ai_long", command.args)


@dp.message(GROUP_CHAT, Command("foto", "фото", "photo"))
async def group_slash_photo(message: Message, command: CommandObject, state: FSMContext):
    await _run_group_slash(message, state, "ai_photo", command.args)


@dp.message(GROUP_CHAT, Command("status", "статус", "profile", "профиль"))
async def group_slash_status(message: Message, state: FSMContext):
    await _run_group_slash(message, state, "статус", None)


@dp.message(Command("frazy", "фразы", "phrases"))
async def slash_phrases(message: Message, state: FSMContext):
    await _run_group_slash(message, state, "фразы", None)


async def _run_group_slash(message: Message, state: FSMContext, cmd: str, args: str | None):
    await main_text_handler(
        message, state,
        forced_cmd=cmd,
        forced_args=(args or "").strip() if args else "",
    )


# --- ПРИЁМ СТАВКИ ЦУЕФА В ЛС (ДОЛЖЕН БЫТЬ ДО main_text_handler) ---
@dp.message(F.chat.type == "private", TsuefaState.waiting_stake)
async def tsuefa_stake_input(message: Message, state: FSMContext):
    """Приём ставки в ЛС для ЦУЕФА."""
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.reply(f"❌ Введи число от {TSUEFA_MIN_BET} до {TSUEFA_MAX_BET} ⭐.")
        return
    bet = int(text)
    if bet < TSUEFA_MIN_BET or bet > TSUEFA_MAX_BET:
        await message.reply(f"❌ Ставка должна быть от {TSUEFA_MIN_BET} до {TSUEFA_MAX_BET} ⭐.")
        return

    data = await state.get_data()
    game_id = data.get("tsuefa_game_id")
    g = load_game(game_id)
    if not g or g["state"] != "joining":
        await state.clear()
        await message.reply("❌ Игра уже началась или не найдена.")
        return

    uid_str = str(uid)
    if uid_str in g["players"]:
        await state.clear()
        await message.reply("Ты уже в игре!")
        return

    cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
    row = cursor.fetchone()
    bal = row[0] if row else 0

    # Атомарное списание: UPDATE с условием balance >= bet
    cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (bet, uid, bet))
    if cursor.rowcount > 0:
        conn.commit()
        paid = True
        await message.reply(f"✅ С баланса списано {bet} ⭐. Ты в игре!")
    else:
        shortage = bet - bal
        # НЕ списываем вирт-баланс сейчас! Зашиваем сколько списать в payload
        try:
            await bot.send_invoice(
                chat_id=uid,
                title=f"ЦУЕФА: ставка {bet} ⭐",
                description=f"На балансе {bal} ⭐. Доплати {shortage} ⭐.",
                prices=[LabeledPrice(label="XTR", amount=shortage)],
                provider_token="",
                payload=f"tsuefa_pay_{game_id}_{uid}_{bal}",  # bal = сколько вирт-звёзд списать при успехе
                currency="XTR",
            )
            await message.reply(f"💳 Выставлен счёт на {shortage} ⭐. Оплати — и ты в игре!")
        except Exception as e:
            print(f"[TSUEFA] invoice to {uid}: {e}")
            await message.reply("❌ Не удалось выставить счёт. Попробуй позже.")
            return
        paid = False

    name = message.from_user.username or message.from_user.full_name
    g["players"][uid_str] = {"name": name, "bet": bet, "ready": paid}
    g["bet_pool"] += bet
    if paid:
        g.setdefault("payment_status", {})[uid_str] = True
        g.setdefault("frozen_ids", []).append(uid)
    save_game(game_id, g)

    try:
        await edit_game_message(g)
    except Exception as e:
        print(f"[TSUEFA] edit after stake: {e}")

    await state.clear()


# --- ГЛАВНЫЙ ОБРАБОТЧИК ТЕКСТА И МЕДИА ---
@dp.message(F.text | F.caption | F.photo | F.video | F.animation | F.sticker | F.voice | F.video_note | F.document)
async def main_text_handler(
    message: Message,
    state: FSMContext,
    forced_cmd: str | None = None,
    forced_target: str | None = None,
    forced_args: str | None = "",
):
    msg_text = message.text or message.caption or ""
    text_lower = msg_text.lower()
    user_id = message.from_user.id
    is_pm = message.chat.type == "private"
    
    group_chat_id = message.chat.id
    stealth = False
    
    if is_pm and not forced_cmd:
        current_state = await state.get_state()
        if current_state in (AdminPanel.waiting_input.state, TsuefaState.waiting_stake.state):
            return  # в процессе ввода — пропускаем, есть отдельные хендлеры
        data = await state.get_data()
        if data and data.get('chat_id'):
            group_chat_id = data['chat_id']
            stealth = data.get('mode') == 'stealth'
            # У админа есть активная панель — перенаправляем в неё
        else:
            # Нет активной панели — ничего не отвечаем в ЛС
            return
    elif is_pm and forced_cmd:
        if forced_cmd in ADMIN_PM_COMMANDS and user_id not in ADMIN_IDS:
            return
        data = await state.get_data()
        if data and data.get('chat_id'):
            group_chat_id = data['chat_id']
            stealth = data.get('mode') == 'stealth'

    # Проверка: выключена ли уздечка в этой группе
    if not is_pm and not forced_cmd:
        cursor.execute("SELECT enabled FROM groups WHERE id = ?", (group_chat_id,))
        row = cursor.fetchone()
        enabled = row[0] if row else 1
        if not enabled:
            raw_s = (message.text or message.caption or "").strip().lower()
            # Пропускаем только админские команды и on/off
            if not raw_s.startswith(("/uzdechka_on", "/uzdechka_off")):
                return

    # Слэш в группе ловят отдельные хендлеры — здесь не дублируем
    if not is_pm and not forced_cmd:
        raw = (message.text or message.caption or "").strip()
        if raw.startswith("/") and not raw.lower().startswith(("/frazy", "/фразы", "/phrases", "/id", "/roll", "/shop", "/anon", "/p_mute", "/play", "/tsuefa", "/status", "/otvet", "/ответь", "/sochinenie", "/сочинение", "/foto", "/фото", "/help", "/start", "/admin", "/balance", "/uzdechka_on", "/uzdechka_off")):
            return

    if forced_cmd:
        cmd, target, args = forced_cmd, forced_target, forced_args or ""
    else:
        # Игровые команды обрабатываем здесь же
        if msg_text.strip().lower() == "/id" or msg_text.strip().lower().startswith("/id@"):
            await cmd_my_id(message)
            return
        if msg_text.strip().lower() == "/roll" or msg_text.strip().lower().startswith("/roll@"):
            await roll_9a(message)
            return
        if msg_text.strip().lower() == "/shop" or msg_text.strip().lower().startswith("/shop@"):
            await cmd_shop(message)
            return
        if msg_text.strip().lower() == "/play" or msg_text.strip().lower().startswith("/play@"):
            await cmd_play(message)
            return
        if msg_text.strip().lower() == "/send" or msg_text.strip().lower().startswith("/send@"):
            await cmd_send(message)
            return
        if msg_text.strip().lower() == "/tsuefa" or msg_text.strip().lower().startswith("/tsuefa@"):
            if message.chat.type == "private":
                await message.reply("🎮 ЦУЕФА только в группе! Напиши /tsuefa в чате.")
            else:
                await cmd_tsuefa(message, state)
            return
        # /anon и /p_mute с аргументами — передаём в use_shop_items
        if msg_text.strip().lower().startswith("/anon") or msg_text.strip().lower().startswith("/p_mute"):
            await use_shop_items(message, state)
            return
        cmd, target, args = extract_bot_command(msg_text, is_pm)
    if not cmd and message.photo:
        cap = message.caption or ""
        if message_has_uzdechka(cap):
            m = re.search(UZDECHKA_NAMES_RE, cap, re.I)
            rest = cap[m.end():].strip().lstrip(",:;!-— ") if m else ""
            cmd, target, args = "ai_chat", None, rest
        elif not is_pm:
            # Авто-анализ ВСЕХ фото в группе через Gemini
            cmd, target, args = "ai_photo", None, cap or "Чё на фото? Опиши в стиле Уздечки."
    print(f"[DEBUG] cmd={cmd}, target={target}, args={args}, is_pm={is_pm}")
    
    if cmd:
        # Прямые команды (магазин, рулетка, игра, помощь, id)
        if cmd == "cmd_shop":
            await cmd_shop(message)
            return
        if cmd == "cmd_roll":
            await roll_9a(message)
            return
        if cmd == "cmd_play":
            await cmd_play(message)
            return
        if cmd == "cmd_help":
            await cmd_help(message)
            return
        if cmd == "cmd_status":
            cursor.execute("SELECT xp, lvl, custom_rank FROM users WHERE id = ?", (user_id,))
            res = cursor.fetchone()
            xp, lvl, custom_rank = res if res else (0, 0, None)
            rank = custom_rank if custom_rank else get_rank_name(lvl)
            text = f"👤 Твой профиль:\n🏆 Уровень: {lvl}\n✨ Опыт: {xp}\n🎖 Ранг: {rank}"
            if is_pm:
                await message.reply(text)
            else:
                await message.answer(text)
            return
        if cmd == "cmd_id":
            await cmd_my_id(message)
            return

        is_admin_cmd = cmd in ["бан", "разбан", "мут", "размут", "удали_игрока", "админ_удали_тег", "админ_дай_тег", "админ_дай_ранг", 
                               "название", "описание", "ава", "закрепи", "открепи", "очисти", "напиши", "pay_tsuefa"]
        if is_admin_cmd and user_id not in ADMIN_IDS and cmd not in (
            "ai", "ai_long", "ai_photo", "ai_chat", "фразы", "статус", "создай_тег", "удали_тег"
        ):
            if not is_pm: await message.reply("❌ Эта команда доступна только Главному Админу!")
            return

        # Админские команды в группе не удаляем (reply нужен для resolve_target)
        # Автоудаление — если команда от админа, но без цели (название, очисти и т.д.)
            
        target_id = None
        target_cmds = ["бан", "разбан", "мут", "размут", "удали_игрока", "админ_удали_тег", "админ_дай_тег", "админ_дай_ранг"]
        if (target or (cmd in target_cmds and message.reply_to_message)) and cmd not in ["название", "описание", "ава", "закрепи", "открепи", "очисти", "напиши"]:
            target_id = await resolve_target(target, group_chat_id, message)
            if not target_id:
                not_found_text = f"❌ Не смог найти {target}. Укажи ID, ответь на сообщение участника в группе или отправь упоминание через выбор пользователя Telegram."
                if is_pm and forced_cmd in ADMIN_PM_COMMANDS:
                    await edit_admin_panel_message(
                        state,
                        not_found_text,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu")]
                        ]),
                    )
                    try:
                        await message.delete()
                    except Exception:
                        pass
                else:
                    await message.reply(not_found_text)
                return
            ensure_user_exists(target_id)

        pm_reply = ""
        group_reply = ""
        
        if cmd == "статус":
            cursor.execute("SELECT xp, lvl, custom_rank FROM users WHERE id = ?", (user_id,))
            res = cursor.fetchone()
            xp, lvl, custom_rank = res if res else (0, 0, None)
            rank = custom_rank if custom_rank else get_rank_name(lvl)
            pm_reply = f"👤 Твой профиль:\n🏆 Уровень: {lvl}\n✨ Опыт: {xp}\n🎖 Ранг: {rank}"
            group_reply = pm_reply
            
        elif cmd == "создай_тег":
            tag_name, tag_content = target, args
            cursor.execute("SELECT lvl FROM users WHERE id = ?", (user_id,))
            lvl = cursor.fetchone()[0]
            if lvl < 5 and user_id not in ADMIN_IDS:
                pm_reply = "❌ Создавать теги можно только с 5 уровня!"
            else:
                try:
                    cursor.execute("INSERT INTO tags (name, content, owner_id) VALUES (?, ?, ?)", (tag_name, tag_content, user_id))
                    conn.commit()
                    pm_reply = f"✅ Тег #{tag_name} успешно создан!"
                    group_reply = pm_reply
                except sqlite3.IntegrityError:
                    pm_reply = f"❌ Тег #{tag_name} уже существует!"
            if not group_reply: group_reply = pm_reply
            
        elif cmd == "удали_тег":
            tag_name = target
            cursor.execute("SELECT owner_id FROM tags WHERE name = ?", (tag_name,))
            tag = cursor.fetchone()
            if not tag:
                pm_reply = f"❌ Тег #{tag_name} не найден!"
            else:
                cursor.execute("SELECT lvl FROM users WHERE id = ?", (user_id,))
                lvl = cursor.fetchone()[0]
                if lvl >= 10 or user_id == tag[0] or user_id in ADMIN_IDS:
                    cursor.execute("DELETE FROM tags WHERE name = ?", (tag_name,))
                    conn.commit()
                    pm_reply = f"🗑 Тег #{tag_name} удален!"
                else:
                    pm_reply = "❌ Удалять чужие теги можно только с 10 уровня!"
            if not group_reply: group_reply = pm_reply
            
        elif cmd == "фразы":
            text = format_all_phrases()
            parts = []
            if len(text) > 4000:
                chunk = ""
                for line in text.split("\n"):
                    if len(chunk) + len(line) + 1 > 3900:
                        parts.append(chunk)
                        chunk = line
                    else:
                        chunk = f"{chunk}\n{line}" if chunk else line
                if chunk:
                    parts.append(chunk)
            else:
                parts = [text]

            for i, part in enumerate(parts):
                if is_pm:
                    await message.reply(part, parse_mode="HTML")
                else:
                    await message.answer(part, parse_mode="HTML")
            return

        elif cmd == "ai_chat":
            query = (args or "").strip()
            is_admin = user_id in ADMIN_IDS

            if message.photo:
                photo_msg = message
                try:
                    file_id = photo_msg.photo[-1].file_id
                    file = await bot.get_file(file_id)
                    downloaded = await bot.download_file(file.file_path)
                    image_bytes = downloaded.read()
                    prompt = query or "Опиши картинку в стиле Уздечки."
                    full_prompt = f"{BOT_KNOWLEDGE}\n\n{get_user_profile_context(user_id)}\n\nВопрос: {prompt}"
                    reply, from_ai = await get_chat_response(
                        full_prompt, mode="short", image_bytes=image_bytes, allow_canned=False
                    )
                    icon = "🤖" if from_ai else "💬"
                    group_reply = f"{icon} {reply}"
                    pm_reply = group_reply
                except Exception as e:
                    group_reply = f"❌ Ошибка фото: {e}"
                    pm_reply = group_reply
            elif not query:
                group_reply = random.choice(UZDECHKA_GREETINGS)
                pm_reply = group_reply
            else:
                display, from_ai, acmd, atgt, aargs = await get_uzdechka_brain_reply(
                    query, user_id, is_admin
                )
                if acmd and ai_action_allowed(acmd, user_id, is_pm):
                    # ИИ решил выполнить команду бота — делаем это через dispatch
                    if acmd == "статус":
                        cursor.execute(
                            "SELECT xp, lvl, custom_rank FROM users WHERE id = ?", (user_id,)
                        )
                        res = cursor.fetchone()
                        xp, lvl, cr = res if res else (0, 0, None)
                        rank = cr if cr else get_rank_name(lvl)
                        display = (
                            f"{display}\n\n👤 Уровень: {lvl} | XP: {xp} | Ранг: {rank}"
                        )
                    elif acmd in ("фразы", "создай_тег", "удали_тег") or acmd in ADMIN_PM_COMMANDS:
                        if acmd in ADMIN_PM_COMMANDS and not is_pm and not is_admin:
                            # Не админ в группе просит админ-команду — отказ
                            display += "\n\n👑 Админ-действия доступны только админам."
                        elif acmd in ADMIN_PM_COMMANDS and not is_pm and is_admin:
                            # Админ в группе — выполняем сразу (бот уже знает group_chat_id)
                            await dispatch_admin_cmd(message, state, acmd, atgt, aargs or "")
                            return
                        else:
                            # ЛС или не-админская команда
                            if is_pm:
                                data_s = await state.get_data()
                                if data_s.get("chat_id") or acmd not in ADMIN_PM_COMMANDS:
                                    await dispatch_admin_cmd(message, state, acmd, atgt, aargs or "")
                                    return
                                else:
                                    display += "\n\nСначала /admin → выбери группу."
                            else:
                                await dispatch_admin_cmd(message, state, acmd, atgt, aargs or "")
                                return
                icon = "🤖" if from_ai else "💬"
                group_reply = f"{icon} {display}"
                pm_reply = group_reply

        elif cmd == "ai":
            if not (args or "").strip():
                hint = "❌ Напиши вопрос: `/otvet` или просто `Уздечка твой вопрос`"
                pm_reply = hint
                group_reply = hint
            else:
                reply, from_ai = await get_chat_response(args, mode="short")
                icon = "🤖" if from_ai else "💬"
                pm_reply = f"{icon} {reply}"
                group_reply = pm_reply

        elif cmd == "ai_long":
            if not (args or "").strip():
                hint = "❌ Укажи тему: `/sochinenie про изоленту`"
                pm_reply = hint
                group_reply = hint
            else:
                reply, from_ai = await get_chat_response(args, mode="long")
                icon = "🤖" if from_ai else "💬"
                pm_reply = f"{icon} {reply}"
                group_reply = pm_reply

        elif cmd == "ai_photo":
            photo_msg = message if message.photo else (message.reply_to_message if message.reply_to_message and message.reply_to_message.photo else None)
            if not photo_msg:
                pm_reply = "❌ Прикрепи фото или ответь на сообщение с фото."
                group_reply = pm_reply
            else:
                try:
                    file_id = photo_msg.photo[-1].file_id
                    file = await bot.get_file(file_id)
                    downloaded = await bot.download_file(file.file_path)
                    image_bytes = downloaded.read()
                    prompt = args or "Опиши, что на картинке, в стиле бота Уздечка — с матом и абсурдом."
                    reply, from_ai = await get_chat_response(prompt, mode="short", image_bytes=image_bytes)
                    icon = "🤖" if from_ai else "💬"
                    pm_reply = f"{icon} {reply}"
                    group_reply = pm_reply
                except Exception as e:
                    pm_reply = f"❌ Ошибка анализа фото: {e}"
                    group_reply = pm_reply

        
        # --- АДМИНСКИЕ КОМАНДЫ ---
        elif cmd == "бан":
            if not target_id:
                pm_reply = f"❌ Не указан пользователь для бана."
                group_reply = pm_reply
            else:
                cursor.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (target_id,))
                conn.commit()
                try:
                    await bot.ban_chat_member(group_chat_id, target_id, revoke_messages=True)
                    pm_reply = f"🔨 Пользователь {target} забанен!"
                except Exception as e:
                    err_msg = str(e)
                    if "administrator" in err_msg.lower():
                        pm_reply = f"⚠️ {target} — админ чата, нельзя забанить."
                    elif "not enough rights" in err_msg.lower():
                        pm_reply = f"⚠️ У бота недостаточно прав для бана."
                    else:
                        pm_reply = f"❌ Ошибка бана: {e}"
                    print(f"[MOD] ban_chat_member failed for {target_id}: {e}")
                group_reply = pm_reply
            
        elif cmd == "разбан":
            if not target_id:
                pm_reply = f"❌ Не указан пользователь для разбана."
                group_reply = pm_reply
            else:
                cursor.execute("UPDATE users SET is_banned = 0 WHERE id = ?", (target_id,))
                conn.commit()
                try:
                    await bot.unban_chat_member(group_chat_id, target_id, only_if_banned=False)
                    pm_reply = f"🕊 Пользователь {target} разбанен."
                except Exception as e:
                    pm_reply = f"❌ Ошибка разбана: {e}"
                    print(f"[MOD] unban_chat_member failed: {e}")
                group_reply = pm_reply
            
        elif cmd == "мут":
            if not target_id:
                pm_reply = f"❌ Не указан пользователь для мута."
                group_reply = pm_reply
            else:
                minutes = 10
                if args:
                    nums = re.findall(r'\d+', args)
                    if nums: minutes = int(nums[0])
                mute_time = time.time() + minutes * 60
                cursor.execute("UPDATE users SET mute_until = ? WHERE id = ?", (mute_time, target_id))
                conn.commit()
                until_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                try:
                    await bot.restrict_chat_member(
                        group_chat_id,
                        target_id,
                        permissions=ChatPermissions(
                            can_send_messages=False,
                            can_send_audios=False,
                            can_send_documents=False,
                            can_send_photos=False,
                            can_send_videos=False,
                            can_send_video_notes=False,
                            can_send_voice_notes=False,
                            can_send_polls=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False
                        ),
                        until_date=until_dt
                    )
                    pm_reply = f"🤐 Пользователь {target} замучен на {minutes} минут!"
                except Exception as e:
                    err_msg = str(e)
                    if "administrator" in err_msg.lower():
                        pm_reply = f"⚠️ {target} — админ чата, нельзя замутить."
                    elif "not enough rights" in err_msg.lower():
                        pm_reply = f"⚠️ У бота недостаточно прав для мута."
                    else:
                        pm_reply = f"❌ Ошибка мута: {e}"
                    print(f"[MOD] mute failed for {target_id}: {e}")
                group_reply = pm_reply
            
        elif cmd == "размут":
            if not target_id:
                pm_reply = f"❌ Не указан пользователь для размута."
                group_reply = pm_reply
            else:
                cursor.execute("UPDATE users SET mute_until = 0 WHERE id = ?", (target_id,))
                conn.commit()
                try:
                    chat = await bot.get_chat(group_chat_id)
                    base_perms = chat.permissions or ChatPermissions(
                        can_send_messages=True,
                        can_send_audios=True,
                        can_send_documents=True,
                        can_send_photos=True,
                        can_send_videos=True,
                        can_send_video_notes=True,
                        can_send_voice_notes=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_invite_users=True
                    )
                    await bot.restrict_chat_member(group_chat_id, target_id, permissions=base_perms)
                    pm_reply = f"🔊 Пользователь {target} размучен."
                except Exception as e:
                    pm_reply = f"❌ Ошибка размута: {e}"
                    print(f"[MOD] unmute failed: {e}")
                group_reply = pm_reply
            
        elif cmd == "удали_игрока":
            cursor.execute("DELETE FROM users WHERE id = ?", (target_id,))
            cursor.execute("DELETE FROM tags WHERE owner_id = ?", (target_id,))
            conn.commit()
            pm_reply = f"💥 Игрок {target} и все его теги стерты!"
            group_reply = pm_reply
            
        elif cmd == "админ_удали_тег":
            cursor.execute("DELETE FROM tags WHERE owner_id = ?", (target_id,))
            conn.commit()
            pm_reply = f"🗑 Все теги пользователя {target} удалены!"
            group_reply = pm_reply
            
        elif cmd == "админ_дай_тег":
            title = (args or "")[:16]
            if not title:
                pm_reply = "❌ Укажи надпись: тег @user Текст (макс. 16 символов — лимит Telegram)."
                group_reply = pm_reply
            else:
                pm_reply = f"🏷 Выдаю тег в Telegram для {target}..."
                try:
                    # Для назначения админа нужно can_manage_chat=True (требование Telegram)
                    await bot.promote_chat_member(
                        chat_id=group_chat_id,
                        user_id=target_id,
                        is_anonymous=False,
                        can_manage_chat=True,
                        can_delete_messages=False,
                        can_manage_video_chats=False,
                        can_restrict_members=False,
                        can_promote_members=False,
                        can_change_info=False,
                        can_invite_users=False,
                        can_post_stories=False,
                        can_edit_stories=False,
                        can_delete_stories=False,
                        can_pin_messages=False,
                        can_manage_topics=False,
                    )
                    await bot.set_chat_administrator_custom_title(
                        chat_id=group_chat_id,
                        user_id=target_id,
                        custom_title=title,
                    )
                    pm_reply = f"🏷 У {target} подпись в группе: «{title}»"
                    group_reply = f"🏷 {target} → «{title}»"
                except Exception as e:
                    pm_reply = (
                        f"⚠️ Не удалось выдать тег в Telegram: {e}\n"
                        "Нужно: бот — админ с правом «назначать админов»; группа — супергруппа."
                    )
                    group_reply = pm_reply

        elif cmd == "админ_дай_ранг":
            rank_name = args
            cursor.execute("UPDATE users SET custom_rank = ? WHERE id = ?", (rank_name, target_id))
            conn.commit()
            pm_reply = f"🎖 Игровой ранг для {target}: {rank_name}"
            group_reply = pm_reply

        elif cmd == "название":
            try:
                await bot.set_chat_title(group_chat_id, args)
                pm_reply = f"✅ Название группы изменено на: {args}"
                group_reply = pm_reply
            except Exception as e:
                pm_reply = f"❌ Ошибка при смене названия: {e}"

        elif cmd == "описание":
            try:
                await bot.set_chat_description(group_chat_id, args)
                pm_reply = f"✅ Описание группы изменено!"
                group_reply = pm_reply
            except Exception as e:
                pm_reply = f"❌ Ошибка при смене описания: {e}"

        elif cmd == "ава":
            photo = None
            if message.photo:
                photo = message.photo[-1].file_id
            elif message.reply_to_message and message.reply_to_message.photo:
                photo = message.reply_to_message.photo[-1].file_id
                
            if photo:
                try:
                    file = await bot.get_file(photo)
                    downloaded_file = await bot.download_file(file.file_path)
                    input_file = BufferedInputFile(downloaded_file.read(), filename="avatar.jpg")
                    await bot.set_chat_photo(group_chat_id, photo=input_file)
                    pm_reply = "✅ Аватарка группы успешно изменена!"
                    group_reply = pm_reply
                except Exception as e:
                    pm_reply = f"❌ Ошибка при смене аватарки: {e}"
            else:
                pm_reply = "❌ Чтобы изменить аву, прикрепи фото к команде или ответь на сообщение с фото."

        elif cmd == "закрепи":
            if message.reply_to_message:
                try:
                    await bot.pin_chat_message(group_chat_id, message.reply_to_message.message_id)
                    pm_reply = "✅ Сообщение закреплено!"
                    group_reply = pm_reply
                except Exception as e:
                    pm_reply = f"❌ Ошибка: {e}"
            else:
                pm_reply = "❌ Ответь на сообщение, которое нужно закрепить!"
                
        elif cmd == "открепи":
            if message.reply_to_message:
                try:
                    await bot.unpin_chat_message(group_chat_id, message.reply_to_message.message_id)
                    pm_reply = "✅ Сообщение откреплено!"
                    group_reply = pm_reply
                except Exception as e:
                    pm_reply = f"❌ Ошибка: {e}"
            else:
                try:
                    await bot.unpin_all_chat_messages(group_chat_id)
                    pm_reply = "✅ Все сообщения откреплены!"
                    group_reply = pm_reply
                except Exception as e:
                    pm_reply = f"❌ Ошибка: {e}"

        elif cmd == "очисти":
            try:
                count = int(args)
                if count > 100: count = 100
                if count < 1: count = 1
                
                deleted = 0
                msg_ids_to_delete = []
                
                # Берём ID последнего сообщения в ГРУППЕ
                if is_pm:
                    marker = await bot.send_message(group_chat_id, "🧹 Очистка...")
                    start_msg_id = marker.message_id
                    msg_ids_to_delete.append(marker.message_id)
                else:
                    start_msg_id = message.message_id
                
                # Собираем ID сообщений для удаления (пробуем от start_msg_id вниз)
                # Telegram может иметь пропуски в ID, поэтому пробуем диапазон шире
                attempts = count * 5  # увеличенный запас на пропуски (было *2)
                for offset in range(1, attempts + 1):
                    msg_id = start_msg_id - offset
                    if msg_id <= 0:
                        break
                    msg_ids_to_delete.append(msg_id)
                    if len(msg_ids_to_delete) >= count + 1:  # +1 для маркера
                        break
                
                # Удаляем батчами по 100 (лимит Telegram)
                for i in range(0, len(msg_ids_to_delete), 100):
                    batch = msg_ids_to_delete[i:i+100]
                    try:
                        await bot.delete_messages(group_chat_id, batch)
                        deleted += len(batch)
                    except Exception as e:
                        # Если батч не удалился, пробуем по одному
                        for msg_id in batch:
                            try:
                                await bot.delete_message(group_chat_id, msg_id)
                                deleted += 1
                            except:
                                pass
                        
                pm_reply = f"✅ Попытка удалить {count} сообщений. Удалено: {deleted}"
            except ValueError:
                pm_reply = f"❌ Укажи число: очисти 10"
            except Exception as e:
                pm_reply = f"❌ Ошибка: {e}"

        elif cmd == "напиши":
            group_reply = args
            pm_reply = "✅ Сообщение отправлено!"

        elif cmd == "pay_tsuefa":
            # /pay_tsuefa <game_id> <player_id> <amount>
            if not is_pm:
                pm_reply = "❌ Команда только в ЛС!"
            elif user_id not in ADMIN_IDS:
                pm_reply = "❌ Только для админов!"
            else:
                parts = args.split()
                if len(parts) < 3:
                    pm_reply = "❌ Формат: /pay_tsuefa <game_id> <player_id> <amount>"
                else:
                    try:
                        game_id = int(parts[0])
                        player_id = int(parts[1])
                        amount = int(parts[2])
                        if amount <= 0:
                            pm_reply = "❌ Сумма должна быть > 0"
                        else:
                            g = load_game(game_id)
                            if not g:
                                pm_reply = "❌ Игра не найдена!"
                            elif str(player_id) not in g["players"]:
                                pm_reply = "❌ Игрок не в этой игре!"
                            elif g["payment_status"].get(str(player_id), False):
                                pm_reply = "❌ Игрок уже оплачен!"
                            else:
                                # Отмечаем как оплаченного
                                g["payment_status"][str(player_id)] = True
                                save_game(game_id, g)
                                cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ?", (amount, player_id))
                                conn.commit()
                                player_name = g["players"][str(player_id)]["name"]
                                pm_reply = f"✅ {player_name} оплачен! (-{amount} ⭐)"
                                # Уведомление в группу
                                try:
                                    await bot.send_message(g["chat_id"], f"💳 {player_name} оплатил (-{amount} ⭐)!")
                                except:
                                    pass
                    except ValueError:
                        pm_reply = "❌ Неверные параметры (должны быть числа)"

        # Вывод результата
        if is_pm:
            data = await state.get_data()
            panel_active = data.get("panel_message_id")
            is_admin_flow = forced_cmd in ADMIN_PM_COMMANDS
            cmd_done_via_panel = cmd in ADMIN_PM_COMMANDS and panel_active

            if cmd_done_via_panel:
                # Всегда редактируем ОДНО панельное сообщение — показываем результат + меню
                menu_text = await admin_panel_text(state)
                final_text = f"✅ {pm_reply}\n\n{menu_text}"
                await edit_admin_panel_message(state, final_text, reply_markup=admin_panel_keyboard(user_id))
                # Удаляем сообщение админа (если это не само панельное сообщение)
                try:
                    await message.delete()
                except Exception:
                    pass
            elif panel_active and is_admin_flow:
                # Команда из панели, но не админская — просто показываем результат в панели
                menu_text = await admin_panel_text(state)
                await edit_admin_panel_message(
                    state,
                    f"{pm_reply}\n\n{menu_text}",
                    reply_markup=admin_panel_keyboard(user_id),
                )
                try:
                    await message.delete()
                except Exception:
                    pass
            else:
                # Обычный ЛС ответ или не из панели
                await message.reply(pm_reply)

            # Отправка в группу
            if group_reply and not stealth:
                await bot.send_message(group_chat_id, group_reply)
        else:
            if group_reply:
                if stealth:
                    # В группе шлём через send_message (не answer, чтобы избежать ошибки reply not found)
                    msg = await bot.send_message(chat_id=group_chat_id, text=group_reply)
                    # Автоудаление через 15 минут (900 сек)
                    async def _delayed_delete():
                        await asyncio.sleep(900)
                        try: await msg.delete()
                        except: pass
                    asyncio.create_task(_delayed_delete())
                else:
                    await bot.send_message(chat_id=group_chat_id, text=group_reply)
        return

    # Если админ пишет просто текст или кидает медиа в ЛС (и это не команда) — отправляем в группу от имени бота
    if is_pm:
        try:
            if message.text:
                await bot.send_message(group_chat_id, message.text)
            elif message.photo:
                await bot.send_photo(group_chat_id, message.photo[-1].file_id, caption=message.caption)
            elif message.video:
                await bot.send_video(group_chat_id, message.video.file_id, caption=message.caption)
            elif message.animation:
                await bot.send_animation(group_chat_id, message.animation.file_id, caption=message.caption)
            elif message.sticker:
                await bot.send_sticker(group_chat_id, message.sticker.file_id)
            elif message.voice:
                await bot.send_voice(group_chat_id, message.voice.file_id, caption=message.caption)
            elif message.video_note:
                await bot.send_video_note(group_chat_id, message.video_note.file_id)
            elif message.document:
                await bot.send_document(group_chat_id, message.document.file_id, caption=message.caption)
                
            if not stealth:
                await message.reply("✅ Отправлено в группу.")
        except Exception as e:
            await message.reply(f"❌ Ошибка отправки: {e}")
        return

    # --- ВЫЗОВ ТЕГОВ И ПАСХАЛКИ (ТОЛЬКО ДЛЯ ГРУППЫ) ---
    if not is_pm:
        words = text_lower.split()
        for word in words:
            if word.startswith("#") and len(word) > 1:
                tag_name = word[1:]
                cursor.execute("SELECT content FROM tags WHERE name = ?", (tag_name,))
                res = cursor.fetchone()
                if res:
                    await message.answer(res[0])
                    break

        if user_id == MATVEY_ID and random.random() < 0.2:
            await message.reply(random.choice(MATVEY_PHRASES))

        egg_hit = False
        for keyword, replies in EASTER_EGGS:
            if keyword in text_lower:
                await message.answer(random.choice(replies))
                egg_hit = True
                break
        # «уздечка» без вопроса обрабатывается как ai_chat; пасхалки — только без обращения к боту


# --- ЗОЛОТАЯ БАЗА КЛАССА (заполни сам) ---
# ID можно узнать командой /id (пусть скинут боту), или через @userinfobot
CLASS_9A: dict[int, str] = {
    7843725469: "Дрилер",
    1943087197: "Никита воздух",
    5442690882: "Максим Лобок",
    5215069131: "Коля автобус ",
    8425434588: "Кондиционер",
    1790593438: "Ульяна Онлифанс",
    468253535: "Каблук",
    8782619054: "Калшпротер",
    8188985721: "Шлюха Подзаборная ",
    6148109808: "Ярик 15 лет возьму в рот",
    8782619054: "Сын трудовика",
    6421487067: "Рваный тузик",
    7228273999: "Серёга нализал",
    6681818452: "Хранитель Семени",
}

ROLES_9A = [
    "Главный Воздухан (ученик Жиги)",
    "Тот самый, кто помылся (Яскич в шоке)",
    "Порванная уздечка недели",
    "Каблук года",
    "Дилер Чайна-Цеф",
    "Искатель милф из Малориты",
    "Спонсор Жигулей",
    "Лох дня по версии Влада",
    "Самый эластичный (нет)",
    "Жертва Матвея",
]

# File ID ГС «толстые грязные трактористы» — получи через /voice_id
TRACTOR_VOICE_ID = "AwACAgIAAxkBAAOxaggXtiXnCPv06dt6L6wZDLsX97UAAn-bAAIdk0hIbpu8ETNPeIw7BA"

# Таблица кулдаунов (счётчик бросков в день)
cursor.execute('''CREATE TABLE IF NOT EXISTS cooldowns
    (user_id INTEGER PRIMARY KEY, last_roll REAL DEFAULT 0, roll_count INTEGER DEFAULT 0)''')
try: cursor.execute("ALTER TABLE cooldowns ADD COLUMN roll_count INTEGER DEFAULT 0")
except sqlite3.OperationalError: pass

# Таблица покупок (анонимки, мут-пассы и т.д.)
cursor.execute('''CREATE TABLE IF NOT EXISTS shop_items
    (user_id INTEGER, item TEXT, quantity INTEGER DEFAULT 0,
     PRIMARY KEY (user_id, item))''')

# Виртуальный баланс звёзд
cursor.execute('''CREATE TABLE IF NOT EXISTS balances
    (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)''')

# Таблица игр ЦУЕФА
cursor.execute('''CREATE TABLE IF NOT EXISTS tsuefa_games
    (id INTEGER PRIMARY KEY AUTOINCREMENT,
     chat_id INTEGER,
     host_id INTEGER,
     host_username TEXT,
     state TEXT DEFAULT 'joining',
     bet_pool INTEGER DEFAULT 0,
     players TEXT DEFAULT '{}',
     moves TEXT DEFAULT '{}',
     payment_status TEXT DEFAULT '{}',
     frozen_ids TEXT DEFAULT '[]',
     started_at REAL DEFAULT 0)''')
try: cursor.execute("ALTER TABLE tsuefa_games ADD COLUMN msg_id INTEGER DEFAULT 0")
except sqlite3.OperationalError: pass
conn.commit()

# --- НАЧИСЛЕНИЕ ЗВЁЗД АДМИНОМ ---
@dp.message(Command("addstars"))
async def cmd_addstars(message: Message):
    """Начисление звёзд пользователю (только для админов)."""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.reply("❌ Только для админов!")
        return
    is_pm = message.chat.type == "private"
    if not is_pm:
        await message.reply("❌ Команда /addstars работает только в ЛС.")
        return
    
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply(
            "❌ Формат: <code>/addstars @username сумма</code>\n"
            "или: <code>/addstars ID_пользователя сумма</code>\n\n"
            "Пример: <code>/addstars @durov 100</code>"
        )
        return
    
    target_raw = args[1].strip()
    amount_raw = args[2].strip()
    
    if not amount_raw.isdigit() or int(amount_raw) < 1:
        await message.reply("❌ Сумма должна быть положительным числом.")
        return
    amount = int(amount_raw)
    
    target_id = await resolve_target(target_raw)
    if not target_id:
        await message.reply(f"❌ Пользователь {target_raw} не найден.")
        return
    
    cursor.execute("INSERT INTO balances (user_id, balance) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING", (target_id,))
    cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    
    await message.reply(f"✅ Пользователю {target_raw} начислено {amount} ⭐!")
    
    # Уведомление получателю
    try:
        await bot.send_message(
            target_id,
            f"💸 Админ начислил тебе {amount} ⭐ на вирт. баланс!"
        )
    except Exception:
        pass


# --- ПЕРЕВОД ЗВЁЗД МЕЖДУ ПОЛЬЗОВАТЕЛЯМИ ---
@dp.message(Command("send"))
async def cmd_send(message: Message):
    """Перевод виртуальных звёзд другому пользователю."""
    uid = message.from_user.id
    is_pm = message.chat.type == "private"
    if not is_pm:
        await message.reply("❌ Команда /send работает только в ЛС.")
        return
    
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply(
            "❌ Формат: <code>/send @username сумма</code>\n"
            "или: <code>/send ID_пользователя сумма</code>\n\n"
            "Пример: <code>/send @durov 50</code>"
        )
        return
    
    target_raw = args[1].strip()
    amount_raw = args[2].strip()
    
    if not amount_raw.isdigit() or int(amount_raw) < 1:
        await message.reply("❌ Сумма должна быть положительным числом.")
        return
    amount = int(amount_raw)
    
    target_id = await resolve_target(target_raw)
    if not target_id:
        await message.reply(f"❌ Пользователь {target_raw} не найден.")
        return
    if target_id == uid:
        await message.reply("❌ Нельзя переводить звёзды самому себе.")
        return
    
    # Атомарный перевод: списываем только если хватает баланса
    cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, uid, amount))
    if cursor.rowcount == 0:
        # Проверяем реальный баланс для сообщения
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        sender_bal = row[0] if row else 0
        await message.reply(f"❌ Недостаточно звёзд. У тебя {sender_bal} ⭐.")
        return
    
    # Атомарное начисление получателю
    cursor.execute("INSERT INTO balances (user_id, balance) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING", (target_id,))
    cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    
    sender_name = message.from_user.username or message.from_user.full_name
    
    await message.reply(f"✅ Переведено {amount} ⭐ пользователю {target_raw}!")
    
    # Уведомление получателю
    try:
        await bot.send_message(
            target_id,
            f"💸 {sender_name} перевёл тебе {amount} ⭐ на вирт. баланс!"
        )
    except Exception:
        pass


# --- ВИРТУАЛЬНЫЙ БАЛАНС ---
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    uid = message.from_user.id
    cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
    row = cursor.fetchone()
    bal = row[0] if row else 0
    await message.reply(f"💰 Твой баланс: {bal} ⭐\n\nПополни: купи звёзды через /shop")

# --- МАГАЗИН ЗВЁЗД ---

SHOP_PRICES = {
    "mute_30": ("Мут обидчика на 30 мин", 10),
    "unmute_self": ("Размут себя", 15),
    "vip_7": ("VIP-ранг на 7 дней", 50),
    "admin_40": ("Админка в боте (навсегда)", 40),
    "roll_1": ("Дополнительный бросок рулетки", 1),
}

def get_shop_keyboard() -> InlineKeyboardMarkup:
    p = SHOP_PRICES
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤐 Мут обидчика 30 мин — {p['mute_30'][1]} ⭐", callback_data="shop:mute_30")],
        [InlineKeyboardButton(text=f"🔊 Размут себя — {p['unmute_self'][1]} ⭐", callback_data="shop:unmute_self")],
        [InlineKeyboardButton(text=f"🎭 VIP-ранг на 7 дней — {p['vip_7'][1]} ⭐", callback_data="shop:vip_7")],
        [InlineKeyboardButton(text=f"👑 Админка в боте — {p['admin_40'][1]} ⭐", callback_data="shop:admin_40")],
        [InlineKeyboardButton(text=f"🎲 Доп. бросок рулетки — {p['roll_1'][1]} ⭐", callback_data="shop:roll_1")],
    ])

@dp.message(Command("shop"))
async def cmd_shop(message: Message):
    """Магазин за звёзды Telegram."""
    p = SHOP_PRICES
    await message.reply(
        f"🛒 МАГАЗИН УЗДЕЧКИ\n\n"
        f"🤐 Мут обидчика 30 мин — {p['mute_30'][1]}⭐\n"
        f"🔊 Размут себя — {p['unmute_self'][1]}⭐\n"
        f"🎭 VIP-ранг на 7 дней — {p['vip_7'][1]}⭐\n"
        f"👑 Админка в боте — {p['admin_40'][1]}⭐\n"
        f"🎲 Доп. бросок рулетки — {p['roll_1'][1]}⭐\n\n"
        f"Выбирай:",
        reply_markup=get_shop_keyboard(),
    )


async def _shop_fulfill(item_id: str, user_id: int, username: str = ""):
    """Выдать товар после оплаты (виртуальными звёздами или через Telegram)."""
    if item_id == "anon_1":
        cursor.execute("INSERT INTO shop_items (user_id, item, quantity) VALUES (?, 'anon', 1) ON CONFLICT(user_id, item) DO UPDATE SET quantity = quantity + 1", (user_id,))
        conn.commit()
        return "✅ +1 анонимка! Пиши в ЛС боту: /anon твой текст"
    elif item_id == "anon_5":
        cursor.execute("INSERT INTO shop_items (user_id, item, quantity) VALUES (?, 'anon', 5) ON CONFLICT(user_id, item) DO UPDATE SET quantity = quantity + 5", (user_id,))
        conn.commit()
        return "✅ +5 анонимок! Пиши в ЛС боту: /anon твой текст"
    elif item_id == "mute_30":
        cursor.execute("INSERT INTO shop_items (user_id, item, quantity) VALUES (?, 'mute_pass', 1) ON CONFLICT(user_id, item) DO UPDATE SET quantity = quantity + 1", (user_id,))
        conn.commit()
        return "✅ Мут-пасс куплен! Напиши в ЛС боту: /p_mute @username"
    elif item_id == "unmute_self":
        cursor.execute("INSERT INTO shop_items (user_id, item, quantity) VALUES (?, 'unmute_pass', 1) ON CONFLICT(user_id, item) DO UPDATE SET quantity = quantity + 1", (user_id,))
        conn.commit()
        return "✅ Размут-пасс куплен! Напиши в ЛС боту: /p_mute @username"
    elif item_id == "vip_7":
        expiry = time.time() + 7 * 86400
        # Сохраняем оригинальный ранг перед перезаписью на VIP
        cursor.execute("SELECT custom_rank FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        original_rank = row[0] if row else None
        if original_rank and original_rank != "VIP ⭐":
            cursor.execute("UPDATE users SET saved_rank = ? WHERE id = ?", (original_rank, user_id))
        cursor.execute("UPDATE users SET custom_rank = ?, vip_until = ? WHERE id = ?", ("VIP ⭐", expiry, user_id))
        conn.commit()
        return "✅ VIP-ранг на 7 дней! XP ×1.5, скидка 50% в магазине."
    elif item_id == "admin_40":
        if user_id not in ADMIN_IDS:
            ADMIN_IDS.append(user_id)
        PURCHASED_ADMIN_IDS.add(user_id)
        cursor.execute("UPDATE users SET custom_rank = '👑 Админ' WHERE id = ?", (user_id,))
        conn.commit()
        return "✅ Админка куплена! Теперь у тебя есть доступ к /admin и админ-командам."
    elif item_id == "roll_1":
        cursor.execute("DELETE FROM cooldowns WHERE user_id = ?", (user_id,))
        conn.commit()
        return "✅ Кулдаун сброшен! Пиши /roll"
    return "✅ Товар получен!"


@dp.callback_query(F.data.startswith("shop:"))
async def shop_callback(call: CallbackQuery):
    item_id = call.data[5:]
    if item_id not in SHOP_PRICES:
        await call.answer("Товар не найден", show_alert=True)
        return

    title, price = SHOP_PRICES[item_id]
    uid = call.from_user.id
    
    # Скидка 50% для VIP
    cursor.execute("SELECT vip_until FROM users WHERE id = ?", (uid,))
    vip_row = cursor.fetchone()
    if vip_row and vip_row[0] and vip_row[0] > time.time():
        price = max(1, price // 2)
    
    # Атомарное списание: UPDATE с условием balance >= price
    cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (price, uid, price))
    conn.commit()
    
    # Проверяем, обновилась ли строка (rowcount > 0 → деньги списаны)
    if cursor.rowcount > 0:
        fulfill_text = await _shop_fulfill(item_id, uid)
        result_text = f"💳 Оплачено {price} ⭐ с вирт. баланса!\n{fulfill_text}"
        try:
            if call.message.text or call.message.caption:
                await call.message.edit_text(result_text)
            else:
                await call.message.answer(result_text)
        except Exception as e:
            print(f"[SHOP] edit_text error: {e}")
            await call.message.answer(result_text)
        await call.answer()
    else:
        # Недостаточно — читаем реальный баланс для сообщения
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        bal = row[0] if row else 0
        shortage = price - bal
        if shortage <= 0:
            await call.answer("❌ Недостаточно звёзд", show_alert=True)
            return
        # НЕ списываем вирт-баланс сейчас! Зашиваем сколько списать в payload — спишем после успешной оплаты
        try:
            await call.message.answer_invoice(
                title=title,
                description=f"На вирт. балансе {bal} ⭐. Доплати {shortage} ⭐.",
                prices=[LabeledPrice(label="XTR", amount=shortage)],
                provider_token="",
                payload=f"shop_{item_id}_{bal}",  # bal = сколько вирт-звёзд списать при успехе
                currency="XTR",
            )
            await call.answer(f"💳 Не хватает {shortage} ⭐, выставлен счёт")
        except Exception as e:
            print(f"[SHOP] invoice error: {e}")
            await call.answer("❌ Ошибка выставления счёта", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    print(f"[PAYMENT] user={user_id} payload={payload}")

    if payload.startswith("shop_"):
        # payload = shop_{item_id}_{virtual_deduct} или shop_{item_id}
        rest = payload[5:]  # убираем "shop_"
        # item_id может содержать подчёркивания (mute_30, vip_7 и т.д.) — используем rsplit
        if '_' in rest and rest.rsplit('_', 1)[1].isdigit():
            *item_parts, virtual_deduct_str = rest.rsplit('_', 1)
            item_id = '_'.join(item_parts)
            virtual_deduct = int(virtual_deduct_str)
        else:
            item_id = rest
            virtual_deduct = 0
        
        # Только теперь списываем вирт-баланс, когда реальные деньги пришли
        if virtual_deduct > 0:
            cursor.execute("UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?", (virtual_deduct, user_id))
            conn.commit()
            
        result_text = await _shop_fulfill(item_id, user_id)
        await message.answer(result_text)
    elif payload.startswith("tsuefa_pay_"):
        # payload = tsuefa_pay_{game_id}_{uid}_{virtual_deduct} или tsuefa_pay_{game_id}_{uid}
        rest = payload[11:]  # убираем "tsuefa_pay_"
        parts = rest.split("_")
        game_id = int(parts[0])
        pid = int(parts[1])
        virtual_deduct = int(parts[2]) if len(parts) > 2 else 0
        g = load_game(game_id)
        if not g:
            await message.answer("❌ Игра не найдена.")
            return
        if str(pid) not in g["players"]:
            await message.answer("❌ Ты не в этой игре.")
            return
        # Отмечаем оплату
        g["payment_status"][str(pid)] = True
        
        # Только теперь списываем вирт-баланс, когда реальные деньги пришли
        if virtual_deduct > 0:
            cursor.execute("UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?", (virtual_deduct, pid))
            # Снимаем заморозку, т.к. игрок официально оплатил
            frozen = g.get("frozen_ids", [])
            if pid in frozen:
                frozen.remove(pid)
            # Добавляем в players как оплаченного
            if str(pid) in g["players"]:
                g["players"][str(pid)]["ready"] = True
        
        save_game(game_id, g)
        conn.commit()
        await message.answer("✅ Оплата засчитана! Ты в игре.")
        # Уведомление в чат (автоудаление через 5 мин)
        try:
            pay_msg = await bot.send_message(g["chat_id"], f"💳 {message.from_user.full_name} оплатил ЦУЕФА!")
            async def _delete_pay_msg():
                await asyncio.sleep(300)
                try: await pay_msg.delete()
                except: pass
            asyncio.create_task(_delete_pay_msg())
        except:
            pass
        # Обновляем сообщение игры
        try:
            await edit_game_message(g)
        except:
            pass
# Команды для использования купленного
@dp.message(Command("anon"))
@dp.message(Command("p_mute"))
async def use_shop_items(message: Message, state: FSMContext):
    cmd = message.text.split()[0].lower().lstrip("/")
    user_id = message.from_user.id

    # Берём ID группы из БД — последнюю активную
    cursor.execute("SELECT id FROM groups ORDER BY rowid DESC LIMIT 1")
    row = cursor.fetchone()
    chat_id = row[0] if row else None
    if not chat_id:
        await message.reply("❌ Бот пока не состоит ни в одной группе. Добавь его в чат!")
        return

    if cmd == "anon":
        cursor.execute("SELECT quantity FROM shop_items WHERE user_id = ? AND item = 'anon'", (user_id,))
        row = cursor.fetchone()
        if not row or row[0] <= 0:
            await message.reply("❌ У тебя нет анонимок. Купи в /shop")
            return
        text = message.text[6:].strip()  # после "/anon "
        if not text:
            await message.reply("❌ Напиши текст: /anon Привет, я аноним!")
            return
        await bot.send_message(chat_id, f"👤 Аноним: {text}")
        cursor.execute("UPDATE shop_items SET quantity = quantity - 1 WHERE user_id = ? AND item = 'anon'", (user_id,))
        conn.commit()
        await message.reply("✅ Анонимка отправлена в группу!")

    elif cmd == "p_mute":
        target = message.text[7:].strip()
        if not target:
            await message.reply("❌ Укажи юзера: /p_mute @username")
            return
        # Выполняем мут напрямую
        target_id = await resolve_target(target, chat_id, message)
        if not target_id:
            await message.reply(f"❌ Не смог найти {target}.")
            return
        ensure_user_exists(target_id)
        mute_time = time.time() + 30 * 60
        cursor.execute("UPDATE users SET mute_until = ? WHERE id = ?", (mute_time, target_id))
        conn.commit()
        until_dt = datetime.now(timezone.utc) + timedelta(minutes=30)
        try:
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False
                ),
                until_date=until_dt
            )
            await message.reply(f"🤐 Пользователь {target} замучен на 30 минут!")
        except Exception as e:
            err_msg = str(e)
            if "administrator" in err_msg.lower():
                await message.reply(f"⚠️ {target} — админ чата, нельзя замутить.")
            else:
                await message.reply(f"❌ Ошибка мута: {e}")


# --- РУЛЕТКА 9А ---
@dp.message(Command("id"))
async def cmd_my_id(message: Message):
    await message.reply(f"🆔 Твой Telegram ID: {message.from_user.id}\n\nСкинь это админу, чтобы попасть в рулетку!")


@dp.message(F.voice)
async def get_voice_id_handler(message: Message):
    """Отдаёт file_id любого ГС — для настройки TRACTOR_VOICE_ID."""
    if message.from_user.id not in ADMIN_IDS:
        return
    file_id = message.voice.file_id
    await message.reply(f"✅ ID ГС получен:\n\n{file_id}")
    print(f"\n--- СКОПИРУЙ ЭТОТ FILE_ID ---\n{file_id}\n-----------------------\n")


# --- ИГРА: ТУЗИК, НОЖНИЦЫ, БУМАГА (на XP) ---

@dp.message(Command("play"))
async def cmd_play(message: Message):
    if message.chat.type == "private":
        await message.reply("🎮 Игра теперь в группе! Напиши /play в чате 9А.")
        return

    args = message.text.split()
    stake = 20
    if len(args) > 1:
        try:
            stake = int(args[1])
            if stake < 5: stake = 5
            if stake > 100: stake = 100
        except:
            pass

    user_id = message.from_user.id
    cursor.execute("SELECT xp FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    xp = row[0] if row else 0

    if xp < stake:
        await message.reply(f"❌ У тебя {xp} XP, а ставка {stake} XP. Пиши больше в чат!")
        return

    # Кнопки со ставкой в callback_data — нет глобального словаря, нет утечки!
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗿 Тузик (камень)", callback_data=f"rps:rock:{stake}"),
            InlineKeyboardButton(text="✂️ Изолента (ножницы)", callback_data=f"rps:scissors:{stake}"),
            InlineKeyboardButton(text="📄 Уздечка (бумага)", callback_data=f"rps:paper:{stake}"),
        ],
    ])
    await message.reply(
        f"🎮 ТУЗИК, НОЖНИЦЫ, БУМАГА\nСтавка: {stake} XP\n\n"
        f"🗿 Тузик (камень) бьёт ✂️ Изоленту\n"
        f"✂️ Изолента (ножницы) режет 📄 Уздечку\n"
        f"📄 Уздечка (бумага) душит 🗿 Тузика\n\n"
        f"Выбирай:",
        reply_markup=keyboard,
    )

@dp.callback_query(F.data.startswith("rps:"))
async def rps_callback(call: CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split(":")
    user_choice = parts[1]
    # Ставка прямо из callback_data — словарь GAME_STAKES больше не нужен!
    if len(parts) >= 3 and parts[2].isdigit():
        stake = int(parts[2])
    else:
        await call.answer("Игра не найдена. Напиши /play", show_alert=True)
        return
    
    choices = {"rock": "🗿 Тузик", "scissors": "✂️ Изолента", "paper": "📄 Уздечка"}
    bot_choice = random.choice(["rock", "scissors", "paper"])
    
    # Определяем победителя
    if user_choice == bot_choice:
        result = "Ничья! XP остаются при своих."
        xp_change = 0
    elif (user_choice == "rock" and bot_choice == "scissors") or \
         (user_choice == "scissors" and bot_choice == "paper") or \
         (user_choice == "paper" and bot_choice == "rock"):
        result = f"Ты выиграл +{stake} XP!"
        xp_change = stake
    else:
        result = f"Ты проиграл -{stake} XP!"
        xp_change = -stake
    
    cursor.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (xp_change, user_id))
    conn.commit()
    
    text = (
        f"🎮 Твой выбор: {choices[user_choice]}\n"
        f"🤖 Мой выбор: {choices[bot_choice]}\n\n"
        f"{result}"
    )
    await call.message.edit_text(text)


async def do_roll_9a(message: Message):
    """Общая функция броска рулетки."""
    if not CLASS_9A:
        await message.answer("❌ База класса пуста.")
        return False

    user_id = message.from_user.id
    now = time.time()

    # Сбрасываем счётчик если прошло 24ч
    cursor.execute("SELECT last_roll, roll_count FROM cooldowns WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    last_roll = row[0] if row else 0
    roll_count = row[1] if row and len(row) > 1 else 0
    if now - last_roll >= 86400:
        roll_count = 0
        last_roll = 0

    # 3 бесплатных броска
    if roll_count < 3:
        roll_count += 1
        cursor.execute("INSERT INTO cooldowns (user_id, last_roll, roll_count) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_roll = excluded.last_roll, roll_count = excluded.roll_count", (user_id, now, roll_count))
        conn.commit()
    else:
        # Кулдаун исчерпан
        remaining = 86400 - int(now - last_roll) if last_roll else 86400
        hours = remaining // 3600
        mins = (remaining % 3600) // 60
        p = SHOP_PRICES
        pay_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💰 Купить бросок — {p['roll_1'][1]} ⭐", callback_data="shop:roll_1")],
        ])
        if TRACTOR_VOICE_ID:
            await message.answer_voice(
                voice=TRACTOR_VOICE_ID,
                caption=f"⚠️ 3 броска сегодня израсходованы! Жди ещё {hours}ч {mins}м или купи:",
                reply_markup=pay_kb,
            )
        else:
            await message.answer(
                f"⚠️ 3 броска сегодня израсходованы! Жди ещё {hours}ч {mins}м.",
                reply_markup=pay_kb,
            )
        return False

    victim_id = random.choice(list(CLASS_9A.keys()))
    victim_name = CLASS_9A[victim_id]
    role = random.choice(ROLES_9A)
    left = 3 - roll_count

    result_text = (
        f"🎯 РУЛЕТКА 9А\n\n"
        f"📍 Сегодня почётное звание:\n"
        f"🔥 {role}\n\n"
        f"Достаётся: [{victim_name}](tg://user?id={victim_id})\n\n"
        f"Крутил: [{message.from_user.full_name}](tg://user?id={user_id})\n"
        f"Бросков сегодня: {roll_count}/3"
    )
    if left > 0:
        result_text += f" (осталось {left})"
    await message.answer(result_text, parse_mode="Markdown")
    return True

@dp.message(Command("roll"))
async def roll_9a(message: Message):
    await do_roll_9a(message)


# ─── МУЛЬТИПЛЕЕРНАЯ ЦУЕФА НА ЗВЁЗДЫ ───

TSUEFA_MAX_PLAYERS = 10
TSUEFA_MIN_PLAYERS = 2
TSUEFA_MIN_BET = 1
TSUEFA_MAX_BET = 500
TSUEFA_TIMEOUT_SEC = 120


def _game_json_load(raw: str) -> dict:
    import json
    try: return json.loads(raw or "{}")
    except: return {}

def _game_json_dump(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def load_game(game_id: int) -> dict | None:
    cursor.execute("SELECT * FROM tsuefa_games WHERE id = ?", (game_id,))
    row = cursor.fetchone()
    if not row:
        return None
    keys = [
    "id", "chat_id", "host_id", "host_username", "state",
        "bet_pool", "players", "moves", "payment_status", "frozen_ids", "started_at", "msg_id",
    ]
    g = dict(zip(keys, row))
    g["players"] = _game_json_load(g["players"])
    g["moves"] = _game_json_load(g["moves"])
    g["payment_status"] = _game_json_load(g["payment_status"])
    g["frozen_ids"] = _game_json_load(g["frozen_ids"])
    return g


def save_game(game_id: int, g: dict):
    cursor.execute("""
        UPDATE tsuefa_games SET state=?, bet_pool=?, players=?, moves=?,
        payment_status=?, frozen_ids=?, started_at=?, msg_id=?
        WHERE id=?
    """, (
        g["state"], g["bet_pool"],
        _game_json_dump(g["players"]), _game_json_dump(g["moves"]),
        _game_json_dump(g["payment_status"]), _game_json_dump(g["frozen_ids"]),
        g["started_at"], g.get("msg_id", 0), game_id
    ))
    conn.commit()


def build_game_text(g: dict) -> str:
    if not g:
        return "❌ Игра не найдена"
    host_nick = g.get("host_username") or str(g.get("host_id", "?"))
    text = f"🎮 ЦУЕФА НА ЗВЁЗДЫ\nСоздал: {host_nick}\n"
    text += f"💰 Банк: {g.get('bet_pool', 0)} ⭐\n\n"
    pl = g.get("players") or {}
    if isinstance(pl, str):
        pl = _game_json_load(pl)
    pids = sorted(pl.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    for pid_str in pids:
        pid = int(pid_str) if pid_str.isdigit() else 0
        pdata = pl[pid_str]
        if isinstance(pdata, str):
            pdata = _game_json_load(pdata)
        name = pdata.get("name", str(pid))
        bet = pdata.get("bet", 0)
        paid = (g.get("payment_status") or {}).get(pid_str, False)
        if isinstance(g.get("payment_status"), str):
            paid = _game_json_load(g["payment_status"]).get(pid_str, False)
        ready = pdata.get("ready", False)
        status = "✅" if ready else "⏳"
        if g.get("state") == "playing":
            moves = g.get("moves") or {}
            if isinstance(moves, str):
                moves = _game_json_load(moves)
            moved = str(pid) in moves
            status = "🎯" if moved else "⏳"
        elif g.get("state") == "payment":
            status = "💳" if paid else "⏳"
        text += f"{status} {name} — ставка {bet}⭐\n"
    text += f"\nУчастников: {len(pl)}/{TSUEFA_MAX_PLAYERS}"
    return text


def tsuefa_stake_keyboard(game_id: int, user_id: int, balance: int) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура для выбора суммы ставки в ЦУЕФА (без звёздных кнопок)."""
    rows = [
        [
            InlineKeyboardButton(text="✏️ Другая сумма", callback_data=f"tsuefa:custom:{game_id}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"tsuefa:leave:{game_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_game_keyboard(g: dict) -> InlineKeyboardMarkup | None:
    """Универсальная клавиатура: одна для всех пользователей. Telegram не умеет показывать разные клавиатуры разным юзерам."""
    rows = []
    game_id = g['id']
    if g["state"] == "joining":
        rows.append([InlineKeyboardButton(text="🤝 Вступить", callback_data=f"tsuefa:join:{game_id}")])
        # СТАРТ и Отмена — видны всем, но сработают только у хоста
        rows.append([
            InlineKeyboardButton(text="🚀 СТАРТ", callback_data=f"tsuefa:start:{game_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"tsuefa:cancel:{game_id}"),
        ])
    if g["state"] in ("payment", "playing"):
        if g["state"] == "playing":
            rows.append([
                InlineKeyboardButton(text="🗿 Камень", callback_data=f"tsuefa:move:{game_id}:rock"),
                InlineKeyboardButton(text="✂️ Ножницы", callback_data=f"tsuefa:move:{game_id}:scissors"),
                InlineKeyboardButton(text="📄 Бумага", callback_data=f"tsuefa:move:{game_id}:paper"),
            ])
        rows.append([InlineKeyboardButton(text="❌ Отмена игры", callback_data=f"tsuefa:cancel:{game_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def auto_delete_msg(msg: Message, delay: int = 300):
    """Автоудаление сообщения через delay секунд (по умолчанию 5 мин)."""
    async def _delete():
        await asyncio.sleep(delay)
        try: await msg.delete()
        except: pass
    asyncio.create_task(_delete())


async def edit_game_message(g: dict):
    try:
        text = build_game_text(g)
        kb = build_game_keyboard(g)  # универсальная клавиатура
        msg_id = g.get("msg_id")
        if msg_id:
            try:
                await bot.edit_message_text(text, chat_id=g["chat_id"], message_id=msg_id, reply_markup=kb)
            except Exception as e2:
                err2 = str(e2)
                if "message is not modified" in err2.lower():
                    pass  # не логгируем
                elif "message to edit not found" in err2.lower() or "message to delete" in err2.lower():
                    # Сообщение удалено или невалидное — отправляем новое
                    new_msg = await bot.send_message(g["chat_id"], text, reply_markup=kb)
                    g["msg_id"] = new_msg.message_id
                    save_game(g["id"], g)
                else:
                    print(f"[TSUEFA] edit_game_message: {e2}")
        else:
            # Нет msg_id — отправляем новое сообщение
            new_msg = await bot.send_message(g["chat_id"], text, reply_markup=kb)
            g["msg_id"] = new_msg.message_id
            save_game(g["id"], g)
    except Exception as e:
        print(f"[TSUEFA] edit_game_message critical: {e}")


def _resolve_round(g: dict):
    moves = g["moves"]
    players = g["players"]
    rock_ids, scissor_ids, paper_ids = [], [], []
    for uid_str, move in moves.items():
        pid = int(uid_str)
        if move == "rock": rock_ids.append(pid)
        elif move == "scissors": scissor_ids.append(pid)
        elif move == "paper": paper_ids.append(pid)
    has_rock = bool(rock_ids)
    has_scissors = bool(scissor_ids)
    has_paper = bool(paper_ids)
    if (has_rock and has_scissors and has_paper) or (not has_rock and not has_scissors and not has_paper):
        return None, []  # каша
    if sum([has_rock, has_scissors, has_paper]) <= 1:
        return None, []  # все одно и то же
    if has_rock and has_scissors and not has_paper:
        return rock_ids, scissor_ids
    if has_scissors and has_paper and not has_rock:
        return scissor_ids, paper_ids
    if has_paper and has_rock and not has_scissors:
        return paper_ids, rock_ids
    return None, []


async def tsuefa_start_playing(g: dict):
    g["state"] = "playing"
    g["started_at"] = time.time()
    g["moves"] = {}
    save_game(g["id"], g)
    await edit_game_message(g)
    asyncio.create_task(_tsuefa_timeout(g["id"], time.time()))


async def _tsuefa_timeout(game_id: int, start_time: float):
    await asyncio.sleep(TSUEFA_TIMEOUT_SEC + 5)
    g = load_game(game_id)
    if not g or g["state"] != "playing":
        return
    if time.time() - start_time < TSUEFA_TIMEOUT_SEC:
        return
    # Таймаут — все игроки не сделавшие ход считаются проигравшими
    for uid_str in list(g["players"].keys()):
        if uid_str not in g["moves"]:
            g["moves"][uid_str] = "rock"  # заглушка
    await _finish_round(g)


async def _finish_round(g: dict):
    winners, losers = _resolve_round(g)
    if not winners:
        g["state"] = "playing"
        g["moves"] = {}
        g["started_at"] = time.time()
        save_game(g["id"], g)
        try:
            await bot.edit_message_text(
                build_game_text(g) + "\n\n🔄 Каша! Переигрываем!",
                chat_id=g["chat_id"], message_id=g.get("msg_id"),
                reply_markup=build_game_keyboard(g),
            )
        except: pass
        asyncio.create_task(_tsuefa_timeout(g["id"], time.time()))
        return
    # Распределение банка
    total_pool = g["bet_pool"]
    per_winner = total_pool // len(winners) if winners else total_pool
    winners_text = []
    losers_text = []
    for w_id in winners:
        uid_str = str(w_id)
        pname = g["players"][uid_str].get("name", str(w_id))
        cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (per_winner, w_id))
        winners_text.append(pname)
    for l_id in losers:
        uid_str = str(l_id)
        pname = g["players"][uid_str].get("name", str(l_id))
        losers_text.append(pname)
    conn.commit()
    # Снимаем заморозку
    frozen = g.setdefault("frozen_ids", [])
    for uid_str in g["players"]:
        pid = int(uid_str)
        if pid in frozen:
            frozen.remove(pid)
    cursor.execute("DELETE FROM tsuefa_games WHERE id = ?", (g["id"],))
    conn.commit()
    result_text = (
        build_game_text(g) +
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"🏆 Победители: {', '.join(winners_text)}\n"
        f"💔 Проигравшие: {', '.join(losers_text)}\n"
        f"💰 Каждый победитель получил {per_winner} ⭐"
        "\n\n♻️ Игра удалена из базы."
    )
    try:
        await bot.edit_message_text(result_text, chat_id=g["chat_id"], message_id=g.get("msg_id"))
    except: pass


@dp.message(Command("tsuefa"))
async def cmd_tsuefa(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await message.reply("🎮 ЦУЕФА только в группе! Напиши /tsuefa в чате 9А.")
        return
    host_id = message.from_user.id
    chat_id = message.chat.id
    host_username = message.from_user.username or message.from_user.full_name
    # Проверка: не в игре ли уже
    cursor.execute("SELECT id FROM tsuefa_games WHERE chat_id = ? AND state != 'finished'", (chat_id,))
    active = cursor.fetchone()
    if active:
        await message.reply("⚠️ В этом чате уже идёт игра ЦУЕФА!")
        return
    players = {}
    cursor.execute("""
        INSERT INTO tsuefa_games (chat_id, host_id, host_username, state, bet_pool, players, frozen_ids, started_at, msg_id)
        VALUES (?,?,?,?,0,?,?,?,0)
    """, (chat_id, host_id, host_username, "joining", _game_json_dump(players), "[]", time.time()))
    conn.commit()
    # Получаем ID через отдельный SELECT (lastrowid может не работать в Turso)
    cursor.execute("SELECT MAX(id) FROM tsuefa_games WHERE chat_id = ? AND host_id = ?", (chat_id, host_id))
    row = cursor.fetchone()
    game_id = row[0] if row else 0
    if not game_id:
        await message.reply("❌ Ошибка создания игры. Попробуй ещё раз.")
        return
    g = load_game(game_id)
    if not g:
        await message.reply("❌ Ошибка загрузки игры. Попробуй ещё раз.")
        return
    text = build_game_text(g)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"tsuefa:join:{game_id}")],
        [
            InlineKeyboardButton(text="🚀 СТАРТ", callback_data=f"tsuefa:start:{game_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"tsuefa:cancel:{game_id}"),
        ],
    ])
    msg = await message.answer(text, reply_markup=kb)
    g["msg_id"] = msg.message_id
    save_game(game_id, g)


@dp.callback_query(F.data.startswith("tsuefa:"))
async def tsuefa_callback(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    action = parts[1]
    game_id = int(parts[2])
    g = load_game(game_id)
    if not g:
        await call.answer("Игра не найдена", show_alert=True)
        return
    uid = call.from_user.id
    uid_str = str(uid)

    if action == "join":
        if g["state"] != "joining":
            await call.answer("Набор закрыт", show_alert=True)
            return
        if uid_str in g["players"]:
            await call.answer("Ты уже в игре", show_alert=True)
            return
        if len(g["players"]) >= TSUEFA_MAX_PLAYERS:
            await call.answer("Максимум 10 игроков", show_alert=True)
            return
        await call.answer()
        # Показываем инлайн-кнопки со ставками
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        bal = row[0] if row else 0
        stake_kb = tsuefa_stake_keyboard(game_id, uid, bal)
        await state.update_data(tsuefa_game_id=game_id)
        await state.set_state(TsuefaState.waiting_stake)
        await bot.send_message(
            uid,
            f"🎮 <b>ЦУЕФА</b>\n\n"
            f"💰 Твой баланс: {bal} ⭐\n"
            f"Выбери сумму ставки:",
            parse_mode="HTML",
            reply_markup=stake_kb,
        )
        return

    if action == "stake":
        if len(parts) < 4 or not parts[3].isdigit():
            await call.answer("Неверная сумма", show_alert=True)
            return
        bet = int(parts[3])
        if bet < TSUEFA_MIN_BET or bet > TSUEFA_MAX_BET:
            await call.answer(f"Ставка от {TSUEFA_MIN_BET} до {TSUEFA_MAX_BET} ⭐", show_alert=True)
            return
        if uid_str in g["players"]:
            await call.answer("Ты уже в игре!", show_alert=True)
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except: pass
            await state.clear()
            return
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        bal = row[0] if row else 0
        if bal < bet:
            await call.answer(f"❌ Недостаточно звёзд (баланс {bal} ⭐)", show_alert=True)
            return
        # Атомарное списание: UPDATE с условием balance >= bet
        cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (bet, uid, bet))
        conn.commit()
        name = call.from_user.username or call.from_user.full_name
        g["players"][uid_str] = {"name": name, "bet": bet, "ready": True}
        g["bet_pool"] += bet
        g.setdefault("payment_status", {})[uid_str] = True
        g.setdefault("frozen_ids", []).append(uid)
        save_game(game_id, g)
        g["msg_id"] = g.get("msg_id")
        try:
            await edit_game_message(g)
        except Exception as e:
            print(f"[TSUEFA] edit after stake: {e}")
        try:
            await call.message.edit_text(f"✅ Ты в игре! Ставка: {bet} ⭐", reply_markup=None)
        except: pass
        await call.answer(f"✅ Ставка {bet} ⭐ принята!")
        await state.clear()
        return

    if action == "custom":
        # Пользователь хочет ввести свою сумму
        if uid_str in g["players"]:
            await call.answer("Ты уже в игре!", show_alert=True)
            return
        await state.update_data(tsuefa_game_id=game_id)
        await state.set_state(TsuefaState.waiting_stake)
        try:
            await call.message.edit_text(
                f"🎮 <b>ЦУЕФА</b>\n\nВведи сумму от {TSUEFA_MIN_BET} до {TSUEFA_MAX_BET} ⭐:",
                parse_mode="HTML",
                reply_markup=None
            )
        except: pass
        await call.answer()
        return

    if action == "cancel":
        if uid != g["host_id"]:
            await call.answer("Только создатель может отменить игру", show_alert=True)
            return
        # Возвращаем ставки всем игрокам на вирт. баланс
        for pid_str, pdata in g["players"].items():
            pid = int(pid_str)
            bet = pdata.get("bet", 0)
            if bet > 0:
                cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (bet, pid))
                conn.commit()
        # Снимаем заморозку
        frozen = g.get("frozen_ids", [])
        for pid in frozen:
            pass  # заморозка снимается автоматически при возврате средств
        cursor.execute("DELETE FROM tsuefa_games WHERE id = ?", (game_id,))
        conn.commit()
        cancel_msg = await call.message.edit_text("🚫 Игра отменена создателем. Ставки возвращены на вирт. баланс.")
        # Автоудаление сообщения об отмене через 30 секунд
        async def _del_cancel():
            await asyncio.sleep(30)
            try: await cancel_msg.delete()
            except: pass
        asyncio.create_task(_del_cancel())
        await call.answer("Игра отменена")
        return

    if action == "leave":
        if uid_str not in g["players"]:
            await call.answer("Ты не в игре", show_alert=True)
            return
        if uid == g["host_id"]:
            # Создатель вышел - возвращаем деньги всем
            for pid_str, pdata in g["players"].items():
                pid = int(pid_str)
                bet = pdata.get("bet", 0)
                if bet > 0 and g["payment_status"].get(pid_str, False):
                    cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (bet, pid))
                    conn.commit()
            cursor.execute("DELETE FROM tsuefa_games WHERE id = ?", (game_id,))
            conn.commit()
            await call.message.edit_text("🚫 Создатель вышел. Игра отменена. Ставки возвращены.")
            await call.answer()
            return
        bet = g["players"][uid_str].get("bet", 0)
        # Возвращаем деньги если они были оплачены
        if g["payment_status"].get(uid_str, False):
            cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (bet, uid))
            conn.commit()
            # Снимаем заморозку
            frozen = g.get("frozen_ids", [])
            if uid in frozen:
                frozen.remove(uid)
        del g["players"][uid_str]
        if uid_str in g["payment_status"]:
            del g["payment_status"][uid_str]
        g["bet_pool"] -= bet
        save_game(game_id, g)
        g["msg_id"] = call.message.message_id
        await edit_game_message(g)
        await call.answer("Ты вышел")
        return

    if action == "bet_plus":
        if uid_str not in g["players"]:
            await call.answer("Ты не в игре", show_alert=True)
            return
        cur_bet = g["players"][uid_str]["bet"]
        new_bet = min(cur_bet + 10, TSUEFA_MAX_BET)
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        bal = row[0] if row else 0
        if new_bet > bal:
            await call.answer(f"Недостаточно звёзд (баланс {bal} ⭐)", show_alert=True)
            return
        g["bet_pool"] += (new_bet - cur_bet)
        g["players"][uid_str]["bet"] = new_bet
        save_game(game_id, g)
        g["msg_id"] = call.message.message_id
        await edit_game_message(g)
        await call.answer(f"Ставка: {new_bet} ⭐")
        return

    if action == "bet_minus":
        if uid_str not in g["players"]:
            await call.answer("Ты не в игре", show_alert=True)
            return
        cur_bet = g["players"][uid_str]["bet"]
        new_bet = max(cur_bet - 10, TSUEFA_MIN_BET)
        g["bet_pool"] -= (cur_bet - new_bet)
        g["players"][uid_str]["bet"] = new_bet
        save_game(game_id, g)
        g["msg_id"] = call.message.message_id
        await edit_game_message(g)
        await call.answer(f"Ставка: {new_bet} ⭐")
        return

    if action == "start":
        if uid != g["host_id"]:
            await call.answer("Только создатель может начать", show_alert=True)
            return
        if len(g["players"]) < TSUEFA_MIN_PLAYERS:
            await call.answer(f"Минимум {TSUEFA_MIN_PLAYERS} игрока", show_alert=True)
            return
        
            # Проверяем баланс всех игроков (деньги уже списаны при вступлении)
        unpaid = []
        for pid_str, pdata in g["players"].items():
            pid = int(pid_str)
            bet = pdata.get("bet", 0)
            if g["payment_status"].get(pid_str, False):
                # Уже оплатил при вступлении — всё ок
                continue
            cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (pid,))
            row = cursor.fetchone()
            bal = row[0] if row else 0
            if bal >= bet:
                # Не оплатил заранее, пробуем списать сейчас
                cursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ?", (bet, pid))
                conn.commit()
                g["payment_status"][pid_str] = True
            else:
                # Недостаточно баланса
                unpaid.append(pid)
        
        if unpaid:
            # Есть неоплаченные игроки
            unpaid_text = ""
            for pid in unpaid:
                pname = g["players"][str(pid)]["name"]
                unpaid_text += f"❌ {pname}\n"
            
            # Кнопки для выгнания
            kick_rows = []
            for pid in unpaid:
                pname = g["players"][str(pid)]["name"]
                kick_rows.append([InlineKeyboardButton(text=f"🚫 Выгнать {pname}", callback_data=f"tsuefa:confirm_kick:{game_id}:{pid}")])
            
            kick_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"tsuefa:start:{game_id}")])
            kick_rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data=f"tsuefa:cancel:{game_id}")])
            
            await call.message.edit_text(
                f"⚠️ <b>Недостаточно звёзд!</b>\n\n{unpaid_text}\nВыгните их или пополните баланс!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kick_rows)
            )
            await call.answer("Недостаточно баланса у некоторых игроков")
            return
        
        # Все оплатили - начинаем игру
        save_game(game_id, g)
        await tsuefa_start_playing(g)
        await call.answer("Игра начинается!")
        return



    if action == "confirm_kick":
        if uid != g["host_id"]:
            await call.answer("Только создатель может выгонять", show_alert=True)
            return
        if len(parts) < 4:
            await call.answer("Неверные параметры", show_alert=True)
            return
        kick_id = int(parts[3])
        kick_id_str = str(kick_id)
        if kick_id_str not in g["players"]:
            await call.answer("Игрок не найден", show_alert=True)
            return
        
        # Проверяем, оплатил ли уже
        if g["payment_status"].get(kick_id_str, False):
            await call.answer("❌ Нельзя выгнать оплаченного игрока", show_alert=True)
            return
        
        # Выгняем — возвращаем звёзды
        kick_name = g["players"][kick_id_str]["name"]
        bet = g["players"][kick_id_str].get("bet", 0)
        # Возвращаем звёзды на баланс (если оплачено)
        if g["payment_status"].get(kick_id_str, False):
            cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (bet, kick_id))
            conn.commit()
            # Снимаем заморозку
            frozen = g.get("frozen_ids", [])
            if kick_id in frozen:
                frozen.remove(kick_id)
        del g["players"][kick_id_str]
        if kick_id_str in g["payment_status"]:
            del g["payment_status"][kick_id_str]
        g["bet_pool"] -= bet
        save_game(game_id, g)
        
        # Кнопка "Назад в игру"
        g["msg_id"] = call.message.message_id
        await edit_game_message(g)
        await call.answer(f"✅ {kick_name} выгнан, звёзды возвращены")
        return

    if action == "move":
        if g["state"] != "playing":
            await call.answer("Не этап игры", show_alert=True)
            return
        if uid_str not in g["players"]:
            await call.answer("Ты не в игре", show_alert=True)
            return
        if uid_str in g["moves"]:
            await call.answer("Ход уже сделан", show_alert=True)
            return
        move = parts[3]
        if move not in ("rock", "scissors", "paper"):
            await call.answer("Неверный ход", show_alert=True)
            return
        g["moves"][uid_str] = move
        save_game(game_id, g)
        g["msg_id"] = call.message.message_id
        await edit_game_message(g)
        await call.answer("Принято!")
        if len(g["moves"]) == len(g["players"]):
            await _finish_round(g)
        return

    await call.answer()


# Обработка платежей игры в successful_payment
# Дополняем существующий обработчик — добавим проверку payload tsuefa_pay_
# (будет сделано заменой в successful_payment)


async def on_shutdown():
    print("Завершение бота: возврат ставок ЦУЕФА...")
    # Возвращаем ставки по всем незавершённым играм
    try:
        cursor.execute("SELECT id, players, bet_pool, payment_status, frozen_ids, state FROM tsuefa_games WHERE state IN ('joining', 'playing')")
        pending = cursor.fetchall()
        for row in pending:
            game_id, players_raw, bet_pool, payment_status_raw, frozen_ids_raw, state = row
            import json
            players = json.loads(players_raw or "{}")
            payment_status = json.loads(payment_status_raw or "{}")
            frozen_ids = json.loads(frozen_ids_raw or "[]")
            for uid_str, pdata in players.items():
                pid = int(uid_str)
                bet = pdata.get("bet", 0)
                paid = pdata.get("ready", False) or payment_status.get(uid_str, False)
                if paid and bet > 0:
                    cursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (bet, pid))
            # Помечаем игру как finished и обнуляем банк
            cursor.execute("UPDATE tsuefa_games SET state = ?, bet_pool = 0 WHERE id = ?", ("finished", game_id))
            print(f"[SHUTDOWN] Возвращены ставки по игре {game_id} ({len(players)} игроков, банк {bet_pool} ⭐)")
        if pending:
            conn.commit()
            print(f"[SHUTDOWN] Возврат ставок завершён: {len(pending)} игр обработано.")
    except Exception as e:
        print(f"[SHUTDOWN] Ошибка при возврате ставок: {e}")
    print("Закрытие соединения с базой данных...")
    conn.close()
    await close_http_session()
    print("Бот остановлен.")


async def ping_handler(request):
    """HTTP ping endpoint для Render health check"""
    from aiohttp import web
    return web.Response(text="OK", status=200)


async def start_web_server():
    """Запуск HTTP сервера для пинга"""
    from aiohttp import web
    app = web.Application()
    app.router.add_get('/ping', ping_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 8080)))
    await site.start()
    print(f"[HTTP] Сервер пинга запущен на порту {os.getenv('PORT', 8080)}")


async def main():
    print("Бот Уздечка успешно запущен! Ожидание сообщений...")
    try:
        await register_bot_commands()
        print("Слэш-команды зарегистрированы в Telegram.")
    except Exception as e:
        print(f"Не удалось зарегистрировать команды: {e}")
    
    # Запускаем HTTP сервер для пинга
    await start_web_server()
    
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
