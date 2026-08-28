"""
Дерек қоры (SQLite) — клиенттер мен прогресс туралы барлық ақпаратты сақтайды.
100 клиентке дейін SQLite толық жеткілікті, ешқандай сыртқы сервис керек емес.

МАҢЫЗДЫ: бұл файл (tartip.db) хостингте persistent volume-де болуы керек,
әйтпесе қайта деплой кезінде барлық клиент деректері жоғалады.
"""
import sqlite3
from datetime import datetime, timezone

DB_PATH = "tartip.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            goal TEXT,
            start_date TEXT,
            current_day INTEGER DEFAULT 1,
            streak INTEGER DEFAULT 0,
            last_checkin_date TEXT,
            is_active INTEGER DEFAULT 1,
            paused INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            day INTEGER,
            checkin_date TEXT,
            proof_file_id TEXT,
            text_response TEXT,
            FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
        )
    """)
    conn.commit()
    conn.close()


def add_user(telegram_id: int, username: str, full_name: str, goal: str):
    conn = get_connection()
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()
    cur.execute("""
        INSERT OR IGNORE INTO users (telegram_id, username, full_name, goal, start_date, current_day)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (telegram_id, username, full_name, goal, today))
    conn.commit()
    conn.close()


def get_user(telegram_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def already_checked_in_today(telegram_id: int) -> bool:
    """Клиент бүгін тапсырманы бұрын орындады ма — қайталап есептемес үшін."""
    from datetime import date
    user = get_user(telegram_id)
    if not user or not user["last_checkin_date"]:
        return False
    return user["last_checkin_date"] == date.today().isoformat()


def get_all_active_users():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_active = 1 AND paused = 0")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_users():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def advance_day(telegram_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET current_day = current_day + 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def set_paused(telegram_id: int, paused: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET paused = ? WHERE telegram_id = ?", (1 if paused else 0, telegram_id))
    conn.commit()
    conn.close()


def reset_progress(telegram_id: int):
    """Клиенттің прогресін 1-күнге қайтару (streak те нөлденеді)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET current_day = 1, streak = 0, last_checkin_date = NULL
        WHERE telegram_id = ?
    """, (telegram_id,))
    conn.commit()
    conn.close()


def record_checkin(telegram_id: int, day: int, proof_file_id: str = None, text_response: str = None):
    conn = get_connection()
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()
    cur.execute("""
        INSERT INTO checkins (telegram_id, day, checkin_date, proof_file_id, text_response)
        VALUES (?, ?, ?, ?, ?)
    """, (telegram_id, day, today, proof_file_id, text_response))

    # Streak logic: if last checkin was yesterday or today, increment; otherwise reset to 1
    user = get_user(telegram_id)
    last = user["last_checkin_date"]
    new_streak = 1
    if last:
        from datetime import date, timedelta
        last_date = date.fromisoformat(last)
        yesterday = date.today() - timedelta(days=1)
        if last_date == yesterday or last_date == date.today():
            new_streak = user["streak"] + 1 if last_date != date.today() else user["streak"]

    cur.execute("""
        UPDATE users SET streak = ?, last_checkin_date = ? WHERE telegram_id = ?
    """, (new_streak, today, telegram_id))
    conn.commit()
    conn.close()
    return new_streak


def get_inactive_users(days_threshold: int = 2):
    """2+ күн жауап бермеген клиенттерді табу — админге ескерту үшін."""
    conn = get_connection()
    cur = conn.cursor()
    from datetime import date, timedelta
    threshold_date = (date.today() - timedelta(days=days_threshold)).isoformat()
    cur.execute("""
        SELECT * FROM users
        WHERE is_active = 1 AND paused = 0
        AND (last_checkin_date IS NULL OR last_checkin_date < ?)
    """, (threshold_date,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def categorize_users():
    """
    Барлық активті клиентті 🟢 қалыпты / 🟡 назар керек / 🔴 араласу керек деп бөледі.
    🔴 — 3+ күн жауап жоқ
    🟡 — 1-2 күн жауап жоқ
    🟢 — бүгін немесе кеше жауап берген
    """
    from datetime import date, timedelta
    users = get_all_active_users()
    green, yellow, red = [], [], []
    today = date.today()

    for u in users:
        if not u["last_checkin_date"]:
            red.append(u)
            continue
        last_date = date.fromisoformat(u["last_checkin_date"])
        days_gone = (today - last_date).days
        if days_gone <= 1:
            green.append(u)
        elif days_gone <= 2:
            yellow.append(u)
        else:
            red.append(u)

    return {"green": green, "yellow": yellow, "red": red}


def get_stats():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users WHERE is_active = 1")
    active = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM users")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM users WHERE paused = 1")
    paused = cur.fetchone()["c"]
    cur.execute("SELECT AVG(streak) as avg_streak FROM users WHERE is_active = 1")
    avg_streak = cur.fetchone()["avg_streak"] or 0
    conn.close()
    return {"active": active, "total": total, "paused": paused, "avg_streak": round(avg_streak, 1)}
