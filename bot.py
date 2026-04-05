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

# =========================
# CONFIG
# =========================
PROJECT_NAME = "Project Floppa"

REPORT_CHANNEL_ID = 1490317756136947942       # short auto report at 22:00
REPORTNOW_CHANNEL_ID = 1490325202792353963    # detailed /reportnow channel
MILESTONE_CHANNEL_ID = 1490329238841196584    # milestone alerts channel

USD_PER_ROBUX = 0.0038
REPORT_TIMEZONE = "Europe/Bratislava"
REPORT_HOUR = 22
REPORT_MINUTE = 0

MILESTONES = {
    1000: "🥉",
    5000: "🥈",
    10000: "🥇",
}
# =========================

ROBLOX_GAMES_API = "https://games.roblox.com/v1/games"
ROBLOX_UNIVERSE_API = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

conn = None


# ---------------------------
# DATABASE
# ---------------------------

def get_conn():
    global conn
    if conn is None or conn.closed:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
    return conn


def init_db():
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS games (
                universe_id BIGINT PRIMARY KEY,
                game_link TEXT NOT NULL,
                place_id BIGINT NOT NULL,
                robux_per_visit DOUBLE PRECISION NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS visits_state (
                universe_id BIGINT PRIMARY KEY,
                visits BIGINT NOT NULL,
                game_name TEXT,
                last_report_date TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ccu_milestones (
                universe_id BIGINT NOT NULL,
                milestone BIGINT NOT NULL,
                announced_at TEXT,
                PRIMARY KEY (universe_id, milestone)
            );
        """)


def load_games():
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            SELECT universe_id, game_link, place_id, robux_per_visit
            FROM games
            ORDER BY universe_id ASC
        """)
        rows = cur.fetchall()

    return [
        {
            "universe_id": int(row[0]),
            "game_link": row[1],
            "place_id": int(row[2]),
            "robux_per_visit": float(row[3]),
        }
        for row in rows
    ]


def add_game_to_db(universe_id: int, game_link: str, place_id: int, robux_per_visit: float):
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            INSERT INTO games (universe_id, game_link, place_id, robux_per_visit)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (universe_id) DO NOTHING
        """, (universe_id, game_link, place_id, robux_per_visit))


def remove_game_by_universe_id(universe_id: int):
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("DELETE FROM games WHERE universe_id = %s", (universe_id,))


def load_state():
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            SELECT universe_id, visits, game_name, last_report_date
            FROM visits_state
        """)
        rows = cur.fetchall()

    return {
        str(row[0]): {
            "visits": int(row[1]),
            "name": row[2],
            "last_report_date": row[3],
        }
        for row in rows
    }


def upsert_state(universe_id: int, visits: int, game_name: str, last_report_date: str):
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            INSERT INTO visits_state (universe_id, visits, game_name, last_report_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (universe_id)
            DO UPDATE SET
                visits = EXCLUDED.visits,
                game_name = EXCLUDED.game_name,
                last_report_date = EXCLUDED.last_report_date
        """, (universe_id, visits, game_name, last_report_date))


def milestone_already_sent(universe_id: int, milestone: int) -> bool:
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM ccu_milestones
            WHERE universe_id = %s AND milestone = %s
            LIMIT 1
        """, (universe_id, milestone))
        row = cur.fetchone()
    return row is not None


def mark_milestone_sent(universe_id: int, milestone: int, announced_at: str):
    connection = get_conn()
    with connection.cursor() as cur:
        cur.execute("""
            INSERT INTO ccu_milestones (universe_id, milestone, announced_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (universe_id, milestone) DO NOTHING
        """, (universe_id, milestone, announced_at))


# ---------------------------
# ROBLOX HELPERS
# ---------------------------

def extract_place_id(game_link: str):
    match = re.search(r"roblox\.com/games/(\d+)", game_link)
    if match:
        return int(match.group(1))
    return None


async def fetch_universe_id(session: aiohttp.ClientSession, place_id: int):
    url = ROBLOX_UNIVERSE_API.format(place_id=place_id)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("universeId")


