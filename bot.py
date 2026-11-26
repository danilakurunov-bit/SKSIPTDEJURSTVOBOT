import os
import logging
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "duty.sqlite3")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
SEND_HOUR = int(os.getenv("SEND_HOUR", "8"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))

if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the .env file")

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=ZoneInfo(TIMEZONE))
pending = {}  # chat_id -> setup state
admins = set()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(chat_id, number)
        );
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            members TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            cursor INTEGER NOT NULL DEFAULT 0,
            last_date TEXT
        );
        """)

def get_people(chat_id):
    with db() as c:
        return c.execute("SELECT number,name FROM people WHERE chat_id=? ORDER BY number", (chat_id,)).fetchall()

def get_teams(chat_id):
    with db() as c:
        return c.execute("SELECT id,members FROM teams WHERE chat_id=? AND active=1 ORDER BY id", (chat_id,)).fetchall()

def fmt_team(team, people):
    nums = [int(x) for x in team["members"].split(",")]
    by_num = {p["number"]: p["name"] for p in people}
    return "\n".join(f"• {by_num.get(n, 'Ученик №'+str(n))}" for n in nums)

def is_admin(message):
    return message.from_user and (message.from_user.id in admins)

@dp.message(CommandStart())
async def start(m: Message):
    admins.add(m.from_user.id)
    await m.answer(
        "Привет! Я бот для расписания дежурств.\n\n"
        "1) Отправь список учеников — каждый с новой строки.\n"
        "2) Я пронумерую список.\n"
        "3) Создай пары командой /pair 1 2, затем следующую пару и так далее.\n"
        "Если учеников нечётное количество, одну команду можно сделать тройкой: /pair 19 20 21.\n"
        "4) Напиши /setchat в том чате, куда отправлять ежедневное дежурство.\n\n"
        "Команды: /help, /list, /pairs, /today, /next, /skip, /reset, /settime 08:00"
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "Как настроить:\n"
        "• Отправь список: одно ФИО на строку.\n"
        "• /pair 1 2 — создать постоянную пару (можно /pair 19 20 21 для тройки).\n"
        "• /pairs — проверить команды.\n"
        "• /setchat — включить ежедневные сообщения в этом чате.\n"
        "• /settime 08:00 — время отправки.\n"
        "• /today — кто дежурит сегодня.\n"
        "• /next — следующая команда.\n"
        "• /skip — пропустить следующую команду.\n"
        "• /reset — начать очередь с первой команды.\n"
        "Бот автоматически пропускает субботу и воскресенье."
    )

@dp.message(F.text & ~F.text.startswith("/"))
async def receive_text(m: Message):
    if not m.text or m.text.startswith("/"):
        return
    chat_id = m.chat.id
    lines = [x.strip() for x in m.text.splitlines() if x.strip()]
    if len(lines) < 2:
        await m.answer("Пришли список учеников: каждое ФИО с новой строки.")
        return
    with db() as c:
        c.execute("DELETE FROM people WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM teams WHERE chat_id=?", (chat_id,))
        c.execute("INSERT OR REPLACE INTO settings(chat_id,cursor,last_date) VALUES(?,0,NULL)", (chat_id,))
        for i, name in enumerate(lines, 1):
            c.execute("INSERT INTO people(chat_id,number,name) VALUES(?,?,?)", (chat_id,i,name))
    out = "\n".join(f"{i}. {name}" for i,name in enumerate(lines,1))
    await m.answer("Список сохранён и пронумерован:\n\n"+out+
                   "\n\nТеперь создай команды. Например: /pair 1 2")

@dp.message(Command("pair"))
async def pair_cmd(m: Message):
    if not is_admin(m):
        await m.answer("Сначала отправь /start в личном чате с ботом.")
        return
    try:
        nums = [int(x) for x in m.text.split()[1:]]
    except Exception:
        nums = []
    if len(nums) not in (2,3) or len(set(nums)) != len(nums):
        await m.answer("Формат: /pair 1 2 (пара) или /pair 19 20 21 (тройка).")
        return
    people = get_people(m.chat.id)
    known = {p["number"] for p in people}
    if any(n not in known for n in nums):
        await m.answer("В списке нет одного из указанных номеров. Проверь /list.")
        return
    used = {int(x) for t in get_teams(m.chat.id) for x in t["members"].split(",")}
    if any(n in used for n in nums):
        await m.answer("Кто-то из этих учеников уже состоит в команде. Посмотри /pairs.")
        return
    with db() as c:
        c.execute("INSERT INTO teams(chat_id,members) VALUES(?,?)",
                  (m.chat.id, ",".join(map(str,nums))))
    await m.answer("Команда сохранена:\n"+fmt_team({"members":",".join(map(str,nums))},people)+
                   "\n\nДобавляй следующую команду через /pair.")

@dp.message(Command("list"))
async def list_cmd(m: Message):
    people = get_people(m.chat.id)
    if not people:
        await m.answer("Сначала отправь список учеников.")
        return
    await m.answer("Список учеников:\n"+"\n".join(f'{p["number"]}. {p["name"]}' for p in people))

@dp.message(Command("pairs"))
async def pairs_cmd(m: Message):
    people, teams = get_people(m.chat.id), get_teams(m.chat.id)
    if not teams:
        await m.answer("Команды пока не созданы. Используй /pair 1 2.")
        return
    used = {int(x) for t in teams for x in t["members"].split(",")}
    out = [f"Команда {i}: \n{fmt_team(t,people)}" for i,t in enumerate(teams,1)]
    missing = [p for p in people if p["number"] not in used]
    if missing:
        out.append("Пока без команды:\n"+"\n".join(f'{p["number"]}. {p["name"]}' for p in missing))
    await m.answer("\n\n".join(out))

def due_team(chat_id):
    teams = get_teams(chat_id)
    if not teams:
        return None
    with db() as c:
        row = c.execute("SELECT cursor FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        cursor = row["cursor"] if row else 0
        team = teams[cursor % len(teams)]
        c.execute("INSERT OR REPLACE INTO settings(chat_id,cursor,last_date) VALUES(?,?,?)",
                  (chat_id, (cursor+1) % len(teams), date.today().isoformat()))
    return team

async def send_daily():
    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM settings").fetchall()]
    now = datetime.now(ZoneInfo(TIMEZONE))
    if now.weekday() >= 5:
        return
    for chat_id in chats:
        teams = get_teams(chat_id)
        if not teams:
            continue
        with db() as c:
            row = c.execute("SELECT last_date FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
            if row and row["last_date"] == now.date().isoformat():
                continue
        team = due_team(chat_id)
        if team:
            people = get_people(chat_id)
            text = "Дежурство на сегодня:\n\n" + fmt_team(team,people)
            try:
                await bot.send_message(chat_id, text)
            except Exception:
                logging.exception("Could not send daily message to %s", chat_id)

@dp.message(Command("setchat"))
async def setchat_cmd(m: Message):
    # Пользователь сначала запускает бота в личке, настраивает список и пары,
    # затем вызывает /setchat в целевой группе.
    if not m.from_user or m.from_user.id not in admins:
        await m.answer("Сначала открой бота в личке и отправь /start, затем повтори /setchat в группе.")
        return
    source_chat = m.from_user.id
    target_chat = m.chat.id
    if target_chat != source_chat:
        people = get_people(source_chat)
        teams = get_teams(source_chat)
        if not people:
            await m.answer("Сначала отправь мне в личку список учеников и создай пары командами /pair.")
            return
        with db() as c:
            c.execute("DELETE FROM people WHERE chat_id=?", (target_chat,))
            c.execute("DELETE FROM teams WHERE chat_id=?", (target_chat,))
            for person in people:
                c.execute("INSERT INTO people(chat_id,number,name) VALUES(?,?,?)",
                          (target_chat, person["number"], person["name"]))
            for team in teams:
                c.execute("INSERT INTO teams(chat_id,members) VALUES(?,?)",
                          (target_chat, team["members"]))
            c.execute("INSERT OR REPLACE INTO settings(chat_id,cursor,last_date) VALUES(?,0,NULL)",
                      (target_chat,))
    else:
        with db() as c:
            c.execute("INSERT OR IGNORE INTO settings(chat_id,cursor,last_date) VALUES(?,0,NULL)", (target_chat,))
    await m.answer(
        f"Готово! Этот чат подключён к расписанию. Ежедневные сообщения будут приходить сюда "
        f"в {SEND_HOUR:02d}:{SEND_MINUTE:02d} ({TIMEZONE})."
    )

@dp.message(Command("settime"))
async def settime_cmd(m: Message):
    global SEND_HOUR, SEND_MINUTE
    if m.from_user.id not in admins:
        await m.answer("Сначала отправь /start в личном чате с ботом.")
        return
    try:
        hh, mm = map(int, m.text.split()[1].split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError()
        SEND_HOUR, SEND_MINUTE = hh, mm
        scheduler.reschedule_job("daily", trigger=CronTrigger(day_of_week="mon-fri", hour=hh, minute=mm, timezone=ZoneInfo(TIMEZONE)))
        await m.answer(f"Время отправки изменено на {hh:02d}:{mm:02d} ({TIMEZONE}).")
    except Exception:
        await m.answer("Укажи время в формате /settime 08:00")

@dp.message(Command("today"))
async def today_cmd(m: Message):
    teams = get_teams(m.chat.id)
    if not teams:
        await m.answer("Сначала создай команды через /pair.")
        return
    with db() as c:
        row = c.execute("SELECT cursor,last_date FROM settings WHERE chat_id=?", (m.chat.id,)).fetchone()
    idx = (row["cursor"] - 1) % len(teams) if row and row["last_date"] == date.today().isoformat() else (row["cursor"] if row else 0)
    await m.answer("Сегодня дежурят:\n\n"+fmt_team(teams[idx],get_people(m.chat.id)))

@dp.message(Command("next"))
async def next_cmd(m: Message):
    teams = get_teams(m.chat.id)
    if not teams:
        await m.answer("Сначала создай команды через /pair.")
        return
    with db() as c:
        row = c.execute("SELECT cursor FROM settings WHERE chat_id=?", (m.chat.id,)).fetchone()
    idx = row["cursor"] if row else 0
    await m.answer("Следующая команда:\n\n"+fmt_team(teams[idx % len(teams)],get_people(m.chat.id)))

@dp.message(Command("skip"))
async def skip_cmd(m: Message):
    with db() as c:
        row = c.execute("SELECT cursor FROM settings WHERE chat_id=?", (m.chat.id,)).fetchone()
        if not row:
            await m.answer("Сначала настрой список и команды.")
            return
        count = len(get_teams(m.chat.id))
        if count:
            c.execute("UPDATE settings SET cursor=? WHERE chat_id=?", ((row["cursor"]+1)%count,m.chat.id))
    await m.answer("Следующая команда пропущена.")

@dp.message(Command("reset"))
async def reset_cmd(m: Message):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(chat_id,cursor,last_date) VALUES(?,0,NULL)", (m.chat.id,))
    await m.answer("Очередь сброшена. Следующей будет первая команда.")

async def main():
    init_db()
    scheduler.add_job(send_daily, CronTrigger(day_of_week="mon-fri", hour=SEND_HOUR, minute=SEND_MINUTE, timezone=ZoneInfo(TIMEZONE)), id="daily", replace_existing=True)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
