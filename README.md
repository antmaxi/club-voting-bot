# 📚 Club Voting Bot

<p align="center">
  <img src="docs/img/club-voting-bot.jpg" alt="Club Voting Bot" width="420">
</p>

A Telegram bot (English / Russian / German) for club voting: members add entries, vote on what to pick next, and track the archive. Default is a book bot; set `CLUB_ENTITY=film` for a film bot.

How the code is structured: [Architecture](docs/architecture.md).

## 🌟 Features

- **Languages:** Switch between English, Russian, and German in the `/settings` menu.
- **Book Management:** Add books with details like title, author, page count, fiction/non-fiction status, review links, and descriptions.
- **Voting System:** Users can vote on books with three options:
  - ✅ **Want to read** (+1 point)
  - 😐 **Don't care** (+0.5 points)
  - ❌ **Don't want to read** (-1 point)
  The ranking score is the **sum** of those points (not an average). `/top` has a button that explains this. Admins can optionally count only votes from members with a positive **attendance surplus** (see below).
- **Top Rated Books:** View a list of undiscussed books ranked by their votes score.
- **Smart Notifications:** 
  - Get notified when a new book is added (with a 5-minute delay).
  - Notifications include a voting card to vote directly from the message.
  - Opt-in or out via `/settings`.
  - **Admin Notifications:** The main admin (first ID in `ADMIN_IDS`) receives notifications when the bot starts up or shuts down.
  - **Voting Reminders:** Admins can nudge users who have not voted yet — either for the current Top 5 (including ties at 5th place) or for one chosen book. Reminders go to users who opted in to new-book notifications in `/settings`.
  - **Group Chat Notifications:** Optionally post new book announcements to the club chat (toggle in `/adminconsole`). Admins can also post on-demand voting reminders to the group chat for the Top 5 or a single book — same card format as new-book posts, with inline vote buttons so members can vote in the common chat.
- **Access Control:** Optionally restrict bot usage to members of a specific Telegram chat (via `ALLOWED_CHAT_ID`). For this bot should be inside the chat too
- **Archive:** Track books that have already been discussed.

## 🛠 Commands

### User Commands
- `/start` or `/help`: Welcome message and command list.
- `/info`: About the bot and last update time.
- `/add`: Add a new book to the list. After the title you can fill the other fields with AI help or by hand. Before the book is saved, the bot shows the full entry so you can go back and edit a field. **Save** keeps an unfinished draft; `/add` can continue a saved draft.
- `/list_and_vote`: See all undiscussed books (option to filter for unvoted only).
- `/top`: See the highest-rated books.
- `/settings`: Change your notification and language preferences.
- `/edit`: Edit a book's details (limited to book owner or admins).
- `/delete`: Delete a book (limited to book owner or admins).
- `/discussed`: View books already discussed by the club.
- `/cancel`: Abort the current interactive command.

### Admin Commands
- `/adminconsole`: Centralized panel for admins to:
  - **Mark discussed:** Mark a book as discussed and move it to the archive.
  - **Hide books:** Temporarily hide books from the `/list_and_vote` and `/top` without deleting them.
  - **Send Reminders (DM):** Broadcast a voting reminder in private chat to opted-in users who have not voted yet — for the Top 5 or one selected book.
  - **Post reminders to group chat:** Post voting cards to `ALLOWED_CHAT_ID` on demand (Top 5 or one book), independent of the automatic new-book toggle.
  - **Chat Notifications:** Toggle whether newly added books are posted to the group chat automatically (after the usual 5-minute delay).
  - **Vote counting:** Switch between counting **all votes** and counting only votes from members with a positive **attendance surplus**. When attendance mode is on, the `/top` "How a score is calculated" popup includes this rule.
  - **Meetings:** Record who attended a discussion — **one meeting per entry**. Recording again for the same book updates the attendee list (add/remove people) instead of starting over. The meeting list can edit attendance or delete the meeting. Attendance is what the surplus (below) is built from.
  - **Export / import (JSON):** Copy a single book to another bot instance — pick a book under **Export book (JSON)**, copy the message text, then on the target instance use **Import book (JSON)** and paste it in one message. Votes are not transferred; attribution (`added_by_name` / `@username`) is preserved so the original submitter can still edit on the new instance. Works for discussed or hidden books too.

### Attendance-based vote counting

In `/adminconsole`, **Vote counting** can be set to **attendance**. Rankings (`/list_and_vote`, `/top`, book cards) then ignore votes from people whose running attendance surplus is 0. Everyone can still cast and change votes; only the tally used for ranking changes.

