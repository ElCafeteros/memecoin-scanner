# Meme Coin Scanner 🔎

An **alert** app for pump.fun (Solana). It watches every new coin, runs red-flag checks
(based on Setuh's Memecoin Blueprint, days 2 and 3), and only alerts on coins with **no major red flags**.
Alerts show in a **live dashboard** in your browser, in the console, in `alerts.log`, and optionally in Telegram.

> ⚠️ **It never buys anything and it is not financial advice.** It only gives you information.
> Look at the coin in Axiom and make your own decision. Only use money you can afford to lose completely.

## Install (Windows, 3 steps)
1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/).
   On the first installer screen, tick **"Add python.exe to PATH"**.
2. On this page, click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Open the extracted folder and double-click **`run_scanner.bat`**.

The dashboard opens at **http://localhost:8787**. Keep the black window open while you use it.

**Problems?**
- *"python is not recognized"* or the window closes right away: run the Python installer again → **Modify** → tick **Add Python to environment variables**.
- *No alerts yet*: normal at first. Most coins never reach the filters, so wait a few minutes.

## Start

Or from a terminal:
```
pip install -r requirements.txt
python memecoin_scanner.py
```
To stop: `Ctrl+C`, or close the black window.

## How it works
1. Listens to **every new pump.fun coin** (live stream from PumpPortal).
2. **Final stretch**: a coin aged 2 to 30 min with market cap ≥ **$8.5K** gets checked.
3. **Migrated**: a coin that bonded to PumpSwap with market cap ≥ **$30K** gets checked.
4. Checks:
   | Check | If it fails |
   |---|---|
   | Mint / freeze authority | ❌ rejected |
   | Top holder > 4% | ❌ rejected |
   | 4+ holders with the same % (bundle) | ❌ rejected |
   | Insiders (linked wallets) > 10% | ❌ rejected |
   | RugCheck "danger" risk | ❌ rejected |
   | Profile coin | ❌ rejected |
   | Copies / vamps / not the OG / recycled ticker | ⚠️ warning |
   | News/tweet coin, no socials, dev still holding a lot | ⚠️ warning |
5. No ❌ → **alert**.

## Dashboard
- Live stats: new coins, coins tracked, migrations, checked, alerts, rejected.
- Alert cards with market cap, age, volume, buys/sells, holders, ✅ passed checks and ⚠️ warnings.
- Buttons: **Open in Axiom**, **DexScreener**, **X / socials**, **Copy CA**.
- Filters: All / Final stretch / Migrated / No warnings.
- Sound and desktop notifications for new alerts.
- "Recently rejected" list showing why each coin was rejected.

## Telegram (optional)
1. In Telegram, send `/newbot` to **@BotFather** and copy the **token**.
2. Copy `config.example.json` to `config.json` and put the token in `telegram_bot_token`.
3. Send any message to your bot, then start the scanner: it prints your **chat_id**.
4. Put the chat_id in `telegram_chat_id` and restart.

> 🔒 Never share your Telegram bot token. **This app does not need a wallet or a private key.**
> If anyone tells you to "add your private key to the bot", it is a scam.

## Settings (config.json)
| Key | Default | Meaning |
|---|---|---|
| `final_stretch_min_mcap` | 8500 | Minimum market cap for final stretch coins |
| `final_stretch_max_age_min` | 30 | Maximum age in minutes |
| `migrated_min_mcap` | 30000 | Minimum market cap for migrated coins |
| `max_top_holder_pct` | 4 | Maximum % for the top holder |
| `max_insider_pct` | 10 | Maximum % for linked wallets |
| `same_pct_holders` | 4 | Holders with the same % that count as a bundle |
| `dashboard_port` | 8787 | Dashboard port |
| `open_browser` | true | Open the dashboard automatically |

## Limits
- Checks use **public APIs** (DexScreener, RugCheck), which can lag by seconds or minutes.
- Advanced bundles (mixers, fresh wallets) **may not show up**.
- It does not see page viewers, KOL wallets, or whether the X community is active. **Check those yourself in Axiom.**
- A coin that looks clean now can still rug later. An alert does not mean the coin is good.
