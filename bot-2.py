"""
TARTIP Course Bot V2 — 28 күндік курс үшін Telegram бот.

V2 жаңалықтары:
- Access system: жабық каналда мүше екенін автоматты тексереді
- Бөлек меню: клиентке және админге басқа командалар тізімі көрінеді
- /lessons — 26 видео сабақтың каталогы (еркін таңдап көруге болады)
- /progress, /support — клиентке пайдалы командалар
- /admin — 🟢🟡🔴 клиенттерді жіктеп көрсететін панель
- /pause, /resume, /reset — админ клиентті басқару командалары
- Streak сөзі жеңілдетілді: "X күн тоқтаусыз"
- XP/Level жоқ

Іске қосу алдында .env файлын толтыр (README.md қара).
"""
import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

# ---------- Конфигурация (.env файлынан оқылады) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
DAILY_SEND_HOUR = int(os.environ.get("DAILY_SEND_HOUR", "8"))
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # мысалы: -1003762687569 (жабық каналдың ID-і)

ASKING_NAME, ASKING_GOAL = range(2)

with open("tasks.json", "r", encoding="utf-8") as f:
    TASKS = json.load(f)

with open("lessons.json", "r", encoding="utf-8") as f:
    LESSONS = json.load(f)

TOTAL_DAYS = len(TASKS)

WEEK_TITLES = {
    range(1, 8): "1-апта: Білім + Бастау",
    range(8, 15): "2-апта: Денені ояту",
    range(15, 22): "3-апта: Тұрақтылық",
    range(22, 29): "4-апта: Дальше больше",
}


def get_week_title(day: int) -> str:
    for r, title in WEEK_TITLES.items():
        if day in r:
            return title
    return ""


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


# ---------- Access system: каналда мүшелікті тексеру ----------
async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> bool:
    """Клиент жабық каналда мүше ме, соны тексереді. CHANNEL_ID орнатылмаса, тексеру өткізіп жіберіледі."""
    if not CHANNEL_ID:
        return True  # Access system әлі орнатылмаған — бәріне ашық
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as e:
        logger.error(f"Channel membership check failed for {telegram_id}: {e}")
        return False


# ---------- Меню орнату (клиентке және админге бөлек) ----------
async def setup_menus(application: Application):
    default_commands = [
        BotCommand("start", "Тіркелу / қайта кіру"),
        BotCommand("today", "Бүгінгі тапсырманы көру"),
        BotCommand("progress", "Менің прогресім"),
        BotCommand("lessons", "📚 Видео сабақтар каталогы"),
        BotCommand("support", "Көмек керек болса"),
    ]
    await application.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [
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
            await application.bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except TelegramError as e:
            logger.error(f"Failed to set admin menu for {admin_id}: {e}")


# ---------- Тіркелу ағыны ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id

    user = db.get_user(telegram_id)
    if user:
        week = get_week_title(user["current_day"])
        await update.message.reply_text(
            f"Сәлем, {user['full_name']}! 👋\n\n"
            f"📅 {user['current_day']}/{TOTAL_DAYS}-күн · {week}\n"
            f"🔥 {user['streak']} күн тоқтаусыз"
        )
        return ConversationHandler.END

    # Access тексеру
    has_access = await check_channel_membership(context, telegram_id)
    if not has_access:
        await update.message.reply_text(
            "🔒 Бұл бот тек TARTIP курсының қатысушыларына арналған.\n\n"
            "Access алу үшін админге хабарласыңыз: /support"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Сәлем! TARTIP 28 күндік курсына қош келдің 💪\n\n"
        "Алдымен танысайық — атыңды жаз:"
    )
    return ASKING_NAME


async def ask_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["full_name"] = update.message.text
    await update.message.reply_text(
        f"Танысқаныма қуаныштымын, {update.message.text}! 🙌\n\n"
        "Енді айт: осы курстан не алғың келеді?"
    )
    return ASKING_GOAL


async def finish_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goal = update.message.text
    full_name = context.user_data["full_name"]
    tg_user = update.effective_user

    db.add_user(tg_user.id, tg_user.username or "", full_name, goal)

    await update.message.reply_text(
        f"Керемет! Мақсатыңды жаздым: \"{goal}\"\n\n"
        f"Ертеңнен бастап күн сайын таңғы {DAILY_SEND_HOUR}:00-де саған тапсырма келіп тұрады. "
        f"Дайын бол! 🚀\n\n"
        f"Бүгінгі 1-күн тапсырмасын дереу көргің келсе — /today жаз."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Тіркелу тоқтатылды. Қайта бастау үшін /start жаз.")
    return ConversationHandler.END


# ---------- Тапсырма жіберу ----------
def build_task_keyboard(day: int, task_type: str):
    if task_type == "button_confirm":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Орындадым", callback_data=f"confirm_{day}")]
        ])
    return None


