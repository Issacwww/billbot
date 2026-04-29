# BillBot

Fully automated utility bill splitting for landlords/roommates. Fetches bills from Gmail, calculates tenant shares with pro-rating, and posts expenses to Splitwise.

## How It Works

```
Gmail (label: "Bill")  -->  Parse  -->  Calculate shares  -->  Splitwise (1-on-1 expenses)
                                              |
                                         SQLite (dedup)
```

1. **Fetch**: Searches Gmail for emails with label "Bill"
   - **PG&E**: Extracts amount from email body (no PDF)
   - **City Services**: Downloads PDF attachment, parses with pypdf/pdfplumber
2. **Calculate**: Splits bill by tenant share %, with pro-rating for partial lease overlap
3. **Post**: Creates 1-on-1 expenses on Splitwise
4. **Notify**: Sends email summary + macOS notification
5. **Dedup**: Tracks in SQLite — never double-charges

## Pro-Rating Example

```
Bill: $340.59 (01/05 - 02/03, 30 days)
Tenant moved in: 02/01
Share: $340.59 x 33.33% x (3/30) = $11.35
```

## Setup

### 1. Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Gmail OAuth

1. [Google Cloud Console](https://console.cloud.google.com/) -> create project -> enable Gmail API
2. Create OAuth 2.0 credentials (Desktop app)
3. Download to `~/.billbot/credentials.json`
4. Run `python auto.py --dry-run` to trigger OAuth flow in browser

### 3. Splitwise

1. Register an app at [secure.splitwise.com/apps](https://secure.splitwise.com/apps)
2. Note your Consumer Key, Consumer Secret, and API Key

### 4. Configuration

```bash
mkdir -p ~/.billbot
cp .env.example ~/.billbot/.env        # fill in API keys
cp tenants.example.json tenants.json   # fill in your tenants
```

**tenants.json** fields:
| Field | Description |
|-------|-------------|
| `name` | Display name |
| `email` | Must match their Splitwise account email |
| `share_percent` | Their share (e.g. 33.33) |
| `lease_start` | Move-in date (YYYY-MM-DD), for pro-rating |
| `lease_end` | Move-out date — setting this marks tenant inactive |

### 5. Automation (macOS launchd)

```bash
# Edit com.billbot.daily.plist — replace /path/to/billbot with your path
cp com.billbot.daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.billbot.daily.plist
```

Runs once when loaded at login, then again on the **15th** and **25th** of each month at 10:00 AM.

## Usage

```bash
# Dry run — see calculations without posting
python auto.py --dry-run --since-days 120

# Post to Splitwise
python auto.py --since-days 120

# Backfill historical bills into DB without posting
python auto.py --backfill --since-days 365

# Parse a single PDF (standalone CLI)
python billbot.py --pdf bill.pdf --tenants-file tenants.json
```

### Kill Switch

```bash
touch ~/.billbot/disabled     # pause
rm ~/.billbot/disabled        # resume
```

### Monitoring

- **Email**: notification sent after each post or error
- **macOS notification**: desktop alert on each run
- **Logs**: `~/.billbot/logs/`
- **DB**: `sqlite3 ~/.billbot/history.db "SELECT provider, amount_due, splitwise_expense_id FROM bills ORDER BY processed_at DESC;"`

## Project Structure

```
auto.py               # Main pipeline: fetch -> parse -> post -> notify
billbot.py             # PDF parsing, share calculation, pro-rating
gmail_fetch.py         # Gmail API: search, download, extract amounts
splitwise_post.py      # Splitwise API: create 1-on-1 expenses
db.py                  # SQLite persistence and dedup
tenants.example.json   # Tenant config template
.env.example           # API key template
com.billbot.daily.plist    # macOS launchd schedule template
```

## Notes

- **PG&E billing period**: PG&E emails only have amount + due date. For bills needing pro-rating, add the period to `PGE_PERIOD_OVERRIDES` in `auto.py`.
- **City billing period**: Parsed from PDF automatically.
- **Gmail label**: Both providers should use the same Gmail label (default: "Bill").
- **Splitwise**: 1-on-1 expenses. Tenant email must match their Splitwise account.
