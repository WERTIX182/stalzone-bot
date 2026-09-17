# -*- coding: utf-8 -*-
"""
STALZONE Inventory Scanner - Discord bot.
Recognizes items on an inventory screenshot (OpenCV template matching),
counts quantities (number of matches and/or the stack digit in the icon
corner via OCR) and outputs a list: name / quantity / price / total.
"""

import io
import json
import os
import re

import aiohttp
import cv2
import numpy as np
import requests
import discord
from datetime import datetime
from discord import app_commands
from discord.ext import commands, tasks

# ----------------------------- SETTINGS --------------------------------------

TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"     # token from the Discord Developer Portal
COMMAND_PREFIX = "!"
TRIGGER_COMMANDS = ("скан", "scan", "проверка")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")   # reference icons
PRICES_FILE = os.path.join(BASE_DIR, "prices.json")   # item price book
BOOSTS_FILE = os.path.join(BASE_DIR, "boosts.json")   # boost price book
RESOURCES_FILE = os.path.join(BASE_DIR, "resources.json")     # resource prices (from the sheet)
ITEM_INDEX_FILE = os.path.join(BASE_DIR, "item_index.json")   # game item index (id by name)
REPUTATION_FILE = os.path.join(BASE_DIR, "reputation.json")   # prices per 1k of Arsen reputation
UPGRADES_FILE = os.path.join(BASE_DIR, "upgrades.json")       # shelter skill upgrade prices

UPG_MAX_LEVEL = 5               # maximum shelter skill level
TICKET_ADMIN_ROLE = ""          # role name for ticket management (empty = "Manage Channels" permission)
FAQ_CHANNEL_ID = 0              # FAQ channel ID (0 = FAQ button disabled)

# Default pulp prices (used when not found in the resources price book)
PULP_PRICES = {
    "солевика": 2250, "сластена": 8600, "лимонника": 2000, "куборбуза": 1000,
}

# ------------------------------ BOOSTS ---------------------------------------

GUILD_ID = 0                    # your server ID - slash commands appear instantly.
                                # 0 = global registration (may take up to an hour).
TICKET_PREFIX = "буст-"         # ticket channel names start with this prefix
TICKET_CATEGORY = "Бусты"       # category the tickets are placed into

# Tickets of the other services: (channel prefix, category name)
RES_TICKET_PREFIX, RES_TICKET_CATEGORY = "скуп-", "Скупка ресурсов"
OTHER_TICKET_PREFIX, OTHER_TICKET_CATEGORY = "проч-", "Продажа прочих вещей"
REP_TICKET_PREFIX, REP_TICKET_CATEGORY = "реп-", "Репутация Арсена"
UPG_TICKET_PREFIX, UPG_TICKET_CATEGORY = "убеж-", "Прокачка убежища"

# --------------------- PRICE CHANGE ANNOUNCEMENTS ----------------------------
# Channel ID where the bot posts price change announcements (0 = feature off).
PRICE_NEWS_CHANNEL_ID = 0
# Channel IDs for clickable links in the announcement header (0 = plain text).
CHAN_BOOSTS_ID = 0              # "buy boosts" channel
CHAN_RESOURCES_ID = 0           # "sell resources" channel
CHAN_ITEMS_ID = 0               # "sell items" channel (items/auction)
CHAN_REPUTATION_ID = 0          # Arsen reputation channel
CHAN_UPGRADES_ID = 0            # shelter upgrade channel
CHAN_CALC_ID = 0                # "resource calculator" channel: screenshots there are priced
                                # at buyout rates; the /calc command is bound to it too
# Announcement state file (last post ID + accumulated changes).
# Created automatically on first run - do not edit.
PRICE_NEWS_FILE = os.path.join(BASE_DIR, "price_news.json")

COMMISSION_PER_USE = 0          # Referrals: blogger commission per 1 promo code use (0 = off)

# Canned message the bot sends to a ticket right after creation.
# Empty by default - fill in between the triple quotes (multiline allowed).
TICKET_MESSAGE = """

"""

# ----------------------- PROMO CODES AND GOOGLE SHEET ------------------------

PROMO_DB_FILE = os.path.join(BASE_DIR, "Base.db")          # SQLite database with promo codes
GOOGLE_CREDS_FILE = os.path.join(BASE_DIR, "credentials.json")  # service account key
GOOGLE_SHEET_NAME = "ДЛЯ ЛЮБИМЫХ ПРОГРАММИСТОВ"             # spreadsheet name (do not change)
SHEETS_SYNC_HOURS = 6                   # price auto-sync from the sheet (0 = off)

MATCH_THRESHOLD = 0.85      # template match threshold (0..1); lower to 0.8 if items are not found
NMS_OVERLAP = 0.3           # allowed overlap for duplicate suppression
USE_OCR = True              # OCR the stack digit in the cell corner (requires Tesseract)

# Path to Tesseract on Windows (default install location).
# If you installed it elsewhere - adjust the path. On Linux/Mac set it to None.
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ------------------------ STALZONE API (auction) -----------------------------

REGION = "RU"                       # server region for auction prices
AUTO_UPDATE_HOURS = 3               # how often to auto-update prices (0 = off)

# DEMO mode (for tests, fake data): leave as is.
# PRODUCTION mode: after EXBO approves your application, set API_BASE to prod
# and fill in CLIENT_ID / CLIENT_SECRET from the application
# (or a ready APP ACCESS TOKEN in API_TOKEN).
API_BASE = "https://dapi.stalzone.com"        # prod: "https://eapi.stalzone.net"
OAUTH_TOKEN_URL = "https://exbo.net/oauth/token"
CLIENT_ID = ""                        # from the EXBO application (Bot Access)
CLIENT_SECRET = ""                    # from the EXBO application (Bot Access)
API_TOKEN = "YOUR_EXBO_API_TOKEN_HERE"  # demo token from the docs (test data only!)

# -----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def load_prices() -> dict:
    with open(PRICES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_prices(prices: dict) -> None:
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)


# --------------------- STALZONE AUCTION INTEGRATION --------------------------

_cached_token = {"value": None}


async def get_api_token(session: aiohttp.ClientSession) -> str:
    """Production mode: OAuth client_credentials. Demo: static API_TOKEN."""
    if CLIENT_ID and CLIENT_SECRET:
        if _cached_token["value"]:
            return _cached_token["value"]
        async with session.post(OAUTH_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"OAuth ошибка {resp.status}: {data}")
            _cached_token["value"] = data["access_token"]
            return _cached_token["value"]
    if not API_TOKEN:
        raise RuntimeError("Не задан ни API_TOKEN, ни CLIENT_ID/CLIENT_SECRET")
    return API_TOKEN


async def fetch_min_lot_price(session, token: str, item_id: str) -> int | None:
    """Minimum per-unit price among active lots (buyoutPrice/amount)."""
    url = f"{API_BASE}/{REGION}/auction/{item_id}/lots?limit=200"
    async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
        if resp.status == 401:  # token expired - drop the cache
            _cached_token["value"] = None
            raise RuntimeError(f"401 Unauthorized для {item_id} — проверь токен")
        if resp.status != 200:
            return None
        data = await resp.json()
    best = None
    for lot in data.get("lots", []):
        amount = lot.get("amount") or 1
        price = lot.get("buyoutPrice")
        if price:
            unit = price / amount
            if best is None or unit < best:
                best = unit
    return int(best) if best is not None else None


async def update_prices_from_auction() -> list[str]:
    """Updates prices of all items with item_id (except those marked manual)."""
    prices = load_prices()
    report = []
    async with aiohttp.ClientSession() as session:
        token = await get_api_token(session)
        for key, info in prices.items():
            item_id = info.get("item_id")
            if not item_id or info.get("manual"):
                continue
            try:
                price = await fetch_min_lot_price(session, token, item_id)
            except Exception as e:
                report.append(f"⚠️ {info['name']}: ошибка ({e})")
                continue
            if price is not None:
                old = info.get("price", 0)
                info["price"] = price
                news_track("items", info["name"], old, price)
                report.append(f"✅ {info['name']}: {old} → **{price}**")
            else:
                report.append(f"ℹ️ {info['name']}: нет активных лотов")
    save_prices(prices)
    return report


@tasks.loop(hours=AUTO_UPDATE_HOURS if AUTO_UPDATE_HOURS > 0 else 24)
async def auto_update_prices():
    if AUTO_UPDATE_HOURS <= 0:
        return
    try:
        report = await update_prices_from_auction()
        print("[аукцион] цены обновлены:", "; ".join(report))
    except Exception as e:
        print(f"[аукцион] ошибка автообновления: {e}")


# ------------------------------- BOOSTS --------------------------------------

