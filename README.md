# noice-proice v2

A personal price tracker powered by GitHub Actions + Playwright.
No server, no subscription, no moving parts — just a Python script, a cron job, and a static page.

## How it works

```
watchlist.json  ──→  GitHub Actions (daily cron)
                          │
                          ├─ Playwright visits each URL
                          ├─ Shopify JSON API (fast, no browser needed)
                          ├─ JSON-LD / Open Graph fallback
                          └─ CSS selector fallback
                          │
                      data/prices.json  ──→  index.html (GitHub Pages)
                          │
                      Email alert if price drops or hits target
```

## Setup

### 1. Fork / clone this repo

Make it **public** if you want a free GitHub Pages site.
Private repos work too — Pages is available on all plans.

### 2. Enable GitHub Pages

Settings → Pages → Source: **Deploy from a branch** → `main` / `(root)`

Your site will be at `https://<you>.github.io/noice-proice/`

### 3. Add your watchlist

Edit `watchlist.json`. Each item:

```json
{
  "id": "unique-slug",
  "name": "Product name",
  "url": "https://store.example.com/products/thing",
  "targetPrice": 29.99,
  "notes": "Optional description",
  "shopify": true
}
```

Set `"shopify": true` if the store runs on Shopify (ozsmartthings, usetrmnl, pimoroni, etc.).
The script auto-detects Shopify at runtime too, but the hint skips a browser load.

### 4. Set up email notifications (optional)

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Gmail [App Password](https://myaccount.google.com/apppasswords) (not your account password) |
| `NOTIFY_EMAIL` | address to send alerts to (can be same as SMTP_USER) |

If these secrets are not set, the script still runs — it just skips the email step.

### 5. Run it

The workflow runs automatically every day at 9am AEST.

To run it immediately: **Actions → Check prices → Run workflow**

Prices are committed to `data/prices.json` after each run. The site updates automatically.

---

## Supported store types

| Strategy | Works for |
|---|---|
| Shopify JSON API | ozsmartthings.com.au, usetrmnl.com, pimoroni.com, any Shopify store |
| JSON-LD | Adafruit, most modern e-commerce |
| Open Graph meta | Many smaller stores |
| CSS selectors | Everything else (slower, less reliable) |

## Local testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python scripts/check_prices.py
```
