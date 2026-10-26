"""
TARTIP Course Bot V4.

V3-тен негізгі өзгерістер:
- Тіркеуде қысқа анкета (ояну/жату, ас саны, жаттығу орны, шектеулер, денсаулық белгісі, 18+)
  және миссиялар клиенттің уақытына сай жеке құрылады (plan.py)
- 5 бағыт × 28 күн, әр күн — бір нақты миссия; жауап жазатын күндерде жауап «Менің жүйем»
  құжатына жиналады (/system), 28-күні толық құжат беріледі
- «⚡ Жеңіл нұсқа» батырмасы және кешегі күнді жабу («екі рет қатар өткізбе»)
- Жауап валидациясы: қысқа/кездейсоқ мәтін миссияны жаппайды; дауыстық жауап қабылданады
- Апталық review жауабы бөлек сақталады және келесі аптаға бейімделу ұсынысын береді
- /support нақты админге жетеді, админ батырмамен жауап береді
- /forget растаумен; /wipe, /pause және т.б. қате санға төзімді
- Админ: уақытқа сай күй, апталық орындалу, соңғы жауаптар, клиентке хабар жазу,
  апталық қорытынды жобасы, күндік дайджест
"""
import html
import logging
import os
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)
from telegram.error import TelegramError

import database as db
import plan as plan_mod

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID = os.environ.get("CHANNEL_ID")
DAILY_SEND_HOUR = int(os.environ.get("DAILY_SEND_HOUR", "8"))
DEADLINE_HOUR = int(os.environ.get("DEADLINE_HOUR", "22"))
ADMIN_DIGEST_HOUR = int(os.environ.get("ADMIN_DIGEST_HOUR", "10"))
TOTAL_DAYS = 28
ALMATY_TZ = ZoneInfo("Asia/Almaty")
FITNESS = plan_mod.FITNESS
MEAL_CATEGORIES = plan_mod.MEAL_CATEGORIES

ASKING_CATEGORY, ASKING_NAME, ASKING_GOAL, ASKING_WHY, INTAKE = range(5)

CATEGORIES = plan_mod.load_json("categories.json")
PROGRAMS = plan_mod.load_json("programs.json")
LESSONS = plan_mod.load_json("lessons.json")

WEEKLY_REVIEW_DAYS = {7: 1, 14: 2, 21: 3, 28: 4}
WEEK_START_DAYS = {1, 8, 15, 22}

STATUS_LABELS = {"full": "✅ Миссия орындалды", "light": "⚡ Жеңіл нұсқа есептелді", "catchup": "↩️ Кешегі күн жабылды"}


# ---------- Көмекші функциялар ----------

def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def parse_id(args):
    """/pause 123 сияқты командалардағы ID-ді қауіпсіз оқиды."""
    try:
        return int(args[0])
    except (IndexError, ValueError):
        return None


