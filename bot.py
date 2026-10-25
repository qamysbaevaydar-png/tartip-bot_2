"""
TARTIP Course Bot V3 — категорияға негізделген, дедлайн жүйесімен, күнде
бірнеше тапсырмасы бар нұсқа.

V3 жаңалықтары:
- Онбординг: адам 6 категориядан біреуін таңдайды + мақсатын және себебін жазады
- Күнде 3 тұрақты негізгі әдет + аптаның фокус тапсырмасы (барлығы жеке белгіленеді)
- Әр тапсырмада "неге" түсіндірмесі
- Дедлайн жүйесі: еске салу (2 сағат/30 минут қалғанда) + автоматты күн ауысу
- /whoami — админ диагностикасы үшін (өз telegram ID-іңді көру)
- Бұрынғы: Access system, бөлек меню, /lessons, /admin, /pause/resume/reset
"""
import json
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
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)
from telegram.error import TelegramError

import database as db

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
TOTAL_DAYS = 28
ALMATY_TZ = ZoneInfo("Asia/Almaty")

ASKING_CATEGORY, ASKING_NAME, ASKING_GOAL, ASKING_WHY = range(4)

with open("categories.json", "r", encoding="utf-8") as f:
    CATEGORIES = json.load(f)

# "Салауатты өмір салты" жеке курс емес: оның пайдалы қағидалары басқа
# бағдарламаларға енгізілді. Негізгі таңдауда тек бес нақты бағыт қалады.
CATEGORIES.pop("salauatty_omir", None)

with open("programs.json", "r", encoding="utf-8") as f:
    PROGRAMS = json.load(f)

with open("lessons.json", "r", encoding="utf-8") as f:
    LESSONS = json.load(f)

WEEK_TITLES = {
    range(1, 8): "1-апта: Диагностика + бастау",
    range(8, 15): "2-апта: Белсендіру",
    range(15, 22): "3-апта: Тұрақтылық",
    range(22, 29): "4-апта: Бекіту",
}
WEEK_START_DAYS = {1: "1", 8: "2", 15: "3", 22: "4"}


def get_week_title(day: int) -> str:
    for r, title in WEEK_TITLES.items():
        if day in r:
            return title
    return ""


def get_week_number_str(day: int) -> str:
    if day <= 7:
        return "1"
    if day <= 14:
        return "2"
    if day <= 21:
        return "3"
    return "4"