async def send_task_to_user(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, day: int):
    task = TASKS.get(str(day))
    if not task:
        await context.bot.send_message(
            telegram_id,
            "🎉 Құттықтаймын! Сен 28 күндік TARTIP курсын толық аяқтадың!\n\n"
            "Курсты жалғастырғың келе ме?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Иә, жалғастырам", callback_data="continue_yes")],
                [InlineKeyboardButton("🛑 Жоқ, тоқтатам", callback_data="continue_no")],
            ])
        )
        return

    week = get_week_title(day)
    keyboard = build_task_keyboard(day, task["type"])
    text = f"📅 {day}/{TOTAL_DAYS}-күн · {week}\n\n*{task['title']}*\n\n{task['text']}"
    await context.bot.send_message(
        telegram_id, text, parse_mode="Markdown", reply_markup=keyboard
    )


async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_active_users()
    for user in users:
        # Күн сайын жіберу алдында access-ты қайта тексереміз
        has_access = await check_channel_membership(context, user["telegram_id"])
        if not has_access:
            continue
        try:
            await send_task_to_user(context, user["telegram_id"], user["current_day"])
        except Exception as e:
            logger.error(f"Failed to send task to {user['telegram_id']}: {e}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return
    await send_task_to_user(context, user["telegram_id"], user["current_day"])


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Алдымен /start арқылы тіркел.")
        return

    filled = int((user["current_day"] / TOTAL_DAYS) * 12)
    bar = "█" * filled + "░" * (12 - filled)
    percent = round((user["current_day"] / TOTAL_DAYS) * 100)

    await update.message.reply_text(
        f"📊 *Сенің прогресің*\n\n"
        f"{bar} {percent}%\n\n"
        f"📅 Күн: {user['current_day']}/{TOTAL_DAYS}\n"
        f"🔥 {user['streak']} күн тоқтаусыз\n"
        f"🎯 Мақсатың: {user['goal']}",
        parse_mode="Markdown",
    )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        "Сұрағың болса, осы хабарламаға жауап ретінде жаз — админге тікелей жіберемін. 📩"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 Қолдау сұранысы: {user.full_name} (@{user.username}, id: {user.id})\n"
                f"Клиентке тікелей /user {user.id} арқылы қарай аласың."
            )
        except Exception:
            pass


