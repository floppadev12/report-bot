import os
import json
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

DATA_FILE = Path("games.json")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def load_games() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except Exception:
        return []


def save_games(games: list[dict]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, ensure_ascii=False)


class AddGameModal(discord.ui.Modal, title="Add Roblox Game"):
    project_name = discord.ui.TextInput(
        label="Project name",
        placeholder="Project Floppa",
        max_length=100,
    )

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

    async def on_submit(self, interaction: discord.Interaction) -> None:
        games = load_games()

        try:
            rpv = float(str(self.robux_per_visit).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Robux per visit must be a number, like `0.25`.",
                ephemeral=True,
            )
            return

        link = str(self.game_link).strip()
        name = str(self.project_name).strip()

        games.append(
            {
                "project_name": name,
                "game_link": link,
                "robux_per_visit": rpv,
            }
        )
        save_games(games)

        await interaction.response.send_message(
            f"✅ Added **{name}**\n"
            f"🔗 {link}\n"
            f"💰 Robux per visit: **{rpv}**",
            ephemeral=True,
        )


class RemoveGameSelect(discord.ui.Select):
    def __init__(self):
        games = load_games()

        if not games:
            options = [
                discord.SelectOption(
                    label="No games saved",
                    value="none",
                    description="Add a game first",
                )
            ]
            disabled = True
        else:
            options = []
            for i, game in enumerate(games):
                options.append(
                    discord.SelectOption(
                        label=game["project_name"][:100],
                        value=str(i),
                        description=str(game["game_link"])[:100],
                    )
                )
            disabled = False

        super().__init__(
            placeholder="Choose a game to remove",
            options=options,
            min_values=1,
            max_values=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message(
                "❌ No games to remove.",
                ephemeral=True,
            )
            return

        games = load_games()
        index = int(self.values[0])

        if index < 0 or index >= len(games):
            await interaction.response.send_message(
                "❌ That game was not found.",
                ephemeral=True,
            )
            return

        removed = games.pop(index)
        save_games(games)

        await interaction.response.send_message(
            f"🗑️ Removed **{removed['project_name']}**",
            ephemeral=True,
        )


class RemoveGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(RemoveGameSelect())


class ControlPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Add Game", style=discord.ButtonStyle.success, custom_id="add_game_btn")
    async def add_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddGameModal())

    @discord.ui.button(label="Remove Game", style=discord.ButtonStyle.danger, custom_id="remove_game_btn")
    async def remove_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a game to remove:",
            view=RemoveGameView(),
            ephemeral=True,
        )

    @discord.ui.button(label="List Games", style=discord.ButtonStyle.primary, custom_id="list_games_btn")
    async def list_games_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        games = load_games()

        if not games:
            await interaction.response.send_message(
                "📭 No games saved yet.",
                ephemeral=True,
            )
            return

        lines = []
        for i, game in enumerate(games, start=1):
            lines.append(
                f"**{i}. {game['project_name']}**\n"
                f"🔗 {game['game_link']}\n"
                f"💰 Robux per visit: **{game['robux_per_visit']}**"
            )

        message = "\n\n".join(lines)

        await interaction.response.send_message(
            message[:1900],
            ephemeral=True,
        )


@bot.event
async def on_ready():
    bot.add_view(ControlPanelView())

    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


@bot.tree.command(name="panel", description="Post the Roblox game control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Roblox Report Control Panel",
        description=(
            "Use the buttons below to manage tracked games.\n\n"
            "• Add a game\n"
            "• Remove a game\n"
            "• List saved games"
        ),
    )
    await interaction.response.send_message(embed=embed, view=ControlPanelView())


@bot.tree.command(name="ping", description="Test if the bot is working")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    bot.run(TOKEN)
