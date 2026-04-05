import os
import json
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

# 👇 Set your project name ONCE here
PROJECT_NAME = "Project Floppa"

DATA_FILE = Path("games.json")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def load_games():
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def save_games(games):
    with open(DATA_FILE, "w") as f:
        json.dump(games, f, indent=2)


class AddGameModal(discord.ui.Modal, title="Add Roblox Game"):
    game_link = discord.ui.TextInput(
        label="Game link",
        placeholder="https://www.roblox.com/games/123456789/your-game",
    )

    robux_per_visit = discord.ui.TextInput(
        label="Robux per visit",
        placeholder="0.25",
    )

    async def on_submit(self, interaction: discord.Interaction):
        games = load_games()

        try:
            rpv = float(str(self.robux_per_visit))
        except:
            await interaction.response.send_message(
                "❌ Robux per visit must be a number",
                ephemeral=True,
            )
            return

        games.append({
            "game_link": str(self.game_link),
            "robux_per_visit": rpv
        })

        save_games(games)

        await interaction.response.send_message(
            f"✅ Game added\n"
            f"🔗 {self.game_link}\n"
            f"💰 {rpv} robux/visit",
            ephemeral=True,
        )


class RemoveGameSelect(discord.ui.Select):
    def __init__(self):
        games = load_games()

        options = []
        for i, game in enumerate(games):
            options.append(
                discord.SelectOption(
                    label=f"Game {i+1}",
                    value=str(i),
                    description=game["game_link"][:100],
                )
            )

        super().__init__(
            placeholder="Select game to remove",
            options=options if options else [
                discord.SelectOption(label="No games", value="none")
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No games.", ephemeral=True)
            return

        games = load_games()
        removed = games.pop(int(self.values[0]))
        save_games(games)

        await interaction.response.send_message(
            f"🗑️ Removed game:\n{removed['game_link']}",
            ephemeral=True
        )


class RemoveGameView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(RemoveGameSelect())


class PanelView(discord.ui.View):

    @discord.ui.button(label="Add Game", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddGameModal())

    @discord.ui.button(label="Remove Game", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a game:",
            view=RemoveGameView(),
            ephemeral=True
        )

    @discord.ui.button(label="List Games", style=discord.ButtonStyle.primary)
    async def list_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        games = load_games()

        if not games:
            await interaction.response.send_message("No games added.", ephemeral=True)
            return

        msg = ""
        for i, g in enumerate(games):
            msg += f"**{i+1}.** {g['game_link']}\n💰 {g['robux_per_visit']} robux/visit\n\n"

        await interaction.response.send_message(msg[:1900], ephemeral=True)


@bot.event
async def on_ready():
    bot.add_view(PanelView())

    await bot.tree.sync()

    print(f"Logged in as {bot.user}")


@bot.tree.command(name="panel", description="Open control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Roblox Control Panel",
        description="Manage your tracked games"
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


bot.run(TOKEN)
