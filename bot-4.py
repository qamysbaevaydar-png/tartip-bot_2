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

ASKING_CATEGORY, ASKING_NAME, ASKING_GOAL, ASKING_WHY = range(4)

with open("categories.json", "r", encoding="utf-8") as f:
    CATEGORIES = json.load(f)

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
        BotCommand("progress", "Менің прогресім"),
        BotCommand("lessons", "📚 Видео сабақтар каталогы"),
        BotCommand("support", "Көмек керек болса"),
    ]
    await application.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [
        BotCommand("whoami", "Өз telegram ID-іңді көру (диагностика)"),
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

    cat_name = CATEGORIES[category_key]["name"]
    await query.edit_message_text(f"Таңдадың: {cat_name} ✅")
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
        f"Дедлайн: кешкі {DEADLINE_HOUR}:00.\n\n"
        f"Бүгінгі 1-күн тапсырмаларын дереу көргің келсе — /today жаз."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Тіркелу тоқтатылды. Қайта бастау үшін /start жаз.")
    return ConversationHandler.END


def build_day_keyboard(telegram_id: int, day: int, category_key: str):
    cat = CATEGORIES[category_key]
    completed = db.get_completed_habits_today(telegram_id, day)
    keyboard = []

    for habit in cat["core_habits"]:
        done = habit["id"] in completed
        label = f"✅ {habit['text']}" if done else habit["text"]
        if habit["type"] == "button":
            keyboard.append([InlineKeyboardButton(label, callback_data=f"habit_{habit['id']}_{day}")])
        else:
            status_icon = "✅" if done else "📤"
            keyboard.append([InlineKeyboardButton(f"{status_icon} {habit['text']}", callback_data="noop")])

    week_num = get_week_number_str(day)
    focus = cat["weekly_focus"][week_num]
    focus_done = "week_focus" in completed
    focus_label = f"✅ {focus['text']}" if focus_done else f"🎯 {focus['text']}"
    keyboard.append([InlineKeyboardButton(focus_label, callback_data=f"habit_week_focus_{day}")])

    return InlineKeyboardMarkup(keyboard)


def build_day_text(user: dict, day: int) -> str:
    cat = CATEGORIES[user["category"]]
    week_title = get_week_title(day)
    week_num = get_week_number_str(day)
    focus = cat["weekly_focus"][week_num]

    lines = [f"📅 {day}/{TOTAL_DAYS}-күн · {week_title}", ""]

    if day in WEEK_START_DAYS:
        lines.append("🆕 *Жаңа апта басталды!*")
        lines.append(f"🎯 Аптаның фокусы: {focus['text']}")
        lines.append(f"💡 Неге: {focus['why']}")
        lines.append("")

    lines.append("*Бүгінгі негізгі әдеттер:*")
    for habit in cat["core_habits"]:
        lines.append(f"• {habit['text']}")
        lines.append(f"  💡 {habit['why']}")
    lines.append("")
    lines.append(f"🎯 *Апта фокусы:* {focus['text']}")
    lines.append(f"⏰ Дедлайн: {DEADLINE_HOUR}:00")

    return "\n".join(lines)


async def send_day_to_user(context: ContextTypes.DEFAULT_TYPE, user: dict):
    day = user["current_day"]
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


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await send_day_to_user(context, user)


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


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = len(cat["core_habits"]) + 1
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
        total_habits = len(cat["core_habits"]) + 1
        completed = len(db.get_completed_habits_today(user["telegram_id"], day))
        if completed < total_habits:
            try:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"⚠️ Дедлайнға 30 минут қалды! Әлі {total_habits - completed} тапсырма қалды. Асығып үлгер!"
                )
            except Exception as e:
                logger.error(f"final_reminder_job failed for {user['telegram_id']}: {e}")


async def deadline_advance_job(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        if not db.needs_advance_today(user):
            continue
        day = user["current_day"]
        if day > TOTAL_DAYS:
            continue
        cat = CATEGORIES[user["category"]]
        total_habits = len(cat["core_habits"]) + 1
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
        except Exception as e:
            logger.error(f"deadline_advance_job message failed for {user['telegram_id']}: {e}")

        db.advance_day(user["telegram_id"])


async def handle_habit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        await query.answer("Бұл тапсырманы фото/текст арқылы орында.", show_alert=True)
        return

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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    cat = CATEGORIES[user["category"]]
    photo_habit = next((h for h in cat["core_habits"] if h["type"] == "photo"), None)
    if not photo_habit:
        return

    file_id = update.message.photo[-1].file_id
    streak = db.record_habit_checkin(telegram_id, day, photo_habit["id"], proof_file_id=file_id)

    await update.message.reply_text(f"✅ Фото қабылданды! 🔥 {streak} күн тоқтаусыз.")

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin_id, file_id,
                caption=f"📸 {user['full_name']} (@{user['username']}) — {day}-күн, {photo_habit['text']}"
            )
        except Exception:
            pass


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    cat = CATEGORIES[user["category"]]
    text_habit = next((h for h in cat["core_habits"] if h["type"] == "text"), None)
    if not text_habit:
        return

    streak = db.record_habit_checkin(telegram_id, day, text_habit["id"], text_response=update.message.text)
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


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    cats = db.categorize_users()
    await update.message.reply_text(
        f"👑 *TARTIP ADMIN*\n\n"
        f"👥 Барлығы активті: {len(cats['green']) + len(cats['yellow']) + len(cats['red'])}\n\n"
        f"🟢 Қалыпты: {len(cats['green'])}\n🟡 Назар керек: {len(cats['yellow'])}\n🔴 Араласыңыз: {len(cats['red'])}\n\n"
        f"Толығырақ: /problems",
        parse_mode="Markdown",
    )


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
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("lessons", lessons_command))
    application.add_handler(CommandHandler("whoami", whoami_command))

    application.add_handler(CallbackQueryHandler(handle_habit_callback, pattern=r"^habit_"))
    application.add_handler(CallbackQueryHandler(handle_habit_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(handle_lesson_callback, pattern=r"^lesson_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_continue_callback, pattern=r"^continue_"))

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
    job_queue.run_daily(daily_send_job, time=dtime(hour=DAILY_SEND_HOUR, minute=0))
    reminder_hour = max(0, DEADLINE_HOUR - 2)
    job_queue.run_daily(reminder_job, time=dtime(hour=reminder_hour, minute=0))
    job_queue.run_daily(final_reminder_job, time=dtime(hour=max(0, DEADLINE_HOUR - 1), minute=30))
    job_queue.run_daily(deadline_advance_job, time=dtime(hour=DEADLINE_HOUR, minute=0))

    threading.Thread(target=run_health_server, daemon=True).start()

    logger.info("TARTIP Bot V3 іске қосылды...")
    application.run_polling()


if __name__ == "__main__":
    main()