Surplus is precomputed at bot start (and whenever a meeting is recorded, or when the calendar date changes). Meetings are walked in date order for each person, **excluding discussions whose date is still in the future** (compared with today in the bot’s display timezone):

1. Start at 0.
2. Attended that meeting: **+1**.
3. Missed that meeting: **−1**, but never below 0.
4. That person’s votes count only if the final surplus is **at least 1**.

You can record attendance for a scheduled future discussion; it does not affect rankings until that date. Each discussed entry has at most one meeting: saving again updates who was there. Skipping a long stretch of meetings parks the surplus at 0; coming back to one meeting restores it to 1 and voting ability with it. If no past meetings have been recorded yet, all votes still count.

When recording or viewing attendance, the bot shows each person’s Telegram display name (and `@username` when they have one), sorted by how many meetings they have attended, then by that shown name. Numeric Telegram IDs appear only if the name cannot be resolved.

## 🖼 Screenshots
![Top](docs/img/screenshots/2026-04-04_screenshot_top.png)
![List](docs/img/screenshots/2026-04-04_screenshot_list.png)
![Settings](docs/img/screenshots/2026-04-04_screenshot_settings.png)

## 🚀 Getting Started

### Prerequisites
- Docker and Docker Compose
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### Setup
1. **Clone the repository:**
   ```bash
   git clone https://github.com/antmaxi/club-voting-bot.git
   cd club-voting-bot
   ```

2. **Configure environment variables:**
   Create a `.env` file in the project root with the following content:
   ```env
   BOT_TOKEN="your_token_from_BotFather"
   ADMIN_IDS="ID_1,ID_2"
   GITHUB_REPO="https://github.com/yourusername/your-repo"
   ALLOWED_CHAT_ID="CHAT_ID"  # Optional: Restrict bot usage to members of this chat
   CHAT_LANG="ru"             # Optional: Group-chat language: en, ru, or de (default: ru)
   CLUB_ENTITY="book"         # Optional: What to vote on — book (default) or film
   # Optional: which extra fields to ask on /add and show on cards (title is always required).
   # Unset = all except language_levels. "all" includes CEFR too. Aliases: review_link, runtime.
   # ENTRY_FIELDS="author,pages,fiction,review,original_language,creation_year,description"
   ASK_LANGUAGE_LEVEL="1"     # Optional: also ask CEFR A1–C2 (adds language_levels to ENTRY_FIELDS)
   DISPLAY_UTC_OFFSET_HOURS="2"  # Optional: UTC offset for displayed times (default: 2 → UTC+2)
   INSTANCE_NAME="book"       # Optional: Label prepended to error alerts (see "Logs & error alerts")
   ERROR_ALERTS="1"           # Optional: Forward ERROR-level logs to the main admin (default: on)
   # Optional: LLM for /add field suggestions (after the title).
   # OpenAI-compatible Chat Completions (OpenAI, xAI, OpenRouter, Groq, …).
   # An xai-… key is enough: base URL and grok-4.6 are inferred if unset.
   # LLM_API_KEY="xai-..."     # or XAI_API_KEY / OPENAI_API_KEY
   # LLM_API_BASE="https://api.x.ai/v1"
   # LLM_MODEL="grok-4.6"
   # LLM_REASONING_EFFORT="low"   # grok default in this bot; high needs ~2+ min
   # LLM_TIMEOUT_SECONDS="120"
   # Or bill a Cursor subscription instead of a chat API (Pro+):
   # LLM_PROVIDER="cursor"     # cursor | chat (default). openai/xai aliases stay on chat
   # CURSOR_API_KEY="crsr_..." # https://cursor.com/dashboard/integrations
   # LLM_MODEL="composer-2.5"  # default when LLM_PROVIDER=cursor
   # The Docker image includes cursor-sdk. A local venv needs:
   # pip install -r requirements-cursor.txt
   # Optional (server only): colon-separated paths for deploy_bots.sh / logs.sh
   DEPLOY_REPOS="/root/club-voting-bot:/root/philo-club-bot"
   ```
   `CHAT_LANG` applies only to shared group posts (automatic new-book announcements, admin-posted voting reminders in the group, and the vote cards attached to them). Messages sent to individuals always follow that
   person's own `/settings` language.

### Club entity type (`CLUB_ENTITY`)