def get_daily_plan(category_key: str, day: int) -> dict:
    """28 күндік бағдарламадан дәл сол күннің нақты миссиясын қайтарады."""
    week_index = min(max((day - 1) // 7, 0), 3)
    day_index = (day - 1) % 7
    return PROGRAMS[category_key]["weeks"][week_index]["days"][day_index]


def get_week_theme(category_key: str, day: int) -> str:
    week_index = min(max((day - 1) // 7, 0), 3)
    return PROGRAMS[category_key]["weeks"][week_index]["theme"]


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


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
        BotCommand("today", "Бүгінгі тапсырмаларды көру"),
        BotCommand("roadmap", "🗺 Толық 28 күндік жолыңды көру"),
        BotCommand("progress", "Менің прогресім"),
        BotCommand("lessons", "📚 Видео сабақтар каталогы"),
        BotCommand("support", "Көмек керек болса"),
        BotCommand("forget", "Деректерімді толық өшіру (қайта бастау)"),
    ]
    await application.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [
        BotCommand("whoami", "Өз telegram ID-іңді көру (диагностика)"),
        BotCommand("wipe", "Клиентті толық ұмыту (/wipe id)"),
        BotCommand("admin", "👑 Админ панель"),
        BotCommand("stats", "Жалпы статистика"),
        BotCommand("problems", "Назар керек клиенттер"),
        BotCommand("user", "Клиент профилі (/user id)"),
        BotCommand("pause", "Клиентті тоқтату (/pause id)"),
        BotCommand("resume", "Клиентті қайта қосу (/resume id)"),
        BotCommand("reset", "Прогресті нөлдеу (/reset id)"),
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
        f"Сенің Telegram ID-ің: `{uid}`\n"
        f"Админ құқығың бар ма: {admin_status}\n\n"
        f"Егер админ болу керек болса, осы ID-ді Render-дегі ADMIN_IDS айнымалысына қос.",
        parse_mode="Markdown",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if user:
        cat = CATEGORIES.get(user["category"], {})
        await update.message.reply_text(
            f"Сәлем, {user['full_name']}! 👋\n\n"
            f"📅 {user['current_day']}/{TOTAL_DAYS}-күн · {get_week_title(user['current_day'])}\n"
            f"🎯 Бағыт: {cat.get('name', '?')}\n"
            f"🔥 {user['streak']} күн тоқтаусыз"
        )
        return ConversationHandler.END

    has_access = await check_channel_membership(context, telegram_id)
    if not has_access:
        await update.message.reply_text(
            "🔒 Бұл бот тек TARTIP курсының қатысушыларына арналған.\n\n"
            "Access алу үшін админге хабарласыңыз: /support"
        )
        return ConversationHandler.END

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
    context.user_data["category"] = category_key

    cat = CATEGORIES[category_key]
    await query.edit_message_text(f"Таңдадың: {cat['name']} ✅")

    pdf_link = cat.get("pdf_link")
    if pdf_link:
        await query.message.reply_text(
            f"📄 Алдымен толық гайдыңды оқып шық — онда осы бағыттың негізгі білімі, "
            f"чек-листі және жиі қателіктер жазылған:\n\n{pdf_link}"
        )

    await query.message.reply_text("Енді танысайық — атыңды жаз:")
    return ASKING_NAME


async def ask_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["full_name"] = update.message.text
    await update.message.reply_text(
        f"Танысқаныма қуаныштымын, {update.message.text}! 🙌\n\n"
        "Нақты мақсатыңды жаз (мыс. '5 кг арықтау' немесе '10 кг бұлшық ет қосу'):"
    )
    return ASKING_GOAL


async def ask_why(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["goal"] = update.message.text
    await update.message.reply_text("Неге дәл осы мақсатқа жеткің келеді? (қысқаша жаз)")
    return ASKING_WHY


def build_roadmap_text(category_key: str) -> str:
    """Бүкіл 28 күндік жолды бір хабарламада көрсетеді — адам не үшін не істейтінін алдын ала біледі."""
    cat = CATEGORIES[category_key]
    lines = [
        f"🗺 *Сенің 28 күндік жолың — {cat['name']}*",
        "",
        "Күн сайын осы 3 негізгі әдетті орындайсың (бірінші 2 күнде біртіндеп үйренесің, 3-күннен бастап толық):",
        "",
    ]
    for habit in cat["core_habits"]:
        lines.append(f"• {habit['text']}")
    lines.append("")
    lines.append("Ал әр апта өз фокусымен ерекшеленеді:")
    lines.append("")

    week_titles = {"1": "1-апта: Диагностика + бастау", "2": "2-апта: Белсендіру",
                   "3": "3-апта: Тұрақтылық", "4": "4-апта: Бекіту"}
    for week_num in ["1", "2", "3", "4"]:
        focus = cat["weekly_focus"][week_num]
        lines.append(f"*{week_titles[week_num]}*")
        lines.append(f"🎯 {focus['text']}")
        lines.append(f"💡 {focus['why']}")
        lines.append("")

    lines.append("28-күннің соңында сенде — мақсатыңа жетудің дайын фундаменті болады. "
                  "Әр күн осы жолдың бір қадамы, кездейсоқ емес.")
    return "\n".join(lines)


async def roadmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await update.message.reply_text(build_roadmap_text(user["category"]), parse_mode="Markdown")


async def finish_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goal_why = update.message.text
    tg_user = update.effective_user
    category = context.user_data["category"]
    full_name = context.user_data["full_name"]
    goal = context.user_data["goal"]

    db.add_user(tg_user.id, tg_user.username or "", full_name, category, goal, goal_why)

    cat_name = CATEGORIES[category]["name"]
    await update.message.reply_text(
        f"Керемет! Мақсатыңды жаздым:\n\n"
        f"🎯 Бағыт: {cat_name}\n"
        f"📝 Мақсат: {goal}\n"
        f"💭 Себебі: {goal_why}\n\n"
        f"Ертеңнен бастап күн сайын таңғы {DAILY_SEND_HOUR}:00-де саған тапсырмалар келеді. "
        f"Дедлайн: кешкі {DEADLINE_HOUR}:00."
    )
    await update.message.reply_text(build_roadmap_text(category), parse_mode="Markdown")
    await update.message.reply_text("Бүгінгі 1-күн тапсырмасын дереу көргің келсе — /today жаз.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Тіркелу тоқтатылды. Қайта бастау үшін /start жаз.")
    return ConversationHandler.END


def get_unlocked_habit_count(day: int, total_habits: int) -> int:
    """Алғашқы күндерде әдеттерді біртіндеп ашады — бірден толық жүктемей.
    1-күн: 1 әдет, 2-күн: 2 әдет, 3-күннен бастап: барлығы + апта фокусы."""
    return min(day, total_habits)


def build_day_keyboard(telegram_id: int, day: int, category_key: str):
    completed = db.get_completed_habits_today(telegram_id, day)
    plan = get_daily_plan(category_key, day)
    done = "daily_plan" in completed
    if plan["proof"] == "button":
        primary = InlineKeyboardButton(
            "✅ Басты миссия орындалды" if done else "✅ Орындадым деп белгіле",
            callback_data=f"habit_daily_plan_{day}",
        )
    else:
        action = "Фото жібер" if plan["proof"] == "photo" else "Қысқа жауап жаз"
        primary = InlineKeyboardButton(
            "✅ Есеп қабылданды" if done else f"📤 {action}",
            callback_data="noop",
        )
    return InlineKeyboardMarkup([
        [primary],
        [InlineKeyboardButton("🛟 Жоспар бұзылса не істеймін?", callback_data=f"planhelp_{day}")],
    ])


def build_day_text(user: dict, day: int) -> str:
    plan = get_daily_plan(user["category"], day)
    proof = "фото жібер" if plan["proof"] == "photo" else ("қысқа жауап жаз" if plan["proof"] == "text" else "төмендегі батырмамен белгіле")
    lines = [
        f"📅 *{day}/{TOTAL_DAYS}-күн* · {get_week_theme(user['category'], day)}",
        f"\n🎯 *{plan['title']}*",
        plan["mission"],
        "\n*Нақты жоспар:*",
    ]
    lines.extend(f"• {item}" for item in plan["schedule"])
    lines.extend([
        f"\n📌 *Есеп:* {proof}.",
        f"🛟 *Қиын болса:* {plan['fallback']}",
        f"\n⏰ Күнді {DEADLINE_HOUR}:00-ге дейін жап.",
    ])
    return "\n".join(lines)


async def send_day_to_user(context: ContextTypes.DEFAULT_TYPE, user: dict):
    day = user["current_day"]
    if user["category"] not in PROGRAMS:
        await context.bot.send_message(
            user["telegram_id"],
            "📌 TARTIP бағдарламалары жаңартылды. Сенің бұрынғы бағытың енді "
            "негізгі курсқа кірмейді. Жаңа бағыт таңдау үшін админге жаз - ол "
            "профильді қауіпсіз түрде ауыстырып береді.",
        )
        return
    if day > TOTAL_DAYS:
        await context.bot.send_message(
            user["telegram_id"],
            "🎉 Құттықтаймын! Сен 28 күндік TARTIP курсын толық аяқтадың!\n\n"
            "Бұл — үлкен жетістік. Енді сенде мақсатыңа жетудің фундаменті бар. "
            "Жалғастырғың келе ме?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Иә, жалғастырам", callback_data="continue_yes")],
                [InlineKeyboardButton("🛑 Жоқ, тоқтатам", callback_data="continue_no")],
            ])
        )
        return

    text = build_day_text(user, day)
    keyboard = build_day_keyboard(user["telegram_id"], day, user["category"])
    await context.bot.send_message(user["telegram_id"], text, parse_mode="Markdown", reply_markup=keyboard)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиент өзін толық ұмыттыра алады — қайта /start арқылы бастан бастауға мүмкіндік береді."""
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        await update.message.reply_text("Сен әлі тіркелмегенсің, ұмытатын дерек жоқ. /start жаз.")
        return
    db.delete_user(telegram_id)
    await update.message.reply_text(
        "🗑 Барлық деректерің өшірілді. /start арқылы қайтадан бастай аласың."
    )


async def wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: белгілі бір клиентті толық ұмыттыру (reset-тен айырмашылығы — профиль де өшеді)."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /wipe <telegram_id>")
        return
    target_id = int(context.args[0])
    db.delete_user(target_id)
    await update.message.reply_text(f"🗑 {target_id} толық ұмытылды (профиль де өшті).")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await send_day_to_user(context, user)


def get_total_tasks_for_day(day: int, cat: dict) -> int:
    """Жаңа жүйеде бір негізгі күндік миссия бар; ол фото, мәтін не батырмамен жабылады."""
    return 1


async def daily_send_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        has_access = await check_channel_membership(context, user["telegram_id"])
        if not has_access:
            continue
        try:
            await send_day_to_user(context, user)
        except Exception as e:
            logger.error(f"daily_send_job failed for {user['telegram_id']}: {e}")


async def midday_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Түс кезінде жұмсақ еске салу — тек орындалмаған тапсырмасы барларға ғана жіберіледі."""
    users = db.get_all_active_users()
    for user in users:
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = get_total_tasks_for_day(day, cat)
        completed = len(db.get_completed_habits_today(user["telegram_id"], day))
        if completed < total_habits:
            try:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"👋 Түс болды! Бүгінгі тапсырмалардың {completed}/{total_habits} орындалды. "
                    f"Уақыт бар кезде орында, дедлайн кешке {DEADLINE_HOUR}:00."
                )
            except Exception as e:
                logger.error(f"midday_check_job failed for {user['telegram_id']}: {e}")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = get_total_tasks_for_day(day, cat)
        completed = len(db.get_completed_habits_today(user["telegram_id"], day))
        if completed < total_habits:
            try:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"⏰ Дедлайнға 2 сағат қалды! Бүгінгі тапсырмалардың {completed}/{total_habits} орындалды. "
                    f"/today арқылы қалғанын көр."
                )
            except Exception as e:
                logger.error(f"reminder_job failed for {user['telegram_id']}: {e}")