def load_boosts() -> dict:
    if not os.path.exists(BOOSTS_FILE):
        return {}
    with open(BOOSTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_boosts(boosts: dict) -> None:
    with open(BOOSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(boosts, f, ensure_ascii=False, indent=2)


def _normalize(text: str) -> str:
    """Normalization for name matching: case, ё, quotes, brackets."""
    text = text.lower().replace("ё", "е")
    return re.sub(r"[«»\"'\[\](){}]", "", text).strip()


def find_boost(query: str, boosts: dict):
    """Finds a boost by name or alias. Exact match first, then partial
    (only if it is the single one). Returns (key, info) or None."""
    q = _normalize(query)
    if not q:
        return None
    # exact match by name or alias
    for key, info in boosts.items():
        variants = [_normalize(info["name"])] + [_normalize(a) for a in info.get("aliases", [])]
        if q in variants:
            return key, info
    # partial match (substring)
    partial = []
    for key, info in boosts.items():
        variants = [_normalize(info["name"])] + [_normalize(a) for a in info.get("aliases", [])]
        if any(q in v or v in q for v in variants if v):
            partial.append((key, info))
    if len(partial) == 1:
        return partial[0]
    return None


# ----------------------------- PROMO CODES -----------------------------------

def get_promo(code: str):
    """Looks up a promo code in Base.db. Returns dict(discount, status):
    status: 'ok' | 'not_found' | 'expired'"""
    import sqlite3
    from datetime import date
    if not os.path.exists(PROMO_DB_FILE):
        return {"status": "not_found"}
    con = sqlite3.connect(PROMO_DB_FILE)
    cur = con.cursor()
    try:
        cur.execute("SELECT discount, date FROM Referal WHERE Youtuber = ? COLLATE NOCASE",
                    (code,))
        row = cur.fetchone()
    except sqlite3.Error:
        row = None
    con.close()
    if not row:
        return {"status": "not_found"}
    discount_pct, date_str = row
    if date_str:
        try:
            if date.fromisoformat(str(date_str)) < date.today():
                return {"status": "expired", "discount": discount_pct}
        except ValueError:
            pass
    return {"status": "ok", "discount": discount_pct or 0}


def promo_increment_usage(code: str) -> None:
    """Increments the promo code usage counter (Num)."""
    import sqlite3
    try:
        con = sqlite3.connect(PROMO_DB_FILE)
        con.execute("UPDATE Referal SET Num = COALESCE(Num, 0) + 1 "
                    "WHERE Youtuber = ? COLLATE NOCASE", (code,))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def build_order(text: str, book: dict | None = None):
    """Parses an order like «Солянка 25\\nМорфин 15\\nпромокод Gromtrom».
    book - the price book for entry lookup (boosts by default, resources
    for buyout). Returns (order rows, unrecognized, total, promo info)."""
    boosts = book if book is not None else load_boosts()
    rows, unknown = [], []
    total = 0
    promo = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pm = re.match(r"^(промокод|промо|купон)\s+(.+)$", line, re.IGNORECASE)
        if pm:
            promo = {"code": pm.group(2).strip(), **get_promo(pm.group(2).strip())}
            continue
        m = re.match(r"^(.*?)[\s\-—*xх]+(\d+)\s*(шт\.?|x|х)?$", line)
        if m:
            name, qty = m.group(1).strip(), int(m.group(2))
        else:
            name, qty = line, 1
        found = find_boost(name, boosts)
        if found:
            key, info = found
            subtotal = qty * info["price"]
            total += subtotal
            rows.append({"name": info["name"], "qty": qty,
                         "price": info["price"], "subtotal": subtotal})
        else:
            unknown.append(name)
    return rows, unknown, total, promo


def format_order(rows, unknown, total, promo=None) -> str:
    if not rows and not unknown and not promo:
        return ("🧾 Не вижу позиций заказа. Напиши каждый буст с новой строки "
                "в формате «Название количество», например:\n```Солянка 25\nМорфин 15```\n"
                "Если есть промокод — допиши строкой `промокод КОД`.")
    lines = ["🧾 **Ваш заказ:**"]
    for r in rows:
        lines.append(f"**{r['name']}** — {r['qty']} шт. × {r['price']:,} = **{r['subtotal']:,}**"
                     .replace(",", " "))
    if rows:
        lines.append(f"\n💵 Сумма: {total:,}".replace(",", " "))
    if promo:
        d = promo.get("discount") or 0
        if promo["status"] == "ok" and rows:
            discounted = round(total * (1 - d / 100))
            lines.append(f"🏷 Промокод `{promo['code']}`: скидка {d}% "
                         f"(−{total - discounted:,})".replace(",", " "))
            lines.append(f"💰 **Итого к оплате: {discounted:,}**".replace(",", " "))
        elif promo["status"] == "expired":
            lines.append(f"⚠️ Промокод `{promo['code']}` истёк — счёт без скидки.")
            lines.append(f"💰 **Итого к оплате: {total:,}**".replace(",", " "))
        else:
            lines.append(f"⚠️ Промокод `{promo['code']}` не найден — счёт без скидки.")
            lines.append(f"💰 **Итого к оплате: {total:,}**".replace(",", " "))
    elif rows:
        lines.append(f"💰 **Итого к оплате: {total:,}**".replace(",", " "))
    if unknown:
        lines.append("\n⚠️ Не распознано: " + ", ".join(f"`{u}`" for u in unknown) +
                     "\nПроверь название (`!бусты`) или попроси админа добавить синоним: "
                     "`!синоним <ключ> <синоним>`")
    return "\n".join(lines)


# ----------------------- GOOGLE SHEET SYNC -----------------------------------

def _sheet_pairs(worksheet, cell_range="C2:P24") -> dict:
    """Reads "name - price" pairs from the sheet range."""
    cells = worksheet.range(cell_range)
    pairs = {}
    for i in range(0, len(cells), 2):
        name = (cells[i].value or "").strip("[]").strip()
        price_str = (cells[i + 1].value or "").strip() if i + 1 < len(cells) else ""
        price_str = price_str.replace("\xa0", "").replace(" ", "")
        price = None
        if price_str:
            try:
                price = int(price_str.replace(" ", ""))
            except ValueError:
                pass
        if name and price is not None:
            pairs[name] = price
    return pairs


def sync_prices_from_sheets() -> str:
    """Pulls boost prices from the Google Sheet into boosts.json.
    Matching - by name or alias (case-insensitive)."""
    import gspread
    if not os.path.exists(GOOGLE_CREDS_FILE):
        return "⚠️ Нет credentials.json рядом с bot.py — синхронизация невозможна."
    gc = gspread.service_account(filename=GOOGLE_CREDS_FILE)
    sh = gc.open(GOOGLE_SHEET_NAME)
    ws = sh.worksheet("ЦЕНЫ БУСТОВ")
    pairs = _sheet_pairs(ws)
    boosts = load_boosts()
    updated, unmatched = [], []
    used_keys = set()
    for sheet_name, price in pairs.items():
        found = find_boost(sheet_name, boosts)
        if found:
            key, info = found
            used_keys.add(key)
            if info["price"] != price:
                news_track("boosts", info["name"], info["price"], price)
                updated.append(f"{info['name']}: {info['price']} → **{price}**")
                boosts[key]["price"] = price
        else:
            unmatched.append(sheet_name)
    save_boosts(boosts)
    report = [f"📊 Синхронизация с таблицей «ЦЕНЫ БУСТОВ»: обновлено {len(updated)} позиций."]
    report += [f"✅ {u}" for u in updated]
    if unmatched:
        report.append("➕ В таблице есть позиции, которых нет у бота: "
                      + ", ".join(f"`{u}`" for u in unmatched)
                      + "\nДобавить: `!буст_добавить <ключ> <цена> <название>`")
    missing = [v["name"] for k, v in boosts.items() if k not in used_keys]
    if missing:
        report.append("⚠️ Этих бустов бота нет в таблице (остались старые цены): "
                      + ", ".join(missing))
    # Resources (buyout) from the second sheet + icons for new entries
    try:
        report += sync_resources_from_sheet(sh)
    except Exception as e:
        report.append(f"⚠️ Лист «ЦЕНЫ РЕСУРСОВ»: ошибка ({e})")
    try:
        report.append(sync_icons())
    except Exception as e:
        report.append(f"⚠️ Загрузка иконок: ошибка ({e})")
    return "\n".join(report)


@tasks.loop(hours=SHEETS_SYNC_HOURS if SHEETS_SYNC_HOURS > 0 else 24)
async def auto_sync_sheets():
    if SHEETS_SYNC_HOURS <= 0:
        return
    try:
        import asyncio
        report = await asyncio.to_thread(sync_prices_from_sheets)
        print("[таблица] " + report.replace("\n", "; "))
        print("[объявления] " + await post_price_news())
    except Exception as e:
        print(f"[таблица] ошибка синхронизации: {e}")


# ----------------------------- RESOURCES (BUYOUT) ----------------------------

def load_resources() -> dict:
    if not os.path.exists(RESOURCES_FILE):
        return {}
    with open(RESOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_resources(resources: dict) -> None:
    with open(RESOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(resources, f, ensure_ascii=False, indent=2)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str) -> str:
    """Key from a Russian name: «Ящик гранат Гром» -> yashchik_granat_grom."""
    out = "".join(_TRANSLIT.get(c, c) for c in name.lower())
    out = re.sub(r"[^a-z0-9]+", "_", out).strip("_")
    return out or "item"


def sync_resources_from_sheet(sh) -> list[str]:
    """Loads the "ЦЕНЫ РЕСУРСОВ" sheet into resources.json (creates new entries)."""
    ws = sh.worksheet("ЦЕНЫ РЕСУРСОВ")
    pairs = _sheet_pairs(ws)
    resources = load_resources()
    by_name = {_normalize(v["name"]): k for k, v in resources.items()}
    added, updated = [], []
    for sheet_name, price in pairs.items():
        key = by_name.get(_normalize(sheet_name))
        if key is None:
            key = slugify(sheet_name)
            base, n = key, 2
            while key in resources:
                key = f"{base}_{n}"
                n += 1
            resources[key] = {"name": sheet_name, "price": price, "aliases": []}
            added.append(sheet_name)
        elif resources[key]["price"] != price:
            old = resources[key]["price"]
            resources[key]["price"] = price
            news_track("resources", sheet_name, old, price)
            updated.append(f"{sheet_name}: {old} → **{price}**")
    save_resources(resources)
    report = [f"📦 «ЦЕНЫ РЕСУРСОВ»: {len(pairs)} позиций в таблице, "
              f"новых {len(added)}, обновлено {len(updated)}."]
    if added:
        report.append("🆕 " + ", ".join(added[:15]) + (" ..." if len(added) > 15 else ""))
    return report


# ----------------------- TEMPLATE ICON AUTO-DOWNLOAD -------------------------

ICON_URL = ("https://raw.githubusercontent.com/EXBO-Studio/"
            "stalzone-database/main/ru/icons/{category}/{item_id}.png")


def load_item_index() -> dict:
    if not os.path.exists(ITEM_INDEX_FILE):
        return {}
    with open(ITEM_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def sync_icons() -> str:
    """Downloads official icons as templates for entries without one.
    Covers resources (by name from the index) and price book items (by item_id)."""
    index = load_item_index()
    if not index:
        return "⚠️ Нет item_index.json — авто-загрузка иконок невозможна."
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    existing = set()
    for entry in os.listdir(TEMPLATES_DIR):
        key, ext = os.path.splitext(entry)
        if os.path.isfile(os.path.join(TEMPLATES_DIR, entry)):
            existing.add(key)
        elif os.path.isdir(os.path.join(TEMPLATES_DIR, entry)):
            existing.add(key)

    targets = []  # (template_key, item_id, category)
    for key, info in load_resources().items():
        if key in existing:
            continue
        hit = index.get(_normalize(info["name"]))
        if hit:
            targets.append((key, hit["id"], hit["category"]))
    for key, info in load_prices().items():
        if key in existing or not info.get("item_id"):
            continue
        hit = index.get(_normalize(info["name"]))
        cat = hit["category"] if hit else "misc"
        targets.append((key, info["item_id"], cat))

    ok, fail = [], []
    for key, item_id, category in targets:
        try:
            r = requests.get(ICON_URL.format(category=category, item_id=item_id), timeout=20)
            if r.status_code == 200 and r.content:
                with open(os.path.join(TEMPLATES_DIR, key + ".png"), "wb") as f:
                    f.write(r.content)
                ok.append(key)
            else:
                fail.append(key)
        except Exception:
            fail.append(key)
    return (f"🖼 Иконки: загружено {len(ok)}"
            + (f", не найдено {len(fail)} ({', '.join(fail[:10])})" if fail else ""))


# ---------------------- TICKET MANAGEMENT (BUTTONS) --------------------------

def _is_ticket_admin(member: discord.Member) -> bool:
    """Ticket management access: the configured role or manage_channels permission."""
    if TICKET_ADMIN_ROLE:
        return any(r.name == TICKET_ADMIN_ROLE for r in member.roles)
    return member.guild_permissions.manage_channels


async def _require_channel(interaction: discord.Interaction, chan_id: int) -> bool:
    """Slash command works only in its designated channel (0 = no restriction)."""
    if not chan_id or interaction.channel_id == chan_id:
        return True
    await interaction.response.send_message(
        f"Эта команда работает в канале <#{chan_id}>.", ephemeral=True)
    return False


class TicketControls(discord.ui.View):
    """Buttons under the ticket card. Persistent - work after bot restarts."""

    def __init__(self, faq_url: str | None = None):
        super().__init__(timeout=None)
        if faq_url:
            self.add_item(discord.ui.Button(
                label="❓ Частые вопросы", style=discord.ButtonStyle.link, url=faq_url))

    @discord.ui.button(label="🔒 Закрыть тикет", style=discord.ButtonStyle.red,
                       custom_id="ticket_close_btn")
    async def close_ticket(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        if not _is_ticket_admin(interaction.user):
            await interaction.response.send_message(
                "⛔ Закрывать тикеты могут только администраторы.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Тикет закрывается...")
        await interaction.channel.delete()


def ticket_controls_view(guild: discord.Guild) -> TicketControls:
    """View for the ticket card: close button + FAQ (if a channel is set)."""
    faq_url = None
    if FAQ_CHANNEL_ID:
        faq_url = f"https://discord.com/channels/{guild.id}/{FAQ_CHANNEL_ID}"
    return TicketControls(faq_url=faq_url)


def load_reputation() -> dict:
    if not os.path.exists(REPUTATION_FILE):
        return {}
    with open(REPUTATION_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_reputation(data: dict) -> None:
    with open(REPUTATION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_upgrades() -> dict:
    if not os.path.exists(UPGRADES_FILE):
        return {}
    with open(UPGRADES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_upgrades(data: dict) -> None:
    with open(UPGRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------- PRICE CHANGE ANNOUNCEMENTS ----------------------------
# Category -> (section title, channel ID for the link). Order = order in the post.
_NEWS_META = {
    "resources":  ("Ресурсы",   "CHAN_RESOURCES_ID"),
    "boosts":     ("Бусты",     "CHAN_BOOSTS_ID"),
    "items":      ("Предметы",  "CHAN_ITEMS_ID"),
    "reputation": ("Репутация", "CHAN_REPUTATION_ID"),
    "upgrades":   ("Убежище",   "CHAN_UPGRADES_ID"),
}


def _news_chan_id(key: str) -> int:
    return globals().get(key, 0)


def _news_load() -> dict:
    if not os.path.exists(PRICE_NEWS_FILE):
        return {"message_id": None, "pending": {}}
    try:
        with open(PRICE_NEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("message_id", None)
        data.setdefault("pending", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"message_id": None, "pending": {}}


def _news_save(data: dict) -> None:
    with open(PRICE_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def news_track(category: str, name: str, old, new) -> None:
    """Remembers a price change for the next announcement (a "piggy bank")."""
    if category not in _NEWS_META or old == new:
        return
    data = _news_load()
    data["pending"].setdefault(category, {})[name] = [old, new]
    _news_save(data)


def build_news_text(pending: dict) -> str:
    """Announcement text: header with channel mentions of the changed categories."""
    links, parts = [], []
    for cat, (title, chan_key) in _NEWS_META.items():
        items = pending.get(cat)
        if not items:
            continue
        chan_id = _news_chan_id(chan_key)
        links.append(f"<#{chan_id}>" if chan_id else f"#{title}")
        lines = [f"**{title}**"]
        for name, (old, new) in items.items():
            lines.append(f"> [{name}] {old:,} → **{new:,}**".replace(",", " "))
        parts.append("\n".join(lines))
    header = "**Изменение цен:** " + " / ".join(links)
    return header + "\n\n" + "\n\n".join(parts)


async def post_price_news() -> str:
    """Posts the announcement: deletes the bot's previous post and sends a new dated one."""
    if not PRICE_NEWS_CHANNEL_ID:
        return "⚠️ Не задан PRICE_NEWS_CHANNEL_ID в начале bot.py — объявления выкл."
    channel = bot.get_channel(PRICE_NEWS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(PRICE_NEWS_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            return "⚠️ Канал объявлений не найден — проверь PRICE_NEWS_CHANNEL_ID."
    data = _news_load()
    pending = {c: v for c, v in data["pending"].items() if v}
    if not pending:
        return "ℹ️ Изменений цен не накоплено — объявление не обновляю."
    if data["message_id"]:
        try:
            old_msg = await channel.fetch_message(data["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    text = (build_news_text(pending)
            + f"\n\n_Цены актуальны на {datetime.now():%d.%m.%Y, %H:%M}_")
    if len(text) > 1900:                      # Discord limit safeguard
        text = text[:1880] + "\n… _(список обрезан)_"
    msg = await channel.send(text)
    data["message_id"] = msg.id
    data["pending"] = {}
    _news_save(data)
    return "✅ Объявление об изменении цен обновлено."


@bot.tree.command(name="buy_boost", description="Создать тикет на покупку бустов")
@app_commands.describe(ник="Ваш игровой ник")
async def buy_boost(interaction: discord.Interaction, ник: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Команда работает только на сервере.",
                                                ephemeral=True)
        return
    if not await _require_channel(interaction, CHAN_BOOSTS_ID):
        return
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY)
    if category is None:
        category = await guild.create_category(TICKET_CATEGORY)

    chan_name = re.sub(r"\s+", "-", (TICKET_PREFIX + interaction.user.display_name).lower())
    existing = discord.utils.get(guild.text_channels, name=chan_name)
    if existing:
        await interaction.response.send_message(
            f"У тебя уже есть открытый тикет: {existing.mention}", ephemeral=True)
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True),
    }
    channel = await guild.create_text_channel(chan_name, category=category,
                                              overwrites=overwrites)

    embed = discord.Embed(title="🚀 Запрос на покупку бустов",
                          color=discord.Color.orange())
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Игровой ник:", value=ник, inline=False)
    embed.add_field(
        name="📋 Порядок действий",
        value=("Напишите наименование и количество бустов, каждый с новой строки.\n"
               "Пример:\n```Солянка 25\nМорфин 15```"),
        inline=False)
    embed.set_footer(text=f"Тикет создан: {datetime.now():%d.%m.%Y %H:%M}")
    await channel.send(content=interaction.user.mention, embed=embed,
                       view=ticket_controls_view(guild))

    # Canned message (configured via TICKET_MESSAGE at the top of bot.py)
    if TICKET_MESSAGE.strip():
        await channel.send(TICKET_MESSAGE)

    await interaction.response.send_message(
        f"✅ Тикет для покупки бустов создан: {channel.mention}", ephemeral=True)


# --------------------- OTHER SERVICE TICKETS (SHARED) ------------------------

async def create_ticket(interaction: discord.Interaction, prefix: str,
                        category_name: str, embed: discord.Embed,
                        extra_message: str = "") -> discord.TextChannel:
    """Creates a private ticket channel and sends the order card into it."""
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name=category_name)
    if category is None:
        category = await guild.create_category(category_name)
    chan_name = re.sub(r"\s+", "-", (prefix + interaction.user.display_name).lower())
    existing = discord.utils.get(guild.text_channels, name=chan_name)
    if existing:
        await interaction.response.send_message(
            f"У тебя уже есть открытый тикет: {existing.mention}", ephemeral=True)
        return None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True),
    }
    channel = await guild.create_text_channel(chan_name, category=category,
                                              overwrites=overwrites)
    await channel.send(content=interaction.user.mention, embed=embed,
                       view=ticket_controls_view(guild))
    if extra_message.strip():
        await channel.send(extra_message)
    await interaction.response.send_message(
        f"✅ Тикет создан: {channel.mention}", ephemeral=True)
    return channel


def _find_everywhere(name: str):
    """Searches for an entry by name across all price books: boosts, items, resources."""
    for book in (load_boosts(), load_prices(), load_resources()):
        found = find_boost(name, book)
        if found:
            return found
    return None


# --------------------- RESOURCE BUYOUT (/sell_resources) ---------------------

@bot.tree.command(name="sell_resources",
                  description="Продать ресурсы: скан скриншота, смета, тикет скупщика")
@app_commands.describe(ник="Ваш игровой ник")
async def sell_resources(interaction: discord.Interaction, ник: str):
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.",
                                                ephemeral=True)
        return
    if not await _require_channel(interaction, CHAN_RESOURCES_ID):
        return
    embed = discord.Embed(title="📦 Заявка на продажу ресурсов",
                          color=discord.Color.green())
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Игровой ник:", value=ник, inline=False)
    embed.add_field(
        name="📋 Порядок действий",
        value=("Пришлите **скриншот инвентаря** — бот распознает ресурсы "
               "и посчитает смету по актуальным ценам скупки.\n"
               "Или напишите позиции текстом, каждую с новой строки:\n"
               "```Батарейка аномальная 5\nКлеймора 2```"),
        inline=False)
    embed.set_footer(text=f"Тикет создан: {datetime.now():%d.%m.%Y %H:%M}")
    await create_ticket(interaction, RES_TICKET_PREFIX, RES_TICKET_CATEGORY, embed)


# ------------------- SELLING OTHER ITEMS (/sell_other) -----------------------

@bot.tree.command(name="sell_other", description="Заявка на продажу прочих вещей")
@app_commands.describe(ник="Ваш игровой ник",
                       название="Что продаёте",
                       заметка="Комментарий (необязательно)",
                       скриншот="Скриншот предмета (необязательно)")
async def sell_other(interaction: discord.Interaction, ник: str, название: str,
                     заметка: str = "", скриншот: discord.Attachment = None):
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.",
                                                ephemeral=True)
        return
    if not await _require_channel(interaction, CHAN_ITEMS_ID):
        return
    embed = discord.Embed(title="🧾 Заявка на продажу", color=discord.Color.blurple())
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Игровой ник:", value=ник, inline=False)
    embed.add_field(name="Название:", value=название, inline=False)
    if заметка:
        embed.add_field(name="Заметка:", value=заметка, inline=False)

    found = _find_everywhere(название)
    if found:
        _, info = found
        embed.add_field(name="💰 Ориентировочная цена:",
                        value=f"**{info['name']}** — {info['price']:,}".replace(",", " "),
                        inline=False)
    else:
        embed.add_field(name="💰 Цена:", value="В прайсе не найдено — оценит скупщик.",
                        inline=False)
    if скриншот is not None:
        embed.set_image(url=скриншот.url)
    embed.set_footer(text=f"Заявка создана: {datetime.now():%d.%m.%Y %H:%M}")
    await create_ticket(interaction, OTHER_TICKET_PREFIX, OTHER_TICKET_CATEGORY, embed)


# ------------------- ARSEN REPUTATION (/buy_reputation) ----------------------
# Formula: cost = required rep / 1000 x price per 1k rep (reputation.json)

async def _rep_autocomplete(interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
    cur = _normalize(current)
    names = [v["name"] for v in load_reputation().values()]
    matches = [n for n in names if cur in _normalize(n)][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


@bot.tree.command(name="buy_reputation",
                  description="Репутация Арсена: расчёт стоимости прокачки")
@app_commands.describe(ник="Ваш игровой ник",
                       предмет="Предмет для сдачи (выберите из списка)",
                       репутация="Сколько репутации нужно (например 53840)")
@app_commands.autocomplete(предмет=_rep_autocomplete)
async def buy_reputation(interaction: discord.Interaction, ник: str,
                         предмет: str, репутация: int):
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.",
                                                ephemeral=True)
        return
    if not await _require_channel(interaction, CHAN_REPUTATION_ID):
        return
    if репутация <= 0:
        await interaction.response.send_message("Количество репутации должно быть > 0.",
                                                ephemeral=True)
        return
    found = find_boost(предмет, load_reputation())
    embed = discord.Embed(title="🤝 Прокачка репутации Арсена",
                          color=discord.Color.gold())
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Игровой ник:", value=ник, inline=False)
    embed.add_field(name="Предмет:", value=предмет, inline=True)
    embed.add_field(name="Нужно репутации:", value=f"{репутация:,}".replace(",", " "),
                    inline=True)
    if found:
        _, info = found
        total = round(репутация * info["price_per_1k"] / 1000)
        embed.add_field(name="💰 Стоимость:", value=f"**{total:,} ₽**".replace(",", " "),
                        inline=False)
        embed.add_field(name="📐 Расчёт:",
                        value=f"{репутация:,} ÷ 1000 × {info['price_per_1k']:,} = {total:,}"
                        .replace(",", " "), inline=False)
    else:
        embed.add_field(name="💰 Стоимость:",
                        value="Предмет не найден в прайсе — оценит исполнитель.",
                        inline=False)
    embed.set_footer(text=f"Заявка создана: {datetime.now():%d.%m.%Y %H:%M}")
    await create_ticket(interaction, REP_TICKET_PREFIX, REP_TICKET_CATEGORY, embed)


# ------------------- SHELTER UPGRADES (/buy_upgrade) -------------------------

def read_upgrade_level(image_bytes: bytes) -> int | None:
    """Tries to read the skill level from a shelter screenshot.
    The level is the large digit above the "УРОВЕНЬ" caption (range
    1..UPG_MAX_LEVEL); progress numbers like "830/16000" are dropped
    by the filter."""
    try:
        import pytesseract
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        if TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        text = pytesseract.image_to_string(
            gray, config="--psm 11 -c tessedit_char_whitelist=0123456789")
        for token in re.findall(r"\d+", text):
            n = int(token)
            if 1 <= n <= UPG_MAX_LEVEL:
                return n
    except Exception:
        pass
    return None


async def _upg_autocomplete(interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
    cur = _normalize(current)
    names = [v["name"] for v in load_upgrades().values()]
    matches = [n for n in names if cur in _normalize(n)][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


@bot.tree.command(name="buy_upgrade",
                  description="Прокачка убежища: расчёт и карточка для исполнителя")
@app_commands.describe(
    ник="Ваш игровой ник",
    навык="Какой навык качать (выберите из списка)",
    уровень="Текущий уровень навыка (если не укажете — попробуем считать со скриншота)",
    скриншот="Скриншот навыков (необязательно)")
@app_commands.autocomplete(навык=_upg_autocomplete)
async def buy_upgrade(interaction: discord.Interaction, ник: str, навык: str,
                      уровень: int = None, скриншот: discord.Attachment = None):
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.",
                                                ephemeral=True)
        return
    if not await _require_channel(interaction, CHAN_UPGRADES_ID):
        return
    await interaction.response.defer(ephemeral=True)

    level = уровень
    if level is None and скриншот is not None:
        level = read_upgrade_level(await скриншот.read())

    found = find_boost(навык, load_upgrades())
    embed = discord.Embed(title="🏠 Прокачка убежища", color=discord.Color.teal())
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Игровой ник:", value=ник, inline=False)
    embed.add_field(name="Навык:", value=навык, inline=True)
    embed.add_field(name="📊 Текущий уровень:",
                    value=(str(level) if level is not None
                           else "не определён — укажите в тикете"),
                    inline=True)
    if found:
        _, info = found
        full = info["price"]
        if level is not None and 1 <= level < UPG_MAX_LEVEL:
            steps = UPG_MAX_LEVEL - level
            total = round(full * steps / (UPG_MAX_LEVEL - 1))
            embed.add_field(
                name="💰 Стоимость прокачки:",
                value=(f"**{total:,} ₽** (с {level} до {UPG_MAX_LEVEL} ур.)\n"
                       f"📐 {full:,} × {steps}/{UPG_MAX_LEVEL - 1} = {total:,}")
                .replace(",", " "), inline=False)
        elif level is not None and level >= UPG_MAX_LEVEL:
            embed.add_field(name="💰 Стоимость:",
                            value=f"Навык уже максимального уровня ({UPG_MAX_LEVEL}) ✅",
                            inline=False)
        else:
            embed.add_field(name="💰 Полная прокачка:",
                            value=f"**{full:,} ₽** (1 → {UPG_MAX_LEVEL} ур.)"
                            .replace(",", " "), inline=False)
    else:
        embed.add_field(name="💰 Стоимость:",
                        value="Навык не найден в прайсе — оценит исполнитель.",
                        inline=False)
    if скриншот is not None:
        embed.set_image(url=скриншот.url)
    embed.set_footer(text=f"Заявка создана: {datetime.now():%d.%m.%Y %H:%M}")

    channel = await _create_ticket_deferred(interaction, UPG_TICKET_PREFIX,
                                            UPG_TICKET_CATEGORY, embed)
    if channel:
        await interaction.followup.send(f"✅ Тикет создан: {channel.mention}",
                                        ephemeral=True)


async def _create_ticket_deferred(interaction, prefix, category_name, embed):
    """create_ticket for the case when the response was already deferred."""
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name=category_name)
    if category is None:
        category = await guild.create_category(category_name)
    chan_name = re.sub(r"\s+", "-", (prefix + interaction.user.display_name).lower())
    if discord.utils.get(guild.text_channels, name=chan_name):
        await interaction.followup.send("У тебя уже есть открытый тикет этого типа.",
                                        ephemeral=True)
        return None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True),
    }
    channel = await guild.create_text_channel(chan_name, category=category,
                                              overwrites=overwrites)
    await channel.send(content=interaction.user.mention, embed=embed,
                       view=ticket_controls_view(guild))
    return channel


# ------------------------- BROADCAST (/broadcast) ----------------------------

class BroadcastConfirm(discord.ui.View):
    def __init__(self, author: discord.User, targets: list, text: str):
        super().__init__(timeout=180)
        self.author = author
        self.targets = targets
        self.text = text
        self.done = False

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.author.id

    @discord.ui.button(label="✅ Отправить", style=discord.ButtonStyle.green)
    async def confirm(self, it: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        for child in self.children:
            child.disabled = True
        await it.response.edit_message(content="📨 Рассылка запущена...", view=self)
        import asyncio
        sent, failed = 0, 0
        for member in self.targets:
            try:
                await member.send(self.text)
                sent += 1
            except discord.Forbidden:
                failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(1.2)  # anti-spam pause
        await it.followup.send(
            f"📨 Рассылка завершена: доставлено **{sent}**, "
            f"не доставлено (закрытые ЛС) **{failed}**.", ephemeral=True)

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.red)
    async def cancel(self, it: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await it.response.edit_message(content="Рассылка отменена.", view=self)


@bot.tree.command(name="broadcast", description="Массовая рассылка участникам (админы)")
@app_commands.describe(текст="Текст рассылки",
                       роль="Отправить только этой роли (необязательно)")
@app_commands.default_permissions(administrator=True)
async def broadcast(interaction: discord.Interaction, текст: str,
                    роль: discord.Role = None):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    targets = [m for m in guild.members
               if not m.bot and (роль is None or роль in m.roles)]
    if not targets:
        await interaction.response.send_message("Нет подходящих получателей.",
                                                ephemeral=True)
        return
    view = BroadcastConfirm(interaction.user, targets, текст)
    await interaction.response.send_message(
        f"📨 Получателей: **{len(targets)}**"
        + (f" (роль {роль.mention})" if роль else " (все участники)")
        + f"\n\nТекст:\n>>> {текст}\n\nПодтверди отправку:",
        view=view, ephemeral=True)


# -------------------------- REFERRALS (/referral) ----------------------------

@bot.tree.command(name="referral", description="Статистика реферального промокода")
@app_commands.describe(код="Твой промокод")
async def referral(interaction: discord.Interaction, код: str):
    import sqlite3
    if not os.path.exists(PROMO_DB_FILE):
        await interaction.response.send_message("База промокодов недоступна.",
                                                ephemeral=True)
        return
    con = sqlite3.connect(PROMO_DB_FILE)
    row = con.execute("SELECT Link, Num, discount, date FROM Referal "
                      "WHERE Youtuber = ? COLLATE NOCASE", (код,)).fetchone()
    con.close()
    if not row:
        await interaction.response.send_message(f"Промокод `{код}` не найден.",
                                                ephemeral=True)
        return
    link, uses, discount, valid = row
    commission = (uses or 0) * COMMISSION_PER_USE
    lines = [f"🏷 Промокод **{код}** — скидка {discount}%, действует до {valid}.",
             f"📊 Использований: **{uses or 0}**"]
    if link:
        lines.append(f"🔗 Ссылка: {link}")
    if COMMISSION_PER_USE:
        lines.append(f"💰 Комиссия: **{commission:,}**".replace(",", " "))
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# --------------------------- CALCULATOR (/calc) ------------------------------

_PULP_NAMES = {
    "солевика": "Мякоть солевика",
    "сластена": "Мякоть сластены",
    "лимонника": "Мякоть лимонника",
    "куборбуза": "Мякоть куборбузы",
}


def _pulp_price(kind: str) -> int:
    """Pulp price: from the resources price book, otherwise from PULP_PRICES."""
    name = _PULP_NAMES[kind]
    found = find_boost(name, load_resources())
    return found[1]["price"] if found else PULP_PRICES.get(kind, 0)


@bot.tree.command(name="calc", description="Проверить ценник (мякоть)")
@app_commands.describe(
    мякоть_солевика="Количество мякоти солевика",
    мякоть_сластены="Количество мякоти сластены",
    мякоть_лимонника="Количество мякоти лимонника",
    мякоть_куборбуза="Количество мякоти куборбуза")
async def calc(interaction: discord.Interaction,
               мякоть_солевика: int = 0, мякоть_сластены: int = 0,
               мякоть_лимонника: int = 0, мякоть_куборбуза: int = 0):
    if not await _require_channel(interaction, CHAN_CALC_ID):
        return
    amounts = {"солевика": мякоть_солевика, "сластена": мякоть_сластены,
               "лимонника": мякоть_лимонника, "куборбуза": мякоть_куборбуза}
    lines, total = [], 0
    for kind, qty in amounts.items():
        if qty and qty > 0:
            price = _pulp_price(kind)
            subtotal = qty * price
            total += subtotal
            lines.append(f"{_PULP_NAMES[kind]} — {qty} шт. × {price:,} = {subtotal:,} ₽"
                         .replace(",", " "))
    if not lines:
        await interaction.response.send_message(
            "Укажи количество хотя бы одной мякоти.", ephemeral=True)
        return
    embed = discord.Embed(title="🧮 Расчёт цены (мякоть)",
                          description="\n".join(lines) +
                          f"\n\n**Итого: {total:,} ₽**".replace(",", " "),
                          color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)


def load_templates() -> dict:
    """Loads all reference icons from templates/. File name = item key."""
    templates = {}
    for fname in os.listdir(TEMPLATES_DIR):
        key, ext = os.path.splitext(fname)
        if ext.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        img = cv2.imread(os.path.join(TEMPLATES_DIR, fname))
        if img is not None:
            templates.setdefault(key, []).append(img)
    # Subfolders = several icon variants of one item:
    # templates/katalizator/1.png, templates/katalizator/2.png etc.
    for sub in os.listdir(TEMPLATES_DIR):
        subdir = os.path.join(TEMPLATES_DIR, sub)
        if not os.path.isdir(subdir):
            continue
        for fname in os.listdir(subdir):
            if os.path.splitext(fname)[1].lower() not in (".png", ".jpg", ".jpeg"):
                continue
            img = cv2.imread(os.path.join(subdir, fname))
            if img is not None:
                templates.setdefault(sub, []).append(img)
    return templates


# --------------------------- RECOGNITION -------------------------------------

def non_max_suppression(boxes, scores, overlap_thresh):
    """Removes duplicate boxes (the same icon found several times)."""
    if not boxes:
        return []
    boxes = np.array(boxes, dtype=float)
    scores = np.array(scores, dtype=float)
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0, xx2 - xx1)
        h = np.maximum(0, yy2 - yy1)
        overlap = (w * h) / (areas[order[1:]] + 1e-9)
        order = order[1:][overlap <= overlap_thresh]
    return [boxes[i].astype(int).tolist() for i in keep]


def find_items(screenshot: np.ndarray, templates: dict) -> dict:
    """Returns {item_key: [boxes of found icons]}.
    For each item all icon variants (multi-templates) are tried,
    matches are merged and duplicates removed via NMS."""
    found = {}
    for key, variants in templates.items():
        boxes, scores = [], []
        for tpl in variants:
            th, tw = tpl.shape[:2]
            if screenshot.shape[0] < th or screenshot.shape[1] < tw:
                continue
            result = cv2.matchTemplate(screenshot, tpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= MATCH_THRESHOLD)
            for x, y in zip(xs, ys):
                boxes.append([x, y, x + tw, y + th])
                scores.append(result[y, x])
        kept = non_max_suppression(boxes, scores, NMS_OVERLAP)
        if kept:
            found[key] = kept
    return found


def read_stack_number(screenshot: np.ndarray, box) -> int | None:
    """OCR of the stack digit in the icon corner. None if there is no digit."""
    if not USE_OCR:
        return None
    try:
        import pytesseract
    except ImportError:
        return None
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    # top-RIGHT corner of the cell - that is where the "53x" count sits
    cx1, cy2 = x1 + int(w * 0.45), y1 + int(h * 0.30)
    crop = screenshot[y1:cy2, cx1:x2]
    if crop.size == 0:
        return None
    crop = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(
        bw, config="--psm 7 -c tessedit_char_whitelist=0123456789"
    )
    digits = re.sub(r"\D", "", text)
    if digits:
        n = int(digits)
        return n if 1 < n < 10000 else None
    return None


def analyze(image_bytes: bytes, price_book: dict | None = None) -> list[dict]:
    """Full cycle: screenshot -> list of {key, name, quantity, price, total}.
    price_book - price book {key: {name, price}}; defaults to prices.json,
    buyout tickets pass resources.json."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    screenshot = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if screenshot is None:
        raise ValueError("Не удалось прочитать изображение")

    prices = price_book if price_book is not None else load_prices()
    templates = load_templates()
    if not templates:
        raise ValueError(
            "Папка templates/ пуста — добавь эталонные иконки предметов (.png)"
        )

    found = find_items(screenshot, templates)
    rows = []
    for key, boxes in found.items():
        info = prices.get(key, {"name": key, "price": 0})
        # quantity: number of identical icons x stack digit (if present)
        qty = 0
        for box in boxes:
            stack = read_stack_number(screenshot, box)
            qty += stack if stack else 1
        price = info.get("price", 0)
        rows.append({
            "key": key,
            "name": info.get("name", key),
            "qty": qty,
            "price": price,
            "total": qty * price,
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


# ----------------------------- BOT COMMANDS ----------------------------------

def format_report(rows: list[dict]) -> str:
    if not rows:
        return ("❌ Ни один предмет из базы не распознан. "
                "Проверь качество скриншота или добавь шаблоны иконок.")
    lines = []
    grand_total = 0
    for r in rows:
        lines.append(
            f"**{r['name']}** — {r['qty']} шт. "
            f"× {r['price']:,} = **{r['total']:,}**".replace(",", " ")
        )
        grand_total += r["total"]
    lines.append(f"\n💰 **Итого: {grand_total:,}**".replace(",", " "))
    return "\n".join(lines)


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")
    if AUTO_UPDATE_HOURS > 0 and not auto_update_prices.is_running():
        auto_update_prices.start()
        print(f"[аукцион] автообновление цен каждые {AUTO_UPDATE_HOURS} ч.")
    if SHEETS_SYNC_HOURS > 0 and not auto_sync_sheets.is_running():
        auto_sync_sheets.start()
        print(f"[таблица] авто-синхронизация цен каждые {SHEETS_SYNC_HOURS} ч.")
    bot.add_view(TicketControls())  # persistent ticket buttons (work after restarts)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print("[команды] слэш-команды зарегистрированы на сервере")
        else:
            await bot.tree.sync()
            print("[команды] слэш-команды зарегистрированы глобально "
                  "(могут появиться в течение часа; укажи GUILD_ID для мгновенной)")
    except Exception as e:
        print(f"[команды] ошибка регистрации слэш-команд: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip().lower()
    is_trigger = any(content.startswith(COMMAND_PREFIX + c) for c in TRIGGER_COMMANDS)

    # Message in a boost ticket channel (not a command) = an order -> price it
    chan_name = getattr(message.channel, "name", "") or ""
    if chan_name.startswith(TICKET_PREFIX) and not content.startswith(COMMAND_PREFIX):
        rows, unknown, total, promo = build_order(message.content)
        if rows or unknown or promo:
            if promo and promo["status"] == "ok":
                promo_increment_usage(promo["code"])
            await message.reply(format_order(rows, unknown, total, promo))
        return

    # Resource buyout ticket: screenshot -> estimate, text -> calc by resources
    if chan_name.startswith(RES_TICKET_PREFIX) and not content.startswith(COMMAND_PREFIX):
        if message.attachments:
            att = message.attachments[0]
            if att.content_type and att.content_type.startswith("image/"):
                async with message.channel.typing():
                    try:
                        data = await att.read()
                        rows = analyze(data, price_book=load_resources())
                        await message.reply("📦 **Смета скупки:**\n" + format_report(rows)
                                            if rows else format_report(rows))
                    except Exception as e:
                        await message.reply(f"⚠️ Ошибка: {e}")
                return
        rows, unknown, total, promo = build_order(message.content,
                                                  book=load_resources())
        if rows or unknown or promo:
            await message.reply(format_order(rows, unknown, total, promo))
        return

    # "Resource calculator" channel: screenshot -> calc at buyout prices
    if (CHAN_CALC_ID and message.channel.id == CHAN_CALC_ID
            and message.attachments and not content.startswith(COMMAND_PREFIX)):
        att = message.attachments[0]
        if att.content_type and att.content_type.startswith("image/"):
            async with message.channel.typing():
                try:
                    data = await att.read()
                    rows = analyze(data, price_book=load_resources())
                    await message.reply("🧮 **Расчёт по ценам скупки:**\n"
                                        + format_report(rows))
                except Exception as e:
                    await message.reply(f"⚠️ Ошибка: {e}")
            return

    # Scan an image: on command with an attachment OR on a bare image in the channel
    if message.attachments:
        att = message.attachments[0]
        if att.content_type and att.content_type.startswith("image/"):
            if is_trigger or not content:
                async with message.channel.typing():
                    try:
                        data = await att.read()
                        rows = analyze(data)
                        await message.reply(format_report(rows))
                    except Exception as e:
                        await message.reply(f"⚠️ Ошибка: {e}")
                return

    await bot.process_commands(message)


@bot.command(name="цена")
@commands.has_permissions(administrator=True)
async def set_price(ctx, key: str, price: int):
    """Set a price manually: !цена katalizator 5000
    A manual price overrides the auction - auto-update is disabled for the item."""
    prices = load_prices()
    if key not in prices:
        await ctx.reply(f"Предмет `{key}` не найден в прайсе. "
                        f"Ключи: {', '.join(prices.keys())}")
        return
    old = prices[key]["price"]
    prices[key]["price"] = price
    prices[key]["manual"] = True
    save_prices(prices)
    news_track("items", prices[key]["name"], old, price)
    await ctx.reply(f"✅ {prices[key]['name']}: цена установлена вручную на {price} "
                    f"(автообновление для предмета выкл, вернуть: `!автоцена {key}`)")


@bot.command(name="автоцена")
@commands.has_permissions(administrator=True)
async def set_auto_price(ctx, key: str):
    """Return an item to auction auto-pricing: !автоцена katalizator"""
    prices = load_prices()
    if key not in prices:
        await ctx.reply(f"Предмет `{key}` не найден в прайсе.")
        return
    if not prices[key].get("item_id"):
        await ctx.reply(f"У «{prices[key]['name']}» нет item_id — "
                        f"на аукционе он не отслеживается, только ручная цена.")
        return
    prices[key].pop("manual", None)
    save_prices(prices)
    await ctx.reply(f"✅ {prices[key]['name']}: снова на автоцене с аукциона. "
                    f"Обновится при ближайшем цикле или по `!обновить`.")


@bot.command(name="обновить")
@commands.has_permissions(administrator=True)
async def force_update(ctx):
    """Force-update prices from the auction: !обновить"""
    async with ctx.typing():
        try:
            report = await update_prices_from_auction()
        except Exception as e:
            await ctx.reply(f"⚠️ Ошибка обновления: {e}")
            return
    await ctx.reply("Обновление цен с аукциона:\n" + "\n".join(report))


@bot.command(name="прайс")
async def show_prices(ctx):
    """Show the full price book."""
    prices = load_prices()
    lines = []
    for k, v in prices.items():
        src = "ручн." if v.get("manual") else ("аукцион" if v.get("item_id") else "не задана")
        lines.append(f"**{v['name']}** (`{k}`) — {v['price']:,} _({src})_".replace(",", " "))
    await ctx.reply("\n".join(lines))


# ---------------------------- BOOST COMMANDS ---------------------------------

@bot.command(name="бусты")
async def list_boosts(ctx):
    """Show the boost price book with aliases."""
    boosts = load_boosts()
    if not boosts:
        await ctx.reply("Прайс бустов пуст. Добавь: `!буст_добавить <ключ> <цена> <название>`")
        return
    lines = []
    for k, v in boosts.items():
        aliases = f" _({', '.join(v.get('aliases', []))})_" if v.get("aliases") else ""
        lines.append(f"**{v['name']}** (`{k}`) — {v['price']:,}{aliases}".replace(",", " "))
    # Discord 2000-char limit - split into chunks
    chunk = []
    for line in lines:
        if sum(len(l) + 1 for l in chunk) + len(line) > 1900:
            await ctx.send("\n".join(chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        await ctx.send("\n".join(chunk))


@bot.command(name="буст")
async def quick_order(ctx, *, order_text: str = ""):
    """Quick calc without a ticket: !буст Солянка 25 (each entry on its own line)"""
    if not order_text:
        await ctx.reply("Напиши заказ после команды, каждая позиция с новой строки.\n"
                        "Пример:\n```!буст Солянка 25\nМорфин 15\nпромокод Gromtrom```")
        return
    rows, unknown, total, promo = build_order(order_text)
    await ctx.reply(format_order(rows, unknown, total, promo))


@bot.command(name="буст_цена")
@commands.has_permissions(administrator=True)
async def boost_price(ctx, key: str, price: int):
    """Change a boost price: !буст_цена solyanka 5000"""
    boosts = load_boosts()
    if key not in boosts:
        await ctx.reply(f"Буст `{key}` не найден. Ключи: {', '.join(boosts.keys())}")
        return
    old = boosts[key]["price"]
    boosts[key]["price"] = price
    save_boosts(boosts)
    news_track("boosts", boosts[key]["name"], old, price)
    await ctx.reply(f"✅ {boosts[key]['name']}: новая цена **{price}**")


@bot.command(name="буст_добавить")
@commands.has_permissions(administrator=True)
async def boost_add(ctx, key: str, price: int, *, name: str):
    """Add a boost: !буст_добавить solyanka 4250 Солянка"""
    boosts = load_boosts()
    if key in boosts:
        await ctx.reply(f"Буст `{key}` уже существует. Изменить цену: `!буст_цена {key} <цена>`")
        return
    boosts[key] = {"name": name, "price": price, "aliases": []}
    save_boosts(boosts)
    await ctx.reply(f"✅ Добавлен буст **{name}** (`{key}`) — {price}")


@bot.command(name="буст_удалить")
@commands.has_permissions(administrator=True)
async def boost_remove(ctx, key: str):
    """Remove a boost: !буст_удалить solyanka"""
    boosts = load_boosts()
    if key not in boosts:
        await ctx.reply(f"Буст `{key}` не найден.")
        return
    name = boosts.pop(key)["name"]
    save_boosts(boosts)
    await ctx.reply(f"🗑 Буст **{name}** удалён из прайса.")


@bot.command(name="синоним")
@commands.has_permissions(administrator=True)
async def boost_alias_add(ctx, key: str, *, alias: str):
    """Add a boost alias: !синоним solyanka солянка домашняя"""
    boosts = load_boosts()
    if key not in boosts:
        await ctx.reply(f"Буст `{key}` не найден. Ключи: {', '.join(boosts.keys())}")
        return
    aliases = boosts[key].setdefault("aliases", [])
    if alias in aliases:
        await ctx.reply(f"Синоним «{alias}» уже есть у **{boosts[key]['name']}**.")
        return
    aliases.append(alias)
    save_boosts(boosts)
    await ctx.reply(f"✅ **{boosts[key]['name']}** теперь также распознаётся как «{alias}».")


@bot.command(name="синоним_удалить")
@commands.has_permissions(administrator=True)
async def boost_alias_remove(ctx, key: str, *, alias: str):
    """Remove an alias: !синоним_удалить solyanka солянка домашняя"""
    boosts = load_boosts()
    if key not in boosts:
        await ctx.reply(f"Буст `{key}` не найден.")
        return
    aliases = boosts[key].setdefault("aliases", [])
    if alias not in aliases:
        await ctx.reply(f"У **{boosts[key]['name']}** нет синонима «{alias}».")
        return
    aliases.remove(alias)
    save_boosts(boosts)
    await ctx.reply(f"🗑 Синоним «{alias}» удалён у **{boosts[key]['name']}**.")


@bot.command(name="закрыть")
async def close_ticket(ctx):
    """Close a boost ticket channel (used inside the ticket)."""
    chan_name = getattr(ctx.channel, "name", "") or ""
    if not chan_name.startswith(TICKET_PREFIX):
        await ctx.reply("Эта команда работает только в тикет-каналах бустов.")
        return
    await ctx.send("🔒 Тикет закрывается...")
    await ctx.channel.delete()


# --------------------- PROMO CODES AND SHEET (COMMANDS) ----------------------

@bot.command(name="промокоды")
@commands.has_permissions(administrator=True)
async def promo_list(ctx):
    """Show all promo codes from Base.db."""
    import sqlite3
    if not os.path.exists(PROMO_DB_FILE):
        await ctx.reply("Файл Base.db не найден рядом с bot.py.")
        return
    con = sqlite3.connect(PROMO_DB_FILE)
    rows = con.execute("SELECT Youtuber, Link, Num, discount, date FROM Referal").fetchall()
    con.close()
    if not rows:
        await ctx.reply("Промокодов нет. Добавь: `!промо_добавить <код> <скидка%> <ГГГГ-ММ-ДД>`")
        return
    lines = [f"**{r[0]}** — скидка {r[3]}%, до {r[4]}, использований: {r[2] or 0}"
             + (f", ссылка: {r[1]}" if r[1] else "") for r in rows]
    await ctx.reply("\n".join(lines))


@bot.command(name="промо_добавить")
@commands.has_permissions(administrator=True)
async def promo_add(ctx, code: str, discount: int, valid_until: str, link: str = ""):
    """Add a promo code: !промо_добавить Gromtrom 15 2026-11-11 [link]"""
    import sqlite3
    from datetime import date
    try:
        date.fromisoformat(valid_until)
    except ValueError:
        await ctx.reply("Дата должна быть в формате ГГГГ-ММ-ДД, например 2026-11-11.")
        return
    con = sqlite3.connect(PROMO_DB_FILE)
    con.execute("INSERT OR REPLACE INTO Referal (Youtuber, Link, Num, discount, date) "
                "VALUES (?, ?, COALESCE((SELECT Num FROM Referal WHERE Youtuber = ?), 0), ?, ?)",
                (code, link, code, discount, valid_until))
    con.commit()
    con.close()
    await ctx.reply(f"✅ Промокод **{code}**: скидка {discount}% до {valid_until}")


@bot.command(name="промо_удалить")
@commands.has_permissions(administrator=True)
async def promo_remove(ctx, code: str):
    """Remove a promo code: !промо_удалить Gromtrom"""
    import sqlite3
    con = sqlite3.connect(PROMO_DB_FILE)
    cur = con.execute("DELETE FROM Referal WHERE Youtuber = ? COLLATE NOCASE", (code,))
    con.commit()
    con.close()
    if cur.rowcount:
        await ctx.reply(f"🗑 Промокод **{code}** удалён.")
    else:
        await ctx.reply(f"Промокод `{code}` не найден.")


@bot.command(name="синк_цен")
@commands.has_permissions(administrator=True)
async def sync_prices_cmd(ctx):
    """Sync boost prices with the Google Sheet: !синк_цен"""
    import asyncio
    async with ctx.typing():
        try:
            report = await asyncio.to_thread(sync_prices_from_sheets)
        except Exception as e:
            await ctx.reply(f"⚠️ Ошибка синхронизации: {e}")
            return
    await ctx.reply(report
                    + "\n\n📢 Изменения накоплены для объявления. "
                      "Опубликовать: `!обновить_ценник`")


# ----------------- PRICE IMPORT FROM FILES (.json/.csv) ----------------------

_BOOKS = {
    "бусты": (BOOSTS_FILE, "бустов"),
    "предметы": (PRICES_FILE, "предметов"),
    "ресурсы": (RESOURCES_FILE, "ресурсов"),
}
# import book -> announcement category
_NEWS_BY_BOOK = {"бусты": "boosts", "предметы": "items", "ресурсы": "resources"}


def _load_book(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_book(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_price_file(filename: str, raw: bytes) -> dict:
    """Parses .json or .csv into {name: price}.
    JSON: {"Name": 100} | [{"name": "Name", "price": 100}, ...] | the bot's
    price book format. CSV: "Name;price" or "Name,price" rows."""
    text = raw.decode("utf-8-sig", errors="replace")
    out = {}
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and "price" in v:
                    out[v.get("name", k)] = int(v["price"])
                elif isinstance(v, (int, float)):
                    out[k] = int(v)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item and "price" in item:
                    out[str(item["name"])] = int(item["price"])
    else:  # csv
        import csv as _csv
        sample = text[:2048]
        dialect = _csv.Sniffer().sniff(sample, delimiters=";,\t") if sample.strip() else None
        reader = _csv.reader(text.splitlines(), dialect) if dialect else None
        rows = list(reader) if reader else [ln.split(";") for ln in text.splitlines()]
        for row in rows:
            if len(row) >= 2:
                name = row[0].strip().strip("[]")
                price_str = re.sub(r"[^\d]", "", row[1])
                if name and price_str:
                    out[name] = int(price_str)
    return out


@bot.command(name="импорт_цен")
@commands.has_permissions(administrator=True)
async def import_prices(ctx, book: str = ""):
    """Import prices from a file: !импорт_цен бусты|предметы|ресурсы + .json/.csv attachment"""
    if book not in _BOOKS:
        await ctx.reply("Укажи прайс: `!импорт_цен бусты`, `!импорт_цен предметы` "
                        "или `!импорт_цен ресурсы` + прикрепи файл .json/.csv")
        return
    if not ctx.message.attachments:
        await ctx.reply("Прикрепи к сообщению файл .json или .csv с ценами.")
        return
    att = ctx.message.attachments[0]
    if not att.filename.lower().endswith((".json", ".csv")):
        await ctx.reply("Поддерживаются только файлы .json и .csv")
        return
    path, book_ru = _BOOKS[book]
    try:
        pairs = _parse_price_file(att.filename, await att.read())
    except Exception as e:
        await ctx.reply(f"⚠️ Не удалось разобрать файл: {e}")
        return
    if not pairs:
        await ctx.reply("⚠️ В файле не найдено ни одной пары «название — цена».")
        return

    data = _load_book(path)
    by_name = {_normalize(v.get("name", k)): k for k, v in data.items()}
    updated, added = 0, 0
    for name, price in pairs.items():
        key = by_name.get(_normalize(name))
        if key is not None:
            old = data[key]["price"]
            data[key]["price"] = price
            if book == "предметы":
                data[key]["manual"] = True
            if old != price:
                news_track(_NEWS_BY_BOOK[book], data[key].get("name", name), old, price)
            updated += 1
        else:
            key = slugify(name)
            base, n = key, 2
            while key in data:
                key = f"{base}_{n}"
                n += 1
            entry = {"name": name, "price": price}
            if book == "бусты" or book == "ресурсы":
                entry["aliases"] = []
            if book == "предметы":
                entry.update({"item_id": None, "manual": True})
            data[key] = entry
            added += 1
    _save_book(path, data)
    await ctx.reply(f"✅ Импорт в прайс {book_ru}: обновлено **{updated}**, "
                    f"добавлено **{added}** позиций.")


# ------------- REPUTATION AND SHELTER (PRICE MANAGEMENT) ---------------------

@bot.command(name="репы")
async def rep_list(ctx):
    """Arsen reputation price book (price per 1k rep)."""
    lines = [f"**{v['name']}** (`{k}`) — {v['price_per_1k']:,} за 1к репы".replace(",", " ")
             for k, v in load_reputation().items()]
    await ctx.reply("\n".join(lines) if lines else "Прайс репутации пуст.")


@bot.command(name="реп_цена")
@commands.has_permissions(administrator=True)
async def rep_set_price(ctx, key: str, price_per_1k: int):
    """Change the price per 1k rep: !реп_цена filtr 350000"""
    data = load_reputation()
    if key not in data:
        await ctx.reply(f"Не найдено `{key}`. Ключи: {', '.join(data.keys())}")
        return
    old = data[key]["price_per_1k"]
    data[key]["price_per_1k"] = price_per_1k
    save_reputation(data)
    news_track("reputation", data[key]["name"], old, price_per_1k)
    await ctx.reply(f"✅ {data[key]['name']}: {price_per_1k:,} за 1к репы".replace(",", " "))


@bot.command(name="реп_добавить")
@commands.has_permissions(administrator=True)
async def rep_add(ctx, key: str, price_per_1k: int, *, name: str):
    """Add a reputation item: !реп_добавить filtr 300000 Фильтр"""
    data = load_reputation()
    data[key] = {"name": name, "price_per_1k": price_per_1k, "aliases": []}
    save_reputation(data)
    await ctx.reply(f"✅ Добавлено: **{name}** — {price_per_1k:,} за 1к репы"
                    .replace(",", " "))


@bot.command(name="реп_удалить")
@commands.has_permissions(administrator=True)
async def rep_remove(ctx, key: str):
    """Remove a reputation item: !реп_удалить filtr"""
    data = load_reputation()
    if key not in data:
        await ctx.reply(f"Не найдено `{key}`.")
        return
    name = data.pop(key)["name"]
    save_reputation(data)
    await ctx.reply(f"🗑 **{name}** удалён из прайса репутации.")


@bot.command(name="апгрейды")
async def upg_list(ctx):
    """Shelter skill upgrade price book (full price, level 1->5)."""
    lines = [f"**{v['name']}** (`{k}`) — {v['price']:,}".replace(",", " ")
             for k, v in load_upgrades().items()]
    await ctx.reply("\n".join(lines) if lines else "Прайс прокачки пуст.")


@bot.command(name="апгрейд_цена")
@commands.has_permissions(administrator=True)
async def upg_set_price(ctx, key: str, price: int):
    """Change a skill upgrade price: !апгрейд_цена inzheneriya 21000000"""
    data = load_upgrades()
    if key not in data:
        await ctx.reply(f"Не найдено `{key}`. Ключи: {', '.join(data.keys())}")
        return
    old = data[key]["price"]
    data[key]["price"] = price
    save_upgrades(data)
    news_track("upgrades", data[key]["name"], old, price)
    await ctx.reply(f"✅ {data[key]['name']}: новая цена {price:,}".replace(",", " "))


# ------------------------------ FAQ PANEL ------------------------------------

@bot.command(name="faq_панель")
@commands.has_permissions(administrator=True)
async def faq_panel(ctx):
    """Send a message with a "FAQ" button to the current channel."""
    if not FAQ_CHANNEL_ID:
        await ctx.reply("Сначала укажи `FAQ_CHANNEL_ID` в начале bot.py "
                        "(ID канала с частыми вопросами).")
        return
    url = f"https://discord.com/channels/{ctx.guild.id}/{FAQ_CHANNEL_ID}"
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="❓ Частые вопросы",
                                    style=discord.ButtonStyle.link, url=url))
    await ctx.send("Возникли вопросы? Загляни в раздел частых вопросов 👇", view=view)


@bot.command(name="обновить_ценник")
@commands.has_permissions(administrator=True)
async def price_news_cmd(ctx):
    """Post/update the price change announcement. The command message is deleted."""
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    result = await post_price_news()
    if not result.startswith("✅"):
        await ctx.send(result, delete_after=15)


@bot.command(name="шаблоны")
async def list_templates(ctx):
    """Show which items have reference icons loaded."""
    templates = load_templates()
    if templates:
        await ctx.reply("Загружены шаблоны: " + ", ".join(sorted(templates)))
    else:
        await ctx.reply("Шаблонов нет. Положи .png иконки в папку templates/.")


if __name__ == "__main__":
    bot.run(TOKEN)
