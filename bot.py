import os
import re
import datetime
from zoneinfo import ZoneInfo

import aiohttp
import discord
import psycopg2
from bs4 import BeautifulSoup
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

PROJECT_NAME = "Project Floppa"

REPORT_CHANNEL_ID = 1490317756136947942
REPORTNOW_CHANNEL_ID = 1490325202792353963
MILESTONE_CHANNEL_ID = 1490329238841196584

USD_PER_ROBUX = 0.0038
TIMEZONE = "Europe/Bratislava"

MILESTONES = {
    1000: "🥉",
    5000: "🥈",
    10000: "🥇",
}

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
            robux_per_visit DOUBLE PRECISION NOT NULL DEFAULT 0
        );
        """)

        cur.execute("""
        ALTER TABLE games
        ADD COLUMN IF NOT EXISTS place_id BIGINT;
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS milestone_hits (
            universe_id BIGINT NOT NULL,
            milestone BIGINT NOT NULL,
            PRIMARY KEY (universe_id, milestone)
        );
        """)

        # one daily snapshot per game per date
        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_visit_snapshots (
            universe_id BIGINT NOT NULL,
            snapshot_date DATE NOT NULL,
            visits BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (universe_id, snapshot_date)
        );
        """)


def load_games():
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT universe_id, game_link, robux_per_visit
            FROM games
            ORDER BY universe_id ASC
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


def add_game_to_db(universe_id: int, game_link: str, robux_per_visit: float):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO games (universe_id, game_link, robux_per_visit)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id) DO UPDATE
            SET game_link = EXCLUDED.game_link,
                robux_per_visit = EXCLUDED.robux_per_visit
        """, (universe_id, game_link, robux_per_visit))


def remove_game_by_universe_id(universe_id: int):
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM games WHERE universe_id = %s", (universe_id,))
        cur.execute("DELETE FROM daily_visit_snapshots WHERE universe_id = %s", (universe_id,))
        cur.execute("DELETE FROM milestone_hits WHERE universe_id = %s", (universe_id,))


def save_daily_snapshot(universe_id: int, snapshot_date: datetime.date, visits: int):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO daily_visit_snapshots (universe_id, snapshot_date, visits)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id, snapshot_date)
            DO UPDATE SET visits = EXCLUDED.visits, created_at = NOW()
        """, (universe_id, snapshot_date, visits))


def get_snapshot(universe_id: int, snapshot_date: datetime.date):
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT visits
            FROM daily_visit_snapshots
            WHERE universe_id = %s AND snapshot_date = %s
        """, (universe_id, snapshot_date))
        row = cur.fetchone()
    return int(row[0]) if row else None


def get_latest_snapshot_before(universe_id: int, snapshot_date: datetime.date):
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT snapshot_date, visits
            FROM daily_visit_snapshots
            WHERE universe_id = %s
              AND snapshot_date < %s
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, (universe_id, snapshot_date))
        row = cur.fetchone()

    if not row:
        return None
    return {
        "snapshot_date": row[0],
        "visits": int(row[1]),
    }


def milestone_exists(universe_id: int, milestone: int) -> bool:
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM milestone_hits
            WHERE universe_id = %s AND milestone = %s
        """, (universe_id, milestone))
        return cur.fetchone() is not None