async def final_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = get_total_tasks_for_day(day, cat)
        completed = len(db.get_completed_habits_today(user["telegram_id"], day))
        if completed < total_habits:
            try:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"⚠️ Дедлайнға 30 минут қалды! Әлі {total_habits - completed} тапсырма қалды. Асығып үлгер!"
                )
            except Exception as e:
                logger.error(f"final_reminder_job failed for {user['telegram_id']}: {e}")


WEEKLY_REVIEW_DAYS = {7, 14, 21, 28}


def build_weekly_review_text(day: int) -> str:
    week_num = {7: "1", 14: "2", 21: "3", 28: "4"}[day]
    return (
        f"📊 *{week_num}-АПТА REVIEW*\n\n"
        f"Бұл апта аяқталды! Қысқаша өзіңе есеп бер:\n\n"
        f"• Бұл аптада не жақсы шықты?\n"
        f"• Не қиын болды?\n"
        f"• Қай әдет тұрақты болды?\n"
        f"• Келесі аптада нені жақсартамын?\n\n"
        f"Жауабыңды осы хабарламаға жауап ретінде жаз — өзің үшін де, "
        f"прогресіңді түсіну үшін де пайдалы."
    )


async def deadline_advance_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        if not db.needs_advance_today(user):
            continue
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = get_total_tasks_for_day(day, cat)
        completed = len(db.get_completed_habits_today(user["telegram_id"], day))

        try:
            if completed >= total_habits:
                await context.bot.send_message(
                    user["telegram_id"], f"🎉 {day}-күн толық орындалды! Ертең жаңа тапсырмалар келеді."
                )
            else:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"❌ Бүгінгі күн толық аяқталмады ({completed}/{total_habits}). "
                    f"Ештеңе етпейді, ертең жалғастырамыз — курс тоқтамайды!"
                )

            if day in WEEKLY_REVIEW_DAYS:
                await context.bot.send_message(
                    user["telegram_id"], build_weekly_review_text(day), parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"deadline_advance_job message failed for {user['telegram_id']}: {e}")

        db.advance_day(user["telegram_id"])


