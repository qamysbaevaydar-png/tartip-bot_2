"""
TARTIP жоспар логикасы (Telegram-ға тәуелсіз, сондықтан жеке тексеруге болады).

- parse_time / compute_schedule: ояну, жату және ас санына қарай жеке уақыттар
- render_day: бағдарламадағы үлгі мәтінді клиентке бейімдеп шығарады
- compile_system: клиенттің өз жауаптарынан «Менің жүйем» құжатын жинайды
- weekly_draft / adaptation_text: апталық қорытынды мен келесі аптаға ұсыныс
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOTAL_DAYS = 28
FITNESS = {"arykhtau", "salmaq_qosu", "bulshyq_et"}
MEAL_CATEGORIES = FITNESS | {"uyqy_energy"}
NO_RESTRICTION_WORDS = {"жоқ", "жок", "нет", "no", "-", "–", "ештеңе", "ештене", "жоқ."}


def load_json(name: str):
    with open(os.path.join(BASE_DIR, name), "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Уақыт ----------

_TIME_PATTERNS = (
    re.compile(r"^(\d{1,2})[:.](\d{2})$"),
    re.compile(r"^(\d{1,2})$"),
    re.compile(r"^(\d{2})(\d{2})$"),
)


def parse_time(text: str):
    """'7', '07:30', '7.30', '0730' -> 'HH:MM'. Қате болса None."""
    if not text:
        return None
    s = text.strip()
    for pat in _TIME_PATTERNS:
        m = pat.match(s)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
            return None
    return None


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    minutes = int(round(minutes / 5.0) * 5) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def compute_schedule(wake: str = None, bed: str = None, meals: int = None) -> dict:
    """Ояну, жату және ас санына қарай уақыттар: бірінші ас оянғаннан ~1 сағаттан кейін,
    соңғы толық ас жатуға 3 сағат қалғанда, арасы біркелкі. Күн тым қысқа болса, ас саны азаяды."""
    wake = wake or "07:00"
    bed = bed or "23:00"
    W = _to_min(wake)
    B = _to_min(bed)
    if B <= W:
        B += 1440
    n = max(1, min(int(meals or 3), 5))
    first = W + 60
    last = B - 180
    if last < first:
        last = first
        n = 1
    while n > 1 and (last - first) < 120 * (n - 1):
        n -= 1
    if n == 1:
        times = [first]
    else:
        times = [first + (last - first) * i / (n - 1) for i in range(n)]
    times_rounded = [int(round(t / 5.0) * 5) for t in times]
    mid_target = (W + B) / 2
    mid = min(times_rounded, key=lambda t: abs(t - mid_target))
    return {
        "wake": _fmt(W),
        "bed": _fmt(B),
        "m1": _fmt(times_rounded[0]),
        "mmid": _fmt(mid),
        "mlast": _fmt(times_rounded[-1]),
        "mlist": " · ".join(_fmt(t) for t in times_rounded),
        "screens_off": _fmt(B - 60),
        "walk_time": _fmt(mid + 30),
        "meal_count": n,
    }


def restr_note(user: dict) -> str:
    r = (user.get("restrictions") or "").strip()
    if not r or r.lower() in NO_RESTRICTION_WORDS:
        return ""
    return f" Ескерт: сен «{r}» дегенді қоспайсың."


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def profile_values(user: dict) -> dict:
    values = compute_schedule(user.get("wake_time"), user.get("bed_time"), user.get("meals"))
    values["restr_note"] = restr_note(user)
    return values


def _pick(value, place: str) -> str:
    if isinstance(value, dict):
        return value.get(place) or value.get("home") or next(iter(value.values()))
    return value


def render_day(plan: dict, user: dict) -> dict:
    """Миссияны клиенттің уақыты мен жаттығу орнына сай жасайды (HTML escape жасалмаған)."""
    values = _SafeDict(profile_values(user))
    place = user.get("place") or "home"

    def fill(text):
        return _pick(text, place).format_map(values)

    return {
        "title": plan["title"],
        "mission": fill(plan["mission"]),
        "schedule": [fill(item) for item in plan["schedule"]],
        "ask": plan.get("ask", ""),
        "fallback": fill(plan["fallback"]),
        "proof": plan["proof"],
        "min": plan.get("min", 15),
        "lesson": plan.get("lesson"),
        "med": bool(plan.get("med")),
        "save": plan.get("save"),
    }


# ---------- Апта және жауаптар ----------

def week_of(day: int) -> int:
    return min(max((day - 1) // 7, 0), 3) + 1


def week_range(week: int):
    return (week - 1) * 7 + 1, week * 7


def week_done_counts(done_days: dict, week: int, current_day: int):
    """(орындалған күндер, соның ішінде жеңілдері, өткен күндер саны) — әлі келмеген күндер есепке кірмейді."""
    start, end = week_range(week)
    last = min(end, current_day)
    if last < start:
        return 0, 0, 0
    days = range(start, last + 1)
    done = [d for d in days if d in done_days]
    light = [d for d in done if done_days[d] != "full"]
    return len(done), len(light), len(days)


def adaptation_text(done: int, elapsed: int, next_goal: str = "") -> str:
    ratio = done / elapsed if elapsed else 0
    if ratio >= 0.85:
        base = ("Тұрақты қарқын — жақсы нәтиже. Келесі аптада жүктемені сәл күрделендіреміз: "
                "миссияны толық нұсқамен орындауға тырыс.")
    elif ratio >= 0.5:
        base = ("Негіз қалыптасып келеді. Өткізілген күндердің себебін өзіңе анықтап көр — "
                "соны шешсең, қарқын тұрақтанады. Қиын күнде жеңіл нұсқа да есептеледі.")
    else:
        base = ("Бұл апта қиын болды — бұл қалыпты. Келесі аптада жеңілдетілген режим: "
                "күн сайын тек ең кіші нұсқаны жап та, күнді үзбе. Қолдау керек болса /support жаз.")
    if next_goal:
        base += f"\n\nКелесі апта мақсаты: {next_goal}"
    return base


def weekly_draft(user: dict, done_days: dict, streak: int) -> str:
    """Админге апталық қорытындының дайын жобасы (қолмен өзгертуге/жіберуге болады)."""
    current = min(user["current_day"], TOTAL_DAYS)
    week = week_of(current)
    done, light, elapsed = week_done_counts(done_days, week, current)
    name = user.get("full_name") or "Дос"
    light_part = f", соның {light}-і жеңіл нұсқа" if light else ""
    if elapsed == 0:
        return f"{name}, жаңа аптаға қош келдің! Бірінші күнді бастайық."
    ratio = done / elapsed
    if ratio >= 0.85:
        tail = "Тұрақты қарқын — өте жақсы. Келесі аптада жүктемені сәл күрделендіреміз."
    elif ratio >= 0.5:
        tail = "Негіз қалыптасты. Өткізген күндердің себебін бірге талдап, соны түзетейік."
    else:
        tail = "Бұл апта қиын болды, бұл қалыпты. Келесі аптада жеңіл нұсқадан бастап, күндерді үзбей жүрейік."
    return (f"{name}, осы аптада {elapsed} күннің {done}-ін орындадың{light_part}. "
            f"Қатарынан: {streak} күн. {tail}")


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compile_system(user: dict, programs: dict, responses: list, category_name: str):
    """«Менің жүйем» — клиенттің өз жауаптарынан жиналған құжат (хабарлама бөліктерінің тізімі)."""
    category = user["category"]
    prog = programs.get(category)
    values = profile_values(user)
    header = [f"🧩 Менің жүйем — {category_name}", ""]
    if user.get("goal"):
        header.append(f"🎯 Мақсатым: {user['goal']}")
    if user.get("goal_why"):
        header.append(f"💭 Себебім: {user['goal_why']}")
    header.append("")
    rhythm = [f"⏰ Ояну: {values['wake']}", f"🌙 Жату: {values['bed']}", f"📵 Экранды өшіру: {values['screens_off']}"]
    if category in MEAL_CATEGORIES:
        rhythm.insert(2, f"🍽 Астар: {values['mlist']} (соңғы толық ас — жатуға ~3 сағат қалғанда)")
    header.extend(rhythm)

    by_day = {r["day"]: r["text_response"] for r in responses}
    items = []
    if prog:
        day = 0
        for week in prog["weeks"]:
            for plan in week["days"]:
                day += 1
                label = plan.get("save")
                if label and by_day.get(day):
                    items.append(f"▫️ {label}:\n{_clip(by_day[day], 350)}")

    chunks, current = [], "\n".join(header)
    if not items:
        current += "\n\nӘзірге сақталған жауаптар жоқ. Миссияларға жауап жазған сайын осы құжат толыға береді."
    for item in items:
        if len(current) + len(item) + 2 > 3500:
            chunks.append(current)
            current = ""
        current = (current + "\n\n" + item) if current else item
    chunks.append(current)
    return chunks