def get_daily_plan(category_key: str, day: int) -> dict:
    week_index = min(max((day - 1) // 7, 0), 3)
    day_index = (day - 1) % 7
    return PROGRAMS[category_key]["weeks"][week_index]["days"][day_index]


def get_week(category_key: str, day: int) -> dict:
    return PROGRAMS[category_key]["weeks"][plan_mod.week_of(day) - 1]


def get_week_title(day: int) -> str:
    week = min(max((day - 1) // 7, 0), 3) + 1
    return f"{week}-апта"


async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> bool:
    if not CHANNEL_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        return member.status in (
            ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER,
        )
    except TelegramError as e:
        logger.error(f"Channel membership check failed for {telegram_id}: {e}")
        return False


async def setup_menus(application: Application):
    default_commands = [
        BotCommand("start", "Тіркелу / қайта кіру"),
        BotCommand("today", "Бүгінгі миссияны көру"),
        BotCommand("system", "🧩 Менің жүйем (өз жауаптарым)"),
        BotCommand("roadmap", "🗺 Толық 28 күндік жолым"),
        BotCommand("progress", "Менің прогресім"),
        BotCommand("lessons", "📚 Видео сабақтар каталогы"),
        BotCommand("support", "Кураторға жазу"),
        BotCommand("forget", "Деректерімді толық өшіру"),
    ]
    await application.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [
        BotCommand("whoami", "Өз telegram ID-іңді көру"),
        BotCommand("admin", "👑 Админ панель"),
        BotCommand("stats", "Жалпы статистика"),
        BotCommand("problems", "Назар керек клиенттер"),
        BotCommand("user", "Клиент картасы (/user id)"),
        BotCommand("pause", "Клиентті тоқтату (/pause id)"),
        BotCommand("resume", "Клиентті қайта қосу (/resume id)"),
        BotCommand("reset", "Прогресті нөлдеу (/reset id)"),
        BotCommand("wipe", "Клиентті толық ұмыту (/wipe id)"),
        BotCommand("broadcast", "Барлығына хабарлама"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramError as e:
            logger.error(f"Failed to set admin menu for {admin_id}: {e}")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_status = "ИӘ ✅" if is_admin(uid) else "ЖОҚ ❌"
    await update.message.reply_text(
        f"Сенің Telegram ID-ің: {uid}\n"
        f"Админ құқығың бар ма: {admin_status}\n\n"
        f"Админ болу керек болса, осы ID-ді хостингтегі ADMIN_IDS айнымалысына қос."
    )


# ---------- Тіркеу және анкета ----------

INTAKE_DEFS = {
    "adult": {"type": "buttons", "q": "🔞 Жасың 18-ге толды ма?",
              "options": [("yes", "Иә"), ("no", "Жоқ")]},
    "wake": {"type": "time", "q": "⏰ Әдетте сағат нешеде тұрасың? (мыс. 07:00)"},
    "bed": {"type": "time", "q": "🌙 Әдетте сағат нешеде жатасың? (мыс. 23:30)"},
    "meals": {"type": "buttons", "q": "🍽 Күніне неше рет тамақтана аласың?",
              "options": [("2", "2 рет"), ("3", "3 рет"), ("4", "4 рет"), ("5", "5 рет")]},
    "place": {"type": "buttons", "q": "🏋️ Жаттығуды қайда жасайсың?",
              "options": [("home", "🏠 Үйде"), ("gym", "🏢 Залда")]},
    "restrictions": {"type": "text",
                     "q": "🚫 Жемейтін немесе аллергиялы тағамдарың бар ма? Қысқаша жаз. Жоқ болса «жоқ» деп жаз."},
    "health": {"type": "buttons",
               "q": ("⚕️ Дәрігер қойған шектеу, жүктілік, созылмалы ауру немесе жарақат бар ма? "
                     "Мен диагноз қоймаймын және ауру туралы егжей-тегжей сұрамаймын: бұл жауап тек "
                     "қауіпсіздік ескертуін қосу үшін."),
               "options": [("no", "Жоқ"), ("yes", "Бар / сенімді емеспін"), ("skip", "Айтқым келмейді")]},
}


def intake_steps_for(category: str) -> list:
    steps = ["adult", "wake", "bed"]
    if category in MEAL_CATEGORIES:
        steps.append("meals")
    if category in FITNESS:
        steps += ["place", "restrictions"]
    steps.append("health")
    return steps


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if user:
        cat = CATEGORIES.get(user["category"], {})
        streak = db.get_streak(telegram_id, user["current_day"])
        await update.message.reply_text(
            f"Сәлем, {user['full_name']}! 👋\n\n"
            f"📅 {min(user['current_day'], TOTAL_DAYS)}/{TOTAL_DAYS}-күн · {get_week_title(min(user['current_day'], TOTAL_DAYS))}\n"
            f"🎯 Бағыт: {cat.get('name', '?')}\n"
            f"🔥 {streak} күн қатарынан\n\n"
            f"Бүгінгі миссия: /today · Менің жүйем: /system"
        )
        return ConversationHandler.END

    has_access = await check_channel_membership(context, telegram_id)
    if not has_access:
        await update.message.reply_text(
            "🔒 Бұл бот тек TARTIP курсының қатысушыларына арналған.\n\n"
            "Access алу үшін админге хабарласыңыз: /support"
        )
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(cat["name"], callback_data=f"cat_{key}")]
        for key, cat in CATEGORIES.items()
    ]
    await update.message.reply_text(
        "Сәлем! TARTIP курсына қош келдің 💪\n\nАлдымен өз бағытыңды таңда:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASKING_CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category_key = query.data.split("_", 1)[1]
    if category_key not in CATEGORIES:
        return ASKING_CATEGORY
    context.user_data["category"] = category_key

    cat = CATEGORIES[category_key]
    await query.edit_message_text(f"Таңдадың: {cat['name']} ✅")

    pdf_link = cat.get("pdf_link")
    if pdf_link:
        await query.message.reply_text(
            f"📄 Алдымен гайдыңды оқып шық: онда осы бағыттың негізгі білімі мен чек-листі бар:\n\n{pdf_link}"
        )

    await query.message.reply_text("Енді танысайық — атыңды жаз:")
    return ASKING_NAME


async def ask_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()[:60]
    if not name:
        await update.message.reply_text("Атыңды жаз.")
        return ASKING_NAME
    context.user_data["full_name"] = name
    await update.message.reply_text(
        f"Танысқаныма қуаныштымын, {name}! 🙌\n\n"
        "Нақты мақсатыңды жаз (мыс. '5 кг арықтау', 'таңғы 07:00-де тұру'):"
    )
    return ASKING_GOAL


async def ask_why(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goal = (update.message.text or "").strip()[:200]
    if not goal:
        await update.message.reply_text("Мақсатыңды жаз.")
        return ASKING_GOAL
    context.user_data["goal"] = goal
    await update.message.reply_text("Неге дәл осы мақсатқа жеткің келеді? (қысқаша жаз)")
    return ASKING_WHY


async def start_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    why = (update.message.text or "").strip()[:300]
    if not why:
        await update.message.reply_text("Қысқаша болса да жаз.")
        return ASKING_WHY
    context.user_data["goal_why"] = why
    category = context.user_data["category"]
    context.user_data["intake"] = {"steps": intake_steps_for(category), "i": 0, "answers": {}}
    await update.message.reply_text(
        "Миссияларды өз тәртібіңе бейімдеу үшін бірнеше қысқа сұрақ қоямын. "
        "Деректерің тек ботта және кураторда сақталады, /forget арқылы кез келген уақытта өшіре аласың."
    )
    await send_intake_question(update.message, context.user_data["intake"])
    return INTAKE


async def send_intake_question(message, state: dict):
    key = state["steps"][state["i"]]
    definition = INTAKE_DEFS[key]
    markup = None
    if definition["type"] == "buttons":
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=f"in_{key}_{value}")
            for value, label in definition["options"]
        ]])
    await message.reply_text(definition["q"], reply_markup=markup)


async def intake_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("intake")
    if not state:
        await update.effective_message.reply_text("Тіркеуді қайта бастайық: /start")
        return ConversationHandler.END

    key = state["steps"][state["i"]]
    definition = INTAKE_DEFS[key]
    query = update.callback_query
    category = context.user_data["category"]

    if query:
        await query.answer()
        parts = query.data.split("_", 2)
        message = query.message
        if len(parts) == 3 and parts[1] == "switch":
            new_category = parts[2]
            if new_category not in CATEGORIES or new_category in FITNESS:
                return INTAKE
            context.user_data["category"] = new_category
            state["steps"] = intake_steps_for(new_category)
            state["answers"]["adult"] = "no"
            state["i"] = state["steps"].index("adult") + 1
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            await message.reply_text(f"Таңдадың: {CATEGORIES[new_category]['name']} ✅")
            await send_intake_question(message, state)
            return INTAKE
        if len(parts) < 3 or parts[1] != key:
            return INTAKE
        value = parts[2]
        if value not in [v for v, _ in definition.get("options", [])]:
            return INTAKE
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
    else:
        message = update.message
        text = (message.text or "").strip()
        if definition["type"] == "buttons":
            await message.reply_text("Төмендегі батырмалардың бірін бас 👇")
            return INTAKE
        if definition["type"] == "time":
            value = plan_mod.parse_time(text)
            if not value:
                await message.reply_text("Уақытты сағат:минут түрінде жаз, мысалы 07:30.")
                return INTAKE
        else:
            value = text[:120]
            if not value:
                await message.reply_text("Қысқаша жауап жаз («жоқ» болса да жарайды).")
                return INTAKE

    if key == "adult" and value == "no" and category in FITNESS:
        state["answers"]["adult"] = "no"
        await message.reply_text(
            "Бұл бағыт ересектерге арналған: жасөспірімнің тамағын, салмағын немесе жүктемесін "
            "өзгерту дәрігер мен ата-ананың бақылауында болуы керек. Қауіпсіз бағыттардың бірін таңда:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(CATEGORIES["discipline"]["name"], callback_data="in_switch_discipline")],
                [InlineKeyboardButton(CATEGORIES["uyqy_energy"]["name"], callback_data="in_switch_uyqy_energy")],
            ]),
        )
        return INTAKE

    state["answers"][key] = value
    state["i"] += 1
    if state["i"] >= len(state["steps"]):
        return await finish_registration(update, context, message)
    await send_intake_question(message, state)
    return INTAKE


def build_roadmap_text(category_key: str) -> str:
    cat = CATEGORIES[category_key]
    prog = PROGRAMS[category_key]
    lines = [f"🗺 <b>Сенің 28 күндік жолың — {esc(cat['name'])}</b>", ""]
    for week in prog["weeks"]:
        lines.append(f"<b>{esc(week['theme'])}</b>")
        lines.append(f"🎯 {esc(week['goal'])}")
        lines.append("")
    lines.append(
        "Күн сайын бір нақты миссия келеді: ол сенің уақытыңа бейімделеді. Жауап жазатын күндердегі "
        "жауаптарың «Менің жүйем» құжатына жиналады (/system). 28-күні сенде ботсыз да жалғастыра "
        "алатын жеке жүйең болады."
    )
    return "\n".join(lines)


async def roadmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user or user["category"] not in PROGRAMS:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await update.message.reply_text(build_roadmap_text(user["category"]), parse_mode=ParseMode.HTML)


