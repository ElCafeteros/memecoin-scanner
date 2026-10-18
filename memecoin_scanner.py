"""
Meme Coin Scanner (pump.fun / Solana)

Watches every new pump.fun coin, runs red-flag checks (based on Setuh's Memecoin
Blueprint, days 2 and 3), and only alerts on coins with no major red flags.
Alerts show in a live web dashboard, in the console, in alerts.log, and
optionally in Telegram.

IT NEVER BUYS ANYTHING AND IS NOT FINANCIAL ADVICE. It is a filter that helps you
decide what to look at in Axiom. The decision is yours.

Run:   python memecoin_scanner.py   (the dashboard opens at http://localhost:8787)
Stop:  Ctrl+C
"""

import asyncio
import json
import os
import re
import sys
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone

import aiohttp
import websockets
from aiohttp import web

# ---------------------------------------------------------------- settings ---
# Change anything here, or put the same keys in config.json.
SETTINGS = {
    # "Final stretch" (Setuh day 2): token age max 30 min, market cap min $8.5K
    "final_stretch_min_mcap": 8500,
    "final_stretch_max_age_min": 30,
    "min_age_min": 2,                 # skip coins younger than 2 min (data is not complete yet)
    # Coins that migrated (bonded) to PumpSwap: market cap min $30K
    "migrated_min_mcap": 30000,
    # Red flags
    "max_top_holder_pct": 4.0,        # Setuh: top holder above 4% -> skip
    "max_insider_pct": 10.0,          # linked wallets holding more than 10% -> skip
    "max_dev_pct": 5.0,               # dev still holds more than 5% -> warning
    "same_pct_holders": 4,            # this many holders with the same % (>=1%) -> bundle
    "skip_profile_coins": True,       # Setuh: profile coins are the worst
    "poll_seconds": 45,
    # Dashboard
    "dashboard_port": 8787,
    "open_browser": True,
    # Telegram (leave empty to skip)
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ALERTS_LOG = os.path.join(HERE, "alerts.log")
DASHBOARD_HTML = os.path.join(HERE, "dashboard.html")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={}"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report"

# Settings the dashboard shows (no secrets)
PUBLIC_SETTINGS = ["final_stretch_min_mcap", "final_stretch_max_age_min", "migrated_min_mcap",
                   "max_top_holder_pct", "max_insider_pct", "same_pct_holders"]


def load_settings():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            SETTINGS.update(json.load(f))
    SETTINGS["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", SETTINGS["telegram_bot_token"])
    SETTINGS["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", SETTINGS["telegram_chat_id"])


def now():
    return time.time()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_usd(x):
    if x is None:
        return "?"
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.1f}K"
    return f"${x:.0f}"


