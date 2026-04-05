import os
import re
import json
import html as html_lib
import datetime
from zoneinfo import ZoneInfo

import aiohttp
import discord
import psycopg2
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

PROJECT_NAME = "Project Floppa"
REPORT_CHANNEL_ID = 1490317756136947942
WEEKLY_CHANNEL_ID = 1490329238841196584
TIMEZONE = "Europe/Bratislava"
USD_PER_ROBUX = 0.0038

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

conn = None


# ---------------- DATABASE ----------------

def get_conn():
    global conn
    if conn is None or conn.closed:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
    return conn


def init_db():
    with get_conn().cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            universe_id BIGINT PRIMARY KEY,
            game_link TEXT NOT NULL,
            robux_per_visit DOUBLE PRECISION NOT NULL
        );
        """)


def load_games():
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT universe_id, game_link, robux_per_visit
            FROM games
        """)
        rows = cur.fetchall()

    return [
        {
            "universe_id": int(r[0]),
            "game_link": r[1],
            "robux_per_visit": float(r[2]),
        }
        for r in rows
    ]


def add_game_to_db(universe_id, link, rpv):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO games (universe_id, game_link, robux_per_visit)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id)
            DO UPDATE SET
                game_link = EXCLUDED.game_link,
                robux_per_visit = EXCLUDED.robux_per_visit
        """, (universe_id, link, rpv))


def remove_game_by_universe_id(uid):
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM games WHERE universe_id = %s", (uid,))


# ---------------- HELPERS ----------------

def extract_rorizz_universe_id(link):
    m = re.search(r"rorizz\.com/g/(\d+)", link)
    return int(m.group(1)) if m else None


def now_local():
    return datetime.datetime.now(ZoneInfo(TIMEZONE))


def parse_chart(raw):
    try:
        return json.loads(html_lib.unescape(raw))
    except:
        return None


def extract_chart(html):
    m = re.search(r'data-chart="([^"]+)"', html)
    return parse_chart(m.group(1)) if m else None


def chart_val(points, date):
    label = date.strftime("%b %d")
    for p in points:
        if p.get("time") == label:
            return int(p.get("value"))
    return None


async def fetch(session, uid):
    url = f"https://rorizz.com/g/{uid}"
    async with session.get(url) as r:
        html = await r.text()

    title = re.search(r"<title>(.*?)</title>", html, re.S)
    name = html_lib.unescape(title.group(1).replace(" - RoRizz", "")) if title else str(uid)

    chart = extract_chart(html)

    return {
        "name": name,
        "chart": chart
    }


# ---------------- REPORT LOGIC ----------------

async def daily_calc():
    games = load_games()
    today = now_local().date()
    y = today - datetime.timedelta(days=1)
    p = today - datetime.timedelta(days=2)

    total = 0

    async with aiohttp.ClientSession() as s:
        for g in games:
            d = await fetch(s, g["universe_id"])
            if not d or not d["chart"]:
                continue

            yv = chart_val(d["chart"], y)
            pv = chart_val(d["chart"], p)
            if yv is None or pv is None:
                continue

            diff = max(0, yv - pv)
            robux = diff * g["robux_per_visit"]
            total += robux * USD_PER_ROBUX

    return f"🏆 {PROJECT_NAME} just earned ${int(round(total)):,}"


async def weekly_calc():
    games = load_games()
    today = now_local().date()
    total = 0

    async with aiohttp.ClientSession() as s:
        for g in games:
            d = await fetch(s, g["universe_id"])
            if not d or not d["chart"]:
                continue

            for i in range(7, 0, -1):
                end = today - datetime.timedelta(days=i)
                start = end - datetime.timedelta(days=1)

                ev = chart_val(d["chart"], end)
                sv = chart_val(d["chart"], start)

                if ev is None or sv is None:
                    continue

                diff = max(0, ev - sv)
                robux = diff * g["robux_per_visit"]
                total += robux * USD_PER_ROBUX

    return f"📈 {PROJECT_NAME} made ${int(round(total)):,} this week"


async def prev_calc():
    games = load_games()
    today = now_local().date()
    y = today - datetime.timedelta(days=1)
    p = today - datetime.timedelta(days=2)

    lines = []
    total = 0

    async with aiohttp.ClientSession() as s:
        for g in games:
            d = await fetch(s, g["universe_id"])
            if not d or not d["chart"]:
                continue

            yv = chart_val(d["chart"], y)
            pv = chart_val(d["chart"], p)

            if yv is None or pv is None:
                continue

            diff = max(0, yv - pv)
            robux = int(diff * g["robux_per_visit"])
            usd = robux * USD_PER_ROBUX

            total += usd

            lines.append(f"• {d['name']} | +{diff:,} visits | {robux:,} robux | ${usd:,.2f}")

    return "\n".join(lines) if lines else "No data."


# ---------------- COMMANDS ----------------

@bot.tree.command(name="panel")
async def panel(i: discord.Interaction):
    await i.response.send_message("Use buttons (add/remove/list)", ephemeral=True)


@bot.tree.command(name="add")
async def add(i: discord.Interaction, link: str, robux_per_visit: float):
    uid = extract_rorizz_universe_id(link)
    add_game_to_db(uid, link, robux_per_visit)
    await i.response.send_message("Added.", ephemeral=True)


@bot.tree.command(name="remove")
async def remove(i: discord.Interaction, uid: int):
    remove_game_by_universe_id(uid)
    await i.response.send_message("Removed.", ephemeral=True)


@bot.tree.command(name="prev")
async def prev(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    msg = await prev_calc()
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="reportnow")
async def reportnow(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    msg = await daily_calc()
    await i.followup.send(msg, ephemeral=True)


@bot.tree.command(name="weekly")
async def weekly(i: discord.Interaction):
    await i.response.defer(ephemeral=True)

    ch = bot.get_channel(WEEKLY_CHANNEL_ID)
    msg = await weekly_calc()

    await ch.send(msg)
    await i.followup.send("✅ Weekly sent", ephemeral=True)


# ---------------- DAILY ----------------

@tasks.loop(time=datetime.time(hour=1, minute=0, tzinfo=ZoneInfo(TIMEZONE)))
async def daily():
    ch = bot.get_channel(REPORT_CHANNEL_ID)
    msg = await daily_calc()
    await ch.send(msg)


@daily.before_loop
async def before():
    await bot.wait_until_ready()


# ---------------- START ----------------

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    if not daily.is_running():
        daily.start()
    print("READY")


bot.run(TOKEN)