async def finish_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, message):
    tg_user = update.effective_user
    data = context.user_data
    answers = data["intake"]["answers"]
    category = data["category"]

    profile = {
        "wake_time": answers.get("wake"),
        "bed_time": answers.get("bed"),
        "meals": int(answers["meals"]) if answers.get("meals") else None,
        "place": answers.get("place"),
        "restrictions": answers.get("restrictions"),
        "health_flag": answers.get("health"),
        "adult": answers.get("adult"),
    }
    db.add_user(tg_user.id, tg_user.username or "", data["full_name"], category,
                data["goal"], data["goal_why"], profile=profile)
    user = db.get_user(tg_user.id)
    values = plan_mod.profile_values(user)

    lines = [
        "✅ Профилің дайын.\n",
        f"🎯 Бағыт: {CATEGORIES[category]['name']}",
        f"📝 Мақсат: {data['goal']}",
        f"💭 Себебі: {data['goal_why']}\n",
        f"⏰ Ояну: {values['wake']} · 🌙 Жату: {values['bed']} · 📵 Экранды өшіру: {values['screens_off']}",
    ]
    if category in MEAL_CATEGORIES:
        lines.append(f"🍽 Астар уақыты: {values['mlist']}")
        if profile["meals"] and values["meal_count"] < profile["meals"]:
            lines.append("ℹ️ Күн қысқа болғандықтан, ас санын азайттым: соңғы ас жатуға ~3 сағат қалғанда болады.")
    lines.append(f"\nБірінші миссияң ертең таңғы {DAILY_SEND_HOUR}:00-де келеді. Дедлайн: кешкі {DEADLINE_HOUR}:00. "
                 "Дереу көргің келсе — /today.")
    await message.reply_text("\n".join(lines))
    if profile["health_flag"] == "yes":
        await message.reply_text(
            "⚕️ Денсаулығыңда шектеу бар деп белгіледің. TARTIP диагноз қоймайды және дәрігер "
            "кеңесін алмастырмайды: тамақ немесе жаттығу жүйесін өзгертер алдында дәрігермен келіс."
        )
    await message.reply_text(build_roadmap_text(category), parse_mode=ParseMode.HTML)
    await message.reply_text("Жауаптарың мен фотоларыңды тек куратор көреді. Көмек керек болса — /support.")
    data.pop("intake", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("intake", None)
    await update.message.reply_text("Тіркелу тоқтатылды. Қайта бастау үшін /start жаз.")
    return ConversationHandler.END


# ---------- Күндік миссия ----------

def build_day_text(user: dict, day: int) -> str:
    plan = get_daily_plan(user["category"], day)
    r = plan_mod.render_day(plan, user)
    week = get_week(user["category"], day)

    lines = [
        f"📅 <b>{day}/{TOTAL_DAYS}-күн</b> · {esc(week['theme'])}",
        "",
        f"🎯 <b>{esc(r['title'])}</b>",
        esc(r["mission"]),
        "",
        "<b>Нақты жоспар:</b>",
    ]
    lines.extend(f"• {esc(item)}" for item in r["schedule"])

    lesson = LESSONS.get(str(r["lesson"])) if r["lesson"] else None
    if lesson:
        lines.append(f"\n📚 Сабақ: {esc(lesson['title'])}\n▶️ {lesson['link']}")

    if week.get("core"):
        lines.append(f"\n🔁 <b>Апта өзегі:</b> {esc(week['core'])}")

    if r["proof"] == "button":
        proof_line = "төмендегі батырманы бас."
    elif r["proof"] == "photo":
        proof_line = "фото жібер."
    else:
        proof_line = f"осы чатқа жауап жаз (дауыстық хабарлама да болады): {esc(r['ask'])}"
    lines.extend([
        f"\n📌 <b>Есеп:</b> {proof_line}",
        f"🛟 <b>Қиын күн нұсқасы (2–10 минут):</b> {esc(r['fallback'])}",
        f"\n⏰ Күнді {DEADLINE_HOUR}:00-ге дейін жап.",
    ])
    if user.get("health_flag") == "yes" and (r["med"] or day in WEEK_START_DAYS):
        lines.append("\n⚕️ Денсаулығыңда шектеу бар деп белгіледің: тамақ немесе жаттығу жүктемесін өзгертер алдында дәрігермен келіс.")
    return "\n".join(lines)


def build_day_keyboard(telegram_id: int, day: int, category_key: str) -> InlineKeyboardMarkup:
    done = db.get_done_days(telegram_id)
    plan = get_daily_plan(category_key, day)
    rows = []
    if day in done:
        rows.append([InlineKeyboardButton(STATUS_LABELS.get(done[day], "✅ Орындалды"), callback_data=f"noop_{day}")])
    else:
        if plan["proof"] == "button":
            rows.append([InlineKeyboardButton("✅ Орындадым", callback_data=f"done_{day}")])
        else:
            action = "📸 Фото жібер" if plan["proof"] == "photo" else "✍️ Жауап жаз"
            rows.append([InlineKeyboardButton(action, callback_data=f"noop_{day}")])
        rows.append([InlineKeyboardButton("⚡ Жеңіл нұсқаны орындадым", callback_data=f"light_{day}")])
    rows.append([
        InlineKeyboardButton("🛟 Жоспар B", callback_data=f"planhelp_{day}"),
        InlineKeyboardButton("💬 Кураторға жазу", callback_data="ask_support"),
    ])
    return InlineKeyboardMarkup(rows)


async def send_finish(context: ContextTypes.DEFAULT_TYPE, user: dict):
    """Курс біткенде бір рет қана жіберіледі (күнде қайталанбайды)."""
    if user.get("awaiting") == "finish_sent":
        return
    uid = user["telegram_id"]
    done = db.get_done_days(uid)
    if TOTAL_DAYS not in done:
        for chunk in plan_mod.compile_system(user, PROGRAMS, db.get_text_responses(uid), CATEGORIES[user["category"]]["name"]):
            await context.bot.send_message(uid, chunk)
    await context.bot.send_message(
        uid,
        f"🎉 Құттықтаймын! Сен 28 күндік TARTIP курсын аяқтадың: {len(done)}/{TOTAL_DAYS} күн орындалды.\n\n"
        "«Менің жүйем» құжатың жоғарыда (және кез келген уақытта /system). Енді оны ботсыз да қолдана аласың.\n\n"
        "Қайта бастасаң, прогресс пен жауаптар өшеді (құжатың чатта қалады).",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Сол бағытты 1-күннен қайта бастау", callback_data="continue_yes")],
            [InlineKeyboardButton("🛑 Тоқтату", callback_data="continue_no")],
        ]),
    )
    db.set_awaiting(uid, "finish_sent")


async def send_day_to_user(context: ContextTypes.DEFAULT_TYPE, user: dict):
    day = user["current_day"]
    uid = user["telegram_id"]
    if user["category"] not in PROGRAMS:
        await context.bot.send_message(
            uid,
            "📌 TARTIP бағдарламалары жаңартылды. Сенің бұрынғы бағытың енді негізгі курсқа кірмейді. "
            "Жаңа бағыт таңдау үшін админге жаз (/support): ол профильді қауіпсіз түрде ауыстырып береді.",
        )
        return
    if day > TOTAL_DAYS:
        await send_finish(context, user)
        return

    await context.bot.send_message(
        uid, build_day_text(user, day), parse_mode=ParseMode.HTML,
        reply_markup=build_day_keyboard(uid, day, user["category"]),
        disable_web_page_preview=True,
    )

    # «Екі рет қатар өткізбе»: кешегі миссия жабылмаса, жеңіл нұсқамен жабуға мүмкіндік
    if day > 1:
        done = db.get_done_days(uid)
        prev = day - 1
        if prev not in done:
            prev_plan = plan_mod.render_day(get_daily_plan(user["category"], prev), user)
            await context.bot.send_message(
                uid,
                f"↩️ Кешегі миссия («{esc(prev_plan['title'])}») жабылмады — бұл қалыпты. "
                f"Қаласаң, жеңіл нұсқасын орындап жабуға болады:\n\n{esc(prev_plan['fallback'])}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Кешегіні жабу", callback_data=f"catchup_{prev}")
                ]]),
            )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await send_day_to_user(context, user)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Сен әлі тіркелмегенсің, ұмытатын дерек жоқ. /start жаз.")
        return
    await update.message.reply_text(
        "⚠️ Барлық деректерің (профиль, жауаптар, прогресс) өшеді және қайтарылмайды. Растайсың ба?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Иә, барлығын өшір", callback_data="forget_yes")],
            [InlineKeyboardButton("Жоқ, қалдыр", callback_data="forget_no")],
        ]),
    )


async def handle_forget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "forget_yes":
        db.delete_user(update.effective_user.id)
        await query.edit_message_text("🗑 Барлық деректерің өшірілді. /start арқылы қайтадан бастай аласың.")
    else:
        await query.edit_message_text("Жақсы, ештеңе өшпеді. Жалғастыра бер 💪")