def norm_symbol(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ------------------------------------------------------------------- state ---
class Token:
    def __init__(self, mint, name, symbol, uri, created, dev, bonding_curve):
        self.mint = mint
        self.name = name
        self.symbol = symbol
        self.uri = uri
        self.created = created
        self.dev = dev
        self.bonding_curve = bonding_curve
        self.checked = False


tokens: dict[str, Token] = {}
seen_checked: set[str] = set()
rug_sem = asyncio.Semaphore(2)

alerts: deque = deque(maxlen=200)
rejected: deque = deque(maxlen=100)
stats = {
    "started": now(), "connected": False, "new_coins": 0, "migrations": 0,
    "checked": 0, "alerts": 0, "rejected": 0, "last_scan": None, "tracked": 0,
}


# ------------------------------------------------------------------- http ----
async def get_json(session, url, retries=3):
    for i in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 429:
                    await asyncio.sleep(3 * (i + 1))
                    continue
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception:
            await asyncio.sleep(1 + i)
    return None


# ----------------------------------------------------------------- checks ----
def classify_socials(meta):
    """Setuh day 3: community > tweet+community > tweet/news; profile = worst."""
    tw = (meta or {}).get("twitter") or ""
    site = (meta or {}).get("website") or ""
    tg = (meta or {}).get("telegram") or ""
    community = (meta or {}).get("coin_community") or ""
    if "/i/communities" in tw or "/i/communities" in site:
        return "community", tw
    if "/status/" in tw:
        return ("tweet+community" if community else "tweet/news"), tw
    if re.match(r"https?://(x|twitter)\.com/[A-Za-z0-9_]+/?$", tw):
        return "profile", tw
    if tw or site or tg:
        return "other", tw or site or tg
    return "none", ""


async def check_token(session, mint, source, known: Token | None):
    hard, soft, good = [], [], []

    # 1) Market data
    dex = await get_json(session, DEX_TOKENS.format(mint)) or []
    pair = max(dex, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None) if dex else None
    if not pair:
        return None
    mcap = pair.get("marketCap") or pair.get("fdv")
    name = pair["baseToken"]["name"]
    symbol = pair["baseToken"]["symbol"]
    created_ms = min((p.get("pairCreatedAt") or 9e15) for p in dex)
    created = known.created if known else created_ms / 1000
    age_min = (now() - created) / 60
    vol = sum(((p.get("volume") or {}).get("h1") or 0) for p in dex)
    tx = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    chg = (pair.get("priceChange") or {}).get("h1")
    image = (pair.get("info") or {}).get("imageUrl")

    # 2) RugCheck (contract + holders + insiders)
    async with rug_sem:
        rc = await get_json(session, RUGCHECK.format(mint))
    if not rc:
        soft.append("RugCheck did not respond (incomplete data)")
        rc = {}
    tok = rc.get("token") or {}
    supply = tok.get("supply") or 0
    if rc:
        if tok.get("mintAuthority"):
            hard.append("Mint authority still active (dev can print tokens)")
        if tok.get("freezeAuthority"):
            hard.append("Freeze authority active (dev can freeze your wallet)")
        if not tok.get("mintAuthority") and not tok.get("freezeAuthority"):
            good.append("Clean contract (no mint / no freeze)")
        for r in rc.get("risks") or []:
            if r.get("level") == "danger":
                hard.append(f"RugCheck: {r.get('name')}")
            elif r.get("level") == "warn":
                soft.append(f"RugCheck: {r.get('name')}")

    # Holders (skip pools / bonding curve)
    pool_accounts = set()
    for m in rc.get("markets") or []:
        pool_accounts.update([m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")])
    if known and known.bonding_curve:
        pool_accounts.add(known.bonding_curve)
    holders = [h for h in (rc.get("topHolders") or [])
               if h.get("address") not in pool_accounts and h.get("owner") not in pool_accounts]
    top10 = holders[:10]
    top_pct = None
    if top10:
        top_pct = top10[0]["pct"]
        if top_pct > SETTINGS["max_top_holder_pct"]:
            hard.append(f"Top holder owns {top_pct:.1f}% (> {SETTINGS['max_top_holder_pct']}%)")
        else:
            good.append(f"Top holder {top_pct:.1f}%")
        rounded = [round(h["pct"], 1) for h in top10 if h["pct"] >= 1]
        dupes = max((rounded.count(x) for x in set(rounded)), default=0)
        if dupes >= SETTINGS["same_pct_holders"]:
            hard.append(f"{dupes} holders own the same % (bundle)")
        top10_sum = sum(h["pct"] for h in top10)
        if top10_sum > 35:
            soft.append(f"Top 10 own {top10_sum:.0f}%")
    insider_pct = 0
    for net in rc.get("insiderNetworks") or []:
        if supply:
            insider_pct += (net.get("currentHolding") or 0) / supply * 100
    if insider_pct > SETTINGS["max_insider_pct"]:
        hard.append(f"Insiders (linked wallets) own {insider_pct:.1f}%")
    elif insider_pct > 0:
        soft.append(f"Insiders own {insider_pct:.1f}%")
    if supply and rc.get("creatorBalance"):
        dev_pct = rc["creatorBalance"] / supply * 100
        if dev_pct > SETTINGS["max_dev_pct"]:
            soft.append(f"Dev still holds {dev_pct:.1f}%")
    holders_n = rc.get("totalHolders")

    # 3) Metadata / socials
    uri = (known.uri if known else None) or (rc.get("tokenMeta") or {}).get("uri")
    meta = await get_json(session, uri) if uri else None
    kind, link = classify_socials(meta)
    if kind == "profile" and SETTINGS["skip_profile_coins"]:
        hard.append("Profile coin (Setuh: avoid these)")
    elif kind == "none":
        soft.append("No socials")
    elif kind == "tweet/news":
        soft.append("News/tweet coin (high vamp risk)")
    img = (meta or {}).get("image") or ""
    image = image or img or None
    m_img = re.search(r"([1-9A-HJ-NP-Za-km-z]{32,44}pump)", img)
    if m_img and m_img.group(1) != mint:
        soft.append("Image copied from another coin (copy/vamp)")

    # 4) Copies / OG check
    search = await get_json(session, DEX_SEARCH.format(symbol)) or {}
    same = [p for p in search.get("pairs") or []
            if norm_symbol(p["baseToken"]["symbol"]) == norm_symbol(symbol)
            and p["baseToken"]["address"] != mint]
    same_by_addr = {}
    for p in same:
        same_by_addr.setdefault(p["baseToken"]["address"], p)
    recent = [p for p in same_by_addr.values()
              if p.get("pairCreatedAt") and abs(p["pairCreatedAt"] / 1000 - created) < 3 * 3600]
    older_recent = [p for p in recent if p["pairCreatedAt"] / 1000 < created - 5]
    old_runs = [p for p in same_by_addr.values()
                if p.get("pairCreatedAt") and p["pairCreatedAt"] / 1000 < created - 3 * 3600
                and (p.get("marketCap") or 0) > 30000 and p["chainId"] == "solana"]
    if len(recent) >= 5:
        soft.append(f"{len(recent)} copies with the same ticker in 3h (vamp war)")
    elif len(recent) >= 1:
        soft.append(f"{len(recent)} cop{'y' if len(recent) == 1 else 'ies'} with the same ticker")
    if older_recent:
        soft.append("Not the OG (another one launched earlier)")
    if old_runs:
        soft.append("This ticker already ran before (recycled)")
    if not recent and not old_runs:
        good.append("OG: no copies found")

    return {
        "mint": mint, "pair": pair.get("pairAddress"), "name": name, "symbol": symbol, "source": source,
        "mcap": mcap, "age_min": age_min, "vol_h1": vol, "buys": buys, "sells": sells,
        "chg_h1": chg, "holders": holders_n, "top_pct": top_pct, "insider_pct": insider_pct,
        "kind": kind, "link": link, "image": image,
        "hard": hard, "soft": soft, "good": good, "found_at": now(),
    }


# ----------------------------------------------------------------- alerts ----
def format_alert(r):
    lines = [
        f"🔔 {r['name']} (${r['symbol']})  [{r['source']}]",
        f"MC {fmt_usd(r['mcap'])} | Age {r['age_min']:.0f} min | Vol 1h {fmt_usd(r['vol_h1'])} | "
        f"Buys/Sells {r['buys']}/{r['sells']} | Holders {r['holders'] or '?'}",
        f"Type: {r['kind']}" + (f"  {r['link']}" if r['link'] else ""),
    ]
    if r["good"]:
        lines.append("✅ " + " · ".join(r["good"]))
    if r["soft"]:
        lines.append("⚠️ " + " · ".join(r["soft"]))
    lines.append(f"Axiom: https://axiom.trade/meme/{r['pair']}")
    lines.append(f"Chart: https://dexscreener.com/solana/{r['mint']}")
    lines.append("(Not financial advice. Do your own checks before buying.)")
    return "\n".join(lines)


async def send_telegram(session, text):
    token, chat = SETTINGS["telegram_bot_token"], SETTINGS["telegram_chat_id"]
    if not token or not chat:
        return
    try:
        await session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                           timeout=aiohttp.ClientTimeout(total=15))
    except Exception as e:
        log(f"Telegram error: {e}")


async def handle_result(session, r):
    if r is None:
        return
    stats["checked"] += 1
    if r["hard"]:
        stats["rejected"] += 1
        rejected.appendleft({k: r[k] for k in ("mint", "name", "symbol", "mcap", "source", "hard", "found_at")})
        log(f"❌ {r['symbol']:<10} MC {fmt_usd(r['mcap']):>7}  rejected: {r['hard'][0]}")
        return
    stats["alerts"] += 1
    alerts.appendleft(r)
    text = format_alert(r)
    print("\n" + "=" * 70 + "\n" + text + "\n" + "=" * 70 + "\n", flush=True)
    with open(ALERTS_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}]\n{text}\n\n")
    await send_telegram(session, text)


async def run_check(session, mint, source):
    if mint in seen_checked:
        return
    seen_checked.add(mint)
    try:
        r = await check_token(session, mint, source, tokens.get(mint))
        await handle_result(session, r)
    except Exception as e:
        log(f"Check error {mint[:6]}: {e}")


# ------------------------------------------------------------------ loops ----
async def ws_loop(session):
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                stats["connected"] = True
                log("Connected to the pump.fun stream (new coins + migrations)")
                async for raw in ws:
                    m = json.loads(raw)
                    tx = m.get("txType")
                    if tx == "create" and m.get("mint"):
                        stats["new_coins"] += 1
                        tokens[m["mint"]] = Token(m["mint"], m.get("name"), m.get("symbol"), m.get("uri"),
                                                  now(), m.get("traderPublicKey"), m.get("bondingCurveKey"))
                    elif tx == "migrate" and m.get("mint"):
                        stats["migrations"] += 1
                        asyncio.create_task(migrated_check(session, m["mint"]))
        except Exception as e:
            stats["connected"] = False
            log(f"Stream disconnected ({e}), retrying in 5s...")
            await asyncio.sleep(5)


async def migrated_check(session, mint):
    await asyncio.sleep(20)  # give DexScreener and RugCheck time to index the pool
    dex = await get_json(session, DEX_TOKENS.format(mint)) or []
    mcap = max(((p.get("marketCap") or 0) for p in dex), default=0)
    if mcap >= SETTINGS["migrated_min_mcap"]:
        await run_check(session, mint, "MIGRATED")


async def poll_loop(session):
    """Final stretch: coins aged 2-30 min with MC >= $8.5K."""
    while True:
        await asyncio.sleep(SETTINGS["poll_seconds"])
        t = now()
        max_age = SETTINGS["final_stretch_max_age_min"] * 60
        for mint in [m for m, tk in tokens.items() if t - tk.created > max_age]:
            del tokens[mint]
        due = [tk.mint for tk in tokens.values()
               if not tk.checked and tk.mint not in seen_checked
               and t - tk.created >= SETTINGS["min_age_min"] * 60]
        hits = 0
        for i in range(0, len(due), 30):
            batch = due[i:i + 30]
            data = await get_json(session, DEX_TOKENS.format(",".join(batch))) or []
            for p in data:
                if (p.get("marketCap") or 0) >= SETTINGS["final_stretch_min_mcap"]:
                    mint = p["baseToken"]["address"]
                    if mint in tokens:
                        tokens[mint].checked = True
                    hits += 1
                    asyncio.create_task(run_check(session, mint, "FINAL STRETCH"))
            await asyncio.sleep(0.3)
        stats["tracked"] = len(tokens)
        stats["last_scan"] = now()
        log(f"Scan: tracking {len(tokens)} coins, {len(due)} checked, "
            f"{hits} above ${SETTINGS['final_stretch_min_mcap']:,}")


async def telegram_chat_id_helper(session):
    """If you have a token but no chat_id: message your bot, and this prints your chat_id."""
    token = SETTINGS["telegram_bot_token"]
    if not token or SETTINGS["telegram_chat_id"]:
        return
    data = await get_json(session, f"https://api.telegram.org/bot{token}/getUpdates")
    ids = {u["message"]["chat"]["id"] for u in (data or {}).get("result", []) if "message" in u}
    if ids:
        log(f"Your Telegram chat_id: {', '.join(map(str, ids))}  -> put it in config.json")
    else:
        log("Telegram: send 'hi' to your bot in Telegram, then restart the scanner to see your chat_id.")


# -------------------------------------------------------------- dashboard ----
async def page_index(_request):
    return web.FileResponse(DASHBOARD_HTML)


async def api_state(_request):
    return web.json_response({
        "stats": stats,
        "now": now(),
        "settings": {k: SETTINGS[k] for k in PUBLIC_SETTINGS},
        "telegram": bool(SETTINGS["telegram_bot_token"] and SETTINGS["telegram_chat_id"]),
        "alerts": list(alerts),
        "rejected": list(rejected)[:40],
    })


async def start_dashboard():
    app = web.Application()
    app.router.add_get("/", page_index)
    app.router.add_get("/api/state", api_state)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = SETTINGS["dashboard_port"]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    url = f"http://localhost:{port}"
    log(f"Dashboard: {url}")
    if SETTINGS["open_browser"]:
        webbrowser.open(url)


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_settings()
    log("Meme Coin Scanner started. Press Ctrl+C to stop.")
    log("Not financial advice: this only filters red flags. It never buys anything.")
    async with aiohttp.ClientSession(headers={"User-Agent": "memecoin-scanner/1.0"}) as session:
        await start_dashboard()
        await telegram_chat_id_helper(session)
        if SETTINGS["telegram_bot_token"] and SETTINGS["telegram_chat_id"]:
            await send_telegram(session, "✅ Meme Coin Scanner is running.")
            log("Telegram: alerts will be sent to your phone.")
        await asyncio.gather(ws_loop(session), poll_loop(session))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped.")
