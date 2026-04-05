import os
import re
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
REPORTNOW_CHANNEL_ID = 1490325202792353963
MILESTONE_CHANNEL_ID = 1490329238841196584

USD_PER_ROBUX = 0.0038
TIMEZONE = "Europe/Bratislava"

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
            game_link TEXT,
            robux_per_visit FLOAT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS visits_state (
            universe_id BIGINT PRIMARY KEY,
            visits BIGINT
        );
        """)


def load_games():
    with get_conn().cursor() as cur:
        cur.execute("SELECT universe_id, game_link, robux_per_visit FROM games")
        rows = cur.fetchall()

    return [
        {
            "universe_id": int(r[0]),
            "game_link": r[1],
            "robux_per_visit": float(r[2])
        }
        for r in rows
    ]


# ---------------- RORIZZ FETCH ----------------

async def fetch_rorizz(session, universe_id):
    url = f"https://rorizz.com/g/{universe_id}"

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"RoRizz failed: {universe_id}")
                return None

            html = await resp.text()

        # extract values
        playing = re.search(r'([\d,]+)\s+Playing', html)
        visits = re.search(r'([\d,.]+)\s+Visits', html)
        name = re.search(r'<title>(.*?)</title>', html)

        if not visits:
            return None

        return {
            "name": name.group(1) if name else "Unknown",
            "playing": int(playing.group(1).replace(",", "")) if playing else 0,
            "visits": int(float(visits.group(1).replace(",", "").replace("M","000000"))),
        }

    except Exception as e:
        print("RoRizz error:", e)
        return None


# ---------------- REPORT ----------------

async def build_report(update=False):
    games = load_games()
    if not games:
        return "No games."

    async with aiohttp.ClientSession() as session:
        total_visits = 0
        total_robux = 0
        lines = []

        for g in games:
            data = await fetch_rorizz(session, g["universe_id"])

            if not data:
                lines.append(f"• {g['game_link']} could not fetch data")
                continue

            visits_now = data["visits"]

            with get_conn().cursor() as cur:
                cur.execute("SELECT visits FROM visits_state WHERE universe_id=%s", (g["universe_id"],))
                row = cur.fetchone()

            prev = row[0] if row else visits_now
            gained = max(0, visits_now - prev)

            robux = gained * g["robux_per_visit"]

            total_visits += gained
            total_robux += robux

            lines.append(f"• {data['name']}: +{gained:,} visits")

            if update:
                with get_conn().cursor() as cur:
                    cur.execute("""
                    INSERT INTO visits_state VALUES (%s,%s)
                    ON CONFLICT (universe_id) DO UPDATE SET visits=%s
                    """, (g["universe_id"], visits_now, visits_now))

    usd = total_robux * USD_PER_ROBUX

    return (
        f"🏆 {PROJECT_NAME} just earned ${usd:,.2f}\n\n"
        f"• Visits: {total_visits:,}\n"
        f"• Robux: {int(total_robux):,}\n\n"
        + "\n".join(lines)
    )


# ---------------- COMMANDS ----------------

@bot.tree.command(name="reportnow")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    text = await build_report(update=False)

    ch = bot.get_channel(REPORTNOW_CHANNEL_ID)
    await ch.send(text)

    await interaction.followup.send("Done.", ephemeral=True)


@bot.tree.command(name="ccu")
async def ccu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    games = load_games()

    async with aiohttp.ClientSession() as session:
        total = 0
        lines = []

        for g in games:
            data = await fetch_rorizz(session, g["universe_id"])

            if not data:
                continue

            total += data["playing"]
            lines.append(f"• {data['name']}: {data['playing']:,} CCU")

    msg = f"📈 {PROJECT_NAME} has {total:,} CCU\n\n" + "\n".join(lines)

    await interaction.followup.send(msg)


# ---------------- DAILY ----------------

@tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=ZoneInfo(TIMEZONE)))
async def daily():
    ch = bot.get_channel(REPORT_CHANNEL_ID)
    text = await build_report(update=True)

    # short version
    short = text.split("\n")[0]
    await ch.send(short)


# ---------------- START ----------------

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print("READY")

    if not daily.is_running():
        daily.start()


bot.run(TOKEN)