async def wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = parse_id(context.args)
    if target_id is None:
        await update.message.reply_text("Қолдану: /wipe <telegram_id>")
        return
    db.delete_user(target_id)
    await update.message.reply_text(f"🗑 {target_id} толық ұмытылды (профиль де өшті).")


# ---------- Еске салулар және күн жылжыту ----------

def mission_open(user: dict) -> bool:
    day = user["current_day"]
    if day > TOTAL_DAYS or user["category"] not in PROGRAMS:
        return False
    return day not in db.get_done_days(user["telegram_id"])


def open_title(user: dict) -> str:
    return esc(get_daily_plan(user["category"], user["current_day"])["title"])


async def daily_send_job(context: ContextTypes.DEFAULT_TYPE):
    for user in db.get_all_active_users():
        has_access = await check_channel_membership(context, user["telegram_id"])
        if not has_access:
            continue
        try:
            db.clear_awaiting_prefixes(user["telegram_id"])
            user = db.get_user(user["telegram_id"])
            await send_day_to_user(context, user)
        except Exception as e:
            logger.error(f"daily_send_job failed for {user['telegram_id']}: {e}")


async def _remind(context: ContextTypes.DEFAULT_TYPE, template: str):
    for user in db.get_all_active_users():
        if not mission_open(user):
            continue
        try:
            await context.bot.send_message(
                user["telegram_id"], template.format(title=open_title(user), hour=DEADLINE_HOUR),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"reminder failed for {user['telegram_id']}: {e}")


async def midday_check_job(context: ContextTypes.DEFAULT_TYPE):
    await _remind(context, "👋 Бүгінгі миссия: «{title}». Уақыт бар кезде орында; қиын болса ⚡ жеңіл нұсқа да есептеледі. /today")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    await _remind(context, "⏰ Дедлайнға 2 сағат қалды. Миссия: «{title}». Кемі жеңіл нұсқасын жап: /today")


async def final_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    await _remind(context, "⚠️ Дедлайнға 30 минут қалды. Миссия: «{title}». 2 минуттық жеңіл нұсқа да тізбекті сақтайды.")


def build_weekly_review_text(week: int) -> str:
    return (
        f"📊 <b>{week}-АПТА REVIEW</b>\n\n"
        "Апта аяқталды. 3 сұраққа бір хабарламамен жауап жаз:\n\n"
        "1) Бұл аптада не жақсы шықты?\n"
        "2) Не қиын болды?\n"
        "3) Келесі аптада нені өзгертемін?\n\n"
        "Жауабыңды осы чатқа жібер (кемі 1–2 сөйлем). Жауабыңа қарап келесі аптаға ұсыныс беремін."
    )


async def deadline_advance_job(context: ContextTypes.DEFAULT_TYPE):
    for user in db.get_all_active_users():
        if not db.needs_advance_today(user):
            continue
        day = user["current_day"]
        uid = user["telegram_id"]
        if day > TOTAL_DAYS or user["category"] not in PROGRAMS:
            continue
        done = db.get_done_days(uid)
        try:
            if day in done:
                suffix = " (жеңіл нұсқа)" if done[day] != "full" else ""
                await context.bot.send_message(uid, f"🎉 {day}-күн жабылды{suffix}! Ертең жаңа миссия келеді.")
            else:
                await context.bot.send_message(
                    uid,
                    f"❌ {day}-күн жабылмады. Ештеңе етпейді: ертең таңертең оны жеңіл нұсқамен жауып, "
                    "тізбекті сақтауға болады."
                )
            if day in WEEKLY_REVIEW_DAYS:
                week = WEEKLY_REVIEW_DAYS[day]
                await context.bot.send_message(uid, build_weekly_review_text(week), parse_mode=ParseMode.HTML)
                db.set_awaiting(uid, f"review_{week}")
        except Exception as e:
            logger.error(f"deadline_advance_job message failed for {uid}: {e}")
        db.advance_day(uid)


# ---------- Батырмалар ----------

async def _refresh_keyboard(query, telegram_id: int, day: int, category: str):
    try:
        await query.edit_message_reply_markup(reply_markup=build_day_keyboard(telegram_id, day, category))
    except TelegramError:
        pass


async def handle_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = db.get_user(update.effective_user.id)
    day = None
    try:
        day = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        pass
    if user and day and day in db.get_done_days(user["telegram_id"]):
        await query.answer("Бұл күн жабылған ✅", show_alert=False)
    else:
        await query.answer("Бұл миссияны осы чатқа жауап жазып (мәтін, дауыс немесе фото) жабасың.", show_alert=True)


async def handle_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        day = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    user = db.get_user(update.effective_user.id)
    if not user or user["current_day"] != day or user["category"] not in PROGRAMS:
        await query.answer("Бұл миссия ескірген.", show_alert=True)
        return
    if get_daily_plan(user["category"], day)["proof"] != "button":
        await query.answer("Бұл миссия жауап жазып жабылады.", show_alert=True)
        return
    streak = db.record_checkin(user["telegram_id"], day, mode="full")
    await query.answer(f"✅ Белгіленді! 🔥 {streak} күн қатарынан")
    await _refresh_keyboard(query, user["telegram_id"], day, user["category"])


async def handle_light_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        day = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    user = db.get_user(update.effective_user.id)
    if not user or user["current_day"] != day or user["category"] not in PROGRAMS:
        await query.answer("Бұл миссия ескірген.", show_alert=True)
        return
    streak = db.record_checkin(user["telegram_id"], day, mode="light")
    await query.answer(f"⚡ Жеңіл нұсқа есептелді. 🔥 {streak} күн қатарынан", show_alert=True)
    await _refresh_keyboard(query, user["telegram_id"], day, user["category"])