def save_milestone(universe_id: int, milestone: int):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO milestone_hits (universe_id, milestone)
            VALUES (%s, %s)
            ON CONFLICT (universe_id, milestone) DO NOTHING
        """, (universe_id, milestone))


# ---------------- RORIZZ HELPERS ----------------

def extract_rorizz_universe_id(link: str):
    match = re.search(r"rorizz\.com/g/(\d+)", link)
    return int(match.group(1)) if match else None


def parse_compact_number(value: str) -> int:
    value = value.strip().replace(",", "")
    multiplier = 1

    if value.endswith(("K", "k")):
        multiplier = 1_000
        value = value[:-1]
    elif value.endswith(("M", "m")):
        multiplier = 1_000_000
        value = value[:-1]
    elif value.endswith(("B", "b")):
        multiplier = 1_000_000_000
        value = value[:-1]

    return int(float(value) * multiplier)


def extract_stat_from_text(text: str, label: str):
    # matches:
    # 585.6K Playing
    # 454.8M Visits
    # 10,421 Playing
    pattern = rf"(\d[\d,]*(?:\.\d+)?[KMBkmb]?)\s+{re.escape(label)}\b"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    return parse_compact_number(match.group(1))


async def fetch_rorizz(session: aiohttp.ClientSession, universe_id: int):
    url = f"https://rorizz.com/g/{universe_id}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=25),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                print(f"RoRizz failed for {universe_id}: HTTP {resp.status}")
                return None

            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        # title
        title = None
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

        if not title:
            title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip().replace("— RoRizz", "").replace("- RoRizz", "").strip()

        if not title:
            title = f"Game {universe_id}"

        # visible text first
        playing = extract_stat_from_text(text, "Playing")
        visits = extract_stat_from_text(text, "Visits")

        # fallback JSON-ish patterns if they exist
        if playing is None:
            m = re.search(r'"playing"\s*:\s*(\d+)', html, re.IGNORECASE)
            if m:
                playing = int(m.group(1))

        if visits is None:
            m = re.search(r'"visits"\s*:\s*(\d+)', html, re.IGNORECASE)
            if m:
                visits = int(m.group(1))

        if playing is None:
            playing = 0
        if visits is None:
            visits = 0

        if playing == 0 and visits == 0:
            print(f"RoRizz returned page but no usable stats for {universe_id}")
            return None

        return {
            "name": title,
            "playing": playing,
            "visits": visits,
            "url": str(resp.url),
        }

    except Exception as e:
        print(f"RoRizz error for {universe_id}: {e}")
        return None


# ---------------- REPORT HELPERS ----------------

def now_local():
    return datetime.datetime.now(ZoneInfo(TIMEZONE))


def format_money(value: float) -> str:
    return f"${value:,.2f}"


def build_day_range_text(day_a: datetime.date, day_b: datetime.date) -> str:
    return f"{day_a.strftime('%b %d')} → {day_b.strftime('%b %d')}"


async def collect_and_store_today_snapshots():
    today = now_local().date()
    games = load_games()
    results = []

    if not games:
        return results

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz(session, game["universe_id"])
            if not data:
                results.append({
                    "ok": False,
                    "universe_id": game["universe_id"],
                    "game_link": game["game_link"],
                })
                continue

            save_daily_snapshot(game["universe_id"], today, int(data["visits"]))
            results.append({
                "ok": True,
                "universe_id": game["universe_id"],
                "game_link": game["game_link"],
                "name": data["name"],
                "visits": int(data["visits"]),
                "playing": int(data["playing"]),
                "robux_per_visit": game["robux_per_visit"],
            })

    return results


async def build_daily_difference_report():
    today = now_local().date()
    games = load_games()

    if not games:
        return {
            "short": f"🏆 {PROJECT_NAME}: no tracked games",
            "full": "📭 No tracked games added yet.",
        }

    # first save today's 22:00 snapshots
    current = await collect_and_store_today_snapshots()

    total_diff = 0
    total_robux = 0.0
    lines = []

    for row in current:
        if not row["ok"]:
            lines.append(f"• **{row['game_link']}**: could not fetch data")
            continue

        today_visits = row["visits"]
        prev_row = get_latest_snapshot_before(row["universe_id"], today)

        if not prev_row:
            lines.append(
                f"• **{row['name']}**: snapshot saved for today ({today_visits:,} visits), no previous day to compare yet"
            )
            continue

        diff = max(0, today_visits - prev_row["visits"])
        robux = diff * row["robux_per_visit"]
        usd = robux * USD_PER_ROBUX

        total_diff += diff
        total_robux += robux

        lines.append(
            f"• **{row['name']}** ({build_day_range_text(prev_row['snapshot_date'], today)}): "
            f"+{diff:,} visits | {robux:,.2f} robux | {format_money(usd)}"
        )

    total_usd = total_robux * USD_PER_ROBUX

    full = (
        f"🏆 **{PROJECT_NAME} daily report**\n\n"
        f"**Report time:** {today.strftime('%Y-%m-%d')} 22:00 ({TIMEZONE})\n"
        f"• Total gained visits: **{total_diff:,}**\n"
        f"• Total earned robux: **{total_robux:,.2f}**\n"
        f"• Revenue: **{format_money(total_usd)}**\n\n"
        f"**Tracked games**\n"
        + "\n".join(lines)
    )

    short = f"🏆 {PROJECT_NAME} daily report: +{total_diff:,} visits | {format_money(total_usd)}"

    return {
        "short": short,
        "full": full,
    }


async def build_compare_dates_report(date_a: datetime.date, date_b: datetime.date):
    games = load_games()
    if not games:
        return "📭 No tracked games added yet."

    lines = []
    total_diff = 0
    total_robux = 0.0

    for game in games:
        a = get_snapshot(game["universe_id"], date_a)
        b = get_snapshot(game["universe_id"], date_b)

        if a is None or b is None:
            lines.append(
                f"• **{game['game_link']}**: missing snapshot for {date_a} or {date_b}"
            )
            continue

        diff = max(0, b - a)
        robux = diff * game["robux_per_visit"]
        usd = robux * USD_PER_ROBUX

        total_diff += diff
        total_robux += robux

        lines.append(
            f"• Universe `{game['universe_id']}`: +{diff:,} visits | "
            f"{robux:,.2f} robux | {format_money(usd)}"
        )

    total_usd = total_robux * USD_PER_ROBUX

    return (
        f"📊 **Compare {date_a.strftime('%b %d')} vs {date_b.strftime('%b %d')}**\n\n"
        f"• Total gained visits: **{total_diff:,}**\n"
        f"• Total earned robux: **{total_robux:,.2f}**\n"
        f"• Revenue: **{format_money(total_usd)}**\n\n"
        + "\n".join(lines)
    )


# ---------------- COMMANDS ----------------

@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


@bot.tree.command(name="reportnow", description="Build the daily report now")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await build_daily_difference_report()
        await interaction.followup.send(data["full"][:1900], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="compare", description="Compare two saved dates (YYYY-MM-DD)")
async def compare(
    interaction: discord.Interaction,
    date_a: str,
    date_b: str,
):
    await interaction.response.defer(ephemeral=True)
    try:
        d1 = datetime.date.fromisoformat(date_a)
        d2 = datetime.date.fromisoformat(date_b)
        msg = await build_compare_dates_report(d1, d2)
        await interaction.followup.send(msg[:1900], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="ccu", description="Show current total CCU and tracked games")
async def ccu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    games = load_games()
    if not games:
        await interaction.followup.send("📭 No tracked games added yet.", ephemeral=True)
        return

    total = 0
    lines = []

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz(session, game["universe_id"])

            if not data:
                lines.append(f"• **{game['game_link']}**: could not fetch data")
                continue

            total += int(data["playing"])
            lines.append(f"• **{data['name']}**: {int(data['playing']):,} CCU | {int(data['visits']):,} visits")

    msg = f"📈 {PROJECT_NAME} currently has {total:,} CCU"

    if lines:
        msg += "\n\n" + "\n".join(lines)

    await interaction.followup.send(msg[:1900], ephemeral=True)


# ---------------- AUTOMATIONS ----------------

@tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=ZoneInfo(TIMEZONE)))
async def daily():
    ch = bot.get_channel(REPORT_CHANNEL_ID)
    if ch is None:
        print("Daily report channel not found.")
        return

    try:
        data = await build_daily_difference_report()
        await ch.send(data["full"][:1900])
        print("Daily report sent.")
    except Exception as e:
        print(f"Daily report failed: {e}")


@daily.before_loop
async def before_daily():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def milestone_check():
    channel = bot.get_channel(MILESTONE_CHANNEL_ID)
    if channel is None:
        print("Milestone channel not found.")
        return

    games = load_games()
    if not games:
        return

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz(session, game["universe_id"])
            if not data:
                continue

            ccu = int(data["playing"])

            for milestone, emoji in sorted(MILESTONES.items()):
                if ccu >= milestone and not milestone_exists(game["universe_id"], milestone):
                    await channel.send(
                        f"{emoji} {PROJECT_NAME} hit {milestone:,} CCU with {data['name']}"
                    )
                    save_milestone(game["universe_id"], milestone)


@milestone_check.before_loop
async def before_milestone_check():
    await bot.wait_until_ready()


# ---------------- START ----------------

@bot.event
async def on_ready():
    init_db()
    synced = await bot.tree.sync()
    print(f"READY - synced {len(synced)} slash command(s)")

    if not daily.is_running():
        daily.start()

    if not milestone_check.is_running():
        milestone_check.start()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    bot.run(TOKEN)
