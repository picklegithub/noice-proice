#!/usr/bin/env python3
"""
noice-proice: Playwright-based price tracker.

Extraction cascade (most reliable first):
  1. Shopify JSON API  – for any Shopify store (handles 80% of niche shops)
  2. JSON-LD           – standard e-commerce structured data
  3. Open Graph meta   – og:price:amount, product:price:amount
  4. CSS selectors     – common price element patterns

Runs via GitHub Actions on a daily cron.  Price data committed back to the
repo so index.html can read it statically with no server.
"""

import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

WATCHLIST_FILE = Path("watchlist.json")
DATA_FILE = Path("data/prices.json")

# ── helpers ──────────────────────────────────────────────────────────────────

def parse_price(raw) -> float | None:
    """Pull the first valid price number out of anything."""
    if raw is None:
        return None
    text = re.sub(r"[A-Z$€£¥₹,\s]", "", str(raw).strip())
    m = re.search(r"\d+(?:\.\d{1,2})?", text)
    if m:
        v = float(m.group())
        return v if v > 0 else None
    return None

# ── extraction strategies ─────────────────────────────────────────────────────

async def try_shopify_api(url: str) -> float | None:
    """
    Hit /products/handle.json directly.  No browser needed, never rate-limited.
    Works for ozsmartthings.com.au, usetrmnl.com, pimoroni.com, etc.
    """
    base = re.sub(r"\?.*$", "", url.rstrip("/"))
    json_url = base + ".json"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(json_url, headers={"User-Agent": "price-checker/2.0"})
            if r.status_code == 200:
                variants = r.json().get("product", {}).get("variants", [])
                if variants:
                    return parse_price(variants[0].get("price"))
    except Exception as e:
        print(f"    shopify_api error: {e}")
    return None