Set `CLUB_ENTITY=book` (default) or `CLUB_ENTITY=film` to choose what members add and vote on. Voting, rankings, and the archive work the same; only prompts, labels, and command menu text change. If you do not set `ALLOWED_CHAT_NAME`, the default group name follows the entity (`Книжный клуб` vs `Киноклуб`).

The database schema is shared. For films, fields are reused as follows:

| Column in DB | Books | Films |
|--------------|-------|-------|
| `author` | Author | Director |
| `pages` | Page count | Runtime (minutes) |
| `fiction` | Fiction / non-fiction | Feature film / documentary |
| `language_levels` | Comma-separated CEFR levels (A1–C2), if enabled | Same |

### Entry fields (`ENTRY_FIELDS`)

Title is always required. Every other property can be turned off. `ENTRY_FIELDS` is a comma-separated list of:

`author`, `pages`, `fiction`, `review`, `original_language`, `creation_year`, `language_levels`, `description`

Aliases: `review_link` → `review`, `runtime` → `pages`. Listing `title` is ignored (it is always on).

- **Unset or empty:** all of the fields above except `language_levels` (same as the original `/add` flow).
- **`ENTRY_FIELDS=all`** or **`*`:** every optional field, including CEFR levels.
- **`ASK_LANGUAGE_LEVEL=1`:** still adds `language_levels` even if you listed a subset (or left `ENTRY_FIELDS` unset).
- Disabled fields are skipped on `/add` and `/edit`, and hidden on cards, compact lists, and `/top` even if older database rows still have values.

Example: `ENTRY_FIELDS=author,description` asks only title, author, and description.

Set `ASK_LANGUAGE_LEVEL=1` (or include `language_levels` in `ENTRY_FIELDS`) to prompt members to pick one or more CEFR levels (A1 through C2) via inline buttons when adding or editing an entry.

You can run separate bot instances (different tokens, different `.env` files) for a book bot and a film bot on the same codebase. Additional entity kinds (e.g. podcasts, TV series, board games) can be added later by extending the overlay tables in `bookclub/i18n.py`.

### `/add` AI suggestions

After you enter a title (and confirm if a similar title already exists), `/add` asks whether to fill the other enabled `ENTRY_FIELDS` with AI help or by hand — or, if you already have unfinished drafts, `/add` starts with **Use AI**, **I'll fill it in**, and **Continue a saved draft**. The review link is asked next (when enabled), before author, pages, and the rest.

**Use AI** first looks up a real catalog/review URL (Wikipedia, Google Books, or Open Library; LLM guesses are fetched and dropped if the page is not about the title). You confirm or replace that link, then the bot reads the page and fills the other fields — including page count or runtime when the page lists it. By default it uses an OpenAI-compatible Chat Completions API (`POST {LLM_API_BASE}/chat/completions`) for extraction. Set `LLM_PROVIDER=cursor` with `CURSOR_API_KEY` to use a local Cursor agent (`composer-2.5`, no tools) billed to the Cursor subscription instead of that API. Tap **Forward** to keep a suggestion or a previously saved answer, or send a new value to replace it. **Edit** puts the current text in the message field so you can change it (then send). **I'll fill it in** is the original manual wizard. After the last field, the bot shows the full entry so you can go back to change a field or confirm the add. Tap **Save** (or `/save`) to keep progress; a later `/add` can resume that draft, including which fields are still AI suggestions (unless you edited them).

If the bot has **inline mode** enabled in @BotFather (`/setinline`), **Edit** inserts the current value into the compose field. Otherwise it copies the text so you can paste and change it. Values longer than Telegram’s 256-character copy/inline limit open a reply prompt with the text ready to copy.

The AI choice is offered only when a provider is configured: a chat API key (`LLM_API_KEY`, or `XAI_API_KEY` / `OPENAI_API_KEY`), or `LLM_PROVIDER=cursor` with `CURSOR_API_KEY` (from [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations); Pro or higher). An `xai-…` key infers `LLM_API_BASE=https://api.x.ai/v1` and `LLM_MODEL=grok-4.6` if those are unset or still set to OpenAI defaults (`api.openai.com` / `gpt-4o-mini`). `LLM_PROVIDER=cursor` defaults to `composer-2.5` and ignores leftover `gpt-*` / `grok-4.6` model names. `CURSOR_API_KEY` is not used as the chat-completions bearer. The Docker image includes `cursor-sdk`; a local venv needs `pip install -r requirements-cursor.txt`. Code lives in the image, so after `git pull` rebuild: `docker compose up -d --build --force-recreate`. Recreate is also required after editing `.env`. If the request fails, the same add wizard continues and you fill the fields yourself. Failures send the problem type and provider detail on their own lines (plain text, so Telegram HTML cannot drop them), and as an ERROR alert to the main admin.