async def handle_habit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "noop":
        await query.answer("Бұл тапсырманы фото/текст арқылы орында.", show_alert=True)
        return

    await query.answer()
    parts = query.data.split("_")
    day = int(parts[-1])
    habit_id = "_".join(parts[1:-1])
    telegram_id = update.effective_user.id

    user = db.get_user(telegram_id)
    if not user or user["current_day"] != day:
        await query.answer("Бұл тапсырма ескірген.", show_alert=True)
        return

    db.record_habit_checkin(telegram_id, day, habit_id)

    new_keyboard = build_day_keyboard(telegram_id, day, user["category"])
    try:
        await query.edit_message_reply_markup(reply_markup=new_keyboard)
    except Exception:
        pass
    await query.answer("✅ Белгіленді!")


async def handle_plan_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return
    day = int(query.data.split("_")[1])
    if user["current_day"] != day:
        await query.answer("Бұл күннің жоспары ескірген.", show_alert=True)
        return
    plan = get_daily_plan(user["category"], day)
    await query.answer()
    await query.message.reply_text(
        f"🛟 *Жоспар B — {plan['title']}*\n\n{plan['fallback']}\n\n"
        "Бүгін 100% мінсіз болу міндет емес. Ең кіші нұсқасын орында да, ертең қайта жалғастыр.",
        parse_mode="Markdown",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    plan = get_daily_plan(user["category"], day)
    if plan["proof"] != "photo":
        return

    file_id = update.message.photo[-1].file_id
    streak = db.record_habit_checkin(telegram_id, day, "daily_plan", proof_file_id=file_id)

    await update.message.reply_text(f"✅ Фото қабылданды! 🔥 {streak} күн тоқтаусыз.")

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin_id, file_id,
                caption=f"📸 {user['full_name']} (@{user['username']}) — {day}-күн, {plan['title']}"
            )
        except Exception:
            pass


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    plan = get_daily_plan(user["category"], day)
    if plan["proof"] != "text":
        return

    streak = db.record_habit_checkin(telegram_id, day, "daily_plan", text_response=update.message.text)
    await update.message.reply_text(f"✅ Жауабың қабылданды! 🔥 {streak} күн тоқтаусыз.")


