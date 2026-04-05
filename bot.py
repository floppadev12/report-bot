import os
import re
import json
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")

# =========================
# CONFIG
# =========================
PROJECT_NAME = "Project Floppa"
REPORT_CHANNEL_ID = 1490317756136947942  # <-- replace with your Discord channel ID
USD_PER_ROBUX = 0.0038
REPORT_TIMEZONE = "Europe/Bratislava"
REPORT_HOUR = 22
REPORT_MINUTE = 0
# =========================

DATA_FILE = Path("games.json")
STATE_FILE = Path("visits_state.json")

ROBLOX_GAMES_API = "https://games.roblox.com/v1/games"
ROBLOX_UNIVERSE_API = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------
# FILE HELPERS
# ---------------------------

def load_games():
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_games(games):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, ensure_ascii=False)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


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
    async with session.get(ROBLOX_GAMES_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
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

        games = load_games()

        for game in games:
            if game.get("universe_id") == universe_id:
                await interaction.response.send_message(
                    "❌ That game is already added.",
                    ephemeral=True,
                )
                return

        games.append({
            "game_link": link,
            "place_id": place_id,
            "universe_id": universe_id,
            "robux_per_visit": rpv,
        })
        save_games(games)

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
                        value=str(i),
                        description=str(game.get("game_link", ""))[:100],
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

        games = load_games()
        index = int(self.values[0])

        if index < 0 or index >= len(games):
            await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)
            return

        removed = games.pop(index)
        save_games(games)

        await interaction.response.send_message(
            f"🗑️ Removed game:\n{removed['game_link']}",
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
        return "📭 No tracked games added yet."

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
    updated_state = {}

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

        updated_state[str(universe_id)] = {
            "visits": current_visits,
            "name": name,
            "last_report_date": date_str,
        }

    updated_state["_meta"] = {"last_report_date": date_str}
    save_state(updated_state)

    total_usd = total_robux * USD_PER_ROBUX

    report = (
        f"🏆 **{PROJECT_NAME} just earned ${total_usd:,.2f}**\n\n"
        f"**Past 24 hours**\n"
        f"• Total gained visits: **{total_new_visits:,}**\n"
        f"• Total earned robux: **{total_robux:,.2f}**\n"
        f"• USD per robux: **${USD_PER_ROBUX:.4f}**\n\n"
        f"**Tracked games**\n"
        + "\n".join(per_game_lines)
    )

    return report


@tasks.loop(time=datetime.time(hour=REPORT_HOUR, minute=REPORT_MINUTE, tzinfo=ZoneInfo(REPORT_TIMEZONE)))
async def daily_report():
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if channel is None:
        print("Report channel not found. Check REPORT_CHANNEL_ID.")
        return

    try:
        report = await build_report()
        await channel.send(report)
        print("Daily report sent.")
    except Exception as e:
        print(f"Failed to send daily report: {e}")


@daily_report.before_loop
async def before_daily_report():
    await bot.wait_until_ready()


# ---------------------------
# BOT EVENTS / COMMANDS
# ---------------------------

@bot.event
async def on_ready():
    bot.add_view(PanelView())

    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Synced {len(synced)} slash command(s)")

    if not daily_report.is_running():
        daily_report.start()


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


@bot.tree.command(name="reportnow", description="Send the earnings report right now")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        report = await build_report()
        channel = bot.get_channel(REPORT_CHANNEL_ID)

        if channel is None:
            await interaction.followup.send(
                "❌ Report channel not found. Check REPORT_CHANNEL_ID.",
                ephemeral=True,
            )
            return

        await channel.send(report)
        await interaction.followup.send("✅ Report sent.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to build report: `{e}`", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    bot.run(TOKEN)
