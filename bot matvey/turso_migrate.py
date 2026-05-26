"""
Скрипт переноса данных из локальной SQLite (uzdechka_bot.db) в Turso.
Запустить: python turso_migrate.py
"""
import sqlite3
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")

if not TURSO_URL or not TURSO_TOKEN:
    print("ERROR: TURSO_URL and TURSO_TOKEN must be set in .env")
    exit(1)

LOCAL_DB = "uzdechka_bot.db"

TABLES = [
    {
        "name": "users",
        "create": """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, xp INTEGER DEFAULT 0, lvl INTEGER DEFAULT 0,
            last_t REAL DEFAULT 0, username TEXT, is_banned INTEGER DEFAULT 0,
            mute_until REAL DEFAULT 0, custom_rank TEXT, vip_until REAL DEFAULT 0
        )"""
    },
    {
        "name": "tags",
        "create": """CREATE TABLE IF NOT EXISTS tags (
            name TEXT PRIMARY KEY, content TEXT, owner_id INTEGER
        )"""
    },
    {
        "name": "groups",
        "create": """CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER DEFAULT 1
        )"""
    },
    {
        "name": "cooldowns",
        "create": """CREATE TABLE IF NOT EXISTS cooldowns (
            user_id INTEGER PRIMARY KEY, last_roll REAL DEFAULT 0, roll_count INTEGER DEFAULT 0
        )"""
    },
    {
        "name": "shop_items",
        "create": """CREATE TABLE IF NOT EXISTS shop_items (
            user_id INTEGER, item TEXT, quantity INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, item)
        )"""
    },
    {
        "name": "balances",
        "create": """CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0
        )"""
    },
    {
        "name": "tsuefa_games",
        "create": """CREATE TABLE IF NOT EXISTS tsuefa_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, host_id INTEGER,
            host_username TEXT, state TEXT DEFAULT 'joining', bet_pool INTEGER DEFAULT 0,
            players TEXT DEFAULT '{}', moves TEXT DEFAULT '{}',
            payment_status TEXT DEFAULT '{}', frozen_ids TEXT DEFAULT '[]',
            started_at REAL DEFAULT 0, msg_id INTEGER DEFAULT 0
        )"""
    },
]


async def migrate_table(turso, table_info):
    table_name = table_info["name"]
    print(f"Migrating table: {table_name}...")

    await turso.execute(table_info["create"])

    local = sqlite3.connect(LOCAL_DB)
    local.row_factory = sqlite3.Row
    try:
        rows = local.execute(f"SELECT * FROM {table_name}").fetchall()
    except sqlite3.OperationalError as e:
        print(f"  Error reading {table_name}: {e}")
        local.close()
        return

    if not rows:
        print(f"  No data in {table_name}")
        local.close()
        return

    columns = list(rows[0].keys())
    placeholders = ", ".join(["?" for _ in columns])
    col_names = ", ".join(columns)

    count = 0
    for row in rows:
        values = [row[col] for col in columns]
        if table_name == "tsuefa_games":
            try:
                await turso.execute(
                    f"INSERT OR IGNORE INTO {table_name} ({col_names}) VALUES ({placeholders})",
                    args=values
                )
                count += 1
            except Exception as e:
                print(f"  Insert error in {table_name}: {e}")
        else:
            try:
                await turso.execute(
                    f"INSERT OR REPLACE INTO {table_name} ({col_names}) VALUES ({placeholders})",
                    args=values
                )
                count += 1
            except Exception as e:
                print(f"  Insert error in {table_name}: {e}")

    print(f"  OK: {count}/{len(rows)} rows migrated")
    local.close()


async def main():
    from libsql_client import create_client

    print("=== Starting migration to Turso ===")
    print(f"URL: {TURSO_URL}")

    # Turso requires HTTPS URL for REST API
    http_url = TURSO_URL.replace("libsql://", "https://")
    turso = create_client(url=http_url, auth_token=TURSO_TOKEN)

    for table_info in TABLES:
        await migrate_table(turso, table_info)

    await turso.close()
    print("\nMigration complete!")
    print("Bot will now work with Turso DB.")


if __name__ == "__main__":
    asyncio.run(main())