async def scrape_page(url: str, browser) -> tuple[float | None, str | None]:
    """
    Real browser page load + multi-strategy extraction.
    Returns (price, strategy_label).
    """
    page = await browser.new_page()
    try:
        await page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-AU,en;q=0.9",
        })
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_500)   # let JS paint

        # ── If Shopify is detected at runtime, prefer their API ──────────────
        is_shopify = await page.evaluate("() => !!window.Shopify")
        if is_shopify:
            price = await try_shopify_api(url)
            if price:
                return price, "shopify_api"

        # ── JSON-LD structured data ──────────────────────────────────────────
        raw = await page.evaluate("""() => {
            for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const items = [].concat(JSON.parse(s.textContent));
                    for (const d of items) {
                        if (d['@type'] === 'Product' && d.offers) {
                            const o = [].concat(d.offers)[0];
                            if (o?.price) return String(o.price);
                        }
                    }
                } catch {}
            }
            return null;
        }""")
        if (p := parse_price(raw)):
            return p, "json_ld"

        # ── Open Graph / meta price tags ─────────────────────────────────────
        raw = await page.evaluate("""() => {
            for (const sel of [
                'meta[property="product:price:amount"]',
                'meta[property="og:price:amount"]',
                'meta[name="twitter:data1"]',
            ]) {
                const el = document.querySelector(sel);
                if (el?.content) return el.content;
            }
            return null;
        }""")
        if (p := parse_price(raw)):
            return p, "meta_tag"

        # ── Common CSS selectors (rough-to-fine) ─────────────────────────────
        for sel in [
            "[itemprop='price']",
            "[data-product-price]",
            ".price__current",
            ".price-item--regular",
            ".product__price .price",
            ".product-price__price",
            ".js-price",
            "#price",
            ".price--main",
            ".current-price",
            ".price:not(.price--compare)",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    attrs = await page.evaluate(
                        "(e) => ({c:e.getAttribute('content'),d:e.getAttribute('data-price'),t:e.innerText})",
                        el,
                    )
                    raw = attrs["c"] or attrs["d"] or attrs["t"]
                    if (p := parse_price(raw)):
                        return p, f"css:{sel}"
            except Exception:
                continue

        return None, None

    except Exception as e:
        print(f"    playwright error: {e}")
        return None, None
    finally:
        await page.close()

# ── main loop ─────────────────────────────────────────────────────────────────

async def check_all(watchlist: list, existing: dict) -> tuple[dict, list]:
    data = {k: v for k, v in existing.items()}
    notifications: list[dict] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for item in watchlist:
            iid = item["id"]
            print(f"\n[{iid}]  {item['name']}")

            price: float | None = None
            strategy: str | None = None

            # Explicit hint or known Shopify domain → try API before loading browser
            if item.get("shopify"):
                price = await try_shopify_api(item["url"])
                if price:
                    strategy = "shopify_api"
                    print(f"  ✓ shopify_api  → ${price:.2f}")

            if price is None:
                price, strategy = await scrape_page(item["url"], browser)
                if price:
                    print(f"  ✓ {strategy}  → ${price:.2f}")
                else:
                    print(f"  ✗ price not found")

            # ── Update data store ────────────────────────────────────────────
            if iid not in data:
                data[iid] = {"history": []}

            entry = data[iid]
            prev = entry.get("currentPrice")

            entry.update({
                "name":        item["name"],
                "url":         item["url"],
                "targetPrice": item.get("targetPrice"),
                "notes":       item.get("notes", ""),
                "lastChecked": datetime.now(timezone.utc).isoformat(),
            })

            if price is not None:
                entry["currentPrice"]  = price
                entry["previousPrice"] = prev
                entry["source"]        = strategy
                entry["lastError"]     = None

                # One history entry per day
                hist = [h for h in entry.get("history", []) if h.get("date") != today]
                hist.append({"date": today, "price": price})
                entry["history"] = hist[-90:]       # 90-day rolling window

                # ── Notifications ────────────────────────────────────────────
                target = item.get("targetPrice")
                if target and price <= target and (prev is None or prev > target):
                    notifications.append({"type": "target", "item": item, "price": price, "target": target})
                    print(f"  🎯  TARGET HIT  ${price:.2f} ≤ ${target:.2f}")
                elif prev and price < prev:
                    pct = (prev - price) / prev * 100
                    if pct >= 5:
                        notifications.append({"type": "drop", "item": item, "price": price, "prev": prev, "pct": pct})
                        print(f"  📉  Down {pct:.1f}%  ${prev:.2f} → ${price:.2f}")
            else:
                entry["lastError"] = "Price not found"

            await asyncio.sleep(2)      # polite delay between requests

        await browser.close()

    return data, notifications

# ── email ─────────────────────────────────────────────────────────────────────

def send_email(notifications: list) -> None:
    user  = os.getenv("SMTP_USER")
    pwd   = os.getenv("SMTP_PASS")
    to    = os.getenv("NOTIFY_EMAIL", user)
    if not user or not pwd:
        print("SMTP_USER / SMTP_PASS not set — skipping email")
        return

    rows = []
    for n in notifications:
        name = n["item"]["name"]
        url  = n["item"]["url"]
        if n["type"] == "target":
            rows.append(
                f"<li>🎯 <b>{name}</b> hit target — ${n['price']:.2f} "
                f"(target ${n['target']:.2f}) &nbsp;<a href='{url}'>Buy now →</a></li>"
            )
        else:
            rows.append(
                f"<li>📉 <b>{name}</b> dropped {n['pct']:.1f}% — "
                f"${n['prev']:.2f} → ${n['price']:.2f} &nbsp;<a href='{url}'>View →</a></li>"
            )

    html = f"""
<html><body style="font-family:system-ui,sans-serif;max-width:580px;margin:32px auto;color:#1a1a1a">
  <h2 style="margin:0 0 8px;font-size:20px">Price alerts</h2>
  <p style="color:#666;margin:0 0 20px;font-size:14px">
    {datetime.now(timezone.utc).strftime("%d %b %Y")} — noice-proice
  </p>
  <ul style="line-height:2.2;padding-left:16px">{"".join(rows)}</ul>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="color:#aaa;font-size:12px">
    Your automated price watchlist via GitHub Actions.
  </p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"]    = user
    msg["To"]      = to
    msg["Subject"] = f"noice-proice: {len(notifications)} alert{'s' if len(notifications) > 1 else ''}"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print(f"Email sent → {to}")
    except Exception as e:
        print(f"Email failed: {e}")

# ── entry ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    DATA_FILE.parent.mkdir(exist_ok=True)

    if not WATCHLIST_FILE.exists():
        print(f"ERROR: {WATCHLIST_FILE} not found")
        return 1

    watchlist = json.loads(WATCHLIST_FILE.read_text())
    existing  = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}

    print(f"noice-proice  {datetime.now(timezone.utc).isoformat()}")
    print(f"Checking {len(watchlist)} item(s) ...\n")

    data, notifications = await check_all(watchlist, existing)

    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n✓ data/prices.json updated  ({len(data)} items)")

    if notifications:
        send_email(notifications)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