async def handle_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == "continue_yes":
        db.reset_progress(telegram_id)
        await query.edit_message_text("Керемет! 1-күннен қайта бастаймыз 💪 /today арқылы көре аласың.")
    else:
        db.set_paused(telegram_id, True)
        await query.edit_message_text(
            "Түсінікті! Курсты аяқтадың. Кез келген уақытта /start арқылы қайта бастай аласың. Сау бол! 👋"
        )


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return

    filled = int((user["current_day"] / TOTAL_DAYS) * 12)
    bar = "█" * filled + "░" * (12 - filled)
    percent = round((user["current_day"] / TOTAL_DAYS) * 100)
    cat_name = CATEGORIES[user["category"]]["name"]

    await update.message.reply_text(
        f"📊 *Сенің прогресің*\n\n{bar} {percent}%\n\n"
        f"📅 Күн: {user['current_day']}/{TOTAL_DAYS}\n🎯 Бағыт: {cat_name}\n"
        f"🔥 {user['streak']} күн тоқтаусыз\n📝 Мақсатың: {user['goal']}",
        parse_mode="Markdown",
    )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text("Сұрағың болса, осы хабарламаға жауап ретінде жаз — админге тікелей жіберемін. 📩")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 Қолдау сұранысы: {user.full_name} (@{user.username}, id: {user.id})\n"
                f"/user {user.id} арқылы қарай аласың."
            )
        except Exception:
            pass


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
        "📚 *Видео сабақтар каталогы*\n\nНөмірді бас:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_lesson_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    num = query.data.split("_")[1]
    lesson = LESSONS.get(num)
    if lesson:
        await query.message.reply_text(f"{lesson['title']}\n\n▶️ {lesson['link']}")