To move one entry between instances (e.g. after spinning up a new bot or merging clubs), use **Export book (JSON)** / **Import book (JSON)** in `/adminconsole` on each side — see Admin Commands above. The payload is a small JSON document (`format`: `bookclub-bot-book`); `entity` in the file is informational if book vs film labels differ.

3. **Run the bot using Docker:**
   ```bash
   mkdir -p data logs
   docker compose up -d --build
   ```

   Compose runs the container as uid `1000` by default so host-owned `data/` and `logs/` stay writable. If your login is not `1000:1000`, or those folders were previously chowned to `10001`, fix ownership once:

   ```bash
   export BOT_UID="$(id -u)" BOT_GID="$(id -g)"
   sudo chown -R "$BOT_UID:$BOT_GID" data logs
   docker compose up -d --build
   ```

(if not yet installed before, install docker as in https://docs.docker.com/engine/install/ubuntu/)

## 🛡️ Backups

To ensure your data is safe, a backup script `scripts/remote_backup.sh` is provided. It creates a "safe" snapshot of the SQLite database while the bot is running to avoid corruption.

### Pull Backups (From another machine - Recommended)

If you want to run the backup from a **different Linux machine** (e.g., a dedicated backup server), use `scripts/remote_backup.sh`. This script connects to your bot server via SSH, triggers a safe backup, and pulls the file back to the local machine.

1.  **Copy the script** to your backup machine.
2.  **Configure the variables** inside `scripts/remote_backup.sh` (IP address, user, paths).
3.  **Ensure SSH Key-based authentication** is set up between the machines for automation.
4.  **Run it:** `./remote_backup.sh [bot-name]`

If you provide an argument, it will be used as the subfolder name on the remote server and included in the local filename. Defaults to `club-voting-bot`.

### Regular Backups (Recommended)
Add a cron job to run the backup daily at 2:00 AM:
1. Open crontab: `crontab -e`
2. Add the following line (adjust the path to your bot directory):
   ```cron
   0 2 * * * /bin/bash /path/to/remote_backup.sh >> /path/to/club-voting-bot/logs/backup.log 2>&1
   ```

## 🚀 Deploying updates

`scripts/deploy_bots.sh` updates one or more running instances on the server:
for each configured instance it checks whether anyone has actually been using
the bot recently and asks for confirmation. After you have decided for every
instance, selected updates run in parallel: stop containers, pull the latest
code, and bring them back up rebuilt (`docker compose up -d --build`).

The activity check reads `last_non_admin_activity` from the bot's own
persistence file (`data/bot_persistence`) — the same timestamp
`membership_gate()` stamps on every non-admin update. Admin activity never
counts as "in use", so testing the deploy yourself doesn't block the next run.

1. In the **same clone** you run deploy from, add `DEPLOY_REPOS` to `.env` — colon-separated
   absolute paths to each bot instance on this server (each folder needs its own
   `docker-compose.yml`, `.env`, and `data/`):
   ```env
   DEPLOY_REPOS="/root/club-voting-bot:/root/philo-club-bot"
   ```
   `scripts/deploy_bots.sh` and `scripts/logs.sh` read this via `scripts/load_deploy_repos.sh`.
2. Run it: `./scripts/deploy_bots.sh`
   - `--check-only` — report activity status for every instance and exit; changes nothing.
   - `--yes` — auto-confirm instances with no recent activity; instances that look active are still prompted.
   - `--skip-active` — combine with `--yes` for unattended/cron use: idle instances are updated, active ones are skipped without prompting.
   - `--threshold <minutes>` — how recent counts as "active" (default 10).

An instance with uncommitted local changes is always skipped rather than
pulled over. If `git pull` or the rebuild fails partway through, the script
tries to bring the previous containers back up rather than leaving the bot
down, and exits nonzero if anything failed — useful for alerting if run from cron.

**Why not Kubernetes?** These bots are single-process, long-polling (no
inbound HTTP traffic to load-balance), and keep their state in a local SQLite
file and a pickle file — the opposite of the stateless, horizontally-scaled
workload Kubernetes is for. Running each instance as a separate `docker
compose` stack on one small VPS, updated by this script, is simpler to
operate and debug for this shape of workload. If instance count grows large
enough that per-server `REPOS` lists become unwieldy, a lighter next step
than Kubernetes would be a `systemd` timer calling this script instead of
cron (structured logs via `journalctl`), or a dedicated inventory file if the
instance list outgrows a single `.env` line.

## 🔎 Logs & error alerts

Each instance writes to `logs/bookclub_bot.log` (rotated at 5 MB, 3 backups
kept) as well as to the container's stdout (`docker compose logs`). Two things
make failures easier to catch when something breaks.

### Get alerted when an error happens

Anything logged at `ERROR` or above — including every unhandled exception
caught by the global error handler — is forwarded to the main admin (the first
ID in `ADMIN_IDS`) as a Telegram message, so you find out without watching the
logs. Alerts are coalesced (up to a few errors per message, at most one message
every few seconds) so an error storm can't turn into a notification storm.

- On by default whenever `ADMIN_IDS` is set. Disable with `ERROR_ALERTS=0`.
- Set `INSTANCE_NAME` (e.g. `philo-club`) so a shared admin can tell which bot
  an alert came from — it's prepended to every alert.

### Search logs across all instances

`scripts/logs.sh` searches and tails the logs of **every** instance on the
server from one place, prefixing each line with the instance it came from so
you don't have to `grep` three separate files by hand. It uses the same
`DEPLOY_REPOS` list as `deploy_bots.sh` (from the project root `.env`).

```bash
./scripts/logs.sh                # last 50 ERROR/WARNING lines across all bots
./scripts/logs.sh -e             # errors only (ERROR/CRITICAL)
./scripts/logs.sh -g "notify"    # lines matching a regex
./scripts/logs.sh -b philo -e    # errors from instances whose name matches "philo"
./scripts/logs.sh --today -e     # today's errors only
./scripts/logs.sh -f             # live tail (ERROR/WARNING) — add -a for everything
```

Run `./scripts/logs.sh --help` for the full flag list.

**Why not Prometheus / Loki / ELK?** Prometheus stores *numeric metrics*, not
log text, so it can't answer "what was the error message?" — the log-search
tool in that ecosystem is Grafana Loki. A full Loki+Grafana (or ELK) stack is
real value once you have many services or need dashboards and long retention,
but for a handful of single-process bots on one VPS it's more moving parts to
run and secure than the problem needs. Push-on-error (above) plus a
cross-instance `grep` wrapper covers "tell me when it breaks" and "let me find
the error" without new infrastructure. If the fleet grows, the natural next
step is shipping these same log files to Loki via Promtail.

## 🧪 Testing

The project includes a suite of unit and integration tests.

### Code quality (Ruff, Black, mypy, pytest)

Dev dependencies live in `requirements-dev.txt` (runtime deps are in `requirements.txt`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check bookclub bookclub_bot.py scripts/check_bot_idle.py tests/
black bookclub bookclub_bot.py scripts/check_bot_idle.py tests/
mypy
pytest tests/
```

Configuration: `pyproject.toml` (`[tool.ruff]`, `[tool.black]`, `[tool.mypy]`, `[tool.pytest.ini_options]`).

To run **all** local checks (Ruff, Black, mypy, pytest, and Docker pytest):

```bash
./scripts/precommit_checks.sh
```

### pre-commit framework

Install hooks (Ruff, Black, mypy, pytest on commit; Docker pytest on **pre-push**):

```bash
pip install -r requirements-dev.txt
pre-commit install
pre-commit install --hook-type pre-push
```

Run every hook manually:

```bash
pre-commit run --all-files
pre-commit run --hook-stage pre-push docker-tests --all-files
```

To run tests in Docker only:

```bash
./scripts/docker_tests.sh
```

Or:

```bash
docker compose run --rm \
  -v "$(pwd)/bookclub:/app/bookclub:ro" \
  -v "$(pwd)/bookclub_bot.py:/app/bookclub_bot.py:ro" \
  -v "$(pwd)/tests:/app/tests:ro" \
  bot python -m pytest tests/
```

### Git pre-commit hook (shell)

`scripts/precommit_checks.sh` runs the full pipeline. Install:

```bash
cp scripts/git-pre-commit-hook.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit scripts/precommit_checks.sh scripts/docker_tests.sh
```

You need `pip install -r requirements-dev.txt` in a venv (or the tools on your PATH). Docker is required for the final test step.

Quick test run in a venv (no Docker):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/
```
