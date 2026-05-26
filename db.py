"""
Модуль для работы с Turso (libsql) вместо локального SQLite.
Полная совместимость со старым кодом sqlite3 в main.py
"""
import os
import sqlite3 as _native_sqlite3
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")

_turso_client = None
_use_turso = bool(TURSO_URL and TURSO_TOKEN)

# Глобальный кэш для эмуляции fetchone/fetchall
_last_rows = []
_row_index = 0

# Локальное SQLite-соединение как fallback
_local_conn = None
_local_cursor = None


def _get_local_connection():
    global _local_conn, _local_cursor
    if _local_conn is None:
        db_path = os.getenv("LOCAL_DB_PATH", "uzdechka_bot.db")
        _local_conn = _native_sqlite3.connect(db_path, check_same_thread=False)
        _local_conn.row_factory = _native_sqlite3.Row
        _local_cursor = _local_conn.cursor()
    return _local_conn, _local_cursor


def _get_client():
    global _turso_client, _use_turso
    if not _use_turso:
        return None
    if _turso_client is None:
        try:
            from libsql_client import create_client_sync
            _turso_client = create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)
        except Exception as e:
            print(f"[DB] Turso init failed: {e}. Falling back to local SQLite.")
            _use_turso = False
    return _turso_client


# Класс-заглушка, чтобы main.py мог вызывать методы .cursor(), fetchone(), fetchall()
class FakeConnection:
    def __init__(self):
        self.row_factory = None
    def cursor(self):
        return self
    def execute(self, sql, *args):
        return db_execute(sql, *args)
    def fetchone(self):
        return fetchone()
    def fetchall(self):
        return fetchall()
    def commit(self):
        pass
    def close(self):
        pass


def execute(sql, *args):
    global _last_rows, _row_index, _use_turso
    
    # Распаковка: main.py шлёт cursor.execute(sql, (param1, param2)),
    # FakeConnection.execute получает это как sql, ((param1, param2),),
    # и передаёт в execute(sql, (param1, param2)). 
    # В execute sql="...", args=((param1, param2),) — кортеж в кортеже.
    # Исправляем: если args[0] — кортеж/список, разворачиваем.
    _actual_args = args
    if len(args) == 1 and isinstance(args[0], (tuple, list)):
        _actual_args = args[0]
    
    if _use_turso:
        client = _get_client()
        try:
            parameters = list(_actual_args) if _actual_args else []
            result = client.execute(sql, parameters)
            _last_rows = result.rows if hasattr(result, 'rows') else []
            _row_index = 0
            return result
        except Exception as e:
            print(f"[DB] Turso execute failed: {e}. Отключаем Turso, используем локальный SQLite.")
            _use_turso = False  # больше не пробуем Turso
            _last_rows = []
            _row_index = 0
            # При ошибке — пробуем через локальную БД
            conn, cursor = _get_local_connection()
            try:
                res = cursor.execute(sql, _actual_args)
                _last_rows = cursor.fetchall()
                _row_index = 0
                return res
            except _native_sqlite3.OperationalError as e2:
                raise OperationalError(str(e2))
            except _native_sqlite3.IntegrityError as e2:
                raise IntegrityError(str(e2))
            except Exception as e2:
                raise OperationalError(str(e2))
    else:
        # Fallback на локальную БД
        conn, cursor = _get_local_connection()
        try:
            res = cursor.execute(sql, _actual_args)
            _last_rows = cursor.fetchall()
            _row_index = 0
            return res
        except _native_sqlite3.OperationalError as e:
            raise OperationalError(str(e))
        except _native_sqlite3.IntegrityError as e:
            raise IntegrityError(str(e))
        except Exception as e:
            raise OperationalError(str(e))


def fetchone():
    global _last_rows, _row_index
    if _row_index < len(_last_rows):
        row = _last_rows[_row_index]
        _row_index += 1
        # Приводим к списку для совместимости с main.py (row[0])
        if hasattr(row, 'keys'):
            return list(row)
        if isinstance(row, dict):
            return list(row.values())
        return row
    return None


def fetchall():
    global _last_rows, _row_index
    rows = _last_rows[_row_index:]
    _row_index = len(_last_rows)
    result = []
    for r in rows:
        if hasattr(r, 'keys'):
            result.append(list(r))
        elif isinstance(r, dict):
            result.append(list(r.values()))
        else:
            result.append(r)
    return result


def connect(db_path=None):
    if _use_turso:
        return FakeConnection()
    else:
        conn, _ = _get_local_connection()
        return conn


def commit():
    if not _use_turso:
        global _local_conn
        if _local_conn:
            _local_conn.commit()


def close():
    global _local_conn, _turso_client
    if _local_conn:
        _local_conn.close()
    if _turso_client:
        try:
            _turso_client.close()
        except:
            pass

class OperationalError(_native_sqlite3.OperationalError):
    """Совместимость с sqlite3.OperationalError"""
    pass


class IntegrityError(_native_sqlite3.IntegrityError):
    """Совместимость с sqlite3.IntegrityError"""
    pass


# Прямой вызов для совместимости — дублирует execute
# (чтобы main.py мог вызывать db.execute напрямую без connect)
db_execute = execute