ADMIN_PAGE_SIZE = 6


def get_admin_user_snapshot(user: dict) -> dict:
    """Админге сандар емес, клиентпен не істеу керегін көрсететін қысқа күй."""
    day = user["current_day"]
    if user["paused"]:
        return {"label": "⏸ Паузада", "priority": "paused", "completed": 0, "total": 0}
    if day > TOTAL_DAYS:
        return {"label": "🏁 Курс аяқталды", "priority": "done", "completed": 0, "total": 0}

    cat = CATEGORIES.get(user["category"], {})
    total = get_total_tasks_for_day(day, cat) if cat else 0
    completed = len(db.get_completed_habits_today(user["telegram_id"], day))
    if not user["last_checkin_date"] and day <= 1:
        label, priority = "🆕 Жаңа қатысушы", "new"
    elif completed == 0:
        label, priority = "🔴 Бүгін байланыс керек", "red"
    elif completed < total:
        label, priority = "🟡 Бүгін аяқтамаған", "yellow"
    else:
        label, priority = "🟢 Бүгін орындаған", "green"
    return {"label": label, "priority": priority, "completed": completed, "total": total}


def get_admin_users(filter_name: str = "all") -> list[dict]:
    users = db.get_all_users()
    if filter_name == "attention":
        return [u for u in users if get_admin_user_snapshot(u)["priority"] in {"red", "yellow"}]
    if filter_name == "new":
        return [u for u in users if get_admin_user_snapshot(u)["priority"] == "new"]
    return users


def build_admin_dashboard() -> tuple[str, InlineKeyboardMarkup]:
    users = db.get_all_users()
    snapshots = [get_admin_user_snapshot(u) for u in users]
    attention = sum(s["priority"] in {"red", "yellow"} for s in snapshots)
    new_users = sum(s["priority"] == "new" for s in snapshots)
    completed_today = sum(s["priority"] == "green" for s in snapshots)
    text = (
        "👑 *TARTIP ADMIN — бүгінгі басқару*\n\n"
        f"🔴🟡 Назар керек: *{attention}*\n"
        f"🆕 Жаңа қатысушы: *{new_users}*\n"
        f"🟢 Бүгін жоспарды орындаған: *{completed_today}*\n"
        f"👥 Барлық клиент: *{len(users)}*\n\n"
        "Төменнен адамды аш: әр карточкада бүгінгі орындалуы, мақсаты және бірден әрекет ету батырмалары бар."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔴🟡 Назар керек ({attention})", callback_data="admin_list_attention_0")],
        [InlineKeyboardButton(f"🆕 Жаңа қатысушылар ({new_users})", callback_data="admin_list_new_0")],
        [InlineKeyboardButton("👥 Барлық клиенттер каталогы", callback_data="admin_list_all_0")],
        [InlineKeyboardButton("📊 Қысқа статистика", callback_data="admin_stats")],
    ])
    return text, keyboard


