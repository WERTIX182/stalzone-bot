# STALZONE Bot

Discord bot for a STALZONE game community: recognizes items on inventory screenshots, prices them, and runs boost orders, resource buyout, Arsen reputation and shelter upgrade services through ticket channels.

Russian readme: [README.ru.md](README.ru.md)

## What it does

1. A player posts an inventory screenshot (as a plain image or with the `!скан` command).
2. The bot matches screenshot regions against reference icons in `templates/` (OpenCV template matching - fast, free, works offline).
3. Quantity is counted two ways at once: multiple identical icons = multiple items, plus the stack digit in the icon corner is read via OCR (Tesseract).
4. Prices come from JSON price books and can be auto-updated from the official EXBO auction API or synced from a Google Sheet.
5. The bot replies with a formatted report: name - quantity - price - total.

## Key features

- **Inventory scanner** - recognizes items on screenshots, OCR of stack counts, priced report.
- **Boost orders** - `/buy_boost` creates a private ticket; the player writes the order in free form (one position per line), the bot prices it instantly, with name aliases and promo code discounts (SQLite).
- **Resource buyout** - `/sell_resources` ticket: a screenshot turns into an estimate at current buyout prices.
- **More services** - `/sell_other` (misc items), `/buy_reputation` (Arsen reputation, priced per 1k rep), `/buy_upgrade` (shelter skills, price scales with remaining levels, level can be OCR-read from a screenshot), `/calc` (pulp calculator).
- **Auction auto-pricing** - pulls minimum lot prices from the EXBO API (OAuth client_credentials or a static token) every `AUTO_UPDATE_HOURS` hours; manual prices (`!цена`) override it per item.
- **Google Sheets sync** - boost and resource prices sync from a spreadsheet every `SHEETS_SYNC_HOURS` hours or on `!синк_цен`; official item icons are auto-downloaded as recognition templates.
- **Price change announcements** - changes accumulate and are published to a dedicated channel on `!обновить_ценник` or after an auto-sync.
- **Admin tooling** - price management commands, price import from .json/.csv files, member broadcast with confirmation, referral promo code stats.

## Stack

Python 3.10+, discord.py, OpenCV, NumPy, pytesseract (Tesseract OCR), aiohttp, requests, gspread, SQLite.

## Installation

1. Install **Python 3.10+**.
2. Install dependencies: `pip install -r requirements.txt`
3. Install **Tesseract OCR** (only needed for stack digits and shelter level reading):
   - Windows: installer from https://github.com/UB-Mannheim/tesseract/wiki
   - Linux: `sudo apt install tesseract-ocr`
   - If you do not need OCR, set `USE_OCR = False` at the top of `bot.py`.
4. Create a bot at https://discord.com/developers/applications:
   - New Application -> Bot -> copy the **token**;
   - enable **Message Content Intent** (Bot -> Privileged Gateway Intents);
   - invite the bot with permissions to read/send messages, attach files and **Manage Channels** (required for tickets).
5. Fill in the settings at the top of `bot.py` (see below).
6. Run: `python bot.py` - the console should print that the bot is online.

## Configuration

All settings are constants at the top of `bot.py`. Placeholders to fill in:

