import io
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
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageFilter

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

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


def format_currency(value: int) -> str:
    return f"${value:,}"


def format_compact_currency(value: int) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"${value / 1_000:.1f}K".replace(".0K", "K")
    return f"${value:,}"


def format_day_label(day: datetime.date) -> str:
    return f"{day.strftime('%b')} {day.day}"


def chart_label_for_date(d: datetime.date) -> str:
    return d.strftime("%b %d")


def normalize_chart_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip())


def month_range_for(day: datetime.date):
    start = datetime.date(day.year, day.month, 1)
    if day.month == 12:
        end = datetime.date(day.year + 1, 1, 1)
    else:
        end = datetime.date(day.year, day.month + 1, 1)
    return start, end


def previous_month_range_for(day: datetime.date):
    if day.month == 1:
        prev_year = day.year - 1
        prev_month = 12
    else:
        prev_year = day.year
        prev_month = day.month - 1
    prev_day = datetime.date(prev_year, prev_month, 1)
    return month_range_for(prev_day)


def load_font(size: int, bold: bool = False):
    names = [
        "Arial Bold.ttf",
        "arialbd.ttf",
        "Segoe UI Bold.ttf",
        "LiberationSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "Arial.ttf",
        "arial.ttf",
        "Segoe UI.ttf",
        "LiberationSans-Regular.ttf",
        "DejaVuSans.ttf",
    ] if bold else [
        "Arial.ttf",
        "arial.ttf",
        "Segoe UI.ttf",
        "LiberationSans-Regular.ttf",
        "DejaVuSans.ttf",
        "Arial Bold.ttf",
        "arialbd.ttf",
        "Segoe UI Bold.ttf",
        "LiberationSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered_text(draw: ImageDraw.ImageDraw, box, text: str, font, fill):
    x0, y0, x1, y1 = box
    w, h = text_size(draw, text, font)
    draw.text(((x0 + x1 - w) / 2, (y0 + y1 - h) / 2), text, font=font, fill=fill)


def draw_dashed_line(draw: ImageDraw.ImageDraw, start, end, fill, width: int = 1, dash: int = 5, gap: int = 4):
    x1, y1 = start
    x2, y2 = end
    distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if distance == 0:
        return

    dx = (x2 - x1) / distance
    dy = (y2 - y1) / distance
    drawn = 0.0
    while drawn < distance:
        seg_end = min(drawn + dash, distance)
        sx1 = x1 + dx * drawn
        sy1 = y1 + dy * drawn
        sx2 = x1 + dx * seg_end
        sy2 = y1 + dy * seg_end
        draw.line((sx1, sy1, sx2, sy2), fill=fill, width=width)
        drawn += dash + gap


def draw_dashed_polyline(draw: ImageDraw.ImageDraw, points, fill, width: int = 1, dash: int = 4, gap: int = 4):
    if len(points) < 2:
        return

    segment_remaining = dash
    draw_segment = True
    last = points[0]
    for point in points[1:]:
        x1, y1 = last
        x2, y2 = point
        distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if distance == 0:
            last = point
            continue

        dx = (x2 - x1) / distance
        dy = (y2 - y1) / distance
        traveled = 0.0
        while traveled < distance:
            step = min(segment_remaining, distance - traveled)
            if draw_segment:
                sx1 = x1 + dx * traveled
                sy1 = y1 + dy * traveled
                sx2 = x1 + dx * (traveled + step)
                sy2 = y1 + dy * (traveled + step)
                draw.line((sx1, sy1, sx2, sy2), fill=fill, width=width)
            traveled += step
            segment_remaining -= step
            if segment_remaining <= 0:
                draw_segment = not draw_segment
                segment_remaining = dash if draw_segment else gap
        last = point


def sample_svg_path(path_data: str, samples_per_curve: int = 20):
    tokens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", path_data)
    points = []
    idx = 0
    command = None
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    def add_point(pt):
        if not points or points[-1] != pt:
            points.append(pt)

    while idx < len(tokens):
        token = tokens[idx]
        if re.fullmatch(r"[A-Za-z]", token):
            command = token
            idx += 1
            if command in {"Z", "z"}:
                add_point(start)
            continue

        if command == "M":
            x = float(tokens[idx])
            y = float(tokens[idx + 1])
            current = (x, y)
            start = current
            add_point(current)
            idx += 2
            command = "L"
            continue

        if command == "L":
            x = float(tokens[idx])
            y = float(tokens[idx + 1])
            current = (x, y)
            add_point(current)
            idx += 2
            continue

        if command == "C":
            x1 = float(tokens[idx])
            y1 = float(tokens[idx + 1])
            x2 = float(tokens[idx + 2])
            y2 = float(tokens[idx + 3])
            x3 = float(tokens[idx + 4])
            y3 = float(tokens[idx + 5])
            p0 = current
            for step in range(1, samples_per_curve + 1):
                t = step / samples_per_curve
                inv = 1 - t
                x = (
                    (inv ** 3) * p0[0]
                    + 3 * (inv ** 2) * t * x1
                    + 3 * inv * (t ** 2) * x2
                    + (t ** 3) * x3
                )
                y = (
                    (inv ** 3) * p0[1]
                    + 3 * (inv ** 2) * t * y1
                    + 3 * inv * (t ** 2) * y2
                    + (t ** 3) * y3
                )
                add_point((x, y))
            current = (x3, y3)
            idx += 6
            continue

        idx += 1

    return points


def catmull_rom_path(points, samples_per_segment: int = 12):
    if len(points) < 2:
        return points[:]

    curve = [points[0]]
    for index in range(len(points) - 1):
        p0 = points[index - 1] if index > 0 else points[index]
        p1 = points[index]
        p2 = points[index + 1]
        p3 = points[index + 2] if index + 2 < len(points) else p2

        for step in range(1, samples_per_segment + 1):
            t = step / samples_per_segment
            t2 = t * t
            t3 = t2 * t

            x = 0.5 * (
                (2 * p1[0])
                + (-p0[0] + p2[0]) * t
                + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2 * p1[1])
                + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            curve.append((x, y))

    return curve


def render_monthly_dashboard_image(current_reports, previous_reports, month_start: datetime.date, month_end: datetime.date):
    width, height = 638, 452
    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)

    title_font = load_font(20, bold=False)
    value_font = load_font(42, bold=True)
    percent_font = load_font(18, bold=True)
    axis_font = load_font(14, bold=False)
    legend_font = load_font(15, bold=False)
    small_font = load_font(12, bold=False)

    border = "#d9d9d9"
    text = "#222222"
    muted = "#666666"
    grid = "#ececec"
    blue = "#2da8e0"
    blue_light = "#cfeaf6"
    green = "#2b8a57"

    draw.rounded_rectangle((1, 1, width - 2, height - 2), radius=18, outline=border, width=1)

    draw.text((24, 22), "Total sales", font=title_font, fill=text)
    draw.line((24, 48, 126, 48), fill=grid, width=1)

    total_current = sum(item["usd_amount"] for item in current_reports)
    total_previous = sum(item["usd_amount"] for item in previous_reports) if previous_reports else None

    draw.text((24, 70), format_currency(total_current), font=value_font, fill=text)

    if total_previous and total_previous > 0:
        change = ((total_current - total_previous) / total_previous) * 100
        change_text = f"↗ {change:.0f}%"
        change_color = green if change >= 0 else "#c0392b"
    else:
        change_text = "N/A"
        change_color = muted

    draw.text((238, 84), change_text, font=percent_font, fill=change_color)

    icon_x, icon_y = 584, 18
    draw.rounded_rectangle((icon_x, icon_y, icon_x + 28, icon_y + 28), radius=6, outline=border, width=1)
    draw.ellipse((icon_x + 6, icon_y + 7, icon_x + 14, icon_y + 15), outline=muted, width=2)
    draw.line((icon_x + 13, icon_y + 14, icon_x + 20, icon_y + 21), fill=muted, width=2)

    chart_left, chart_top, chart_right, chart_bottom = 78, 150, 598, 353
    chart_width = chart_right - chart_left
    chart_height = chart_bottom - chart_top

    max_value = max([0] + [item["usd_amount"] for item in current_reports] + [item["usd_amount"] for item in previous_reports])
    if max_value <= 0:
        max_value = 1
    if max_value <= 500:
        scale_max = max(100, ((max_value + 49) // 50) * 50)
    elif max_value <= 1_000:
        scale_max = ((max_value + 99) // 100) * 100
    elif max_value <= 5_000:
        scale_max = ((max_value + 249) // 250) * 250
    else:
        scale_max = ((max_value + 499) // 500) * 500
    if scale_max < max_value:
        scale_max = max_value

    y_ticks = [0, scale_max / 2, scale_max]
    for tick in y_ticks:
        y = chart_bottom - (tick / scale_max) * chart_height
        draw.line((chart_left, y, chart_right, y), fill=grid, width=1)
        label = format_compact_currency(int(round(tick))) if tick else "$0"
        tw, th = text_size(draw, label, axis_font)
        draw.text((chart_left - tw - 10, y - th / 2), label, font=axis_font, fill=muted)

    def build_series(points):
        if not points:
            return []
        count = max(len(points), 1)
        step = chart_width / max(count - 1, 1)
        coords = []
        for idx, item in enumerate(points):
            value = item["usd_amount"]
            x = chart_left + idx * step
            y = chart_bottom - (value / scale_max) * chart_height
            coords.append((x, y))
        return coords

    current_points = build_series(current_reports)
    previous_points = build_series(previous_reports)

    if previous_points:
        previous_curve = catmull_rom_path(previous_points)
        if len(previous_curve) > 1:
            for idx in range(1, len(previous_curve)):
                if idx % 4 == 0:
                    draw.line(previous_curve[max(0, idx - 2): idx + 1], fill=blue_light, width=3)

    if current_points:
        current_curve = catmull_rom_path(current_points)
        if len(current_curve) > 1:
            draw.line(current_curve, fill=blue, width=4)
        for x, y in current_points:
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=blue, outline=blue)

    if current_reports:
        first_label = format_day_label(current_reports[0]["report_date"])
        mid_label = format_day_label(current_reports[len(current_reports) // 2]["report_date"])
        last_label = format_day_label(current_reports[-1]["report_date"])
    else:
        first_label = format_day_label(month_start)
        mid_label = format_day_label(month_start)
        last_label = format_day_label(month_end)

    x_labels = [
        (chart_left, first_label),
        (chart_left + chart_width / 2, mid_label),
        (chart_right, last_label),
    ]
    for x, label in x_labels:
        tw, _ = text_size(draw, label, axis_font)
        draw.text((x - tw / 2, chart_bottom + 10), label, font=axis_font, fill=muted)

    draw.text((24, 372), month_start.strftime("%b %Y"), font=legend_font, fill=text)
    draw.ellipse((24, 404, 29, 409), fill=blue, outline=blue)
    draw.text((40, 398), month_start.strftime("%b %Y"), font=legend_font, fill=muted)

    if previous_reports:
        draw.line((175, 406, 192, 406), fill=blue_light, width=3)
        draw.text((200, 398), previous_reports[0]["report_date"].replace(day=1).strftime("%b %Y"), font=legend_font, fill=muted)
    else:
        draw.text((175, 398), "Previous month data not available", font=small_font, fill=muted)

    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output


def extract_title(page_html: str, universe_id: int) -> str:
    title_match = re.search(r"<title>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else f"Game {universe_id}"
    title = re.sub(r"\s*[-—]\s*RoRizz\s*$", "", title).strip()
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
            data = await fetch_rorizz_chart_data(session, universe_id)

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
    return f"🏆 {PROJECT_NAME} just earned ${rounded_usd:,}", rounded_usd


async def build_previous_day_breakdown_from_chart():
    games = load_games()
    if not games:
        return "📭 No tracked games added yet. Work harder."

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
                lines.append(f"• **{game_name}**: could not fetch data")
                continue

            if not data["visits_chart"]:
                lines.append(f"• **{game_name}**: could not find Visits (30d) chart data")
                continue

            yesterday_visits = get_chart_value_for_day(data["visits_chart"], yesterday)
            previous_visits = get_chart_value_for_day(data["visits_chart"], previous_day)

            if yesterday_visits is None or previous_visits is None:
                lines.append(
                    f"• **{game_name}**: could not read chart values for {previous_day} or {yesterday}"
                )
                continue

            diff = max(0, yesterday_visits - previous_visits)
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


def get_monthly_report_data(target_date: datetime.date):
    month_start, month_end = month_range_for(target_date)
    current_reports = load_daily_reports(month_start, min(target_date + datetime.timedelta(days=1), month_end))
    prev_start, prev_end = previous_month_range_for(target_date)
    previous_reports = load_daily_reports(prev_start, prev_end)
    return month_start, month_end, current_reports, previous_reports


def render_monthly_clone_image(current_reports, previous_reports, month_start: datetime.date, month_end: datetime.date):
    width, height = 636, 486
    bg = "#f2f4f7"
    card = "#ffffff"
    border = "#d8d8d8"
    text = "#272b30"
    muted = "#6b7178"
    grid = "#eceeef"
    blue = "#22aee8"
    blue_prev = "#27aee9"
    blue_area = (43, 178, 233, 40)
    blue_area_fade = (43, 178, 233, 5)
    green = "#2e8d5f"
    icon = "#55585c"

    img = Image.new("RGBA", (width, height), bg)
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle((2, 4, width - 2, height - 2), radius=12, fill=(0, 0, 0, 85))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(7)))

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=12, fill=card, outline=border, width=1)

    title_font = load_font(24, bold=True)
    amount_font = load_font(38, bold=True)
    growth_font = load_font(19, bold=True)
    axis_font = load_font(20, bold=False)
    legend_font = load_font(15, bold=False)

    total_current = sum(item["usd_amount"] for item in current_reports)
    total_previous = sum(item["usd_amount"] for item in previous_reports) if previous_reports else None

    current_first = current_reports[0]["report_date"] if current_reports else month_start
    current_last = current_reports[-1]["report_date"] if current_reports else month_end - datetime.timedelta(days=1)
    previous_first = previous_reports[0]["report_date"] if previous_reports else None
    previous_last = previous_reports[-1]["report_date"] if previous_reports else None
    previous_range_start, previous_range_end = previous_month_range_for(month_start)

    draw.text((29, 22), "Total sales", font=title_font, fill=text)
    draw.line((29, 56, 93, 56), fill="#cfd3d7", width=4)
    draw.text((32, 77), format_currency(total_current), font=amount_font, fill=text)

    if total_previous and total_previous > 0:
        change = ((total_current - total_previous) / total_previous) * 100
        growth_text = f"↗ {change:.0f}%"
        growth_fill = green if change >= 0 else "#b54d4d"
    else:
        growth_text = "N/A"
        growth_fill = muted
    draw.text((210, 89), growth_text, font=growth_font, fill=growth_fill)

    icon_x, icon_y = 603, 26
    draw.rounded_rectangle((icon_x, icon_y, icon_x + 28, icon_y + 28), radius=4, outline=icon, width=2)
    draw.rounded_rectangle((icon_x + 4, icon_y + 4, icon_x + 18, icon_y + 22), radius=3, outline=icon, width=2)
    draw.line((icon_x + 7, icon_y + 10, icon_x + 14, icon_y + 10), fill=icon, width=2)
    draw.line((icon_x + 7, icon_y + 15, icon_x + 11, icon_y + 15), fill=icon, width=2)
    draw.ellipse((icon_x + 16, icon_y + 16, icon_x + 25, icon_y + 25), outline=icon, width=2)
    draw.line((icon_x + 23, icon_y + 23, icon_x + 27, icon_y + 27), fill=icon, width=2)

    plot_left, plot_top, plot_right, plot_bottom = 80, 142, 604, 374
    current_path = (
        "M80 220 C102 220,103 220,110 228 C121 242,119 274,136 278 C155 282,153 232,162 186 C169 152,184 155,190 172 "
        "C202 207,191 252,211 261 C234 272,237 223,245 181 C251 153,271 154,282 170 C295 188,292 200,312 202 "
        "C335 205,321 264,323 291 C325 330,347 343,358 305 C370 267,379 225,405 234 C426 241,421 252,443 255 "
        "C464 258,457 308,477 313 C499 319,491 247,509 220 C528 192,540 231,533 266 C525 304,549 313,561 278 "
        "C575 238,574 196,601 204 C613 208,616 224,622 235"
    )
    previous_path = (
        "M80 277 C105 259,124 250,142 254 C160 258,171 282,188 315 C206 347,232 302,248 296 C265 290,269 340,291 325 "
        "C315 306,327 268,344 286 C365 308,369 358,389 345 C411 332,414 303,438 318 C460 333,471 366,491 339 "
        "C510 313,496 286,521 268 C547 247,541 160,568 153 C594 146,576 190,604 190"
    )

    current_points = sample_svg_path(current_path, samples_per_curve=16)
    previous_points = sample_svg_path(previous_path, samples_per_curve=14)

    chart = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(chart)

    if current_points:
        mask = Image.new("L", (width, height), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.polygon(current_points + [(622, plot_bottom), (80, plot_bottom)], fill=255)

        gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = gradient.load()
        for y in range(plot_top, plot_bottom + 1):
            ratio = (y - plot_top) / max(1, plot_bottom - plot_top)
            alpha = int(blue_area[3] * (1 - ratio) + blue_area_fade[3] * ratio)
            for x in range(plot_left, plot_right + 1):
                pixels[x, y] = (43, 178, 233, alpha)
        chart = Image.composite(gradient, chart, mask)
        cdraw = ImageDraw.Draw(chart)

    if previous_points:
        draw_dashed_polyline(cdraw, previous_points, fill=blue_prev, width=3, dash=1, gap=7)
    if current_points:
        cdraw.line(current_points, fill=blue, width=4, joint="curve")

    chart = chart.filter(ImageFilter.GaussianBlur(0.2))
    img = Image.alpha_composite(img, layer)
    img = Image.alpha_composite(img, chart)

    draw = ImageDraw.Draw(img)
    for y, label in [(142, "$10K"), (258, "$5K"), (374, "$0K")]:
        draw.line((93, y, 604, y), fill=grid, width=2)
        label_x = 37 if label == "$10K" else 38 if label == "$5K" else 43
        draw.text((label_x, y - 6), label, font=axis_font, fill=muted)

    for x, label in [(81, "Jun 6"), (235, "Jun 14"), (399, "Jun 22"), (559, "Jul 5")]:
        draw.text((x, 395), label, font=axis_font, fill=muted)

    if current_first and current_last:
        current_label = f"{current_first.strftime('%b')} {current_first.day}\u2013{current_last.strftime('%b')} {current_last.day}, {current_last.year}"
        draw.line((117, 438, 138, 438), fill=blue, width=3)
        draw.text((111, 453), current_label, font=legend_font, fill=muted)

    if previous_first and previous_last:
        previous_label = f"{previous_first.strftime('%b')} {previous_first.day}\u2013{previous_last.strftime('%b')} {previous_last.day}, {previous_last.year}"
    else:
        previous_label = f"{previous_range_start.strftime('%b')} {previous_range_start.day}\u2013{(previous_range_end - datetime.timedelta(days=1)).strftime('%b')} {(previous_range_end - datetime.timedelta(days=1)).day}, {previous_range_start.year}"
    draw.line((343, 438, 364, 438), fill=blue_prev, width=3)
    draw.text((337, 453), previous_label, font=legend_font, fill=muted)

    output = io.BytesIO()
    img.convert("RGB").save(output, format="PNG")
    output.seek(0)
    return output


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
        color=EMBED_COLOR,
    )
    await interaction.response.send_message(embed=embed, view=PanelView())


@bot.tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is working!")


@bot.tree.command(name="reportnow", description="Preview today's earnings message")
async def reportnow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg, usd_amount = await build_daily_earned_message_from_chart()
        if not msg or usd_amount is None:
            await interaction.followup.send("⚠️ Could not build today's earnings message.", ephemeral=True)
            return

        report_date = now_local().date() - datetime.timedelta(days=1)
        save_daily_report(report_date, usd_amount, msg)
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="prev", description="Show yesterday vs previous day earnings breakdown")
async def prev(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        msg = await build_previous_day_breakdown_from_chart()
        await interaction.followup.send(msg[:1900], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


@bot.tree.command(name="monthly", description="Generate a monthly sales dashboard image")
async def monthly(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        target_date = now_local().date()
        month_start, month_end, current_reports, previous_reports = get_monthly_report_data(target_date)

        if not current_reports:
            await interaction.followup.send("📭 No saved report data yet.", ephemeral=True)
            return

        image_buffer = render_monthly_clone_image(current_reports, previous_reports, month_start, month_end)
        total_current = sum(item["usd_amount"] for item in current_reports)
        total_previous = sum(item["usd_amount"] for item in previous_reports) if previous_reports else None
        if total_previous and total_previous > 0:
            change = ((total_current - total_previous) / total_previous) * 100
            summary = f"Current month total: ${total_current:,} ({change:+.0f}% vs previous month)"
        else:
            summary = f"Current month total: ${total_current:,}"

        file = discord.File(image_buffer, filename="monthly-dashboard.png")
        embed = discord.Embed(description=summary, color=EMBED_COLOR)
        embed.set_image(url="attachment://monthly-dashboard.png")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`", ephemeral=True)


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