async def handle_catchup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        day = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    user = db.get_user(update.effective_user.id)
    if not user or day != user["current_day"] - 1 or day < 1:
        await query.answer("Бұл күнді енді жабу мүмкін емес.", show_alert=True)
        return
    if day in db.get_done_days(user["telegram_id"]):
        await query.answer("Бұл күн жабылған ✅")
        return
    streak = db.record_checkin(user["telegram_id"], day, mode="catchup")
    await query.answer(f"↩️ Кешегі күн жабылды. 🔥 {streak} күн қатарынан", show_alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def handle_plan_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = db.get_user(update.effective_user.id)
    if not user or user["category"] not in PROGRAMS:
        await query.answer()
        return
    try:
        day = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    if user["current_day"] != day:
        await query.answer("Бұл күннің жоспары ескірген.", show_alert=True)
        return
    r = plan_mod.render_day(get_daily_plan(user["category"], day), user)
    await query.answer()
    await query.message.reply_text(
        f"🛟 <b>Жоспар B — {esc(r['title'])}</b>\n\n{esc(r['fallback'])}\n\n"
        "Бүгін мінсіз болу міндет емес. Ең кіші нұсқасын орында да, «⚡ Жеңіл нұсқаны орындадым» батырмасын бас.",
        parse_mode=ParseMode.HTML,
    )


async def handle_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    if query.data == "continue_yes":
        db.reset_progress(telegram_id)
        await query.edit_message_text("Керемет! 1-күннен қайта бастаймыз 💪 /today арқылы көре аласың.")
    else:
        db.set_paused(telegram_id, True)
        await query.edit_message_text("Түсінікті! Курсты аяқтадың. Кез келген уақытта админге жазып қайта бастай аласың. Сау бол! 👋")


# ---------- Жауаптар: мәтін, фото, дауыс ----------

async def _forward_to_admins(context: ContextTypes.DEFAULT_TYPE, text: str, user: dict, kind: str = "text", file_id: str = None):
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ Жауап жазу", callback_data=f"admin_msg_{user['telegram_id']}_all_0")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            if kind == "photo":
                await context.bot.send_photo(admin_id, file_id, caption=text[:1000], reply_markup=markup)
            elif kind == "voice":
                await context.bot.send_voice(admin_id, file_id, caption=text[:1000], reply_markup=markup)
            else:
                await context.bot.send_message(admin_id, text[:4000], reply_markup=markup)
        except TelegramError as e:
            logger.error(f"forward to admin {admin_id} failed: {e}")


def _user_tag(user: dict) -> str:
    username = f"@{user['username']}" if user.get("username") else "username жоқ"
    return f"{user['full_name']} ({username}, id: {user['telegram_id']})"


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await _start_support(update.message, user)


async def _start_support(message, user: dict):
    if not ADMIN_IDS:
        await message.reply_text("Қазір куратор қолжетімсіз. Кейінірек қайталап көр.")
        return
    db.set_awaiting(user["telegram_id"], "support")
    await message.reply_text(
        "Сұрағыңды немесе мәселеңді келесі хабарламамен жаз (мәтін, дауыс немесе фото) — куратор көреді. 📩",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Болдырмау", callback_data="support_cancel")]]),
    )


async def handle_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = db.get_user(update.effective_user.id)
    if not user:
        return
    if query.data == "support_cancel":
        db.set_awaiting(user["telegram_id"], "")
        await query.edit_message_text("Жақсы, болдырмадым.")
        return
    await _start_support(query.message, user)


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # Админ клиентке хабар жазып жатқан режим
    target = context.user_data.get("admin_msg_to")
    if is_admin(telegram_id) and target:
        context.user_data.pop("admin_msg_to", None)
        try:
            await context.bot.send_message(target, f"💬 <b>Куратордан:</b>\n{esc(text)}", parse_mode=ParseMode.HTML)
            await update.message.reply_text("✅ Хабар клиентке жіберілді.")
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ Жіберілмеді: {e}")
        return

    user = db.get_user(telegram_id)
    if not user:
        await update.message.reply_text("Тіркелу үшін /start жаз.")
        return

    awaiting = user.get("awaiting") or ""
    if awaiting == "support":
        db.set_awaiting(telegram_id, "")
        await _forward_to_admins(context, f"📩 Қолдау: {_user_tag(user)}\n{user['current_day']}-күн\n\n{text}", user)
        await update.message.reply_text("✅ Куратор көрді, жауап береді. Бұл арада миссияңды жалғастыра бер.")
        return

    if awaiting.startswith("review_"):
        await _handle_review_reply(update, user, awaiting, text)
        return

    await _handle_mission_text(update, context, user, text)


async def _handle_review_reply(update: Update, user: dict, awaiting: str, text: str):
    try:
        week = int(awaiting.split("_")[1])
    except (IndexError, ValueError):
        db.set_awaiting(user["telegram_id"], "")
        return
    if len(text) < 20:
        await update.message.reply_text("Жауап тым қысқа: кемі 1–2 сөйлем жаз. Үш сұраққа жауап бер.")
        return
    uid = user["telegram_id"]
    db.record_checkin(uid, week * 7, habit_id=f"review_{week}", text_response=text)
    db.set_awaiting(uid, "")
    done_days = db.get_done_days(uid)
    done, light, elapsed = plan_mod.week_done_counts(done_days, week, week * 7)
    next_goal = PROGRAMS[user["category"]]["weeks"][week]["goal"] if week < 4 and user["category"] in PROGRAMS else ""
    light_part = f" (оның {light}-і жеңіл нұсқа)" if light else ""
    await update.message.reply_text(
        f"✅ Review қабылданды. Осы аптада {elapsed} күннің {done}-і жабылды{light_part}.\n\n"
        + plan_mod.adaptation_text(done, elapsed, next_goal)
    )


async def _handle_mission_text(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, text: str):
    uid = user["telegram_id"]
    day = user["current_day"]
    if user["paused"]:
        await update.message.reply_text("Курсың қазір паузада. Қайта қосу үшін /support жаз.")
        return
    if day > TOTAL_DAYS or user["category"] not in PROGRAMS:
        return
    plan = get_daily_plan(user["category"], day)
    r = plan_mod.render_day(plan, user)

    if r["proof"] == "photo":
        await update.message.reply_text("Бүгінгі миссия фотомен жабылады 📸 — суретті осы чатқа жібер.")
        return
    if r["proof"] == "button":
        await update.message.reply_text("Бүгінгі миссия батырмамен жабылады ✅ — /today арқылы ашып, «Орындадым» бас.")
        return
    if len(text) < r["min"]:
        await update.message.reply_text(
            f"Жауап тым қысқа (кемі {r['min']} таңба). Формат: {r['ask']}\n\n"
            "Егер бұл миссияға жауап емес, сұрағың болса — /support."
        )
        return

    already = day in db.get_done_days(uid)
    streak = db.record_checkin(uid, day, text_response=text, mode="full")
    msg = "✅ Жауабың жаңартылды." if already else f"✅ Жауабың қабылданды! 🔥 {streak} күн қатарынан."
    if r["save"]:
        msg += f"\n🧩 «{r['save']}» «Менің жүйем» құжатыңа сақталды (/system)."
    await update.message.reply_text(msg)

    if day == TOTAL_DAYS:
        for chunk in plan_mod.compile_system(db.get_user(uid), PROGRAMS, db.get_text_responses(uid), CATEGORIES[user["category"]]["name"]):
            await context.bot.send_message(uid, chunk)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return
    file_id = update.message.photo[-1].file_id
    awaiting = user.get("awaiting") or ""
    if awaiting == "support":
        db.set_awaiting(telegram_id, "")
        await _forward_to_admins(context, f"📩 Қолдау (фото): {_user_tag(user)}", user, kind="photo", file_id=file_id)
        await update.message.reply_text("✅ Куратор көрді, жауап береді.")
        return

    day = user["current_day"]
    if user["paused"] or day > TOTAL_DAYS or user["category"] not in PROGRAMS:
        return
    plan = get_daily_plan(user["category"], day)
    if plan["proof"] != "photo":
        await update.message.reply_text("Бүгінгі миссия фотомен жабылмайды. Миссияны көру: /today. Куратормен байланыс: /support.")
        return

    streak = db.record_checkin(telegram_id, day, proof_file_id=file_id, mode="full")
    await update.message.reply_text(f"✅ Фото қабылданды! 🔥 {streak} күн қатарынан.")
    await _forward_to_admins(context, f"📸 {_user_tag(user)} — {day}-күн, {plan['title']}", user, kind="photo", file_id=file_id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return
    file_id = update.message.voice.file_id
    awaiting = user.get("awaiting") or ""
    if awaiting == "support":
        db.set_awaiting(telegram_id, "")
        await _forward_to_admins(context, f"📩 Қолдау (дауыс): {_user_tag(user)}", user, kind="voice", file_id=file_id)
        await update.message.reply_text("✅ Куратор тыңдап, жауап береді.")
        return

    day = user["current_day"]
    if user["paused"] or day > TOTAL_DAYS or user["category"] not in PROGRAMS:
        return
    plan = get_daily_plan(user["category"], day)
    if plan["proof"] != "text":
        await update.message.reply_text("Бүгінгі миссия дауыстық жауаппен жабылмайды. Миссияны көру: /today.")
        return
    streak = db.record_checkin(telegram_id, day, proof_file_id=file_id, text_response="🎤 дауыстық жауап (куратор тыңдайды)", mode="full")
    await update.message.reply_text(f"✅ Дауыстық жауабың қабылданды! 🔥 {streak} күн қатарынан.")
    await _forward_to_admins(context, f"🎤 {_user_tag(user)} — {day}-күн, {plan['title']}", user, kind="voice", file_id=file_id)


# ---------- Прогресс, жүйе, сабақтар ----------

async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user or user["category"] not in PROGRAMS:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    day = min(user["current_day"], TOTAL_DAYS)
    done = db.get_done_days(user["telegram_id"])
    streak = db.compute_streak(done.keys(), user["current_day"])
    light = sum(1 for m in done.values() if m != "full")
    filled = round(len(done) / TOTAL_DAYS * 12)
    bar = "█" * filled + "░" * (12 - filled)
    cat_name = CATEGORIES[user["category"]]["name"]
    week = plan_mod.week_of(day)
    w_done, w_light, w_elapsed = plan_mod.week_done_counts(done, week, user["current_day"])
    await update.message.reply_text(
        f"📊 Сенің прогресің\n\n{bar} {len(done)}/{TOTAL_DAYS} күн жабылды\n\n"
        f"📅 Күн: {day}/{TOTAL_DAYS} · {week}-апта: {w_done}/{w_elapsed}\n"
        f"🎯 Бағыт: {cat_name}\n"
        f"🔥 {streak} күн қатарынан" + (f" · ⚡ жеңіл нұсқа: {light}" if light else "") + "\n"
        f"📝 Мақсатың: {user['goal']}\n\n"
        "Жауаптарың: /system"
    )


async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user or user["category"] not in PROGRAMS:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    for chunk in plan_mod.compile_system(user, PROGRAMS, db.get_text_responses(user["telegram_id"]), CATEGORIES[user["category"]]["name"]):
        await update.message.reply_text(chunk)


async def lessons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard, row = [], []
    for num in LESSONS:
        row.append(InlineKeyboardButton(num, callback_data=f"lesson_{num}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    await update.message.reply_text(
        "📚 Видео сабақтар каталогы\n\nНөмірді бас:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_lesson_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    num = query.data.split("_")[1]
    lesson = LESSONS.get(num)
    if lesson:
        await query.message.reply_text(f"{lesson['title']}\n\n▶️ {lesson['link']}")


# ---------- Админ ----------

ADMIN_PAGE_SIZE = 6


def get_admin_user_snapshot(user: dict, now: datetime = None, done: dict = None) -> dict:
    """Админге сан емес, клиентпен не істеу керегін көрсететін күй. Уақытты ескереді:
    күн басында «әлі орындамады» деп қызыл шығармайды."""
    now = now or datetime.now(ALMATY_TZ)
    uid = user["telegram_id"]
    day = user["current_day"]
    done = db.get_done_days(uid) if done is None else done
    streak = db.compute_streak(done.keys(), day)
    base = {"streak": streak, "done_total": len(done), "today_done": day in done, "mode": done.get(day)}

    if user["paused"]:
        return {**base, "label": "⏸ Паузада", "priority": "paused"}
    if day > TOTAL_DAYS:
        return {**base, "label": "🏁 Курс аяқталды", "priority": "done"}

    week = plan_mod.week_of(day)
    w_done, w_light, w_elapsed = plan_mod.week_done_counts(done, week, day)
    base.update({"week_done": w_done, "week_light": w_light, "week_elapsed": w_elapsed})

    missed_run = 0
    for d in range(day - 1, 0, -1):
        if d in done:
            break
        missed_run += 1
    base["missed_run"] = missed_run

    if day in done:
        label = "🟢 Бүгін жеңіл нұсқа" if done[day] != "full" else "🟢 Бүгін орындады"
        return {**base, "label": label, "priority": "green"}
    if day == 1 and not done:
        return {**base, "label": "🆕 Жаңа қатысушы", "priority": "new"}
    if missed_run >= 2:
        return {**base, "label": f"🔴 {missed_run} күн өткізді — жазу керек", "priority": "red"}
    if missed_run == 1:
        return {**base, "label": "🟡 Кеше өткізді", "priority": "yellow"}
    if now.hour >= 18:
        return {**base, "label": "🟡 Бүгін әлі жасамады", "priority": "yellow"}
    return {**base, "label": "⚪ Күн жүріп жатыр", "priority": "ok"}


def get_admin_users(filter_name: str = "all") -> list:
    users = db.get_all_users()
    if filter_name == "attention":
        return [u for u in users if get_admin_user_snapshot(u)["priority"] in {"red", "yellow"}]
    if filter_name == "new":
        return [u for u in users if get_admin_user_snapshot(u)["priority"] == "new"]
    return users


def build_admin_dashboard():
    users = db.get_all_users()
    snapshots = [get_admin_user_snapshot(u) for u in users]
    attention = sum(s["priority"] in {"red", "yellow"} for s in snapshots)
    new_users = sum(s["priority"] == "new" for s in snapshots)
    completed_today = sum(s["priority"] == "green" for s in snapshots)
    text = (
        "👑 TARTIP ADMIN — бүгінгі басқару\n\n"
        f"🔴🟡 Назар керек: {attention}\n"
        f"🆕 Жаңа қатысушы: {new_users}\n"
        f"🟢 Бүгін миссияны орындаған: {completed_today}\n"
        f"👥 Барлық клиент: {len(users)}\n\n"
        "Төменнен адамды аш: карточкада мақсаты, апталық орындалуы, соңғы жауаптары және "
        "бірден әрекет ету батырмалары бар."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔴🟡 Назар керек ({attention})", callback_data="admin_list_attention_0")],
        [InlineKeyboardButton(f"🆕 Жаңа қатысушылар ({new_users})", callback_data="admin_list_new_0")],
        [InlineKeyboardButton("👥 Барлық клиенттер каталогы", callback_data="admin_list_all_0")],
        [InlineKeyboardButton("📊 Қысқа статистика", callback_data="admin_stats")],
    ])
    return text, keyboard


def build_admin_list(filter_name: str, page: int):
    users = get_admin_users(filter_name)
    max_page = max(0, (len(users) - 1) // ADMIN_PAGE_SIZE)
    page = max(0, min(page, max_page))
    shown = users[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
    titles = {"all": "👥 Барлық клиенттер", "attention": "🔴🟡 Назар керек", "new": "🆕 Жаңа қатысушылар"}
    text = f"{titles.get(filter_name, '👥')}\n\nАдамды басып, толық картасын аш. {len(users)} адам · {page + 1}/{max_page + 1} бет"
    keyboard = []
    for user in shown:
        snapshot = get_admin_user_snapshot(user)
        name = user["full_name"] or "Аты жазылмаған"
        label = f"{snapshot['label']} · {name} · {min(user['current_day'], TOTAL_DAYS)}/{TOTAL_DAYS}"
        keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"admin_user_{user['telegram_id']}_{filter_name}_{page}")])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️ Алдыңғы", callback_data=f"admin_list_{filter_name}_{page - 1}"))
    if page < max_page:
        navigation.append(InlineKeyboardButton("Келесі ▶️", callback_data=f"admin_list_{filter_name}_{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton("🏠 Админ мәзірі", callback_data="admin_home")])
    return text, InlineKeyboardMarkup(keyboard)


def _recent_answers(user: dict, limit: int = 3) -> list:
    rows = db.get_text_responses(user["telegram_id"])[-limit:]
    result = []
    for row in rows:
        title = ""
        if user["category"] in PROGRAMS and 1 <= row["day"] <= TOTAL_DAYS:
            title = get_daily_plan(user["category"], row["day"])["title"]
        text = " ".join((row["text_response"] or "").split())
        result.append(f"{row['day']}-күн · {title}: {text[:180]}{'…' if len(text) > 180 else ''}")
    return result


def build_admin_user_card(user: dict, filter_name: str, page: int):
    snapshot = get_admin_user_snapshot(user)
    category_name = CATEGORIES.get(user["category"], {}).get("name", "Белгісіз бағыт")
    username = f"@{user['username']}" if user.get("username") else "username жоқ"
    day = min(user["current_day"], TOTAL_DAYS)

    if user["current_day"] > TOTAL_DAYS:
        today_line = "Курс аяқталды"
    elif snapshot.get("today_done"):
        today_line = "орындалды (жеңіл нұсқа)" if snapshot["mode"] != "full" else "орындалды"
    else:
        today_line = "әлі орындалмаған"

    values = plan_mod.profile_values(user)
    profile_bits = [f"ояну {values['wake']}", f"жату {values['bed']}"]
    if user.get("meals"):
        profile_bits.append(f"{user['meals']} ас")
    if user.get("place"):
        profile_bits.append("залда" if user["place"] == "gym" else "үйде")
    restr = (user.get("restrictions") or "").strip()
    if restr and restr.lower() not in plan_mod.NO_RESTRICTION_WORDS:
        profile_bits.append(f"шектеу: {restr}")
    flags = []
    if user.get("health_flag") == "yes":
        flags.append("⚕️ денсаулық белгісі бар (диагноз жоқ)")
    if user.get("adult") == "no":
        flags.append("🔞 18-ге толмаған")

    lines = [
        f"👤 {user['full_name']} ({username})",
        "",
        f"Күйі: {snapshot['label']}",
        f"Бағыты: {category_name}",
        f"Мақсаты: {user['goal']}",
        f"Неге келді: {user.get('goal_why') or 'жазылмаған'}",
        "",
        f"🧾 Профиль: {', '.join(profile_bits)}",
    ]
    lines.extend(flags)
    lines.append("")
    lines.append(f"📅 Күн: {day}/{TOTAL_DAYS}")
    if "week_done" in snapshot:
        light = f" (жеңіл: {snapshot['week_light']})" if snapshot["week_light"] else ""
        lines.append(f"🗓 Осы апта: {snapshot['week_done']}/{snapshot['week_elapsed']}{light}")
    lines.append(f"✅ Бүгін: {today_line}")
    lines.append(f"🔥 Қатарынан: {snapshot['streak']} күн · барлығы жабылған: {snapshot['done_total']}/{TOTAL_DAYS}")
    lines.append(f"🕓 Соңғы белгі: {user['last_checkin_date'] or 'әлі жоқ'}")
    answers = _recent_answers(user)
    if answers:
        lines.append("")
        lines.append("💬 Соңғы жауаптары:")
        lines.extend(f"• {a}" for a in answers)
    lines.append(f"\nID: {user['telegram_id']}")

    action = "▶️ Қайта қосу" if user["paused"] else "⏸ Паузаға қою"
    suffix = f"{user['telegram_id']}_{filter_name}_{page}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Хабар жазу", callback_data=f"admin_msg_{suffix}"),
         InlineKeyboardButton("📨 Апталық қорытынды", callback_data=f"admin_weekly_{suffix}")],
        [InlineKeyboardButton("📝 Барлық жауаптары", callback_data=f"admin_answers_{suffix}")],
        [InlineKeyboardButton(action, callback_data=f"admin_toggle_{suffix}")],
        [InlineKeyboardButton("🔄 Прогресті 1-күнге қайтару", callback_data=f"admin_reset_{suffix}")],
        [InlineKeyboardButton("◀️ Каталогқа оралу", callback_data=f"admin_list_{filter_name}_{page}")],
        [InlineKeyboardButton("🏠 Админ мәзірі", callback_data="admin_home")],
    ])
    return "\n".join(lines)[:4000], keyboard


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, keyboard = build_admin_dashboard()
    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Бұл бөлім тек админге арналған.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")
    action = parts[1] if len(parts) > 1 else ""

    if action == "home":
        text, keyboard = build_admin_dashboard()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if action == "stats":
        s = db.get_stats()
        await query.edit_message_text(
            f"📊 TARTIP қысқа статистика\n\nБарлығы: {s['total']}\nАктивті: {s['active']}\n"
            f"Паузада: {s['paused']}\nОрташа қатарынан: {s['avg_streak']} күн",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Админ мәзірі", callback_data="admin_home")]]),
        )
        return
    if action == "cancelmsg":
        context.user_data.pop("admin_msg_to", None)
        await query.edit_message_text("Болдырмадым.")
        return
    if action == "list":
        try:
            filter_name, page = parts[2], int(parts[3])
        except (IndexError, ValueError):
            return
        text, keyboard = build_admin_list(filter_name, page)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    try:
        telegram_id, filter_name, page = int(parts[2]), parts[3], int(parts[4])
    except (IndexError, ValueError):
        return
    user = db.get_user(telegram_id)
    if not user:
        await query.edit_message_text("Клиент табылмады. Ол өшірілген болуы мүмкін.")
        return
    suffix = f"{telegram_id}_{filter_name}_{page}"

    if action == "msg":
        context.user_data["admin_msg_to"] = telegram_id
        await query.message.reply_text(
            f"✉️ {user['full_name']} клиентке жазатын хабарыңды келесі хабарламамен жібер.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Болдырмау", callback_data="admin_cancelmsg")]]),
        )
        return
    if action == "weekly":
        done = db.get_done_days(telegram_id)
        draft = plan_mod.weekly_draft(user, done, db.compute_streak(done.keys(), user["current_day"]))
        await query.message.reply_text(
            f"📨 Апталық қорытынды жобасы ({user['full_name']}):\n\n{draft}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Осылай жіберу", callback_data=f"admin_sendweekly_{suffix}")],
                [InlineKeyboardButton("✏️ Өзім жазамын", callback_data=f"admin_msg_{suffix}")],
            ]),
        )
        return
    if action == "sendweekly":
        done = db.get_done_days(telegram_id)
        draft = plan_mod.weekly_draft(user, done, db.compute_streak(done.keys(), user["current_day"]))
        try:
            await context.bot.send_message(telegram_id, f"💬 <b>Куратордан:</b>\n{esc(draft)}", parse_mode=ParseMode.HTML)
            await query.edit_message_text(f"✅ Жіберілді ({user['full_name']}):\n\n{draft}")
        except TelegramError as e:
            await query.edit_message_text(f"⚠️ Жіберілмеді: {e}")
        return
    if action == "answers":
        rows = db.get_text_responses(telegram_id)[-8:]
        reviews = db.get_reviews(telegram_id)
        lines = [f"📝 {user['full_name']} — соңғы жауаптары\n"]
        for row in rows:
            title = get_daily_plan(user["category"], row["day"])["title"] if user["category"] in PROGRAMS else ""
            lines.append(f"{row['day']}-күн · {title}:\n{' '.join((row['text_response'] or '').split())[:400]}\n")
        for rev in reviews[-2:]:
            lines.append(f"📊 {rev['habit_id']}:\n{' '.join((rev['text_response'] or '').split())[:400]}\n")
        if len(lines) == 1:
            lines.append("Әзірге жазбаша жауап жоқ.")
        await query.edit_message_text(
            "\n".join(lines)[:4000],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Карточкаға оралу", callback_data=f"admin_user_{suffix}")]]),
        )
        return
    if action == "toggle":
        db.set_paused(telegram_id, not bool(user["paused"]))
        user = db.get_user(telegram_id)
    elif action == "reset":
        await query.edit_message_text(
            f"⚠️ {user['full_name']} клиентінің прогресін 1-күнге қайтару керек пе?\n"
            "Барлық бұрынғы орындалған миссиялар мен жауаптар өшеді.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Иә, прогресті нөлдеу", callback_data=f"admin_confirmreset_{suffix}")],
                [InlineKeyboardButton("Жоқ, клиент картасына оралу", callback_data=f"admin_user_{suffix}")],
            ]),
        )
        return
    elif action == "confirmreset":
        db.reset_progress(telegram_id)
        user = db.get_user(telegram_id)
    text, keyboard = build_admin_user_card(user, filter_name, page)
    await query.edit_message_text(text, reply_markup=keyboard)


