import os
import re
import json
import html as html_lib
import datetime
from zoneinfo import ZoneInfo

import aiohttp
import discord
import psycopg2
from dotenv import load_dotenv
from aiohttp import web
from discord.ext import commands, tasks

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "f123")
DASHBOARD_COOKIE_NAME = "clover_dashboard_auth"
DASHBOARD_COOKIE_VALUE = os.getenv("DASHBOARD_COOKIE_VALUE", "clover-dashboard-ok")

PROJECT_NAME = "Project Floppa"
REPORT_CHANNEL_ID = 1490317756136947942
TIMEZONE = "Europe/Bratislava"
USD_PER_ROBUX = 0.0038
EMBED_COLOR = discord.Color.from_rgb(255, 255, 255)

try:
    tz = ZoneInfo(TIMEZONE)
except Exception:
    tz = datetime.timezone.utc

INITIAL_DAILY_REPORTS = [
    (datetime.date(2026, 6, 1), 251),
    (datetime.date(2026, 6, 2), 438),
    (datetime.date(2026, 6, 3), 337),
    (datetime.date(2026, 6, 4), 393),
    (datetime.date(2026, 6, 5), 520),
    (datetime.date(2026, 6, 6), 694),
    (datetime.date(2026, 6, 7), 885),
    (datetime.date(2026, 6, 8), 570),
    (datetime.date(2026, 6, 9), 500),
]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

conn = None
web_runner = None


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
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date DATE PRIMARY KEY,
            usd_amount INTEGER NOT NULL,
            message_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)


def seed_daily_reports():
    with get_conn().cursor() as cur:
        for report_date, usd_amount in INITIAL_DAILY_REPORTS:
            cur.execute(
                """
                INSERT INTO daily_reports (report_date, usd_amount)
                VALUES (%s, %s)
                ON CONFLICT (report_date)
                DO UPDATE SET usd_amount = EXCLUDED.usd_amount
                """,
                (report_date, usd_amount),
            )


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


