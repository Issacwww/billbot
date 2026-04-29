# BillBot Setup Guide

## 1. Install Dependencies

```bash
cd projects/billbot
pip install -r requirements.txt
```

## 2. Create `~/.billbot/` Directory

```bash
mkdir -p ~/.billbot/downloads ~/.billbot/logs
```

## 3. Gmail OAuth Setup

You need a Google Cloud project with Gmail API enabled.

### Step 1: Create Google Cloud Project
1. Go to https://console.cloud.google.com/
2. Create a new project (e.g., "BillBot")
3. Select the project

### Step 2: Enable Gmail API
1. Go to **APIs & Services → Library**
2. Search for "Gmail API"
3. Click **Enable**

### Step 3: Configure OAuth Consent Screen
1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** → Create
3. Fill in app name ("BillBot"), your email
4. Add scopes: `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/gmail.send`
5. Add your email as a test user
6. **Important:** Click **Publish App** to move from "Testing" to "Production"
   - This prevents the refresh token from expiring every 7 days
   - The app will show "unverified" warning — that's fine for personal use

### Step 4: Create OAuth Credentials
1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Download the JSON file
5. Save it as `~/.billbot/credentials.json`

### Step 5: First-Time Authorization
```bash
cd projects/billbot
python auto.py --dry-run
```
A browser window will open asking you to log in and grant Gmail read access.
After authorization, a `~/.billbot/token.json` will be created automatically.
Subsequent runs (including cron) will use the refresh token — no browser needed.

## 4. Splitwise API Setup

### Step 1: Register an App
1. Go to https://secure.splitwise.com/apps/new
2. Fill in:
   - Application name: "BillBot"
   - Application description: "Automated utility bill splitting"
   - Homepage URL: "http://localhost"
   - Callback URL: "http://localhost"
3. Click **Register and get API key**

### Step 2: Get Your Credentials
After registration, you'll see:
- **Consumer Key**
- **Consumer Secret**
- **API Key**

### Step 3: Verify Tenant Emails
Make sure the `email` field in `tenants.json` matches the email each tenant uses in Splitwise.
BillBot creates 1-on-1 expenses (no group needed) and matches tenants by email from your friends list.

## 5. Create `~/.billbot/.env`

```bash
cat > ~/.billbot/.env << 'EOF'
# Gmail (credentials.json handles OAuth, no env vars needed for Gmail itself)

# Splitwise
SPLITWISE_CONSUMER_KEY=your_consumer_key_here
SPLITWISE_CONSUMER_SECRET=your_consumer_secret_here
SPLITWISE_API_KEY=your_api_key_here

# OpenAI (optional, for AI fallback parsing)
OPENAI_API_KEY=your_openai_key_here
OPENAI_MODEL=gpt-4.1-mini
EOF
```

Replace the placeholder values with your actual credentials.

## 6. Gmail Label Setup

Make sure you have a Gmail label called **"Bill"** applied to your PG&E and City Services emails.
You can set up a Gmail filter to automatically label incoming bills:

1. In Gmail, click the search bar → **Show search options**
2. From: `pge.com` (or the City Services sender)
3. Click **Create filter**
4. Check **Apply the label** → select "Bill"
5. Check **Also apply filter to matching conversations** (for existing emails)

## 7. Test Run

### Backfill (process old bills, populate database, no Splitwise posting):
```bash
python auto.py --backfill --debug
```

### Dry run (check new bills):
```bash
python auto.py --dry-run --debug
```

### Production run:
```bash
python auto.py
```

## 8. Set Up Daily Schedule (launchd)

```bash
cp com.billbot.daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.billbot.daily.plist
```

The job runs once when loaded at login, then on the 15th and 25th of each month at 10:00 AM. If your Mac is asleep, launchd catches up when you wake it.

### Kill Switch
To stop daily runs:
```bash
touch ~/.billbot/disabled
```

To resume:
```bash
rm ~/.billbot/disabled
```

## 9. Verify

Check the SQLite database:
```bash
sqlite3 ~/.billbot/history.db "SELECT provider, amount_due, bill_period_start, splitwise_expense_id FROM bills;"
```

Check logs:
```bash
cat ~/.billbot/logs/billbot.stdout.log
```