def build_admin_list(filter_name: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    users = get_admin_users(filter_name)
    max_page = max(0, (len(users) - 1) // ADMIN_PAGE_SIZE)
    page = max(0, min(page, max_page))
    shown = users[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
    titles = {"all": "👥 Барлық клиенттер", "attention": "🔴🟡 Назар керек", "new": "🆕 Жаңа қатысушылар"}
    text = f"{titles[filter_name]}\n\nАдамды басып, толық картасын аш. {len(users)} адам · {page + 1}/{max_page + 1} бет"
    keyboard = []
    for user in shown:
        snapshot = get_admin_user_snapshot(user)
        name = user["full_name"] or "Аты жазылмаған"
        label = f"{snapshot['label']} · {name} · {user['current_day']}/{TOTAL_DAYS}"
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


def build_admin_user_card(user: dict, filter_name: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    snapshot = get_admin_user_snapshot(user)
    category_name = CATEGORIES.get(user["category"], {}).get("name", "Белгісіз бағыт")
    username = f"@{user['username']}" if user.get("username") else "username жоқ"
    progress = "Курс аяқталды" if user["current_day"] > TOTAL_DAYS else f"{snapshot['completed']}/{snapshot['total']} тапсырма"
    last_checkin = user["last_checkin_date"] or "Әлі check-in жоқ"
    text = (
        f"👤 {user['full_name']} ({username})\n\n"
        f"Күйі: {snapshot['label']}\n"
        f"Бағыты: {category_name}\n"
        f"Мақсаты: {user['goal']}\n"
        f"Неге келді: {user.get('goal_why') or 'жазылмаған'}\n\n"
        f"📅 Күн: {user['current_day']}/{TOTAL_DAYS}\n"
        f"✅ Бүгін: {progress}\n"
        f"🔥 Қатарынан: {user['streak']} күн\n"
        f"🕓 Соңғы белгі: {last_checkin}\n"
        f"ID: {user['telegram_id']}"
    )
    action = "▶️ Қайта қосу" if user["paused"] else "⏸ Паузаға қою"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(action, callback_data=f"admin_toggle_{user['telegram_id']}_{filter_name}_{page}")],
        [InlineKeyboardButton("🔄 Прогресті 1-күнге қайтару", callback_data=f"admin_reset_{user['telegram_id']}_{filter_name}_{page}")],
        [InlineKeyboardButton("◀️ Каталогқа оралу", callback_data=f"admin_list_{filter_name}_{page}")],
        [InlineKeyboardButton("🏠 Админ мәзірі", callback_data="admin_home")],
    ])
    return text, keyboard


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, keyboard = build_admin_dashboard()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админге командаларды есте сақтамай, каталог арқылы басқаруға мүмкіндік береді."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Бұл бөлім тек админге арналған.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")
    action = parts[1]

    if action == "home":
        text, keyboard = build_admin_dashboard()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return
    if action == "stats":
        s = db.get_stats()
        await query.edit_message_text(
            f"📊 TARTIP қысқа статистика\n\nБарлығы: {s['total']}\nАктивті: {s['active']}\n"
            f"Паузада: {s['paused']}\nОрташа қатарынан: {s['avg_streak']} күн",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Админ мәзірі", callback_data="admin_home")]]),
        )
        return
    if action == "list":
        filter_name, page = parts[2], int(parts[3])
        text, keyboard = build_admin_list(filter_name, page)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    telegram_id, filter_name, page = int(parts[2]), parts[3], int(parts[4])
    user = db.get_user(telegram_id)
    if not user:
        await query.edit_message_text("Клиент табылмады. Ол өшірілген болуы мүмкін.")
        return
    if action == "toggle":
        db.set_paused(telegram_id, not bool(user["paused"]))
        user = db.get_user(telegram_id)
    elif action == "reset":
        await query.edit_message_text(
            f"⚠️ {user['full_name']} клиентінің прогресін 1-күнге қайтару керек пе?\n"
            "Барлық бұрынғы орындалған тапсырмалар өшеді.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Иә, прогресті нөлдеу", callback_data=f"admin_confirmreset_{telegram_id}_{filter_name}_{page}")],
                [InlineKeyboardButton("Жоқ, клиент картасына оралу", callback_data=f"admin_user_{telegram_id}_{filter_name}_{page}")],
            ]),
        )
        return
    elif action == "confirmreset":
        db.reset_progress(telegram_id)
        user = db.get_user(telegram_id)
    text, keyboard = build_admin_user_card(user, filter_name, page)
    await query.edit_message_text(text, reply_markup=keyboard)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    s = db.get_stats()
    await update.message.reply_text(
        f"📊 *Статистика*\n\nБарлығы: {s['total']}\nАктивті: {s['active']}\n"
        f"Тоқтатылған: {s['paused']}\nОрташа streak: {s['avg_streak']}",
        parse_mode="Markdown",
    )