def save_daily_report(report_date: datetime.date, usd_amount: int, message_text: str | None = None):
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_reports (report_date, usd_amount, message_text)
            VALUES (%s, %s, %s)
            ON CONFLICT (report_date)
            DO UPDATE SET
                usd_amount = EXCLUDED.usd_amount,
                message_text = EXCLUDED.message_text
            """,
            (report_date, usd_amount, message_text),
        )


def load_daily_reports(start_date: datetime.date, end_date: datetime.date):
    with get_conn().cursor() as cur:
        cur.execute(
            """
            SELECT report_date, usd_amount
            FROM daily_reports
            WHERE report_date >= %s AND report_date < %s
            ORDER BY report_date ASC
            """,
            (start_date, end_date),
        )
        rows = cur.fetchall()

    return [{"report_date": row[0], "usd_amount": int(row[1])} for row in rows]


def load_latest_report_date():
    with get_conn().cursor() as cur:
        cur.execute("SELECT MAX(report_date) FROM daily_reports")
        row = cur.fetchone()

    return row[0] if row and row[0] else None


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
    return datetime.datetime.now(tz)


def format_robux(value: float) -> str:
    return f"{int(round(value)):,}"


def chart_label_for_date(d: datetime.date) -> str:
    return d.strftime("%b %d")


def normalize_chart_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip())


def extract_title(page_html: str, universe_id: int) -> str:
    title_match = re.search(r"<title>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else f"Game {universe_id}"
    title = re.sub(r"\s*[-â€”]\s*RoRizz\s*$", "", title).strip()
    return html_lib.unescape(title)


def extract_current_stat(page_html: str, label: str):
    clean_text = re.sub(r"<script.*?</script>", " ", page_html, flags=re.IGNORECASE | re.DOTALL)
    clean_text = re.sub(r"<style.*?</style>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
    clean_text = re.sub(r"<[^>]+>", " ", clean_text)
    clean_text = html_lib.unescape(clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    pattern = rf"(\d[\d,]*(?:\.\d+)?[KMBkmb]?)\s+{re.escape(label)}\b"
    match = re.search(pattern, clean_text, re.IGNORECASE)
    if match:
        return parse_compact_number(match.group(1))

    json_match = re.search(rf'"{label.lower()}"\s*:\s*(\d+)', page_html, re.IGNORECASE)
    if json_match:
        return int(json_match.group(1))

    return None


def parse_data_chart_attribute(raw_chart: str):
    """
    raw_chart example:
    [{&quot;value&quot;:231529929,&quot;time&quot;:&quot;Mar 26&quot;}, ...]
    """
    try:
        decoded = html_lib.unescape(raw_chart)
        points = json.loads(decoded)
        if isinstance(points, list):
            return points
    except Exception:
        return None
    return None


def extract_visits_chart_points(page_html: str):
    """
    Looks specifically for the Visits (30d) chart block and reads its data-chart attribute.
    """
    patterns = [
        r'Visits\s*\(30d\).*?data-chart="([^"]+)"',
        r'Visits\s*\(30d\).*?data-chart=\'([^\']+)\'',
        r'data-chart="([^"]+)".{0,1200}?Visits\s*\(30d\)',
        r'data-chart=\'([^\']+)\'.{0,1200}?Visits\s*\(30d\)',
    ]

    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
        if not match:
            continue

        points = parse_data_chart_attribute(match.group(1))
        if points:
            return points

    # fallback: try every data-chart attribute and choose the most likely visits one
    for match in re.finditer(r'data-chart="([^"]+)"', page_html, re.IGNORECASE | re.DOTALL):
        points = parse_data_chart_attribute(match.group(1))
        if not points or not isinstance(points, list):
            continue

        if all(isinstance(p, dict) and "value" in p and "time" in p for p in points):
            # visits chart usually has large cumulative values
            values = [p.get("value") for p in points if isinstance(p.get("value"), (int, float))]
            if values and max(values) > 100000:
                return points

    return None


def get_chart_value_for_day(points, day: datetime.date):
    if not points:
        return None

    wanted = normalize_chart_label(chart_label_for_date(day))

    for point in points:
        if not isinstance(point, dict):
            continue

        time_label = point.get("time")
        value = point.get("value")

        if time_label is None or value is None:
            continue

        if normalize_chart_label(str(time_label)) == wanted:
            try:
                return int(value)
            except Exception:
                return None

    return None


async def fetch_rorizz_chart_data(session: aiohttp.ClientSession, universe_id: int):
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

            page_html = await resp.text()

        title = extract_title(page_html, universe_id)
        visits = extract_current_stat(page_html, "Visits") or 0
        playing = extract_current_stat(page_html, "Playing") or 0
        visits_chart = extract_visits_chart_points(page_html)

        return {
            "name": title,
            "visits": int(visits),
            "playing": int(playing),
            "visits_chart": visits_chart,
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
                "âŒ Invalid RoRizz link.",
                ephemeral=True,
            )
            return

        try:
            robux_per_visit_value = float(str(self.robux_per_visit).strip())
        except ValueError:
            await interaction.response.send_message(
                "âŒ Robux per visit must be a number.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with aiohttp.ClientSession() as session:
            data = await fetch_rorizz_chart_data(session, universe_id)

        if not data:
            await interaction.followup.send(
                "âŒ Could not fetch this game from RoRizz.",
                ephemeral=True,
            )
            return

        add_game_to_db(universe_id, link, robux_per_visit_value)

        await interaction.followup.send(
            f"âœ… Added **{data['name']}**\n"
            f"ðŸ†” `{universe_id}`\n"
            f"ðŸ’° Robux per visit: `{robux_per_visit_value}`",
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
        await interaction.response.send_message("ðŸ—‘ï¸ Game removed.", ephemeral=True)


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
                f"ðŸ†” `{g['universe_id']}`\n"
                f"ðŸ’° Robux per visit: `{g['robux_per_visit']}`"
            )

        await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)


# ---------------- REPORT LOGIC ----------------

async def build_daily_earned_message_from_chart():
    games = load_games()
    if not games:
        return None, None

    today = now_local().date()
    yesterday = today - datetime.timedelta(days=1)
    previous_day = today - datetime.timedelta(days=2)

    total_usd = 0.0
    found_any = False

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz_chart_data(session, game["universe_id"])
            if not data or not data["visits_chart"]:
                continue

            yesterday_visits = get_chart_value_for_day(data["visits_chart"], yesterday)
            previous_visits = get_chart_value_for_day(data["visits_chart"], previous_day)

            if yesterday_visits is None or previous_visits is None:
                continue

            diff = max(0, yesterday_visits - previous_visits)
            earned_robux = diff * game["robux_per_visit"]
            earned_usd = earned_robux * USD_PER_ROBUX

            total_usd += earned_usd
            found_any = True

    if not found_any:
        return None, None

    rounded_usd = int(round(total_usd))
    return f"ðŸ† {PROJECT_NAME} just earned ${rounded_usd:,}", rounded_usd


async def build_previous_day_breakdown_from_chart():
    games = load_games()
    if not games:
        return "ðŸ“­ No tracked games added yet. Work harder."

    today = now_local().date()
    yesterday = today - datetime.timedelta(days=1)
    previous_day = today - datetime.timedelta(days=2)

    total_visits = 0
    total_robux = 0
    total_usd = 0.0
    lines = []

    async with aiohttp.ClientSession() as session:
        for game in games:
            data = await fetch_rorizz_chart_data(session, game["universe_id"])
            game_name = data["name"] if data and data.get("name") else f"Game {game['universe_id']}"

            if not data:
                lines.append(f"â€¢ **{game_name}**: could not fetch data")
                continue

            if not data["visits_chart"]:
                lines.append(f"â€¢ **{game_name}**: could not find Visits (30d) chart data")
                continue

            yesterday_visits = get_chart_value_for_day(data["visits_chart"], yesterday)
            previous_visits = get_chart_value_for_day(data["visits_chart"], previous_day)

            if yesterday_visits is None or previous_visits is None:
                lines.append(
                    f"â€¢ **{game_name}**: could not read chart values for {previous_day} or {yesterday}"
                )
                continue

            diff = max(0, yesterday_visits - previous_visits)
            earned_robux = int(round(diff * game["robux_per_visit"]))
            earned_usd = earned_robux * USD_PER_ROBUX

            total_visits += diff
            total_robux += earned_robux
            total_usd += earned_usd

            lines.append(
                f"â€¢ **{game_name}** | +{diff:,} visits | {earned_robux:,} robux | ${earned_usd:,.2f}"
            )

    header = (
        f"ðŸ“Š **Yesterday breakdown**\n"
        f"**{previous_day} â†’ {yesterday}**\n\n"
        f"â€¢ Total visits: **{total_visits:,}**\n"
        f"â€¢ Total robux: **{total_robux:,}**\n"
        f"â€¢ Total USD: **${total_usd:,.2f}**\n\n"
        f"**Per game**\n"
    )

    return header + "\n".join(lines)


# ---------------- WEB DASHBOARD ----------------

def dashboard_is_authenticated(request):
    return request.cookies.get(DASHBOARD_COOKIE_NAME) == DASHBOARD_COOKIE_VALUE


def login_html(error: str | None = None):
    error_markup = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Clover Dashboard Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box}}
  body{{
    margin:0;
    min-height:100vh;
    display:grid;
    place-items:center;
    background:#f2f4f7;
    color:#272b30;
    font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  }}
  .card{{
    width:min(390px,calc(100vw - 32px));
    background:#fff;
    border:1px solid #d8d8d8;
    border-radius:12px;
    box-shadow:0 2px 0 rgba(0,0,0,.55),0 12px 28px rgba(0,0,0,.08);
    padding:28px;
  }}
  h1{{margin:0;font-size:25px;line-height:1;font-weight:760;letter-spacing:-.3px}}
  p{{margin:10px 0 24px;color:#6b7178;font-size:14px}}
  label{{display:block;margin-bottom:8px;color:#555b62;font-size:14px;font-weight:650}}
  input{{
    width:100%;
    height:44px;
    border:1px solid #d8d8d8;
    border-radius:8px;
    padding:0 12px;
    font:inherit;
    outline:none;
  }}
  input:focus{{border-color:#22aee8;box-shadow:0 0 0 3px rgba(34,174,232,.15)}}
  button{{
    width:100%;
    height:44px;
    margin-top:14px;
    border:0;
    border-radius:8px;
    background:#22aee8;
    color:#fff;
    font:inherit;
    font-weight:730;
    cursor:pointer;
  }}
  .error{{
    margin-bottom:14px;
    padding:10px 12px;
    border-radius:8px;
    background:#fdecec;
    color:#b42318;
    font-size:14px;
    font-weight:650;
  }}
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>Clover Revenue</h1>
    <p>Enter the dashboard password.</p>
    {error_markup}
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autofocus required />
    <button type="submit">Open dashboard</button>
  </form>
</body>
</html>"""


