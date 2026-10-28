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

Python 3.10+, discord.py, OpenCV, NumPy, pytesseract (Tesseract OCR), aiohttp, requests, gspread, SQLite, Docker, GitHub Actions (lint + image build).

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
5. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` (and the other secrets, see below). Other settings are constants at the top of `bot.py`.
6. Run: `python bot.py` - the console should print that the bot is online.

## Configuration

Secrets and deployment settings are read from environment variables (or a local `.env` file, see `.env.example`). Never put tokens into `bot.py` and never commit `.env`.

| Environment variable | What to put | Required |
| --- | --- | --- |
| `DISCORD_TOKEN` | Discord bot token | Yes |
| `GUILD_ID` | Your server ID (right-click the server -> Copy Server ID; slash commands appear instantly) | Recommended |
| `EXBO_CLIENT_ID` / `EXBO_CLIENT_SECRET` | From the EXBO application (Bot Access), for the production auction API | When approved |
| `EXBO_API_TOKEN` | Demo token from the EXBO docs; used only when the client ID/secret are empty | For demo mode |
| `EXBO_API_BASE` | Switch to `https://eapi.stalzone.net` for production | When approved |
| `TESSERACT_CMD` | Path to the tesseract binary (Windows default install path is detected automatically; on Linux/Docker it is taken from PATH) | If path differs |
| `DATA_DIR` | Folder the bot writes to (prices, templates, databases). Defaults to the project folder; Docker uses `/data` | Optional |

The remaining settings are constants at the top of `bot.py`:

| Setting | What to put | Required |
| --- | --- | --- |
| `TICKET_MESSAGE` | Canned message sent into every new boost ticket | Optional |
| `FAQ_CHANNEL_ID` | FAQ channel ID (enables the FAQ button on tickets) | Optional |
| `TICKET_ADMIN_ROLE` | Role name allowed to close tickets (empty = "Manage Channels" permission) | Optional |
| `PRICE_NEWS_CHANNEL_ID`, `CHAN_*_ID` | Channel IDs for announcements and per-service slash command binding (0 = disabled) | Optional |
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

## Run with Docker

```
cp .env.example .env        # fill in DISCORD_TOKEN and the other values
mkdir -p data               # put credentials.json (Google Sheets key) here if you use sheet sync
docker compose up -d --build
```

The image bundles Python and Tesseract. Everything the bot writes (prices, downloaded icons, `Base.db`, announcement state) lives in the mounted `./data` folder, so it survives restarts and image rebuilds; default price books are copied there on first start. GitHub Actions lints the code and builds the image on every push.

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
├── bot.py              - the whole bot (non-secret settings at the top of the file)
├── prices.json         - item price book (scanner + auction)
├── boosts.json         - boost price book (+ aliases)
├── reputation.json     - prices per 1k of Arsen reputation
├── upgrades.json       - shelter skill upgrade prices
├── item_index.json     - game item index (for auto icon download)
├── requirements.txt    - dependencies
├── .env.example        - template for secrets (copy to .env)
├── Dockerfile          - container image (Python + Tesseract)
├── docker-compose.yml  - one-command run with a persistent ./data volume
├── .github/workflows/  - CI: lint + Docker build
└── templates/          - reference icons for recognition
```

## Links

- Repository: https://github.com/WERTIX182/stalzone-bot
- Author: WERTIX182