async def problems_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    cats = db.categorize_users()
    if not cats["red"] and not cats["yellow"]:
        await update.message.reply_text("Проблемалы клиент жоқ 👍")
        return
    lines = []
    if cats["red"]:
        lines.append("🔴 *Араласу керек:*")
        for u in cats["red"]:
            lines.append(f"— {u['full_name']} (@{u['username']}) — {u['current_day']}-күн, id: {u['telegram_id']}")
    if cats["yellow"]:
        lines.append("\n🟡 *Назар керек:*")
        for u in cats["yellow"]:
            lines.append(f"— {u['full_name']} (@{u['username']}) — {u['current_day']}-күн, id: {u['telegram_id']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /user <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("telegram_id сан болу керек.")
        return

    user = db.get_user(target_id)
    if not user:
        await update.message.reply_text("Клиент табылмады.")
        return

    cat_name = CATEGORIES.get(user["category"], {}).get("name", "?")
    status = "Тоқтатылған" if user["paused"] else ("Активті" if user["is_active"] else "Белсенді емес")
    await update.message.reply_text(
        f"👤 *{user['full_name']}* (@{user['username']})\n\n"
        f"Бағыт: {cat_name}\nМақсаты: {user['goal']}\nСебебі: {user.get('goal_why', '-')}\n"
        f"Ағымдағы күн: {user['current_day']}/{TOTAL_DAYS}\n🔥 {user['streak']} күн тоқтаусыз\n"
        f"Соңғы check-in: {user['last_checkin_date']}\nСтатус: {status}",
        parse_mode="Markdown",
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /pause <telegram_id>")
        return
    db.set_paused(int(context.args[0]), True)
    await update.message.reply_text(f"⏸ {context.args[0]} тоқтатылды.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /resume <telegram_id>")
        return
    db.set_paused(int(context.args[0]), False)
    await update.message.reply_text(f"▶️ {context.args[0]} қайта қосылды.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /reset <telegram_id>")
        return
    db.reset_progress(int(context.args[0]))
    await update.message.reply_text(f"🔄 {context.args[0]} прогресі 1-күнге нөлделді.")


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


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN орнатылмаған! .env файлын тексер.")

    db.init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASKING_CATEGORY: [CallbackQueryHandler(category_chosen, pattern=r"^cat_")],
            ASKING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_goal)],
            ASKING_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_why)],
            ASKING_WHY: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_registration)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("roadmap", roadmap_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("lessons", lessons_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("forget", forget_command))
    application.add_handler(CommandHandler("wipe", wipe_command))

    application.add_handler(CallbackQueryHandler(handle_habit_callback, pattern=r"^habit_"))
    application.add_handler(CallbackQueryHandler(handle_habit_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(handle_plan_help_callback, pattern=r"^planhelp_"))
    application.add_handler(CallbackQueryHandler(handle_lesson_callback, pattern=r"^lesson_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_continue_callback, pattern=r"^continue_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin_"))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("problems", problems_command))
    application.add_handler(CommandHandler("user", user_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_reply))

    job_queue = application.job_queue
    job_queue.run_daily(daily_send_job, time=dtime(hour=DAILY_SEND_HOUR, minute=0, tzinfo=ALMATY_TZ))
    midday_hour = DAILY_SEND_HOUR + max(1, (DEADLINE_HOUR - DAILY_SEND_HOUR) // 2)
    job_queue.run_daily(midday_check_job, time=dtime(hour=min(midday_hour, 23), minute=0, tzinfo=ALMATY_TZ))
    reminder_hour = max(0, DEADLINE_HOUR - 2)
    job_queue.run_daily(reminder_job, time=dtime(hour=reminder_hour, minute=0, tzinfo=ALMATY_TZ))
    job_queue.run_daily(final_reminder_job, time=dtime(hour=max(0, DEADLINE_HOUR - 1), minute=30, tzinfo=ALMATY_TZ))
    job_queue.run_daily(deadline_advance_job, time=dtime(hour=DEADLINE_HOUR, minute=0, tzinfo=ALMATY_TZ))

    threading.Thread(target=run_health_server, daemon=True).start()

    logger.info("TARTIP Bot V3 іске қосылды...")
    application.run_polling()


if __name__ == "__main__":
    main()