async def fetch_games_data(session: aiohttp.ClientSession, universe_ids: list[int]):
    if not universe_ids:
        return []

    params = {"universeIds": ",".join(str(x) for x in universe_ids)}
    async with session.get(
        ROBLOX_GAMES_API,
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("data", [])


# ---------------------------
# PANEL UI
# ---------------------------

class AddGameModal(discord.ui.Modal, title="Add Roblox Game"):
    game_link = discord.ui.TextInput(
        label="Game link",
        placeholder="https://www.roblox.com/games/123456789/your-game",
        max_length=300,
    )

    robux_per_visit = discord.ui.TextInput(
        label="Robux per visit",
        placeholder="0.25",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        link = str(self.game_link).strip()

        try:
            rpv = float(str(self.robux_per_visit).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Robux per visit must be a number.",
                ephemeral=True,
            )
            return

        place_id = extract_place_id(link)
        if not place_id:
            await interaction.response.send_message(
                "❌ That Roblox link does not look valid.",
                ephemeral=True,
            )
            return

        try:
            async with aiohttp.ClientSession() as session:
                universe_id = await fetch_universe_id(session, place_id)
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Could not read this Roblox game.\nError: `{e}`",
                ephemeral=True,
            )
            return

        if not universe_id:
            await interaction.response.send_message(
                "❌ Could not find a universe ID for that game.",
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

        add_game_to_db(
            universe_id=universe_id,
            game_link=link,
            place_id=place_id,
            robux_per_visit=rpv,
        )

        await interaction.response.send_message(
            f"✅ Game added\n"
            f"🔗 {link}\n"
            f"🆔 Universe ID: `{universe_id}`\n"
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
            options = []
            for i, game in enumerate(games):
                options.append(
                    discord.SelectOption(
                        label=f"Game {i+1}",
                        value=str(game["universe_id"]),
                        description=str(game["game_link"])[:100],
                    )
                )
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

        await interaction.response.send_message(
            "🗑️ Game removed.",
            ephemeral=True,
        )


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


# ---------------------------
# REPORT LOGIC
# ---------------------------

async def build_report():
    games = load_games()
    if not games:
        return {
            "short": f"🏆 {PROJECT_NAME} just earned $0",
            "full": "📭 No tracked games added yet.",
        }

    state = load_state()
    tz = ZoneInfo(REPORT_TIMEZONE)
    now_local = datetime.datetime.now(tz)
    date_str = now_local.strftime("%Y-%m-%d")

    universe_ids = [g["universe_id"] for g in games]

    async with aiohttp.ClientSession() as session:
        api_games = await fetch_games_data(session, universe_ids)

    by_universe = {item["id"]: item for item in api_games}

    total_new_visits = 0
    total_robux = 0.0
    per_game_lines = []

    for game in games:
        universe_id = game["universe_id"]
        robux_per_visit = float(game["robux_per_visit"])
        item = by_universe.get(universe_id)

        if not item:
            per_game_lines.append(f"• `{universe_id}`: could not fetch data")
            continue

        current_visits = int(item.get("visits", 0))
        previous_visits = int(state.get(str(universe_id), {}).get("visits", current_visits))
        gained_visits = max(0, current_visits - previous_visits)

        earned_robux = gained_visits * robux_per_visit

        total_new_visits += gained_visits
        total_robux += earned_robux

        name = item.get("name", f"Game {universe_id}")
        per_game_lines.append(
            f"• **{name}**: +{gained_visits:,} visits, {earned_robux:,.2f} robux"
        )

        upsert_state(
            universe_id=universe_id,
            visits=current_visits,
            game_name=name,
            last_report_date=date_str,
        )

    total_usd = total_robux * USD_PER_ROBUX

    full_report = (
        f"🏆 **{PROJECT_NAME} just earned ${total_usd:,.2f}**\n\n"
        f"**Past 24 hours**\n"
        f"• Total gained visits: **{total_new_visits:,}**\n"
        f"• Total earned robux: **{total_robux:,.2f}**\n"
        f"• USD per robux: **${USD_PER_ROBUX:.4f}**\n\n"
        f"**Tracked games**\n"
        + "\n".join(per_game_lines)
    )

    short_report = f"🏆 {PROJECT_NAME} just earned ${int(round(total_usd)):,}"

    return {
        "short": short_report,
        "full": full_report,
    }


# ---------------------------
# AUTOMATIONS
# ---------------------------

@tasks.loop(time=datetime.time(hour=REPORT_HOUR, minute=REPORT_MINUTE, tzinfo=ZoneInfo(REPORT_TIMEZONE)))
async def daily_report():
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if channel is None:
        print("Report channel not found. Check REPORT_CHANNEL_ID.")
        return

    try:
        data = await build_report()
        await channel.send(data["short"])
        print("Daily short report sent.")
    except Exception as e:
        print(f"Failed to send daily report: {e}")


@daily_report.before_loop
async def before_daily_report():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def check_ccu_milestones():
    milestone_channel = bot.get_channel(MILESTONE_CHANNEL_ID)
    if milestone_channel is None:
        print("Milestone channel not found. Check MILESTONE_CHANNEL_ID.")
        return

    games = load_games()
    if not games:
        return

    universe_ids = [g["universe_id"] for g in games]

    try:
        async with aiohttp.ClientSession() as session:
            api_games = await fetch_games_data(session, universe_ids)
    except Exception as e:
        print(f"Failed to fetch CCU for milestones: {e}")
        return

    now_str = datetime.datetime.now(ZoneInfo(REPORT_TIMEZONE)).isoformat()
    by_universe = {item["id"]: item for item in api_games}

    for game in games:
        universe_id = game["universe_id"]
        item = by_universe.get(universe_id)
        if not item:
            continue

        game_name = item.get("name", f"Game {universe_id}")
        playing = int(item.get("playing", 0))

        for milestone, emoji in sorted(MILESTONES.items()):
            if playing >= milestone and not milestone_already_sent(universe_id, milestone):
                message = f"{emoji} {PROJECT_NAME} hit {milestone:,} CCU with {game_name}"
                try:
                    await milestone_channel.send(message)
                    mark_milestone_sent(universe_id, milestone, now_str)
                    print(f"Milestone sent: {game_name} -> {milestone}")
                except Exception as e:
                    print(f"Failed to send milestone for {game_name}: {e}")


@check_ccu_milestones.before_loop
async def before_check_ccu_milestones():
    await bot.wait_until_ready()


# ---------------------------
# COMMANDS
# ---------------------------

@bot.tree.command(name="panel", description="Open control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Roblox Report Control Panel",
        description="Manage your tracked games",
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


@bot.tree.command(name="reportnow", description="Send the detailed earnings report right now")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        data = await build_report()
        channel = bot.get_channel(REPORTNOW_CHANNEL_ID)

        if channel is None:
            await interaction.followup.send(
                "❌ Report-now channel not found. Check REPORTNOW_CHANNEL_ID.",
                ephemeral=True,
            )
            return

        await channel.send(data["full"])
        await interaction.followup.send("✅ Detailed report sent.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to build report: `{e}`", ephemeral=True)


@bot.tree.command(name="ccu", description="Show current total CCU and tracked games")
async def ccu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        games = load_games()
        if not games:
            await interaction.followup.send("📭 No tracked games added yet.", ephemeral=True)
            return

        universe_ids = [g["universe_id"] for g in games]

        async with aiohttp.ClientSession() as session:
            api_games = await fetch_games_data(session, universe_ids)

        by_universe = {item["id"]: item for item in api_games}

        total_ccu = 0
        lines = []

        for game in games:
            universe_id = game["universe_id"]
            item = by_universe.get(universe_id)

            if not item:
                lines.append(f"• `{universe_id}`: could not fetch data")
                continue

            name = item.get("name", f"Game {universe_id}")
            playing = int(item.get("playing", 0))

            total_ccu += playing
            lines.append(f"• **{name}**: {playing:,} CCU")

        message = (
            f"📈 {PROJECT_NAME} currently has {total_ccu:,} CCU\n\n"
            + "\n".join(lines)
        )

        await interaction.followup.send(message[:1900], ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to fetch CCU: `{e}`", ephemeral=True)


# ---------------------------
# BOT EVENTS
# ---------------------------

@bot.event
async def on_ready():
    init_db()
    bot.add_view(PanelView())

    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Synced {len(synced)} slash command(s)")

    if not daily_report.is_running():
        daily_report.start()

    if not check_ccu_milestones.is_running():
        check_ccu_milestones.start()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    bot.run(TOKEN)
