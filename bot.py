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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            universe_id BIGINT NOT NULL,
            snapshot_date DATE NOT NULL,
            visits BIGINT NOT NULL,
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
            "universe_id": int(row[0]),
            "game_link": str(row[1]),
            "robux_per_visit": float(row[2]),
        }
        for row in rows
    ]


def add_game_to_db(universe_id: int, game_link: str, robux_per_visit: float):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO games (universe_id, game_link, robux_per_visit)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id)
            DO UPDATE SET
                game_link = EXCLUDED.game_link,
                robux_per_visit = EXCLUDED.robux_per_visit
        """, (universe_id, game_link, robux_per_visit))


def remove_game_by_universe_id(universe_id: int):
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM games WHERE universe_id = %s", (universe_id,))
        cur.execute("DELETE FROM daily_snapshots WHERE universe_id = %s", (universe_id,))


def save_snapshot(universe_id: int, snapshot_date: datetime.date, visits: int):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO daily_snapshots (universe_id, snapshot_date, visits)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id, snapshot_date)
            DO UPDATE SET visits = EXCLUDED.visits
        """, (universe_id, snapshot_date, visits))


def get_snapshot(universe_id: int, snapshot_date: datetime.date):
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT visits
            FROM daily_snapshots
            WHERE universe_id = %s AND snapshot_date = %s
        """, (universe_id, snapshot_date))
        row = cur.fetchone()
    return int(row[0]) if row else None


# ---------------- HELPERS ----------------

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


def now_local():
    return datetime.datetime.now(ZoneInfo(TIMEZONE))


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

        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else f"Game {universe_id}"
        title = re.sub(r"\s*[-—]\s*RoRizz\s*$", "", title).strip()

        clean_text = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        clean_text = re.sub(r"<style.*?</style>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
        clean_text = re.sub(r"<[^>]+>", " ", clean_text)
        clean_text = re.sub(r"&nbsp;|&#160;", " ", clean_text)
        clean_text = re.sub(r"&amp;", "&", clean_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        def extract_stat(label: str):
            pattern = rf"(\d[\d,]*(?:\.\d+)?[KMBkmb]?)\s+{re.escape(label)}\b"
            match = re.search(pattern, clean_text, re.IGNORECASE)
            if not match:
                return None
            return parse_compact_number(match.group(1))

        visits = extract_stat("Visits")
        playing = extract_stat("Playing")

        if visits is None:
            m = re.search(r'"visits"\s*:\s*(\d+)', html, re.IGNORECASE)
            if m:
                visits = int(m.group(1))

        if playing is None:
            m = re.search(r'"playing"\s*:\s*(\d+)', html, re.IGNORECASE)
            if m:
                playing = int(m.group(1))

        if visits is None:
            visits = 0
        if playing is None:
            playing = 0

        if visits == 0 and playing == 0:
            print(f"RoRizz returned page but no usable stats for {universe_id}")
            return None

        return {
            "name": title,
            "visits": int(visits),
            "playing": int(playing),
        }

    except Exception as e:
        print(f"RoRizz error for {universe_id}: {e}")
        return None


# ---------------- PANEL UI ----------------

class AddGameModal(discord.ui.Modal, title="Add RoRizz Game"):
    game_link = discord.ui.TextInput(
        label="RoRizz game link",
        placeholder="https://rorizz.com/g/9358783717/your-game",
        max_length=300,
    )

    robux_per_visit = discord.ui.TextInput(
        label="Robux per visit",
        placeholder="0.25",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        link = str(self.game_link).strip()
        universe_id = extract_rorizz_universe_id(link)

        if not universe_id:
            await interaction.response.send_message(
                "❌ Invalid RoRizz link.",
                ephemeral=True,
            )
            return

        try:
            robux_per_visit_value = float(str(self.robux_per_visit).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Robux per visit must be a number.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with aiohttp.ClientSession() as session:
            data = await fetch_rorizz(session, universe_id)

        if not data:
            await interaction.followup.send(
                "❌ Could not fetch this game from RoRizz.",
                ephemeral=True,
            )
            return

        add_game_to_db(universe_id, link, robux_per_visit_value)

        await interaction.followup.send(
            f"✅ Added **{data['name']}**\n"
            f"🆔 `{universe_id}`\n"
            f"💰 Robux per visit: `{robux_per_visit_value}`",
            ephemeral=True,
        )


class RemoveGameSelect(discord.ui.Select):
    def __init__(self):
        games = load_games()

        if not games:
            options = [discord.SelectOption(label="No games", value="none")]
            disabled = True
        else:
            options = [
                discord.SelectOption(
                    label=str(g["universe_id"]),
                    value=str(g["universe_id"]),
                    description=g["game_link"][:100],
                )
                for g in games
            ]
            disabled = False

        super().__init__(
            placeholder="Select game to remove",
            options=options,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No games to remove.", ephemeral=True)
            return

        universe_id = int(self.values[0])
        remove_game_by_universe_id(universe_id)
        await interaction.response.send_message("🗑️ Game removed.", ephemeral=True)


class RemoveGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(RemoveGameSelect())


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Add Game", style=discord.ButtonStyle.success, custom_id="add_game_btn")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddGameModal())

    @discord.ui.button(label="Remove Game", style=discord.ButtonStyle.danger, custom_id="remove_game_btn")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a game:",
            view=RemoveGameView(),
            ephemeral=True,
        )

    @discord.ui.button(label="List Games", style=discord.ButtonStyle.primary, custom_id="list_games_btn")
    async def list_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        games = load_games()

        if not games:
            await interaction.response.send_message("No games added.", ephemeral=True)
            return

        lines = []
        for i, g in enumerate(games, start=1):
            lines.append(
                f"**{i}.** {g['game_link']}\n"
                f"🆔 `{g['universe_id']}`\n"
                f"💰 Robux per visit: `{g['robux_per_visit']}`"
            )

        await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)


# ---------------- REPORT LOGIC ----------------

async def build_daily_earned_message():
    games = load_games()
    if not games:
        return "📭 No tracked games added yet."

    today = now_local().date()
    yesterday = today - datetime.timedelta(days=1)

    total_usd = 0.0

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz(session, game["universe_id"])
            if not data:
                continue

            visits_today = int(data["visits"])
            save_snapshot(game["universe_id"], today, visits_today)

            visits_yesterday = get_snapshot(game["universe_id"], yesterday)
            if visits_yesterday is None:
                continue

            diff = max(0, visits_today - visits_yesterday)
            earned_robux = diff * game["robux_per_visit"]
            earned_usd = earned_robux * USD_PER_ROBUX
            total_usd += earned_usd

    rounded_usd = int(round(total_usd))
    return f"🏆 {PROJECT_NAME} just earned ${rounded_usd:,}"


async def build_previous_day_breakdown():
    games = load_games()
    if not games:
        return "📭 No tracked games added yet."

    today = now_local().date()
    yesterday = today - datetime.timedelta(days=1)
    previous_day = today - datetime.timedelta(days=2)

    total_visits = 0
    total_robux = 0
    total_usd = 0.0
    lines = []

    async with aiohttp.ClientSession() as session:
        for game in games:
            visits_yesterday = get_snapshot(game["universe_id"], yesterday)
            visits_previous = get_snapshot(game["universe_id"], previous_day)

            game_name = f"Game {game['universe_id']}"
            data = await fetch_rorizz(session, game["universe_id"])
            if data and data.get("name"):
                game_name = data["name"]

            if visits_yesterday is None or visits_previous is None:
                lines.append(
                    f"• **{game_name}**: missing snapshot for {previous_day} or {yesterday}"
                )
                continue

            diff = max(0, visits_yesterday - visits_previous)
            earned_robux = int(round(diff * game["robux_per_visit"]))
            earned_usd = earned_robux * USD_PER_ROBUX

            total_visits += diff
            total_robux += earned_robux
            total_usd += earned_usd

            lines.append(
                f"• **{game_name}** | +{diff:,} visits | {earned_robux:,} robux | ${earned_usd:,.2f}"
            )

    header = (
        f"📊 **Yesterday breakdown**\n"
        f"**{previous_day} → {yesterday}**\n\n"
        f"• Total visits: **{total_visits:,}**\n"
        f"• Total robux: **{total_robux:,}**\n"
        f"• Total USD: **${total_usd:,.2f}**\n\n"
        f"**Per game**\n"
    )

    return header + "\n".join(lines)


# ---------------- COMMANDS ----------------

@bot.tree.command(name="panel", description="Open control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="RoRizz Report Control Panel",
        description=(
            "Add or remove tracked RoRizz games.\n\n"
            "For each game, enter:\n"
            "• RoRizz link\n"
            "• Robux per visit"
        ),
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


@bot.tree.command(name="reportnow", description="Preview today's earnings message")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg = await build_daily_earned_message()
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="prev", description="Show yesterday vs previous day earnings breakdown")
async def prev(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg = await build_previous_day_breakdown()
        await interaction.followup.send(msg[:1900], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


# ---------------- DAILY TASK ----------------

@tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=ZoneInfo(TIMEZONE)))
async def daily_report():
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if channel is None:
        print("Daily report channel not found.")
        return

    try:
        msg = await build_daily_earned_message()
        await channel.send(msg)
        print("Daily report sent.")
    except Exception as e:
        print(f"Daily report failed: {e}")


@daily_report.before_loop
async def before_daily_report():
    await bot.wait_until_ready()


# ---------------- START ----------------

@bot.event
async def on_ready():
    init_db()
    bot.add_view(PanelView())
    synced = await bot.tree.sync()
    print(f"READY - synced {len(synced)} slash command(s)")

    if not daily_report.is_running():
        daily_report.start()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    bot.run(TOKEN)
