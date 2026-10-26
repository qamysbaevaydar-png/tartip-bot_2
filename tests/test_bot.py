import os, sys, tempfile, asyncio, re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
tmp = tempfile.mkdtemp(); os.environ["DB_PATH"]=os.path.join(tmp,"b.db"); os.environ["ADMIN_IDS"]="900"; os.environ["BOT_TOKEN"]="1:x"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import database as db, plan as pm, bot
from telegram.ext import ConversationHandler
db.init_db()
n=0
def check(name, cond, extra=""):
    global n; print(("PASS " if cond else "FAIL ")+name, extra); assert cond, name; n+=1

class Msg:
    def __init__(s, text=None, uid=1): s.text=text; s.replies=[]; s.photo=None; s.voice=None; s.from_user=NS(id=uid)
    async def reply_text(s, text, **kw): s.replies.append((text, kw)); return s
class Q:
    def __init__(s, data, uid=1): s.data=data; s.from_user=NS(id=uid); s.message=Msg(uid=uid); s.answers=[]; s.edited=[]
    async def answer(s, text=None, show_alert=False): s.answers.append((text,show_alert))
    async def edit_message_text(s, text, **kw): s.edited.append((text,kw))
    async def edit_message_reply_markup(s, reply_markup=None): s.edited.append(("<markup>",{}))
def upd(uid=1, text=None, data=None, username="u"):
    m = Msg(text, uid); q = Q(data, uid) if data else None
    return NS(effective_user=NS(id=uid, username=username), message=None if q else m, callback_query=q, effective_message=(q.message if q else m)), (q or m)
def ctx():
    c = NS(user_data={}, args=[], bot=NS(send_message=AsyncMock(), send_photo=AsyncMock(), send_voice=AsyncMock(), get_chat_member=AsyncMock()))
    return c
async def register(uid, category="arykhtau", adult="yes", c=None, name="Айдар"):
    c = c or ctx(); bot.CHANNEL_ID=None
    u,_ = upd(uid, "/start"); await bot.start(u,c)
    u,_ = upd(uid, data=f"cat_{category}"); await bot.category_chosen(u,c)
    for text,fn in [(name,bot.ask_goal),("5 кг арықтау",bot.ask_why),("денсаулық үшін",bot.start_intake)]:
        u,_ = upd(uid, text); st = await fn(u,c)
    answers = {"adult":adult,"wake":"07:00","bed":"23:00","meals":"3","place":"home","restrictions":"сүт","health":"no"}
    while True:
        s = c.user_data["intake"]; key = s["steps"][s["i"]]
        if INTAKE_DEF(key)=="buttons": u,_=upd(uid,data=f"in_{key}_{answers[key]}")
        else: u,_=upd(uid,answers[key])
        st = await bot.intake_answer(u,c)
        if st==ConversationHandler.END: return c
        if st=="STOP": return c
def INTAKE_DEF(k): return bot.INTAKE_DEFS[k]["type"]