async def lessons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for num, lesson in LESSONS.items():
        row.append(InlineKeyboardButton(f"{num}", callback_data=f"lesson_{num}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "📚 *Видео сабақтар каталогы*\n\nНөмірді бас, сол сабақтың сілтемесін аласың:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_lesson_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    num = query.data.split("_")[1]
    lesson = LESSONS.get(num)
    if not lesson:
        await query.message.reply_text("Сабақ табылмады.")
        return
    await query.message.reply_text(
        f"{lesson['title']}\n\n▶️ {lesson['link']}"
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
        await query.edit_message_text(
            "Түсінікті! Курсты аяқтадың. Кез келген уақытта /start арқылы қайта бастай аласың. Сау бол! 👋"
        )


# ---------- Check-in өңдеу ----------
async def handle_button_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = int(query.data.split("_")[1])
    telegram_id = update.effective_user.id

    user = db.get_user(telegram_id)
    if not user or user["current_day"] != day:
        await query.edit_message_text("Бұл тапсырма ескірген немесе бұрын орындалған.")
        return

    streak = db.record_checkin(telegram_id, day)
    db.advance_day(telegram_id)

    await query.edit_message_text(
        f"✅ Тамаша! {day}-күн орындалды.\n🔥 {streak} күн тоқтаусыз!"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    task = TASKS.get(str(day))
    if not task or task["type"] != "photo_proof":
        await update.message.reply_text(
            "Рахмет! Бірақ бүгінгі тапсырма фото түрінде емес. /today арқылы тексер."
        )
        return

    file_id = update.message.photo[-1].file_id
    streak = db.record_checkin(telegram_id, day, proof_file_id=file_id)
    db.advance_day(telegram_id)

    await update.message.reply_text(
        f"✅ Фото қабылданды! {day}-күн орындалды.\n🔥 {streak} күн тоқтаусыз!"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin_id, file_id,
                caption=f"📸 {user['full_name']} (@{user['username']}) — {day}-күн есебі"
            )
        except Exception:
            pass


async def handle_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        return

    day = user["current_day"]
    task = TASKS.get(str(day))
    if not task or task["type"] != "text_reply":
        return

    streak = db.record_checkin(telegram_id, day, text_response=update.message.text)
    db.advance_day(telegram_id)

    await update.message.reply_text(
        f"✅ Рахмет, жауабыңды алдым! {day}-күн орындалды.\n🔥 {streak} күн тоқтаусыз!"
    )


# ---------- Админ командалары ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    cats = db.categorize_users()
    await update.message.reply_text(
        f"👑 *TARTIP ADMIN*\n\n"
        f"👥 Барлығы активті: {len(cats['green']) + len(cats['yellow']) + len(cats['red'])}\n\n"
        f"🟢 Қалыпты: {len(cats['green'])}\n"
        f"🟡 Назар керек: {len(cats['yellow'])}\n"
        f"🔴 Араласыңыз: {len(cats['red'])}\n\n"
        f"Толығырақ үшін /problems жаз.",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    s = db.get_stats()
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"Барлығы тіркелген: {s['total']}\n"
        f"Активті: {s['active']}\n"
        f"Тоқтатылған: {s['paused']}\n"
        f"Орташа streak: {s['avg_streak']}",
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

    status = "Тоқтатылған" if user["paused"] else ("Активті" if user["is_active"] else "Белсенді емес")
    await update.message.reply_text(
        f"👤 *{user['full_name']}* (@{user['username']})\n\n"
        f"Мақсаты: {user['goal']}\n"
        f"Ағымдағы күн: {user['current_day']}/{TOTAL_DAYS}\n"
        f"🔥 {user['streak']} күн тоқтаусыз\n"
        f"Соңғы check-in: {user['last_checkin_date']}\n"
        f"Бастаған күні: {user['start_date']}\n"
        f"Статус: {status}",
        parse_mode="Markdown",
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /pause <telegram_id>")
        return
    target_id = int(context.args[0])
    db.set_paused(target_id, True)
    await update.message.reply_text(f"⏸ {target_id} тоқтатылды. Тапсырма келмейді.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /resume <telegram_id>")
        return
    target_id = int(context.args[0])
    db.set_paused(target_id, False)
    await update.message.reply_text(f"▶️ {target_id} қайта қосылды.")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Қолдану: /reset <telegram_id>")
        return
    target_id = int(context.args[0])
    db.reset_progress(target_id)
    await update.message.reply_text(f"🔄 {target_id} прогресі 1-күнге нөлделді.")


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


# ---------- Render.com тегін "Web Service" үшін жалған веб-сервер ----------
# Render Web Service типі сыртқы порт күтеді (health check), әйтпесе
# қызметті "өлі" деп есептейді. Бот өзі Telegram-мен polling арқылы
# сөйлеседі, сыртқы порттың қажеті жоқ, сондықтан тек "мен тірімін" деп
# жауап беретін минималды сервер қосамыз.
def run_health_server():
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"TARTIP bot is running")

        def log_message(self, format, *args):
            pass  # консольде артық логты болдырмау үшін

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health check server {port} портта іске қосылды")
    server.serve_forever()


# ---------- Негізгі функция ----------
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
            ASKING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_goal)],
            ASKING_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_registration)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("lessons", lessons_command))

    application.add_handler(CallbackQueryHandler(handle_button_checkin, pattern=r"^confirm_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_lesson_callback, pattern=r"^lesson_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_continue_callback, pattern=r"^continue_"))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Админ командалары
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
    job_queue.run_daily(
        daily_broadcast,
        time=datetime.strptime(f"{DAILY_SEND_HOUR}:00", "%H:%M").time(),
    )

    # Render.com тегін Web Service талабы бойынша health check серверін
    # бөлек ағында (thread) қосамыз, негізгі бот polling жұмысына кедергі жасамас үшін
    threading.Thread(target=run_health_server, daemon=True).start()

    logger.info("TARTIP Bot V2 іске қосылды...")
    application.run_polling()


if __name__ == "__main__":
    main()
