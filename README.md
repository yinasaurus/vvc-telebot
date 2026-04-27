# VVC Telegram loan bot

A Telegram bot that records **equipment loan requests**, **two-sided confirmations** (logistics records what went out; borrower acknowledges), and **returns** with admin approval. Everything is stored in **Google Sheets** as an audit log—no LLM, rule-based messages only.

Use this in **private chat** with the bot (not groups).

---

## How it works (big picture)

1. **Borrower** unlocks the bot with a shared passcode (once per Telegram account on each server).
2. **Borrower** creates a **New request**: CCA name, then a structured line describing what they need.
3. A new row appears in the sheet with status **pending_admin**.
4. **Admin** (logistics) opens **Admin: pending loans**, picks the request, and sends **what was actually loaned** in the same structured format.
5. The sheet updates; status becomes **awaiting_user_ack**, with timestamps and admin Telegram identity recorded.
6. **Borrower** opens **My loans** and taps **Acknowledge** for that row.
7. Status becomes **on_loan**; borrower’s acknowledgement time is recorded.
8. When returning: **Borrower** uses **Return an item** → status **pending_return**.
9. **Admin** uses **Admin: pending returns** → approves → status **returned**, with approver and time recorded.

If a message does not match the required **three-part comma format**, the bot rejects it and shows the template again.

```mermaid
flowchart LR
  subgraph borrower
    A[Unlock passcode]
    B[New request]
    C[Acknowledge loan]
    D[Request return]
  end
  subgraph sheet[Google Sheet]
    S[(Row + status)]
  end
  subgraph admin
    E[Record loan details]
    F[Approve return]
  end
  A --> B --> S
  E --> S
  C --> S
  D --> S
  F --> S
```

---

## Status values (column `status`)

| Status | Meaning |
|--------|---------|
| `pending_admin` | Request logged; logistics has not recorded what was loaned yet. |
| `awaiting_user_ack` | Logistics entered loan details; borrower must acknowledge under **My loans**. |
| `on_loan` | Borrower acknowledged; item is considered out on loan. |
| `pending_return` | Borrower started return; logistics has not confirmed yet. |
| `returned` | Logistics approved return; transaction closed on the sheet. |

---

## Message format: `item, qty, reason`

Both **borrower need** and **admin loan line** must be **one message** with **three parts**, separated by the **first two commas**:

- **Item** — what (trimmed).
- **Qty** — how many / how much (trimmed; can be text like `2` or `1 set`).
- **Reason** — why; **may include commas** (everything after the second comma counts as reason).

Examples:

- `HDMI cable, 2, Year-end concert booth`
- `Mic set A, 1, Signed from store — backup for assembly` (comma inside the reason is OK)

The bot stores a normalized line in the sheet, e.g. `HDMI cable | qty 2 | Year-end concert booth`.

---

## Google Sheet columns

Row 1 must match the bot headers (created automatically on an empty sheet). Columns include:

| Column | Purpose |
|--------|---------|
| `id` | Unique transaction id |
| `created_at` / `updated_at` | ISO timestamps (UTC) |
| `requester_*` | Borrower Telegram id, username, display name |
| `cca` | CCA / group name from the borrower |
| `need_description` | Borrower request (formatted) |
| `status` | One of the status values above |
| `loan_description` | What logistics recorded as loaned |
| `admin_*` / `loan_recorded_at` | Who recorded the loan and when |
| `user_ack_at` | When the borrower acknowledged |
| `return_requested_at` | When return was started |
| `return_approved_at` | When logistics approved return |
| `return_approver_*` | Who approved the return |

---

## Who can do what

| Role | How it’s determined | Capabilities |
|------|---------------------|--------------|
| **Anyone** | Opens the bot on Telegram | Nothing useful until unlocked. |
| **Member** | Correct **BOT_PASSCODE** once | Menu: New request, My loans, Return an item. |
| **Admin** | Telegram user id in **ADMIN_TELEGRAM_IDS** | Same as member **plus** Admin: pending loans / pending returns. |

**Passcode notes:**

- Wrong guesses are counted; after **5** failures, that Telegram account waits **15 minutes** before trying again (in-memory; resets if the bot process restarts).
- Unlocked users are saved in **`verified_users.json`** on the machine running the bot (gitignored).

---

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `BOT_PASSCODE` | Shared unlock code (**≥ 12 characters**) |
| `ADMIN_TELEGRAM_IDS` | Comma-separated numeric Telegram user ids |
| `GOOGLE_SHEET_ID` | Spreadsheet id from the Google Sheet URL |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the service account JSON key file |

Share the Google Sheet with the **service account email** (`client_email` inside the JSON) as **Editor**.

Never commit `.env`, `service_account.json`, or `verified_users.json`.

---

## Run locally

```bash
pip install -r requirements.txt
python bot.py
```

Keep the process running for the bot to stay online (your laptop, a VPS, or a host like Railway/Fly.io). Prefer **one** running instance per deployment so `verified_users.json` stays consistent.

---

## Commands

| Command | Purpose |
|---------|---------|
| `/start` | Introduction; if not unlocked, asks for passcode |
| `/admin` | Short admin help (admins only) |

---

## Tech stack

- **Python**, **python-telegram-bot** (long polling)
- **gspread** + Google service account for Sheets

---

## Limitations

- **Shared passcode**: anyone with the code can unlock; rotate it if it leaks.
- **Single-server file**: `verified_users.json` is local to the host unless you redesign storage.

---

## License

Specify your license here if you publish the repo.