async def main():
    # --- registration (fitness) ---
    c = await register(1)
    u = db.get_user(1)
    check("registration saves profile", u["wake_time"]=="07:00" and u["meals"]==3 and u["place"]=="home" and u["restrictions"]=="сүт" and u["adult"]=="yes" and u["category"]=="arykhtau")
    # --- intake validation ---
    c2 = ctx(); u_,_=upd(2,"/start"); await bot.start(u_,c2)
    u_,_=upd(2,data="cat_arykhtau"); await bot.category_chosen(u_,c2)
    for t,f in [("Ай",bot.ask_goal),("м",bot.ask_why),("с",bot.start_intake)]:
        u_,_=upd(2,t); await f(u_,c2)
    u_,m=upd(2,"бүгін"); r=await bot.intake_answer(u_,c2); check("buttons step rejects text", r==bot.INTAKE and "батырма" in m.replies[-1][0])
    u_,q=upd(2,data="in_adult_no"); r=await bot.intake_answer(u_,c2)
    check("minor on fitness is blocked & offered safe categories", r==bot.INTAKE and c2.user_data["intake"]["steps"][c2.user_data["intake"]["i"]]=="adult" and db.get_user(2) is None)
    u_,q=upd(2,data="in_switch_uyqy_energy"); r=await bot.intake_answer(u_,c2)
    check("switch to safe category", c2.user_data["category"]=="uyqy_energy" and c2.user_data["intake"]["steps"]==["adult","wake","bed","meals","health"] and c2.user_data["intake"]["i"]==1)
    u_,m=upd(2,"25:99"); r=await bot.intake_answer(u_,c2); check("bad time rejected", "07:30" in m.replies[-1][0])
    u_,q=upd(2,data="in_switch_arykhtau"); await bot.intake_answer(u_,c2); check("cannot switch back to fitness", c2.user_data["category"]=="uyqy_energy")
    # --- day text: HTML safe, escape user input ---
    db.update_user(1, restrictions="<b>x</b> & y")
    user = db.get_user(1)
    for day in (1,9,11,14,28):
        t = bot.build_day_text(user, day); check(f"day {day} text <4096 and no raw placeholders", len(t)<4096 and "{" not in t)
    t = bot.build_day_text(user, 9); check("user input escaped in HTML", "&lt;b&gt;x&lt;/b&gt; &amp; y" in t and "<b>x</b>" not in t)
    check("all 140 day texts fit Telegram", all(len(bot.build_day_text({**user,"category":cat},d))<4096 for cat in bot.PROGRAMS for d in range(1,29)))
    # --- mission text validation (day 1, text proof, min 5) ---
    u_,m=upd(1,"ok"); await bot.handle_text_reply(u_,c); check("too short text rejected", "тым қысқа" in m.replies[-1][0] and 1 not in db.get_done_days(1))
    u_,m=upd(1,"82 кг / 94 см"); await bot.handle_text_reply(u_,c); check("valid answer accepted", 1 in db.get_done_days(1) and "қабылданды" in m.replies[-1][0] and "Бастапқы өлшем" in m.replies[-1][0])
    # --- light + noop + done callbacks ---
    db.update_user(1)  # no-op
    # move to day 6 (button proof) for arykhtau
    for _ in range(5): db.advance_day(1)
    u_,q=upd(1,data="done_6"); await bot.handle_done_callback(u_,c); check("button day done", 6 in db.get_done_days(1))
    u_,q=upd(1,data="done_5"); await bot.handle_done_callback(u_,c); check("stale done rejected", "ескірген" in q.answers[-1][0] and 5 not in db.get_done_days(1))
    db.advance_day(1)  # day 7
    u_,q=upd(1,data="light_7"); await bot.handle_light_callback(u_,c); check("light counts", db.get_done_days(1)[7]=="light")
    # --- catch-up: advance to 8 without day 7 full; day 7 is light so test with a missed day ---
    db.advance_day(1); db.advance_day(1)  # day 9; day 8 missed
    c.bot.send_message.reset_mock(); await bot.send_day_to_user(c, db.get_user(1))
    sent = [call.args[1] if len(call.args)>1 else call.kwargs.get("text") for call in c.bot.send_message.call_args_list]
    check("catch-up offered when yesterday missed", any("Кешегі миссия" in s for s in sent) and any("9/28" in s for s in sent))
    u_,q=upd(1,data="catchup_8"); await bot.handle_catchup_callback(u_,c); check("catch-up closes yesterday", db.get_done_days(1).get(8)=="catchup")
    u_,q=upd(1,data="catchup_3"); await bot.handle_catchup_callback(u_,c); check("old catch-up refused", "мүмкін емес" in q.answers[-1][0])
    check("streak keeps through catch-up", db.get_streak(1,9)>=3, str(db.get_done_days(1)))
    # --- photo day (arykhtau day 8 = photo). set user to day 8-> use day index 8 is 'photo'
    db.update_user(1)
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=8 WHERE telegram_id=1"); conn.execute("DELETE FROM habit_checkins WHERE telegram_id=1 AND day=8"); conn.commit(); conn.close()
    pm_ = bot.get_daily_plan("arykhtau",8); check("day 8 is photo", pm_["proof"]=="photo")
    u_,m=upd(1,"сурет жоқ, мәтін"); await bot.handle_text_reply(u_,c); check("text on photo day gives hint", "фото" in m.replies[-1][0] and 8 not in db.get_done_days(1))
    u_,m=upd(1); u_.message.photo=[NS(file_id="F1")]; await bot.handle_photo(u_,c); check("photo accepted + forwarded to admin", 8 in db.get_done_days(1) and c.bot.send_photo.await_count==1)
    # --- voice on text day ---
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=10 WHERE telegram_id=1"); conn.commit(); conn.close()
    u_,m=upd(1); u_.message.voice=NS(file_id="V1"); await bot.handle_voice(u_,c); check("voice answer accepted", 10 in db.get_done_days(1) and c.bot.send_voice.await_count==1)
    # --- support ---
    u_,m=upd(1,"/support"); await bot.support_command(u_,c); check("support sets awaiting", db.get_user(1)["awaiting"]=="support")
    c.bot.send_message.reset_mock()
    u_,m=upd(1,"Көмек керек, тамақ жоспары қиын"); await bot.handle_text_reply(u_,c)
    admin_calls=[x for x in c.bot.send_message.call_args_list if x.args[0]==900]
    check("support forwarded to admin, NOT counted as mission", len(admin_calls)==1 and "Көмек керек" in admin_calls[0].args[1] and db.get_user(1)["awaiting"]=="")
    # --- weekly review is separate from next day's mission ---
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=15 WHERE telegram_id=1"); conn.commit(); conn.close()
    db.set_awaiting(1,"review_2")
    u_,m=upd(1,"қысқа"); await bot.handle_text_reply(u_,c); check("short review asks more", "қысқа" in m.replies[-1][0] and db.get_user(1)["awaiting"]=="review_2")
    u_,m=upd(1,"Тәрелке әдісі жұмыс істеді, кешкі ас қиын болды, келесі аптада кешкі асты жоспарлаймын"); await bot.handle_text_reply(u_,c)
    check("review saved, adaptation shown, day 15 NOT closed", 15 not in db.get_done_days(1) and len(db.get_reviews(1))==1 and "Review қабылданды" in m.replies[-1][0] and "Келесі апта мақсаты" in m.replies[-1][0])
    # morning job clears stale awaiting
    db.set_awaiting(1,"review_2"); db.clear_awaiting_prefixes(1); check("stale review awaiting cleared", db.get_user(1)["awaiting"]=="")
    # --- day 28 compile + finish once ---
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=28 WHERE telegram_id=1"); conn.commit(); conn.close()
    c.bot.send_message.reset_mock()
    u_,m=upd(1,"Аптасына 3 жаттығу және бел 90 см болсын 30 күнде"); await bot.handle_text_reply(u_,c)
    sent=[x.args[1] for x in c.bot.send_message.call_args_list]
    check("day 28 sends 'Менің жүйем'", any("Менің жүйем" in s and "Келесі 30 күнге мақсатым" in s for s in sent))
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=29 WHERE telegram_id=1"); conn.commit(); conn.close()
    c.bot.send_message.reset_mock(); await bot.send_day_to_user(c, db.get_user(1)); a=c.bot.send_message.await_count
    await bot.send_day_to_user(c, db.get_user(1)); check("finish message sent only once", a>=1 and c.bot.send_message.await_count==a)
    # --- /forget needs confirmation ---
    u_,m=upd(1,"/forget"); await bot.forget_command(u_,c); check("forget asks confirmation, deletes nothing", db.get_user(1) is not None and "Растайсың" in m.replies[-1][0])
    u_,q=upd(1,data="forget_no"); await bot.handle_forget_callback(u_,c); check("forget_no keeps data", db.get_user(1) is not None)
    # --- admin parse safety ---
    ca=ctx(); ca.args=["abc"]; u_,m=upd(900,"/wipe"); await bot.wipe_command(u_,ca); check("wipe invalid id handled", "Қолдану" in m.replies[-1][0])
    ca.args=[]; u_,m=upd(900,"/pause"); await bot.pause_command(u_,ca); check("pause no arg handled", "Қолдану" in m.replies[-1][0])
    u_,m=upd(5,"/admin"); await bot.admin_panel(u_,ca); check("non-admin gets nothing", not m.replies)
    # --- admin snapshot states (time aware) ---
    mk=lambda day,paused=0: dict(telegram_id=77,current_day=day,paused=paused)
    snap=lambda day,done,hour: bot.get_admin_user_snapshot(mk(day), now=datetime(2026,10,26,hour,0,tzinfo=bot.ALMATY_TZ), done=done)
    check("new user", snap(1,{},9)["priority"]=="new")
    check("morning not done = neutral (not red)", snap(5,{1:"full",2:"full",3:"full",4:"full"},9)["priority"]=="ok")
    check("evening not done = yellow", snap(5,{1:"full",2:"full",3:"full",4:"full"},19)["priority"]=="yellow")
    check("missed yesterday = yellow", snap(5,{1:"full",2:"full",3:"full"},9)["priority"]=="yellow")
    check("missed 2 = red", snap(5,{1:"full",2:"full"},9)["priority"]=="red" and snap(5,{1:"full",2:"full"},9)["missed_run"]==2)
    check("done today green", snap(5,{5:"full"},9)["priority"]=="green" and "жеңіл" in snap(5,{5:"light"},9)["label"])
    check("streak not stale after silence", snap(10,{1:"full",2:"full",3:"full"},9)["streak"]==0)
    check("paused / finished", bot.get_admin_user_snapshot(mk(5,1),done={})["priority"]=="paused" and bot.get_admin_user_snapshot(mk(29),done={})["priority"]=="done")
    # --- admin card, list, messaging, weekly ---
    user=db.get_user(1); text,kb=bot.build_admin_user_card(user,"all",0)
    check("admin card shows profile + answers + buttons", "ояну 07:00" in text and "Соңғы жауаптары" in text and any("Хабар жазу" in b.text for r in kb.inline_keyboard for b in r))
    db.add_user(31,"a","Жаңа","discipline","мақ","себ",profile=dict(wake_time="08:00",bed_time="00:00",adult="yes",health_flag="yes"))
    text,kb=bot.build_admin_list("all",0); check("admin list builds", len(kb.inline_keyboard)>=3)
    text,kb=bot.build_admin_user_card(db.get_user(31),"all",0); check("health flag shown to admin without diagnosis", "денсаулық белгісі" in text)
    u_,q=upd(900,data="admin_msg_1_all_0"); await bot.handle_admin_callback(u_,ca); check("admin msg mode set", ca.user_data.get("admin_msg_to")==1)
    ca.bot.send_message.reset_mock(); u_,m=upd(900,"Айдар, жарайсың! <3"); await bot.handle_text_reply(u_,ca)
    check("admin message delivered to client (escaped)", ca.bot.send_message.await_args.args[0]==1 and "Куратордан" in ca.bot.send_message.await_args.args[1] and "&lt;3" in ca.bot.send_message.await_args.args[1])
    u_,q=upd(900,data="admin_weekly_1_all_0"); await bot.handle_admin_callback(u_,ca); check("weekly draft preview", "Апталық қорытынды жобасы" in q.message.replies[-1][0])
    ca.bot.send_message.reset_mock(); u_,q=upd(900,data="admin_sendweekly_1_all_0"); await bot.handle_admin_callback(u_,ca); check("weekly draft sent", ca.bot.send_message.await_args.args[0]==1)
    u_,q=upd(900,data="admin_answers_1_all_0"); await bot.handle_admin_callback(u_,ca); check("answers view", "жауаптары" in q.edited[-1][0])
    u_,q=upd(900,data="admin_reset_1_all_0"); await bot.handle_admin_callback(u_,ca); check("reset requires confirm", db.get_done_days(1)!={} and "Иә" in str(q.edited[-1][1]["reply_markup"]))
    u_,q=upd(900,data="admin_toggle_1_all_0"); await bot.handle_admin_callback(u_,ca); check("pause toggle", db.get_user(1)["paused"]==1)
    u_,q=upd(5,data="admin_toggle_1_all_0"); await bot.handle_admin_callback(u_,ca); check("non-admin callback blocked", "тек админге" in q.answers[-1][0])
    # digest
    ca.bot.send_message.reset_mock(); await bot.admin_digest_job(ca); check("digest job runs", True)
    # --- deadline job: review awaiting + advance ---
    db.reset_progress(1); db.set_paused(1,False)
    conn=db.get_connection(); conn.execute("UPDATE users SET current_day=7, last_advance_date='2000-01-01' WHERE telegram_id=1"); conn.commit(); conn.close()
    cj=ctx(); await bot.deadline_advance_job(cj); u=db.get_user(1)
    check("deadline: day advanced, review awaiting set, missed message", u["current_day"]==8 and u["awaiting"]=="review_1" and any("жабылмады" in x.args[1] for x in cj.bot.send_message.call_args_list))
    # --- application wiring (no network) ---
    app = bot.build_application("123456:TEST"); check("application builds with all handlers & jobs", len(app.handlers[0])>20 and len(app.job_queue.jobs())==6, f"{len(app.handlers[0])} handlers, {len(app.job_queue.jobs())} jobs")
    print("ALL BOT CHECKS PASSED:", n)
asyncio.run(main())
