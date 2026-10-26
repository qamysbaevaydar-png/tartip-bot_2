import os, sys, sqlite3, tempfile, re, collections, itertools
tmp = tempfile.mkdtemp(); os.environ["DB_PATH"] = os.path.join(tmp, "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import database as db, plan as pm
ok = 0
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ")+name, extra)
    assert cond, name
    ok += 1

# 1. parse_time
check("parse_time", [pm.parse_time(x) for x in ["7","07:30","7.30","0730","24:00","abc","7:75"]] == ["07:00","07:30","07:30","07:30",None,None,None])
# 2. schedule edge cases
s = pm.compute_schedule("07:00","23:00",3); check("sched 3 meals", s["mlist"]=="08:00 · 14:00 · 20:00" and s["mlast"]=="20:00" and s["screens_off"]=="22:00", s["mlist"])
s = pm.compute_schedule("07:00","00:30",5); check("sched bed after midnight", s["mlast"]=="21:30" and s["meal_count"]==5, s["mlist"])
s = pm.compute_schedule("15:00","07:00",4); check("sched night shift", s["meal_count"]>=1 and ":" in s["mlist"], s["mlist"])
s = pm.compute_schedule("10:00","14:00",5); check("sched very short day", s["meal_count"]==1, s["mlist"])
# 3. render all 140 days x many profiles
programs = pm.load_json("programs.json")
profiles = []
for place, restr, hf, w, b, m in itertools.product(["home","gym"],["","жоқ","жаңғақ <b>& сүт"],["yes","no"],[("07:00","23:00"),("05:30","21:30"),("11:00","03:00")],[None],[2,3,5]):
    profiles.append(dict(place=place,restrictions=restr,health_flag=hf,wake_time=w[0],bed_time=w[1],meals=m,full_name="Ай",current_day=1))
bad = []; maxlen = 0
for cat, prog in programs.items():
    days = [d for wk in prog["weeks"] for d in wk["days"]]
    for i, d in enumerate(days, 1):
        for u in profiles:
            r = pm.render_day(d, u)
            blob = r["mission"]+" ".join(r["schedule"])+r["fallback"]
            if re.search(r"[{}]", blob): bad.append((cat,i,"brace"))
            maxlen = max(maxlen, len(blob))
check("140 days render for 108 profiles, no leftover placeholders", not bad and sum(len(wk["days"]) for p in programs.values() for wk in p["weeks"])==140, str(bad[:3]))
# restr_note applied only when restrictions given
d = programs["arykhtau"]["weeks"][1]["days"][0]
check("restr note on", "жаңғақ" in pm.render_day(d, dict(restrictions="жаңғақ"))["mission"])
check("restr note off", "Ескерт" not in pm.render_day(d, dict(restrictions="жоқ"))["mission"])
# home/gym variant
d = programs["arykhtau"]["weeks"][1]["days"][3]
check("place variants", "Үйде" in " ".join(pm.render_day(d, dict(place="home"))["schedule"]) and "Залда" in " ".join(pm.render_day(d, dict(place="gym"))["schedule"]))
# no duplicate titles inside a category
dups = [(c,t) for c,p in programs.items() for t,n in collections.Counter(d["title"] for wk in p["weeks"] for d in wk["days"]).items() if n>1]
check("no duplicate titles within a category", not dups, str(dups))
# 4. DB migration from the ORIGINAL (old) schema
old = os.path.join(tmp,"old.db"); c = sqlite3.connect(old)
c.executescript("""CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, category TEXT, goal TEXT, goal_why TEXT, start_date TEXT, current_day INTEGER DEFAULT 1, last_advance_date TEXT, streak INTEGER DEFAULT 0, last_checkin_date TEXT, is_active INTEGER DEFAULT 1, paused INTEGER DEFAULT 0);
CREATE TABLE habit_checkins (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, day INTEGER, habit_id TEXT, checkin_date TEXT, proof_file_id TEXT, text_response TEXT, UNIQUE(telegram_id, day, habit_id));
INSERT INTO users (telegram_id,username,full_name,category,goal,goal_why,current_day) VALUES (5,'old','Ескі','arykhtau','5 кг','себеп',9);
INSERT INTO habit_checkins (telegram_id,day,habit_id,checkin_date) VALUES (5,8,'daily_plan','2026-01-01');""")
c.commit(); c.close()
db.DB_PATH = old; db.init_db()
u = db.get_user(5)
check("migration keeps old user", u["full_name"]=="Ескі" and u["current_day"]==9 and "wake_time" in u and u["awaiting"] is None)
check("migration keeps old checkin as full", db.get_done_days(5)=={8:"full"})
db.init_db(); check("init_db idempotent", True)
# 5. fresh DB logic
db.DB_PATH = os.path.join(tmp,"new.db"); db.init_db()
db.add_user(1,"u","Айдар","arykhtau","г","w",profile=dict(wake_time="07:00",bed_time="23:00",meals=3,place="gym",restrictions="жоқ",health_flag="no",adult="yes"))
u = db.get_user(1); check("profile saved", u["place"]=="gym" and u["meals"]==3 and u["adult"]=="yes")
db.update_user(1, hacker="x", current_day=99); check("update_user whitelist", db.get_user(1)["current_day"]==1)
check("streak day-based", db.compute_streak([1,2,3],3)==3 and db.compute_streak([1,2],3)==2 and db.compute_streak([1,3],3)==1 and db.compute_streak([],1)==0)
db.record_checkin(1,1,text_response="жауап бір", mode="full"); db.record_checkin(1,1,mode="light")
r = db.get_checkin(1,1); check("full not downgraded, text kept", r["mode"]=="full" and r["text_response"]=="жауап бір")
db.record_checkin(1,2,mode="light"); db.record_checkin(1,2,text_response="кейін",mode="full")
check("light upgraded to full", db.get_checkin(1,2)["mode"]=="full")
# review rows don't count as mission completion
db.record_checkin(1,7,habit_id="review_1",text_response="review text 123456789012345")
check("review not counted as day", 7 not in db.get_done_days(1) and len(db.get_reviews(1))==1)
db.advance_day(1); db.advance_day(1); u=db.get_user(1); check("advance", u["current_day"]==3)
db.record_checkin(1,2,habit_id="daily_plan",mode="catchup")
check("streak after advance", db.get_streak(1,3)==2, str(db.get_done_days(1)))
db.set_awaiting(1,"support"); db.clear_awaiting_prefixes(1); check("awaiting cleared", db.get_user(1)["awaiting"]=="")
db.set_awaiting(1,"finish_sent"); db.clear_awaiting_prefixes(1); check("finish_sent kept", db.get_user(1)["awaiting"]=="finish_sent")
db.delete_user(1); check("delete wipes all", db.get_user(1) is None and db.get_done_days(1)=={} )
# 6. compile_system + weekly + adaptation
u = dict(category="arykhtau",goal="5 кг",goal_why="себеп",wake_time="07:00",bed_time="23:00",meals=3,full_name="Ай")
resp=[{"day":1,"text_response":"82 кг / 94 см"},{"day":4,"text_response":"x"*5000}]
ch = pm.compile_system(u, programs, resp, "Арықтау")
check("compile_system chunks <=4096", all(len(c)<=4096 for c in ch) and "Бастапқы өлшем" in ch[0], str([len(c) for c in ch]))
big=[{"day":d,"text_response":"ж"*400} for d in range(1,29)]
ch = pm.compile_system(u, programs, big, "Арықтау"); check("compile_system huge splits", len(ch)>1 and all(len(c)<=4096 for c in ch), str([len(c) for c in ch]))
check("adaptation", "жеңілдетілген" in pm.adaptation_text(1,7) and "күрделендіреміз" in pm.adaptation_text(7,7))
print("ALL CORE CHECKS PASSED:", ok)