| Setting | What to put | Required |
| --- | --- | --- |
| `TOKEN` | Discord bot token (replace `YOUR_DISCORD_BOT_TOKEN_HERE`) | Yes |
| `GUILD_ID` | Your server ID (right-click the server -> Copy Server ID; slash commands appear instantly) | Recommended |
| `CLIENT_ID` / `CLIENT_SECRET` | From the EXBO application (Bot Access), for the production auction API | When approved |
| `API_TOKEN` | Demo token from the EXBO docs (replace `YOUR_EXBO_API_TOKEN_HERE`); used only when CLIENT_ID/SECRET are empty | For demo mode |
| `API_BASE` | Switch to `https://eapi.stalzone.net` for production | When approved |
| `TICKET_MESSAGE` | Canned message sent into every new boost ticket | Optional |
| `FAQ_CHANNEL_ID` | FAQ channel ID (enables the FAQ button on tickets) | Optional |
| `TICKET_ADMIN_ROLE` | Role name allowed to close tickets (empty = "Manage Channels" permission) | Optional |
| `PRICE_NEWS_CHANNEL_ID`, `CHAN_*_ID` | Channel IDs for announcements and per-service slash command binding (0 = disabled) | Optional |
| `TESSERACT_CMD` | Path to tesseract.exe (default is the standard install path; `None` on Linux/Mac) | If path differs |
| `AUTO_UPDATE_HOURS` / `SHEETS_SYNC_HOURS` | Auction update / sheet sync intervals (0 = off) | Optional |
| `MATCH_THRESHOLD` | Recognition accuracy: 0.85 default; 0.80 if items are missed, 0.90 on false positives | Only if needed |

### Files not included in the repository (create them yourself)

- **`credentials.json`** - Google service account key for Sheets sync. Google Cloud Console -> create a service account -> download the JSON key -> enable the Google Sheets API -> share your spreadsheet with the key's `client_email`. The spreadsheet must be named `ДЛЯ ЛЮБИМЫХ ПРОГРАММИСТОВ` with sheets `ЦЕНЫ БУСТОВ` and `ЦЕНЫ РЕСУРСОВ` (name-price pairs in the C2:P24 range). Never commit this file - it is in `.gitignore`.
- **`Base.db`** - SQLite promo code database. Optional: without it promo codes simply report "not found". To create it:

  ```sql
  CREATE TABLE Referal (
      Youtuber TEXT NOT NULL,
      Link     TEXT,
      Num      INTEGER,
      discount INTEGER,
      date     INTEGER,
      PRIMARY KEY (Youtuber)
  );
  ```

- **`resources.json`** and **`price_news.json`** - created automatically at runtime (sheet sync / announcements).
- **`templates/`** - reference icons for recognition. Fill it with item icons named after the price book keys (`katalizator.png`, ...), or let the bot download official icons automatically on sheet sync. Subfolders hold several variants of one icon.

## Usage examples

Player-facing:

| Command | Description |
| --- | --- |
| `!скан` + image (or a bare image) | Recognize items and print a priced report |
| `/buy_boost` | Open a boost order ticket |
| `/sell_resources` | Open a resource buyout ticket (screenshot -> estimate) |
| `/sell_other`, `/buy_reputation`, `/buy_upgrade`, `/calc` | Other services |
| `!прайс`, `!бусты`, `!репы`, `!апгрейды` | Show price books |
| `!буст <order>` | Quick price check without a ticket |

Admin-only:

| Command | Description |
| --- | --- |
| `!цена <key> <price>` / `!автоцена <key>` | Manual price / back to auction auto-pricing |
| `!обновить` | Force auction price update |
| `!синк_цен` | Sync prices with the Google Sheet |
| `!обновить_ценник` | Publish the price change announcement |
| `!буст_добавить`, `!буст_цена`, `!буст_удалить`, `!синоним` | Boost price book management |
| `!промокоды`, `!промо_добавить`, `!промо_удалить` | Promo code management |
| `!импорт_цен бусты\|предметы\|ресурсы` + file | Import prices from .json/.csv |
| `/broadcast` | Mass DM members (with confirmation) |
| `!закрыть` | Close a boost ticket (inside the ticket) |

## Project structure

```
stalzone-bot/
├── bot.py              - the whole bot (all settings at the top of the file)
├── prices.json         - item price book (scanner + auction)
├── boosts.json         - boost price book (+ aliases)
├── reputation.json     - prices per 1k of Arsen reputation
├── upgrades.json       - shelter skill upgrade prices
├── item_index.json     - game item index (for auto icon download)
├── requirements.txt    - dependencies
└── templates/          - reference icons for recognition
```

## Links

- Repository: https://github.com/WERTIX182/stalzone-bot
- Author: WERTIX182