async def admin_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Күнде бір рет: кімге назар керек екенін админге жібереді."""
    if not ADMIN_IDS:
        return
    users = db.get_all_users()
    snaps = [(u, get_admin_user_snapshot(u)) for u in users]
    red = [u for u, s in snaps if s["priority"] == "red"]
    yellow = [u for u, s in snaps if s["priority"] == "yellow"]
    new = [u for u, s in snaps if s["priority"] == "new"]
    if not (red or yellow or new):
        return
    lines = ["🗞 TARTIP күндік дайджест\n"]
    if red:
        lines.append("🔴 Жазу керек:")
        lines.extend(f"— {u['full_name']} · {min(u['current_day'], TOTAL_DAYS)}-күн · id {u['telegram_id']}" for u in red[:15])
    if yellow:
        lines.append("\n🟡 Назар аудар:")
        lines.extend(f"— {u['full_name']} · {min(u['current_day'], TOTAL_DAYS)}-күн · id {u['telegram_id']}" for u in yellow[:15])
    if new:
        lines.append(f"\n🆕 Жаңа қатысушылар: {len(new)}")
    lines.append("\nТолық каталог: /admin")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, "\n".join(lines)[:4000])
        except TelegramError as e:
            logger.error(f"digest failed for {admin_id}: {e}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    s = db.get_stats()
    await update.message.reply_text(
        f"📊 Статистика\n\nБарлығы: {s['total']}\nАктивті: {s['active']}\n"
        f"Тоқтатылған: {s['paused']}\nОрташа қатарынан: {s['avg_streak']} күн"
    )


async def problems_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    snaps = [(u, get_admin_user_snapshot(u)) for u in db.get_all_users()]
    red = [u for u, s in snaps if s["priority"] == "red"]
    yellow = [u for u, s in snaps if s["priority"] == "yellow"]
    if not red and not yellow:
        await update.message.reply_text("Назар керек клиент жоқ 👍")
        return
    lines = []
    if red:
        lines.append("🔴 Араласу керек:")
        lines.extend(f"— {u['full_name']} (@{u.get('username') or '-'}) — {min(u['current_day'], TOTAL_DAYS)}-күн, id: {u['telegram_id']}" for u in red)
    if yellow:
        lines.append("\n🟡 Назар керек:")
        lines.extend(f"— {u['full_name']} (@{u.get('username') or '-'}) — {min(u['current_day'], TOTAL_DAYS)}-күн, id: {u['telegram_id']}" for u in yellow)
    await update.message.reply_text("\n".join(lines)[:4000])


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = parse_id(context.args)
    if target_id is None:
        await update.message.reply_text("Қолдану: /user <telegram_id>")
        return
    user = db.get_user(target_id)
    if not user:
        await update.message.reply_text("Клиент табылмады.")
        return
    text, keyboard = build_admin_user_card(user, "all", 0)
    await update.message.reply_text(text, reply_markup=keyboard)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = parse_id(context.args)
    if target_id is None:
        await update.message.reply_text("Қолдану: /pause <telegram_id>")
        return
    db.set_paused(target_id, True)
    await update.message.reply_text(f"⏸ {target_id} тоқтатылды.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = parse_id(context.args)
    if target_id is None:
        await update.message.reply_text("Қолдану: /resume <telegram_id>")
        return
    db.set_paused(target_id, False)
    await update.message.reply_text(f"▶️ {target_id} қайта қосылды.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = parse_id(context.args)
    if target_id is None:
        await update.message.reply_text("Қолдану: /reset <telegram_id>")
        return
    user = db.get_user(target_id)
    if not user:
        await update.message.reply_text("Клиент табылмады.")
        return
    await update.message.reply_text(
        f"⚠️ {user['full_name']} клиентінің прогресі мен жауаптары өшеді. Растайсың ба?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Иә, нөлдеу", callback_data=f"admin_confirmreset_{target_id}_all_0"),
            InlineKeyboardButton("Жоқ", callback_data="admin_home"),
        ]]),
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /broadcast <хабарлама>")
        return
    message = " ".join(context.args)
    users = db.get_all_active_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(u["telegram_id"], f"📢 {message}")
            sent += 1
        except Exception as e:
            logger.error(f"Broadcast failed for {u['telegram_id']}: {e}")
    await update.message.reply_text(f"Жіберілді: {sent}/{len(users)}")


# ---------- Іске қосу ----------

def run_health_server():
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"TARTIP bot is running")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health check server {port} портта іске қосылды")
    server.serve_forever()


async def post_init(application: Application):
    await setup_menus(application)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASKING_CATEGORY: [CallbackQueryHandler(category_chosen, pattern=r"^cat_")],
            ASKING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_goal)],
            ASKING_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_why)],
            ASKING_WHY: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_intake)],
            INTAKE: [
                CallbackQueryHandler(intake_answer, pattern=r"^in_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, intake_answer),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    application.add_handler(conv_handler)

    for name, handler in [
        ("today", today_command), ("roadmap", roadmap_command), ("progress", progress_command),
        ("system", system_command), ("support", support_command), ("lessons", lessons_command),
        ("whoami", whoami_command), ("forget", forget_command), ("wipe", wipe_command),
        ("admin", admin_panel), ("stats", stats_command), ("problems", problems_command),
        ("user", user_command), ("pause", pause_command), ("resume", resume_command),
        ("reset", reset_command), ("broadcast", broadcast_command),
    ]:
        application.add_handler(CommandHandler(name, handler))

    application.add_handler(CallbackQueryHandler(handle_noop_callback, pattern=r"^noop_"))
    application.add_handler(CallbackQueryHandler(handle_done_callback, pattern=r"^done_"))
    application.add_handler(CallbackQueryHandler(handle_light_callback, pattern=r"^light_"))
    application.add_handler(CallbackQueryHandler(handle_catchup_callback, pattern=r"^catchup_"))
    application.add_handler(CallbackQueryHandler(handle_plan_help_callback, pattern=r"^planhelp_"))
    application.add_handler(CallbackQueryHandler(handle_support_callback, pattern=r"^(ask_support|support_cancel)$"))
    application.add_handler(CallbackQueryHandler(handle_forget_callback, pattern=r"^forget_"))
    application.add_handler(CallbackQueryHandler(handle_lesson_callback, pattern=r"^lesson_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_continue_callback, pattern=r"^continue_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin_"))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))

    job_queue = application.job_queue
    job_queue.run_daily(daily_send_job, time=dtime(hour=DAILY_SEND_HOUR, minute=0, tzinfo=ALMATY_TZ))
    midday_hour = DAILY_SEND_HOUR + max(1, (DEADLINE_HOUR - DAILY_SEND_HOUR) // 2)
    job_queue.run_daily(midday_check_job, time=dtime(hour=min(midday_hour, 23), minute=0, tzinfo=ALMATY_TZ))
    job_queue.run_daily(reminder_job, time=dtime(hour=max(0, DEADLINE_HOUR - 2), minute=0, tzinfo=ALMATY_TZ))
    job_queue.run_daily(final_reminder_job, time=dtime(hour=max(0, DEADLINE_HOUR - 1), minute=30, tzinfo=ALMATY_TZ))
    job_queue.run_daily(deadline_advance_job, time=dtime(hour=DEADLINE_HOUR, minute=0, tzinfo=ALMATY_TZ))
    job_queue.run_daily(admin_digest_job, time=dtime(hour=ADMIN_DIGEST_HOUR, minute=30, tzinfo=ALMATY_TZ))
    return application


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN орнатылмаған! .env файлын тексер.")

    db.init_db()
    application = build_application(BOT_TOKEN)
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("TARTIP Bot V4 іске қосылды...")
    application.run_polling()


if __name__ == "__main__":
    main()
