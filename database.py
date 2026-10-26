"""
Дерек қоры (SQLite) — TARTIP V4.

V3-тен өзгерістер:
- users кестесіне клиент профилі қосылды (ояну/жату уақыты, ас саны, жаттығу орны,
  шектеулер, денсаулық белгісі, ересек белгісі). Ескі дерек жоғалмайды: бағандар
  init_db() кезінде автоматты қосылады (миграция).
- habit_checkins.mode: 'full' (толық), 'light' (жеңіл нұсқа), 'catchup' (кешегіні жабу)
- streak енді күнге негізделіп есептеледі (күндер қатарынан орындалған), ескі сан қалмайды
- барлық күндер Asia/Almaty бойынша (сервер уақытына тәуелді емес)
- DB_PATH айнымалысы арқылы дерекқор орнын таңдауға болады (Railway Volume үшін)
"""
import os
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "tartip.db")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Almaty"))
TOTAL_DAYS = 28

# Клиент профилінің өрістері (users кестесіне миграциямен қосылады)
USER_EXTRA_COLUMNS = {
    "wake_time": "TEXT",
    "bed_time": "TEXT",
    "meals": "INTEGER",
    "place": "TEXT",
    "restrictions": "TEXT",
    "health_flag": "TEXT",   # 'yes' | 'no' | 'skip' — тек белгі, диагноз сақталмайды
    "adult": "TEXT",         # 'yes' | 'no'
    "awaiting": "TEXT",      # 'support' | 'review_N' | 'finish_sent' | ''
}
UPDATABLE = set(USER_EXTRA_COLUMNS) | {"full_name", "goal", "goal_why", "category", "is_active", "paused"}


def today() -> date:
    return datetime.now(TZ).date()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(cur, table: str) -> set:
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}


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
    # Миграция: ескі дерекқорға жаңа бағандарды қосу (бар дерек сақталады)
    user_cols = _columns(cur, "users")
    for col, col_type in USER_EXTRA_COLUMNS.items():
        if col not in user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
    if "mode" not in _columns(cur, "habit_checkins"):
        cur.execute("ALTER TABLE habit_checkins ADD COLUMN mode TEXT DEFAULT 'full'")
    conn.commit()
    conn.close()


def add_user(telegram_id: int, username: str, full_name: str, category: str, goal: str,
             goal_why: str = "", profile: dict = None):
    conn = get_connection()
    cur = conn.cursor()
    day_str = today().isoformat()
    cur.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, full_name, category, goal, goal_why, start_date, current_day, last_advance_date, awaiting)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, '')
    """, (telegram_id, username, full_name, category, goal, goal_why, day_str, day_str))
    conn.commit()
    conn.close()
    if profile:
        update_user(telegram_id, **profile)


def update_user(telegram_id: int, **fields):
    """Тек рұқсат етілген өрістерді жаңартады."""
    clean = {k: v for k, v in fields.items() if k in UPDATABLE}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    conn = get_connection()
    conn.execute(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*clean.values(), telegram_id))
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
    conn.execute("UPDATE users SET paused = ? WHERE telegram_id = ?", (1 if paused else 0, telegram_id))
    conn.commit()
    conn.close()


def set_awaiting(telegram_id: int, value: str):
    conn = get_connection()
    conn.execute("UPDATE users SET awaiting = ? WHERE telegram_id = ?", (value or "", telegram_id))
    conn.commit()
    conn.close()


def clear_awaiting_prefixes(telegram_id: int, prefixes=("support", "review_")):
    """Таңертең ескі «күтіп тұрған жауапты» тазалайды, сонда ол кездейсоқ есепке кетпейді."""
    user = get_user(telegram_id)
    if not user:
        return
    aw = user.get("awaiting") or ""
    if any(aw.startswith(p) for p in prefixes):
        set_awaiting(telegram_id, "")


def delete_user(telegram_id: int):
    """Клиентті ТОЛЫҚ ұмытады — профиль де, тапсырма тарихы да өшеді."""
    conn = get_connection()
    conn.execute("DELETE FROM habit_checkins WHERE telegram_id = ?", (telegram_id,))
    conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def reset_progress(telegram_id: int):
    conn = get_connection()
    conn.execute("""
        UPDATE users SET current_day = 1, streak = 0, last_checkin_date = NULL,
        last_advance_date = ?, awaiting = ''
        WHERE telegram_id = ?
    """, (today().isoformat(), telegram_id))
    conn.execute("DELETE FROM habit_checkins WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


# ---------- Күндік миссия белгілері ----------

def compute_streak(done_days, current_day: int) -> int:
    """Орындалған күндердің қатарын санайды. Бүгін әлі орындалмаса, кешегіден санайды,
    сондықтан күн әлі аяқталмаған кезде қатар үзілмейді."""
    done = set(done_days)
    day = current_day if current_day in done else current_day - 1
    streak = 0
    while day >= 1 and day in done:
        streak += 1
        day -= 1
    return streak


def get_done_days(telegram_id: int) -> dict:
    """{күн: mode} — миссиясы орындалған күндер."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT day, mode FROM habit_checkins WHERE telegram_id = ? AND habit_id = 'daily_plan'",
        (telegram_id,),
    ).fetchall()
    conn.close()
    return {r["day"]: (r["mode"] or "full") for r in rows}