async def login_page(request):
    return web.Response(text=login_html(), content_type="text/html")


async def login_submit(request):
    form = await request.post()
    password = str(form.get("password", ""))

    if password != DASHBOARD_PASSWORD:
        return web.Response(
            text=login_html("Wrong password."),
            content_type="text/html",
            status=401,
        )

    response = web.HTTPFound("/")
    response.set_cookie(
        DASHBOARD_COOKIE_NAME,
        DASHBOARD_COOKIE_VALUE,
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


def serialize_report(row):
    return {
        "date": row["report_date"].isoformat(),
        "amount": row["usd_amount"],
    }


def summarize_reports(current_reports, previous_reports):
    total = sum(item["usd_amount"] for item in current_reports)
    previous_total = sum(item["usd_amount"] for item in previous_reports)
    average = round(total / len(current_reports)) if current_reports else 0
    best = max(current_reports, key=lambda item: item["usd_amount"]) if current_reports else None

    growth = None
    if previous_total > 0:
        growth = ((total - previous_total) / previous_total) * 100

    return {
        "total": total,
        "average": average,
        "best": serialize_report(best) if best else None,
        "growth": growth,
        "count": len(current_reports),
        "previousTotal": previous_total,
    }


async def revenue_api(request):
    if not dashboard_is_authenticated(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        days = int(request.query.get("days", "14"))
    except ValueError:
        days = 14

    days = max(1, min(days, 30))
    latest_date = load_latest_report_date()

    if latest_date is None:
        return web.json_response({
            "project": PROJECT_NAME,
            "days": days,
            "current": [],
            "previous": [],
            "gamesCount": len(load_games()),
            "summary": summarize_reports([], []),
        })

    current_start = latest_date - datetime.timedelta(days=days - 1)
    current_end = latest_date + datetime.timedelta(days=1)
    previous_start = current_start - datetime.timedelta(days=days)

    current_reports = load_daily_reports(current_start, current_end)
    previous_reports = load_daily_reports(previous_start, current_start)
    all_reports = load_daily_reports(datetime.date(1970, 1, 1), current_end)

    return web.json_response({
        "project": PROJECT_NAME,
        "days": days,
        "latestDate": latest_date.isoformat(),
        "current": [serialize_report(item) for item in current_reports],
        "previous": [serialize_report(item) for item in previous_reports],
        "all": [serialize_report(item) for item in all_reports],
        "gamesCount": len(load_games()),
        "summary": summarize_reports(current_reports, previous_reports),
    })


async def dashboard_page(request):
    if not dashboard_is_authenticated(request):
        raise web.HTTPFound("/login")

    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return web.FileResponse(path)


async def healthcheck(request):
    return web.json_response({"ok": True})


async def start_web_server():
    global web_runner
    if web_runner is not None:
        return

    app = web.Application()
    app.router.add_get("/", dashboard_page)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/api/revenue", revenue_api)
    app.router.add_get("/health", healthcheck)

    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Dashboard server running on port {PORT}")


# ---------------- COMMANDS ----------------

@bot.tree.command(name="panel", description="Open control panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="RoRizz Report Control Panel",
        description=(
            "Add or remove tracked RoRizz games.\n\n"
            "For each game, enter:\n"
            "â€¢ RoRizz link\n"
            "â€¢ Robux per visit"
        ),
        color=EMBED_COLOR,
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("âœ… Bot is working!")


@bot.tree.command(name="reportnow", description="Preview today's earnings message")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg, usd_amount = await build_daily_earned_message_from_chart()
        if not msg or usd_amount is None:
            await interaction.followup.send("âš ï¸ Could not build today's earnings message.", ephemeral=True)
            return

        report_date = now_local().date() - datetime.timedelta(days=1)
        save_daily_report(report_date, usd_amount, msg)
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"âŒ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="prev", description="Show yesterday vs previous day earnings breakdown")
async def prev(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg = await build_previous_day_breakdown_from_chart()
        await interaction.followup.send(msg[:1900], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"âŒ Failed: `{e}`", ephemeral=True)


# ---------------- DAILY TASK ----------------

@tasks.loop(time=datetime.time(hour=8, minute=30, tzinfo=tz))
async def daily_report():
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if channel is None:
        print("Daily report channel not found.")
        return

    try:
        msg, usd_amount = await build_daily_earned_message_from_chart()
        if not msg or usd_amount is None:
            print("Daily report could not be built.")
            return

        report_date = now_local().date() - datetime.timedelta(days=1)
        save_daily_report(report_date, usd_amount, msg)
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
    seed_daily_reports()
    bot.add_view(PanelView())
    await start_web_server()
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
