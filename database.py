"""
Дерек қоры (SQLite) — TARTIP V3.
Жаңалықтары:
- users кестесіне category өрісі қосылды
- habit_checkins кестесі: әр әдет (habit) бойынша жеке белгіленеді
- Күн автоматты түрде дедлайн уақытында алға жылжиды (батырма арқылы емес)
"""
import sqlite3
from datetime import datetime, date, timedelta, timezone

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
            category TEXT,
            goal TEXT,
            goal_why TEXT,
            start_date TEXT,
            current_day INTEGER DEFAULT 1,
            last_advance_date TEXT,
            streak INTEGER DEFAULT 0,
            last_checkin_date TEXT,
            is_active INTEGER DEFAULT 1,
            paused INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS habit_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            day INTEGER,
            habit_id TEXT,
            checkin_date TEXT,
            proof_file_id TEXT,
            text_response TEXT,
            UNIQUE(telegram_id, day, habit_id)
        )
    """)
    conn.commit()
    conn.close()


def add_user(telegram_id: int, username: str, full_name: str, category: str, goal: str, goal_why: str = ""):
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, full_name, category, goal, goal_why, start_date, current_day, last_advance_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (telegram_id, username, full_name, category, goal, goal_why, today, today))
    conn.commit()
    conn.close()


def get_user(telegram_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


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


def set_paused(telegram_id: int, paused: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET paused = ? WHERE telegram_id = ?", (1 if paused else 0, telegram_id))
    conn.commit()
    conn.close()


def delete_user(telegram_id: int):
    """Клиентті ТОЛЫҚ ұмытады — профиль де, тапсырма тарихы да өшеді.
    Сынау кезінде /start ағынын қайта бастан өту үшін, немесе клиент
    'деректерімді өшір' деп сұраса қолданылады."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM habit_checkins WHERE telegram_id = ?", (telegram_id,))
    cur.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def reset_progress(telegram_id: int):
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute("""
        UPDATE users SET current_day = 1, streak = 0, last_checkin_date = NULL, last_advance_date = ?
        WHERE telegram_id = ?
    """, (today, telegram_id))
    cur.execute("DELETE FROM habit_checkins WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def record_habit_checkin(telegram_id: int, day: int, habit_id: str, proof_file_id: str = None, text_response: str = None):
    """Бір әдетті (habit) сол күнге белгілейді. Қайталап шақырса, үстінен жазады (қате болмайды)."""
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute("""
        INSERT INTO habit_checkins (telegram_id, day, habit_id, checkin_date, proof_file_id, text_response)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id, day, habit_id) DO UPDATE SET
            checkin_date=excluded.checkin_date,
            proof_file_id=excluded.proof_file_id,
            text_response=excluded.text_response
    """, (telegram_id, day, habit_id, today, proof_file_id, text_response))

    user_row = cur.execute("SELECT streak, last_checkin_date FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    last = user_row["last_checkin_date"]
    cur_streak = user_row["streak"]
    new_streak = cur_streak
    if last != today:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        new_streak = (cur_streak + 1) if last == yesterday else 1
    cur.execute("UPDATE users SET streak = ?, last_checkin_date = ? WHERE telegram_id = ?",
                (new_streak, today, telegram_id))
    conn.commit()
    conn.close()
    return new_streak


def get_habit_checkin(telegram_id: int, day: int, habit_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM habit_checkins WHERE telegram_id = ? AND day = ? AND habit_id = ?
    """, (telegram_id, day, habit_id))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_completed_habits_today(telegram_id: int, day: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT habit_id FROM habit_checkins WHERE telegram_id = ? AND day = ?
    """, (telegram_id, day))
    rows = cur.fetchall()
    conn.close()
    return [r["habit_id"] for r in rows]


def advance_day(telegram_id: int):
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute("""
        UPDATE users SET current_day = current_day + 1, last_advance_date = ?
        WHERE telegram_id = ?
    """, (today, telegram_id))
    conn.commit()
    conn.close()


def needs_advance_today(user: dict) -> bool:
    today = date.today().isoformat()
    return user["last_advance_date"] != today


def get_inactive_users(days_threshold: int = 2):
    conn = get_connection()
    cur = conn.cursor()
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
