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
        # Main games table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            universe_id BIGINT PRIMARY KEY,
            game_link TEXT NOT NULL,
            robux_per_visit FLOAT NOT NULL
        );
        """)

        # Migration safety for old schema that had place_id NOT NULL
        cur.execute("""
        ALTER TABLE games
        ADD COLUMN IF NOT EXISTS place_id BIGINT;
        """)
        cur.execute("""
        ALTER TABLE games
        ALTER COLUMN place_id DROP NOT NULL;
        """)

        cur.execute("""
        ALTER TABLE games
        ADD COLUMN IF NOT EXISTS robux_per_visit FLOAT;
        """)
        cur.execute("""
        UPDATE games
        SET robux_per_visit = 0
        WHERE robux_per_visit IS NULL;
        """)
        cur.execute("""
        ALTER TABLE games
        ALTER COLUMN robux_per_visit SET NOT NULL;
        """)

        # Baseline visits
        cur.execute("""
        CREATE TABLE IF NOT EXISTS visits_state (
            universe_id BIGINT PRIMARY KEY,
            visits BIGINT NOT NULL
        );
        """)

        # Milestone hits
        cur.execute("""
        CREATE TABLE IF NOT EXISTS milestone_hits (
            universe_id BIGINT NOT NULL,
            milestone BIGINT NOT NULL,
            PRIMARY KEY (universe_id, milestone)
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
            ON CONFLICT (universe_id) DO NOTHING
        """, (universe_id, game_link, robux_per_visit))


def remove_game_by_universe_id(universe_id: int):
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM games WHERE universe_id = %s", (universe_id,))


def get_previous_visits(universe_id: int):
    with get_conn().cursor() as cur:
        cur.execute("SELECT visits FROM visits_state WHERE universe_id = %s", (universe_id,))
        row = cur.fetchone()
    return int(row[0]) if row else None


def set_previous_visits(universe_id: int, visits: int):
    with get_conn().cursor() as cur:
        cur.execute("""
            INSERT INTO visits_state (universe_id, visits)
            VALUES (%s, %s)
            ON CONFLICT (universe_id)
            DO UPDATE SET visits = EXCLUDED.visits
        """, (universe_id, visits))


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
    if match:
        return int(match.group(1))
    return None


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


async def fetch_rorizz(session: aiohttp.ClientSession, universe_id: int):
    url = f"https://rorizz.com/g/{universe_id}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                print(f"RoRizz failed for {universe_id}: HTTP {resp.status}")
                return None

            html = await resp.text()

        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else f"Game {universe_id}"
        title = title.replace(" - RoRizz", "").strip()

        # Prefer JSON-like values first
        json_playing = re.search(r'"playing"\s*:\s*(\d+)', html, re.IGNORECASE)
        json_visits = re.search(r'"visits"\s*:\s*(\d+)', html, re.IGNORECASE)

        # Fallback text patterns
        text_playing = re.findall(r'([\d.,]+[KMBkmb]?)\s*Playing', html, re.IGNORECASE)
        text_visits = re.findall(r'([\d.,]+[KMBkmb]?)\s*Visits', html, re.IGNORECASE)

        if json_playing:
            playing = int(json_playing.group(1))
        elif text_playing:
            playing = max(parse_compact_number(x) for x in text_playing)
        else:
            playing = 0

        if json_visits:
            visits = int(json_visits.group(1))
        elif text_visits:
            visits = max(parse_compact_number(x) for x in text_visits)
        else:
            visits = 0

        if visits == 0 and playing == 0:
            print(f"RoRizz returned page but no usable stats for {universe_id}")
            return None

        return {
            "name": title,
            "playing": playing,
            "visits": visits,
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
                "❌ Please enter a valid RoRizz link like `https://rorizz.com/g/9358783717/...`",
                ephemeral=True,
            )
            return

        try:
            rpv = float(str(self.robux_per_visit).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Robux per visit must be a number.",
                ephemeral=True,
            )
            return

        existing = load_games()
        for game in existing:
            if game["universe_id"] == universe_id:
                await interaction.response.send_message(
                    "❌ That game is already added.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)

        # Optional live check before saving
        async with aiohttp.ClientSession() as session:
            data = await fetch_rorizz(session, universe_id)

        if not data:
            await interaction.followup.send(
                "❌ That RoRizz page did not return usable stats. Make sure the game exists on RoRizz first.",
                ephemeral=True,
            )
            return

        add_game_to_db(universe_id, link, rpv)

        await interaction.followup.send(
            f"✅ Game added\n"
            f"🔗 {link}\n"
            f"🆔 Universe ID: `{universe_id}`\n"
            f"🎮 Name: **{data['name']}**\n"
            f"💰 {rpv} robux/visit",
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
                    label=f"Game {i+1}",
                    value=str(game["universe_id"]),
                    description=game["game_link"][:100],
                )
                for i, game in enumerate(games)
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
                f"🆔 Universe ID: `{g['universe_id']}`\n"
                f"💰 {g['robux_per_visit']} robux/visit"
            )

        await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)


# ---------------- REPORTS ----------------

async def build_report(update_baseline: bool):
    games = load_games()
    if not games:
        return {
            "short": f"🏆 {PROJECT_NAME} just earned $0",
            "full": "📭 No tracked games added yet.",
        }

    total_visits = 0
    total_robux = 0.0
    lines = []

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz(session, game["universe_id"])

            if not data:
                lines.append(f"• **{game['game_link']}**: could not fetch data")
                continue

            visits_now = int(data["visits"])
            prev = get_previous_visits(game["universe_id"])

            if prev is None:
                prev = visits_now

            gained = max(0, visits_now - prev)
            earned_robux = gained * game["robux_per_visit"]

            total_visits += gained
            total_robux += earned_robux

            lines.append(
                f"• **{data['name']}**: +{gained:,} visits, {int(round(earned_robux)):,} robux"
            )

            if update_baseline:
                set_previous_visits(game["universe_id"], visits_now)

    total_revenue = total_robux * USD_PER_ROBUX

    full = (
        f"🏆 **{PROJECT_NAME} just earned ${total_revenue:,.2f}**\n\n"
        f"**Past 24 hours**\n"
        f"• Total gained visits: **{total_visits:,}**\n"
        f"• Total earned robux: **{int(round(total_robux)):,}**\n"
        f"• Revenue: **${total_revenue:,.2f}**\n\n"
        f"**Tracked games**\n"
        + "\n".join(lines)
    )

    short = f"🏆 {PROJECT_NAME} just earned ${int(round(total_revenue)):,}"

    return {
        "short": short,
        "full": full,
    }


# ---------------- COMMANDS ----------------

@bot.tree.command(name="panel", description="Open control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="RoRizz Report Control Panel",
        description=(
            "Manage your tracked games.\n\n"
            "Use **RoRizz game links only**.\n"
            "Example: `https://rorizz.com/g/9358783717/...`"
        ),
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


@bot.tree.command(name="reportnow", description="Show the detailed earnings report only to you")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        data = await build_report(update_baseline=False)
        await interaction.followup.send(data["full"][:1900], ephemeral=True)
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
            lines.append(f"• **{data['name']}**: {int(data['playing']):,} CCU")

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
        data = await build_report(update_baseline=True)
        await ch.send(data["short"])
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
    bot.add_view(PanelView())
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
