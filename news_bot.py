"""
Meme Coin News Bot

Reads the main crypto news sites (RSS) every few minutes, keeps the stories about
meme coins, Solana, Elon/Trump and exchange listings, and tags them. Also tracks
the CoinGecko trending list and the biggest meme coin movers.

Runs inside memecoin_scanner.py (News and Meme market tabs on the dashboard),
or on its own:  python news_bot.py

Not financial advice. News moves prices both ways, and fast.
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(HERE, "news_seen.json")
NEWS_LOG = os.path.join(HERE, "news.log")

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoinist": "https://bitcoinist.com/feed/",
    "U.Today": "https://u.today/rss",
    "NewsBTC": "https://www.newsbtc.com/feed/",
}

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_MEMES = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=meme-token"
                   "&order=market_cap_desc&per_page=100&price_change_percentage=1h,24h")

EXCHANGES = r"(binance|coinbase|robinhood|upbit|bybit|okx|kraken|bithumb|kucoin|hyperliquid)"
TOPICS = {
    "Meme coins": r"\b(meme ?coins?|meme ?tokens?|pump\.?fun|pumpswap|letsbonk|bonk|dogecoin|doge|shiba|shib|pepe|"
                  r"floki|dogwifhat|wif|popcat|fartcoin|pengu|pudgy|memecore|bags app|\$trump|trump coin|melania)\b",
    "Elon / Trump": r"\b(elon|musk|trump)\b",
    "Solana": r"\b(solana|raydium|jupiter|phantom wallet|sol price)\b",
    "Listing": rf"(\b(lists?|listing|listed|delist\w*|will list|adds?)\b.{{0,60}}\b{EXCHANGES}\b|\b{EXCHANGES}\b.{{0,60}}\b(lists?|listing|listed|delist\w*|adds?)\b)",
    "Scam / hack": r"\b(rug ?pull\w*|rugged|scam\w*|exploit\w*|hack\w*|drained|phishing|stolen)\b",
    "Regulation": r"\b(sec|etf|regulat\w*|lawsuit|ban\w*|tax\w*|cftc)\b",
}
# A story is kept if it matches at least one of these
MAIN_TOPICS = ["Meme coins", "Elon / Trump", "Solana", "Listing"]
TOPIC_RE = {k: re.compile(v, re.I) for k, v in TOPICS.items()}

DEFAULTS = {
    "news_poll_minutes": 5,
    "market_poll_minutes": 10,
    "news_telegram": True,            # send matching news to Telegram
    "mover_alert_pct_1h": 15,         # Telegram alert when a top-100 meme coin moves this much in 1h
}


def clean_text(s, limit=240):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return time.time()


def tag_story(title, summary):
    text = f"{title} {summary}"
    tags = [k for k, rx in TOPIC_RE.items() if rx.search(text)]
    # Title matches count more than summary-only matches
    score = sum(2 if TOPIC_RE[k].search(title) else 1 for k in tags if k in MAIN_TOPICS)
    return tags, score


class NewsBot:
    def __init__(self, settings=None, send=None, log=print):
        self.settings = {**DEFAULTS, **(settings or {})}
        self.send = send
        self.log = log
        self.items: deque = deque(maxlen=200)
        self.trending: list = []
        self.movers: list = []
        self.stats = {"stories": 0, "last_news": None, "last_market": None, "feeds_ok": 0,
                      "feeds_total": len(FEEDS)}
        self._mover_sent: dict[str, float] = {}
        self._seen = self._load_seen()
        self._first_run = True

    # ---------------------------------------------------------- seen links --
    def _load_seen(self):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_seen(self):
        try:
            with open(SEEN_PATH, "w", encoding="utf-8") as f:
                json.dump(list(self._seen)[-3000:], f)
        except Exception:
            pass

    # --------------------------------------------------------------- news ---
    async def _fetch_feed(self, session, source, url):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20),
                                   headers={"User-Agent": "Mozilla/5.0 memecoin-news-bot"}) as r:
                if r.status != 200:
                    return source, []
                raw = await r.read()
            root = ET.fromstring(raw)
        except Exception:
            return source, []
        out = []
        for it in root.iter("item"):
            title = clean_text(it.findtext("title"), 200)
            link = (it.findtext("link") or "").strip()
            if not title or not link.startswith("https://"):
                continue
            out.append({
                "source": source, "title": title, "link": link,
                "summary": clean_text(it.findtext("description")),
                "published": parse_date(it.findtext("pubDate")),
            })
        return source, out

    async def poll_news(self, session):
        results = await asyncio.gather(*(self._fetch_feed(session, s, u) for s, u in FEEDS.items()))
        self.stats["feeds_ok"] = sum(1 for _, items in results if items)
        new = []
        cutoff = time.time() - 24 * 3600
        known_links = {i["link"] for i in self.items}
        for _, stories in results:
            for st in stories:
                if st["link"] in known_links or st["published"] < cutoff:
                    continue
                tags, score = tag_story(st["title"], st["summary"])
                if score == 0:
                    continue
                st.update(tags=tags, score=score, found_at=time.time())
                known_links.add(st["link"])
                is_new = st["link"] not in self._seen
                self._seen.add(st["link"])
                new.append((st, is_new))
        new.sort(key=lambda x: x[0]["published"])
        for st, is_new in new:
            self.items.appendleft(st)
            self.stats["stories"] += 1
            # On the first run, fill the dashboard but don't spam Telegram with old stories
            if is_new and not self._first_run:
                await self._announce(st)
        if new:
            self.items = deque(sorted(self.items, key=lambda s: -s["published"]), maxlen=200)
            self._save_seen()
        self._first_run = False
        self.stats["last_news"] = time.time()
        self.log(f"News: {self.stats['feeds_ok']}/{len(FEEDS)} sites OK, {len(new)} new meme stories")

    async def _announce(self, st):
        line = f"📰 [{st['source']}] {st['title']}"
        self.log(line)
        with open(NEWS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {st['source']} | "
                    f"{', '.join(st['tags'])} | {st['title']} | {st['link']}\n")
        if self.send and self.settings["news_telegram"]:
            await self.send(f"📰 {st['title']}\n🏷 {', '.join(st['tags'])} · {st['source']}\n{st['link']}")

    # ------------------------------------------------------------- market ---
    async def poll_market(self, session):
        async def get(url):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
            except Exception:
                return None

        trending, memes = await asyncio.gather(get(COINGECKO_TRENDING), get(COINGECKO_MEMES))
        if trending:
            self.trending = []
            for c in trending.get("coins", [])[:15]:
                i = c["item"]
                d = i.get("data") or {}
                self.trending.append({
                    "name": i.get("name"), "symbol": i.get("symbol"), "rank": i.get("market_cap_rank"),
                    "image": i.get("small"), "slug": i.get("id"),
                    "chg_24h": (d.get("price_change_percentage_24h") or {}).get("usd"),
                    "mcap": d.get("market_cap"),
                })
        if isinstance(memes, list):
            rows = [{
                "name": c.get("name"), "symbol": (c.get("symbol") or "").upper(), "slug": c.get("id"),
                "image": c.get("image"), "mcap": c.get("market_cap"), "price": c.get("current_price"),
                "chg_1h": c.get("price_change_percentage_1h_in_currency"),
                "chg_24h": c.get("price_change_percentage_24h"),
                "volume": c.get("total_volume"),
            } for c in memes]
            self.movers = sorted(rows, key=lambda r: -abs(r["chg_24h"] or 0))[:20]
            await self._mover_alerts(rows)
        self.stats["last_market"] = time.time()

    async def _mover_alerts(self, rows):
        limit = self.settings["mover_alert_pct_1h"]
        for r in rows:
            chg = r["chg_1h"] or 0
            if abs(chg) < limit:
                continue
            if time.time() - self._mover_sent.get(r["slug"], 0) < 6 * 3600:
                continue
            self._mover_sent[r["slug"]] = time.time()
            arrow = "🚀" if chg > 0 else "🔻"
            text = (f"{arrow} {r['name']} (${r['symbol']}) {chg:+.1f}% in 1h · 24h {r['chg_24h'] or 0:+.1f}% · "
                    f"MC ${(r['mcap'] or 0) / 1e6:.1f}M\nhttps://www.coingecko.com/en/coins/{r['slug']}")
            self.log(text.split("\n")[0])
            if self.send:
                await self.send(text)

    # -------------------------------------------------------------- loops ---
    async def run(self, session):
        async def news_loop():
            while True:
                try:
                    await self.poll_news(session)
                except Exception as e:
                    self.log(f"News error: {e}")
                await asyncio.sleep(self.settings["news_poll_minutes"] * 60)

        async def market_loop():
            while True:
                try:
                    await self.poll_market(session)
                except Exception as e:
                    self.log(f"Market error: {e}")
                await asyncio.sleep(self.settings["market_poll_minutes"] * 60)

        await asyncio.gather(news_loop(), market_loop())

    def state(self):
        return {"stats": self.stats, "items": list(self.items)[:100],
                "trending": self.trending, "movers": self.movers}


# ------------------------------------------------------------ standalone ----
async def _standalone():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = {}
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", cfg.get("telegram_bot_token", ""))
    chat = os.environ.get("TELEGRAM_CHAT_ID", cfg.get("telegram_chat_id", ""))

    def log(msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    async with aiohttp.ClientSession() as session:
        async def send(text):
            if token and chat:
                try:
                    await session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                       json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                                       timeout=aiohttp.ClientTimeout(total=15))
                except Exception as e:
                    log(f"Telegram error: {e}")

        bot = NewsBot(cfg, send, log)
        log("Meme Coin News Bot started. Press Ctrl+C to stop.")
        await bot.poll_news(session)
        for st in list(bot.items)[:10]:
            age = (time.time() - st["published"]) / 3600
            print(f"  📰 {age:4.1f}h ago [{st['source']}] {st['title']}\n     🏷 {', '.join(st['tags'])}  {st['link']}")
        await bot.poll_market(session)
        print("\n  🔥 Trending on CoinGecko: " + ", ".join(f"{t['symbol']}" for t in bot.trending[:10]))
        print("  📈 Biggest meme movers (24h): " + ", ".join(
            f"{m['symbol']} {m['chg_24h'] or 0:+.0f}%" for m in bot.movers[:8]) + "\n")
        await bot.run(session)


if __name__ == "__main__":
    try:
        asyncio.run(_standalone())
    except KeyboardInterrupt:
        print("Stopped.")