def get_streak(telegram_id: int, current_day: int) -> int:
    return compute_streak(get_done_days(telegram_id).keys(), current_day)


def record_checkin(telegram_id: int, day: int, habit_id: str = "daily_plan",
                   proof_file_id: str = None, text_response: str = None, mode: str = "full") -> int:
    """Күнді белгілейді. Қайталап шақырса, үстінен жазады; толық нұсқа жеңіл нұсқаға түспейді,
    ал бұрынғы мәтін/фото жоғалмайды."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO habit_checkins (telegram_id, day, habit_id, checkin_date, proof_file_id, text_response, mode)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id, day, habit_id) DO UPDATE SET
            checkin_date = excluded.checkin_date,
            proof_file_id = COALESCE(excluded.proof_file_id, habit_checkins.proof_file_id),
            text_response = COALESCE(excluded.text_response, habit_checkins.text_response),
            mode = CASE
                WHEN excluded.mode = 'full' THEN 'full'
                WHEN habit_checkins.mode = 'full' THEN 'full'
                ELSE excluded.mode
            END
    """, (telegram_id, day, habit_id, today().isoformat(), proof_file_id, text_response, mode))
    conn.commit()
    conn.close()

    streak = 0
    if habit_id == "daily_plan":
        user = get_user(telegram_id)
        if user:
            streak = compute_streak(get_done_days(telegram_id).keys(), user["current_day"])
            conn = get_connection()
            conn.execute("UPDATE users SET streak = ?, last_checkin_date = ? WHERE telegram_id = ?",
                         (streak, today().isoformat(), telegram_id))
            conn.commit()
            conn.close()
    return streak


def get_checkin(telegram_id: int, day: int, habit_id: str = "daily_plan"):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM habit_checkins WHERE telegram_id = ? AND day = ? AND habit_id = ?",
        (telegram_id, day, habit_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_text_responses(telegram_id: int) -> list:
    """Клиенттің миссияларға жазған жауаптары (күн ретімен)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT day, text_response, mode, checkin_date FROM habit_checkins
        WHERE telegram_id = ? AND habit_id = 'daily_plan' AND text_response IS NOT NULL AND text_response != ''
        ORDER BY day
    """, (telegram_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reviews(telegram_id: int) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT day, habit_id, text_response FROM habit_checkins
        WHERE telegram_id = ? AND habit_id LIKE 'review_%' ORDER BY day
    """, (telegram_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- Күн жылжыту ----------

def advance_day(telegram_id: int):
    conn = get_connection()
    conn.execute("""
        UPDATE users SET current_day = current_day + 1, last_advance_date = ?
        WHERE telegram_id = ?
    """, (today().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def needs_advance_today(user: dict) -> bool:
    return user["last_advance_date"] != today().isoformat()


def get_stats():
    users = get_all_users()
    active = [u for u in users if u["is_active"]]
    streaks = [get_streak(u["telegram_id"], u["current_day"]) for u in active]
    return {
        "active": len(active),
        "total": len(users),
        "paused": sum(1 for u in users if u["paused"]),
        "avg_streak": round(sum(streaks) / len(streaks), 1) if streaks else 0,
    }
