"""
Only Signals Bot — Version 2 (Production-ready single file)
Architecture: per-user state, token tracking, optional global alerts
Storage: JSON (schema ready for PostgreSQL migration)
Deployment: Railway / any Python host
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
import requests
from datetime import datetime, timedelta
import json
import os
import logging
import asyncio

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = 760930914

CHAIN_FILTER = "bsc"
MIN_LIQUIDITY = 10000
MIN_VOLUME = 5000
CHECK_INTERVAL = 180
MAX_ALERTS_PER_CYCLE = 1
DATA_FILE = "bot_data.json"

# Per-token alert thresholds
PRICE_CHANGE_ALERT_PCT = 10.0
VOLUME_SPIKE_RATIO = 3.0
LIQUIDITY_CHANGE_ALERT_PCT = 20.0
TRACKED_BATCH_SLEEP_EVERY = 10
TRACKED_BATCH_SLEEP_SECONDS = 2
TRACKED_CHECK_MIN_BASELINE_SECONDS = 240  # ignore noisy first re-checks under 4 minutes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# DATA SCHEMA
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_user(chat_id: int) -> dict:
    return {
        "chat_id": chat_id,
        "state": "idle",                  # idle | awaiting_search
        "last_search_query": None,
        "global_alerts": False,
        "blocked": False,
        "first_seen": _now(),
        "last_active": _now(),
    }


def default_tracked_token(chat_id: int, token_key: str, symbol: str, name: str, chain: str) -> dict:
    return {
        "chat_id": chat_id,
        "token_key": token_key,            # format: chain:address or chain:symbol fallback
        "symbol": symbol,
        "name": name,
        "chain": chain,
        "added_at": _now(),
        "alerts": {
            "price_change": True,
            "volume_spike": True,
            "liquidity_change": False,
            "unusual_activity": False,
        },
        "last_price": None,
        "last_volume": None,
        "last_liquidity": None,
        "last_checked": None,
    }


# ─────────────────────────────────────────────
# RUNTIME STATE
# ─────────────────────────────────────────────

seen_tokens: set = set()


# ─────────────────────────────────────────────
# PERSISTENT STATE
# ─────────────────────────────────────────────

class BotData:
    def __init__(self):
        self.users: dict = {}
        self.tracked_tokens: dict = {}
        self.search_history: list = []
        self.search_counts: dict = {}
        self.analyze_count: int = 0
        self.last_alert_message: str = "No alerts yet"
        self.last_check_time: str = "Not started yet"

    # ── user helpers ──────────────────────────

    def get_user(self, chat_id: int) -> dict:
        key = str(chat_id)
        if key not in self.users:
            self.users[key] = default_user(chat_id)
        return self.users[key]

    def touch_user(self, chat_id: int):
        u = self.get_user(chat_id)
        u["last_active"] = _now()

    def set_state(self, chat_id: int, state: str):
        self.get_user(chat_id)["state"] = state

    def get_state(self, chat_id: int) -> str:
        return self.get_user(chat_id).get("state", "idle")

    def set_last_search(self, chat_id: int, query: str):
        self.get_user(chat_id)["last_search_query"] = query

    def get_last_search(self, chat_id: int):
        return self.get_user(chat_id).get("last_search_query")

    def global_alerts_enabled(self, chat_id: int) -> bool:
        return self.get_user(chat_id).get("global_alerts", False)

    def set_global_alerts(self, chat_id: int, enabled: bool):
        self.get_user(chat_id)["global_alerts"] = enabled

    def global_alert_subscribers(self) -> list:
        return [
            int(uid) for uid, u in self.users.items()
            if u.get("global_alerts") and not u.get("blocked")
        ]

    # ── tracked token helpers ─────────────────

    def track_token(self, chat_id: int, token_key: str, symbol: str, name: str, chain: str) -> dict:
        key = f"{chat_id}:{token_key}"
        if key not in self.tracked_tokens:
            self.tracked_tokens[key] = default_tracked_token(chat_id, token_key, symbol, name, chain)
        return self.tracked_tokens[key]

    def untrack_token(self, chat_id: int, token_key: str):
        key = f"{chat_id}:{token_key}"
        self.tracked_tokens.pop(key, None)

    def get_tracked(self, chat_id: int) -> list:
        prefix = f"{chat_id}:"
        return [v for k, v in self.tracked_tokens.items() if k.startswith(prefix)]

    def get_tracked_token(self, chat_id: int, token_key: str):
        return self.tracked_tokens.get(f"{chat_id}:{token_key}")

    def set_alert_pref(self, chat_id: int, token_key: str, pref: str, value: bool):
        entry = self.get_tracked_token(chat_id, token_key)
        if entry:
            entry["alerts"][pref] = value

    # ── search helpers ────────────────────────

    def record_search(self, chat_id: int, query: str):
        cleaned = query.strip().lower()
        if not cleaned:
            return
        self.search_history.append({
            "chat_id": str(chat_id),
            "query": cleaned,
            "timestamp": _now(),
        })
        self.search_counts[cleaned] = self.search_counts.get(cleaned, 0) + 1
        if len(self.search_history) > 5000:
            self.search_history = self.search_history[-5000:]

    def top_searches(self, limit=5) -> list:
        return sorted(self.search_counts.items(), key=lambda x: x[1], reverse=True)[:limit]

    def top_searches_recent(self, days=1, limit=5) -> list:
        now = datetime.now()
        counts: dict = {}
        for item in self.search_history:
            try:
                dt = datetime.strptime(item["timestamp"], "%Y-%m-%d %H:%M:%S")
                if now - dt <= timedelta(days=days):
                    q = item["query"]
                    counts[q] = counts.get(q, 0) + 1
            except Exception:
                continue
        return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]

    # ── activity helpers ──────────────────────

    def active_users(self, days=1) -> int:
        now = datetime.now()
        count = 0
        for u in self.users.values():
            try:
                dt = datetime.strptime(u.get("last_active", ""), "%Y-%m-%d %H:%M:%S")
                if now - dt <= timedelta(days=days):
                    count += 1
            except Exception:
                pass
        return count

    # ── persistence ───────────────────────────

    def load(self):
        global seen_tokens
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            if "users" in raw:
                self.users = raw.get("users", {})
                self.tracked_tokens = raw.get("tracked_tokens", {})
                self.search_history = raw.get("search_history", [])
                self.search_counts = raw.get("search_counts", {})
                self.analyze_count = raw.get("analyze_count", 0)
                self.last_alert_message = raw.get("last_alert_message", "No alerts yet")
                self.last_check_time = raw.get("last_check_time", "Not started yet")
                seen_tokens = set(raw.get("seen_tokens", []))
            else:
                log.info("Migrating V1 data to V2 format...")
                old_subs = set(raw.get("subscribers", []))
                old_blocked = set(raw.get("blocked_users", []))
                old_activity = raw.get("user_activity", {})

                for cid_str in old_activity:
                    cid = int(cid_str)
                    u = self.get_user(cid)
                    u["last_active"] = old_activity[cid_str]
                    if cid in old_subs:
                        u["global_alerts"] = True
                    if cid in old_blocked:
                        u["blocked"] = True

                for cid in old_subs:
                    u = self.get_user(cid)
                    u["global_alerts"] = True

                self.search_history = raw.get("search_history", [])
                self.search_counts = raw.get("search_counts", {})
                self.analyze_count = raw.get("analyze_count", 0)
                self.last_alert_message = raw.get("last_alert_message", "No alerts yet")
                seen_tokens = set(raw.get("seen_tokens", []))
                log.info("V1 migration complete.")

        except Exception as e:
            log.error(f"Error loading data: {e}")

    def save(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "users": self.users,
                    "tracked_tokens": self.tracked_tokens,
                    "search_history": self.search_history,
                    "search_counts": self.search_counts,
                    "analyze_count": self.analyze_count,
                    "last_alert_message": self.last_alert_message,
                    "last_check_time": self.last_check_time,
                    "seen_tokens": list(seen_tokens),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Error saving data: {e}")


db = BotData()


# ─────────────────────────────────────────────
# DEXSCREENER API LAYER
# ─────────────────────────────────────────────

def dex_get(url: str, params: dict = None, timeout: int = 15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning(f"Timeout fetching {url}")
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP error {e} for {url}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Connection error for {url}")
    except Exception as e:
        log.warning(f"Unexpected error for {url}: {e}")
    return None


def get_latest_profiles() -> list:
    data = dex_get("https://api.dexscreener.com/token-profiles/latest/v1")
    if data and isinstance(data, list):
        return data
    return []


def search_pairs(q: str) -> list:
    data = dex_get("https://api.dexscreener.com/latest/dex/search", params={"q": q})
    if data and isinstance(data, dict):
        return data.get("pairs", []) or []
    return []


def get_token_pairs(chain: str, addr: str) -> list:
    data = dex_get(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{addr}")
    if data and isinstance(data, list):
        return data
    return []


# ─────────────────────────────────────────────
# TOKEN ANALYSIS
# ─────────────────────────────────────────────

def choose_best_pair(pairs: list):
    if not pairs:
        return None
    return max(
        pairs,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
    )


def fmt_money(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "N/A"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


def fmt_price(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "N/A"
    if v >= 1:
        return f"{v:.4f}"
    if v >= 0.01:
        return f"{v:.6f}"
    if v >= 0.0001:
        return f"{v:.8f}"
    return f"{v:.10f}".rstrip("0").rstrip(".")


def safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def analyze_pair(pair: dict) -> tuple:
    liquidity = safe_float((pair.get("liquidity") or {}).get("usd"))
    volume = safe_float((pair.get("volume") or {}).get("h24"))
    buys = int(((pair.get("txns") or {}).get("h24") or {}).get("buys") or 0)
    sells = int(((pair.get("txns") or {}).get("h24") or {}).get("sells") or 0)

    score = 0
    notes = []

    if liquidity > 100_000:
        score += 3
        notes.append("very strong liquidity")
    elif liquidity > 50_000:
        score += 2
        notes.append("strong liquidity")
    elif liquidity > 10_000:
        score += 1
        notes.append("acceptable liquidity")
    else:
        notes.append("weak liquidity")

    if volume > 100_000:
        score += 3
        notes.append("very strong volume")
    elif volume > 50_000:
        score += 2
        notes.append("good volume")
    elif volume > 10_000:
        score += 1
        notes.append("moderate volume")
    else:
        notes.append("weak volume")

    if buys > sells:
        score += 1
        notes.append("buy pressure detected")
    elif sells > buys * 2 and sells > 20:
        notes.append("high sell pressure")

    score_pct = min(score * 12, 100)

    if score >= 7:
        verdict = "🟢 Strong"
    elif score >= 4:
        verdict = "🟡 Medium"
    else:
        verdict = "🔴 Risk"

    return verdict, notes, score_pct


def build_scan_msg(pair: dict, header: str = "🧠 *Token Scan*") -> str:
    base = pair.get("baseToken") or {}
    verdict, notes, score_pct = analyze_pair(pair)
    price_usd = pair.get("priceUsd", "N/A")
    price_change = (pair.get("priceChange") or {}).get("h24", "N/A")
    chain = escape_markdown(str(pair.get("chainId") or "N/A").upper(), version=1)
    dex = escape_markdown(str(pair.get("dexId") or "N/A"), version=1)
    symbol = escape_markdown(str(base.get("symbol", "N/A")), version=1)
    name = escape_markdown(str(base.get("name", "N/A")), version=1)
    url = pair.get("url", "")

    link_line = f"\n🔗 [View on Dexscreener]({url})" if url else ""

    return (
        f"{header}\n\n"
        f"🪙 *Token:* {symbol}\n"
        f"📛 *Name:* {name}\n"
        f"🔗 *Chain:* {chain}  |  *DEX:* {dex}\n"
        f"💰 *Price:* ${fmt_price(price_usd)}\n"
        f"📈 *24h Change:* {price_change}%\n"
        f"💧 *Liquidity:* ${fmt_money((pair.get('liquidity') or {}).get('usd'))}\n"
        f"📊 *Volume 24h:* ${fmt_money((pair.get('volume') or {}).get('h24'))}\n"
        f"🎯 *Signal Score:* {score_pct}/100\n\n"
        f"*Verdict:* {verdict}\n"
        f"*Why:* {' | '.join(notes)}"
        f"{link_line}"
    )


def extract_token_key(pair: dict) -> str:
    chain = (pair.get("chainId") or "unknown").lower()
    addr = (pair.get("baseToken") or {}).get("address", "")
    if addr:
        return f"{chain}:{addr.lower()}"
    symbol = (pair.get("baseToken") or {}).get("symbol", "unknown").lower()
    return f"{chain}:{symbol}"


def parse_token_key(token_key: str) -> tuple[str, str]:
    if ":" not in token_key:
        return "", ""
    chain, ident = token_key.split(":", 1)
    return chain.strip().lower(), ident.strip().lower()


def can_query_token_pairs(token_key: str) -> bool:
    chain, ident = parse_token_key(token_key)
    return bool(chain and ident and ident.startswith("0x"))


# ─────────────────────────────────────────────
# KEYBOARDS / MENUS
# ─────────────────────────────────────────────

def main_menu_for(chat_id: int) -> InlineKeyboardMarkup:
    enabled = db.global_alerts_enabled(chat_id)
    toggle_label = "🌐 Global Alerts: ON ✅" if enabled else "🌐 Global Alerts: OFF ❌"
    toggle_data = "global_alerts_off" if enabled else "global_alerts_on"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Token", callback_data="search_prompt")],
        [
            InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ],
        [InlineKeyboardButton(toggle_label, callback_data=toggle_data)],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


def track_prompt_menu(token_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Track this token", callback_data=f"track_add:{token_key}"),
            InlineKeyboardButton("❌ No thanks", callback_data="track_skip"),
        ]
    ])


def alert_prefs_menu(chat_id: int, token_key: str) -> InlineKeyboardMarkup:
    entry = db.get_tracked_token(chat_id, token_key)
    if not entry:
        return InlineKeyboardMarkup([])

    alerts = entry["alerts"]

    def label(pref: str, display: str) -> str:
        return f"{'✅' if alerts.get(pref) else '❌'} {display}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("price_change", "Price Change"), callback_data=f"pref:{token_key}:price_change")],
        [InlineKeyboardButton(label("volume_spike", "Volume Spike"), callback_data=f"pref:{token_key}:volume_spike")],
        [InlineKeyboardButton(label("liquidity_change", "Liquidity Change"), callback_data=f"pref:{token_key}:liquidity_change")],
        [InlineKeyboardButton(label("unusual_activity", "Unusual Activity"), callback_data=f"pref:{token_key}:unusual_activity")],
        [InlineKeyboardButton("🗑 Stop Tracking", callback_data=f"track_remove:{token_key}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="my_tokens")],
    ])


def my_tokens_menu(chat_id: int) -> InlineKeyboardMarkup:
    tracked = db.get_tracked(chat_id)
    if not tracked:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search a Token", callback_data="search_prompt")]
        ])
    rows = []
    for t in tracked:
        label = f"{t['symbol']} ({t['chain'].upper()})"
        rows.append([InlineKeyboardButton(label, callback_data=f"token_detail:{t['token_key']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────
# SMART MONEY PLACEHOLDER
# ─────────────────────────────────────────────

async def smart_money_check(token_key: str) -> dict:
    return {
        "status": "placeholder",
        "whale_activity": None,
        "known_traders": [],
        "confidence": 0,
    }


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.get_user(cid)
    db.touch_user(cid)
    db.save()

    text = (
        "🚀 *Only Signals — V2*\n\n"
        "Your personal crypto intelligence bot.\n\n"
        "*What this bot does:*\n"
        "🔍 Search any token by name, symbol, or contract\n"
        "📌 Track tokens and set your own alert preferences\n"
        "🌐 Optionally subscribe to global new-token alerts\n\n"
        "Start by searching a token below 👇"
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.touch_user(cid)

    text = (
        "❓ *Help — Only Signals V2*\n\n"
        "*Commands:*\n"
        "/start — launch the bot\n"
        "/search — search a token\n"
        "/mytokens — view your tracked tokens\n"
        "/status — bot status\n"
        "/analytics — owner-only metrics\n"
        "/myid — show your chat ID\n\n"
        "*Core flow:*\n"
        "1) Use `/search` or the Search button\n"
        "2) Send token name / symbol / contract\n"
        "3) Get a signal scan\n"
        "4) Choose whether to track the token\n"
        "5) Configure per-token alert preferences\n\n"
        "*Global Alerts:*\n"
        "Optional — receive alerts when new tokens pass the filter.\n"
        "Toggle from the main menu.\n\n"
        "⚠️ Signal scores are filters, not financial advice."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.touch_user(cid)

    global_on = "Enabled ✅" if db.global_alerts_enabled(cid) else "Disabled ❌"
    tracked_count = len(db.get_tracked(cid))

    text = (
        "📊 *Your Status*\n\n"
        f"🌐 Global Alerts: {global_on}\n"
        f"📌 Tokens Tracked: {tracked_count}\n\n"
        f"*Bot Filters*\n"
        f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
        f"💧 Min Liquidity: ${MIN_LIQUIDITY:,}\n"
        f"📊 Min 24h Volume: ${MIN_VOLUME:,}\n"
        f"⏱ Last Check: {db.last_check_time}"
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))


async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    if cid != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    global_subs = len(db.global_alert_subscribers())
    total_users = len(db.users)
    blocked_count = sum(1 for u in db.users.values() if u.get("blocked"))
    tracked_total = len(db.tracked_tokens)

    top_all = db.top_searches(limit=5)
    top_24h = db.top_searches_recent(days=1, limit=5)

    top_all_text = "\n".join(f"  {q}: {c}x" for q, c in top_all) if top_all else "  None"
    top_24h_text = "\n".join(f"  {q}: {c}x" for q, c in top_24h) if top_24h else "  None"

    recent = sorted(db.users.values(), key=lambda u: u.get("last_active", ""), reverse=True)[:10]
    recent_text = "\n".join(
        f"  {u['chat_id']} — {u.get('last_active', 'N/A')}"
        for u in recent
    ) if recent else "  None"

    text = (
        "📈 *Owner Analytics*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🌐 Global Alert Subscribers: {global_subs}\n"
        f"📌 Total Tracked Tokens: {tracked_total}\n"
        f"🚫 Blocked: {blocked_count}\n"
        f"🧠 Total Analyses: {db.analyze_count}\n"
        f"🔥 Active 24h: {db.active_users(1)}\n"
        f"📆 Active 7d: {db.active_users(7)}\n\n"
        f"🔎 *Top Searches (All Time)*\n{top_all_text}\n\n"
        f"⚡ *Top Searches (24h)*\n{top_24h_text}\n\n"
        f"👤 *Recently Active Users*\n{recent_text}"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.set_state(cid, "awaiting_search")
    db.touch_user(cid)
    await update.message.reply_text("🔍 Send a token name, symbol, or contract address:")


async def mytokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.touch_user(cid)
    tracked = db.get_tracked(cid)

    if not tracked:
        await update.message.reply_text(
            "📋 You have no tracked tokens yet.\n\nSearch a token first to start tracking.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Token", callback_data="search_prompt")]
            ])
        )
        return

    text = "📋 *Your Tracked Tokens*\nTap a token to manage alert preferences.\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=my_tokens_menu(cid))


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Chat ID: `{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await search_command(update, context)


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.set_global_alerts(cid, True)
    db.touch_user(cid)
    db.save()
    await update.message.reply_text(
        "🌐 Global alerts enabled. You'll receive new token alerts when they pass the filter.",
        reply_markup=main_menu_for(cid),
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.set_global_alerts(cid, False)
    db.touch_user(cid)
    db.save()
    await update.message.reply_text(
        "🌐 Global alerts disabled.",
        reply_markup=main_menu_for(cid),
    )


# ─────────────────────────────────────────────
# MESSAGE HANDLER — search flow
# ─────────────────────────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    state = db.get_state(cid)

    if state != "awaiting_search":
        await update.message.reply_text(
            "Use the menu or /search to look up a token.",
            reply_markup=main_menu_for(cid),
        )
        return

    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Please send a token name or contract.")
        return

    db.set_state(cid, "idle")
    db.touch_user(cid)
    db.analyze_count += 1
    db.record_search(cid, query_text)
    db.save()

    await update.message.reply_text("🔎 Searching...")

    pairs = search_pairs(query_text)
    if not pairs:
        await update.message.reply_text(
            "❌ No results found. Try a different name, symbol, or contract address.",
            reply_markup=main_menu_for(cid),
        )
        return

    filtered = [p for p in pairs if (p.get("chainId") or "").lower() == CHAIN_FILTER]
    working_pairs = filtered if filtered else pairs

    best = choose_best_pair(working_pairs)
    if not best:
        await update.message.reply_text("❌ No usable pairs found.", reply_markup=main_menu_for(cid))
        return

    scan_text = build_scan_msg(best)
    await update.message.reply_text(scan_text, parse_mode="Markdown", disable_web_page_preview=True)

    token_key = extract_token_key(best)
    symbol = (best.get("baseToken") or {}).get("symbol", "???")
    name = (best.get("baseToken") or {}).get("name", "???")
    chain = (best.get("chainId") or "unknown").lower()

    context.user_data["pending_track"] = {
        "token_key": token_key,
        "symbol": symbol,
        "name": name,
        "chain": chain,
    }

    already_tracked = db.get_tracked_token(cid, token_key) is not None

    if not already_tracked:
        await update.message.reply_text(
            f"📌 Want to track *{escape_markdown(symbol, version=1)}* for alerts?",
            parse_mode="Markdown",
            reply_markup=track_prompt_menu(token_key),
        )
    else:
        await update.message.reply_text(
            f"✅ You're already tracking *{escape_markdown(symbol, version=1)}*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⚙️ Manage {symbol} alerts", callback_data=f"token_detail:{token_key}")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
            ]),
        )


# ─────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = query.message.chat_id
    data = query.data
    db.touch_user(cid)

    if data == "search_prompt":
        db.set_state(cid, "awaiting_search")
        await query.edit_message_text("🔍 Send a token name, symbol, or contract address:")
        return

    if data == "global_alerts_on":
        db.set_global_alerts(cid, True)
        db.save()
        await query.edit_message_text(
            "🌐 *Global Alerts Enabled*\n\nYou'll receive alerts when new tokens pass the filter.",
            parse_mode="Markdown",
            reply_markup=main_menu_for(cid),
        )
        return

    if data == "global_alerts_off":
        db.set_global_alerts(cid, False)
        db.save()
        await query.edit_message_text(
            "🌐 *Global Alerts Disabled*\n\nYou won't receive broadcast alerts.\nYour tracked tokens are unaffected.",
            parse_mode="Markdown",
            reply_markup=main_menu_for(cid),
        )
        return

    if data == "status":
        global_on = "Enabled ✅" if db.global_alerts_enabled(cid) else "Disabled ❌"
        tracked_count = len(db.get_tracked(cid))
        text = (
            "📊 *Your Status*\n\n"
            f"🌐 Global Alerts: {global_on}\n"
            f"📌 Tokens Tracked: {tracked_count}\n\n"
            f"*Bot Filters*\n"
            f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
            f"💧 Min Liquidity: ${MIN_LIQUIDITY:,}\n"
            f"📊 Min 24h Volume: ${MIN_VOLUME:,}\n"
            f"⏱ Last Check: {db.last_check_time}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "help":
        text = (
            "❓ *Help*\n\n"
            "Use the *Search Token* button or `/search` to look up any token.\n"
            "After searching, you can choose to track it and set alert preferences.\n\n"
            "*Global Alerts* — optional broadcast of new tokens that pass the filter.\n"
            "*Tracked Tokens* — your personal watchlist with custom alert settings."
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "back_main":
        await query.edit_message_text(
            "🚀 *Only Signals — V2*\nChoose an option below:",
            parse_mode="Markdown",
            reply_markup=main_menu_for(cid),
        )
        return

    if data == "my_tokens":
        tracked = db.get_tracked(cid)
        if not tracked:
            await query.edit_message_text(
                "📋 You have no tracked tokens.\n\nSearch a token to start tracking.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search Token", callback_data="search_prompt")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
                ]),
            )
        else:
            await query.edit_message_text(
                "📋 *Your Tracked Tokens*\nTap a token to manage its alert settings.",
                parse_mode="Markdown",
                reply_markup=my_tokens_menu(cid),
            )
        return

    if data.startswith("token_detail:"):
        token_key = data.split(":", 1)[1]
        entry = db.get_tracked_token(cid, token_key)
        if not entry:
            await query.edit_message_text("Token not found in your list.", reply_markup=main_menu_for(cid))
            return
        text = (
            f"⚙️ *Alert Settings — {escape_markdown(entry['symbol'], version=1)}*\n"
            f"Chain: {escape_markdown(entry['chain'].upper(), version=1)}\n"
            f"Added: {escape_markdown(entry['added_at'], version=1)}\n\n"
            "Toggle the alerts you want below:"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=alert_prefs_menu(cid, token_key))
        return

    if data.startswith("track_add:"):
        token_key = data.split(":", 1)[1]
        pending = context.user_data.get("pending_track", {})

        symbol = pending.get("symbol", "???")
        name = pending.get("name", "???")
        chain = pending.get("chain", "unknown")

        db.track_token(cid, token_key, symbol, name, chain)
        db.save()

        text = (
            f"✅ *{escape_markdown(symbol, version=1)}* added to your tracked tokens.\n\n"
            "Configure your alert preferences:"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=alert_prefs_menu(cid, token_key))
        return

    if data == "track_skip":
        await query.edit_message_text("👍 No problem. Search another token anytime.", reply_markup=main_menu_for(cid))
        return

    if data.startswith("track_remove:"):
        token_key = data.split(":", 1)[1]
        entry = db.get_tracked_token(cid, token_key)
        symbol = entry["symbol"] if entry else token_key
        db.untrack_token(cid, token_key)
        db.save()
        await query.edit_message_text(
            f"🗑 *{escape_markdown(symbol, version=1)}* removed from tracking.",
            parse_mode="Markdown",
            reply_markup=main_menu_for(cid),
        )
        return

    if data.startswith("pref:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("Invalid preference.", reply_markup=main_menu_for(cid))
            return
        _, token_key, pref = parts
        entry = db.get_tracked_token(cid, token_key)
        if not entry:
            await query.edit_message_text("Token not found.", reply_markup=main_menu_for(cid))
            return
        current = entry["alerts"].get(pref, False)
        db.set_alert_pref(cid, token_key, pref, not current)
        db.save()
        text = (
            f"⚙️ *Alert Settings — {escape_markdown(entry['symbol'], version=1)}*\n"
            f"Chain: {escape_markdown(entry['chain'].upper(), version=1)}\n\n"
            "Toggle the alerts you want below:"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=alert_prefs_menu(cid, token_key))
        return

    await query.edit_message_text("Unknown option.", reply_markup=main_menu_for(cid))


# ─────────────────────────────────────────────
# BACKGROUND JOB — global new token alerts
# ─────────────────────────────────────────────

async def check_new(context: ContextTypes.DEFAULT_TYPE):
    global seen_tokens

    db.last_check_time = _now()

    profiles = get_latest_profiles()
    if not profiles:
        log.info("check_new: no profiles returned.")
        return

    if not seen_tokens:
        for item in profiles:
            chain = item.get("chainId")
            addr = item.get("tokenAddress")
            if chain and addr:
                seen_tokens.add(f"{chain}:{addr}")
        db.save()
        log.info(f"check_new: initialized with {len(seen_tokens)} tokens.")
        return

    alerts_built = []

    for item in profiles:
        chain = item.get("chainId", "")
        addr = item.get("tokenAddress", "")

        if not chain or not addr:
            continue
        if chain.lower() != CHAIN_FILTER:
            continue

        key = f"{chain}:{addr}"
        if key in seen_tokens:
            continue

        seen_tokens.add(key)

        pairs = get_token_pairs(chain, addr)
        best = choose_best_pair(pairs)
        if not best:
            continue

        liquidity = safe_float((best.get("liquidity") or {}).get("usd"))
        volume = safe_float((best.get("volume") or {}).get("h24"))

        if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
            continue

        msg = build_scan_msg(best, header="🚨 *New Token Alert*")
        alerts_built.append(msg)

    alerts_built = alerts_built[:MAX_ALERTS_PER_CYCLE]
    subscribers = db.global_alert_subscribers()

    if alerts_built and subscribers:
        for msg in alerts_built:
            db.last_alert_message = msg
            for cid in subscribers:
                try:
                    await context.bot.send_message(cid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                except Exception as e:
                    err = str(e).lower()
                    log.warning(f"Send error to {cid}: {e}")
                    if "blocked" in err or "chat not found" in err:
                        db.get_user(cid)["blocked"] = True
                        db.get_user(cid)["global_alerts"] = False

    db.save()


# ─────────────────────────────────────────────
# BACKGROUND JOB — tracked token alerts
# ─────────────────────────────────────────────

async def check_tracked_tokens(context: ContextTypes.DEFAULT_TYPE):
    """
    Checks all user-tracked tokens for material changes and sends alerts
    according to each user's token-specific preferences.

    Current rules:
    - price_change: absolute move greater than 10% vs previous snapshot
    - volume_spike: current 24h volume greater than 3x previous snapshot
    - liquidity_change: absolute move greater than 20% vs previous snapshot

    Notes:
    - First observation initializes the baseline and sends no alert.
    - Only token_keys with on-chain addresses are eligible for Dexscreener
      pair re-fetching. Symbol-fallback token_keys are skipped safely.
    - Multiple users tracking the same token share one API fetch per cycle.
    """
    if not db.tracked_tokens:
        return

    token_to_record_keys: dict[str, list[str]] = {}

    for record_key, record in db.tracked_tokens.items():
        token_key = record.get("token_key", "")
        if not can_query_token_pairs(token_key):
            log.debug(f"check_tracked_tokens: skipping non-address token_key {token_key}")
            continue
        token_to_record_keys.setdefault(token_key, []).append(record_key)

    if not token_to_record_keys:
        return

    api_call_count = 0
    alerts_sent = 0

    for token_key, record_keys in token_to_record_keys.items():
        if api_call_count > 0 and api_call_count % TRACKED_BATCH_SLEEP_EVERY == 0:
            await asyncio.sleep(TRACKED_BATCH_SLEEP_SECONDS)

        chain, addr = parse_token_key(token_key)
        if not chain or not addr:
            continue

        pairs = get_token_pairs(chain, addr)
        api_call_count += 1
        best = choose_best_pair(pairs)

        if not best:
            continue

        current_price = safe_float(best.get("priceUsd"))
        current_volume = safe_float((best.get("volume") or {}).get("h24"))
        current_liquidity = safe_float((best.get("liquidity") or {}).get("usd"))
        current_buys = int(((best.get("txns") or {}).get("h24") or {}).get("buys") or 0)
        current_sells = int(((best.get("txns") or {}).get("h24") or {}).get("sells") or 0)
        dex_url = best.get("url", "")
        now_str = _now()

        for record_key in record_keys:
            record = db.tracked_tokens.get(record_key)
            if not record:
                continue

            chat_id = record.get("chat_id")
            user = db.get_user(chat_id)
            if user.get("blocked"):
                continue

            prefs = record.get("alerts", {})
            symbol_raw = str(record.get("symbol", "?"))
            symbol = escape_markdown(symbol_raw, version=1)
            chain_display = escape_markdown(str(record.get("chain", chain)).upper(), version=1)

            last_price = safe_float(record.get("last_price"), default=0.0)
            last_volume = safe_float(record.get("last_volume"), default=0.0)
            last_liquidity = safe_float(record.get("last_liquidity"), default=0.0)
            last_checked_str = record.get("last_checked")

            if last_price <= 0 and last_volume <= 0 and last_liquidity <= 0:
                record.update({
                    "last_price": current_price,
                    "last_volume": current_volume,
                    "last_liquidity": current_liquidity,
                    "last_checked": now_str,
                })
                continue

            if last_checked_str:
                try:
                    last_checked_dt = datetime.strptime(last_checked_str, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - last_checked_dt).total_seconds() < TRACKED_CHECK_MIN_BASELINE_SECONDS:
                        record.update({
                            "last_price": current_price,
                            "last_volume": current_volume,
                            "last_liquidity": current_liquidity,
                            "last_checked": now_str,
                        })
                        continue
                except Exception:
                    pass

            alert_lines = []

            if prefs.get("price_change") and last_price > 0:
                price_pct = ((current_price - last_price) / last_price) * 100
                if abs(price_pct) > PRICE_CHANGE_ALERT_PCT:
                    direction = "📈" if price_pct > 0 else "📉"
                    sign = "+" if price_pct > 0 else ""
                    alert_lines.append(
                        f"{direction} *Price:* {sign}{price_pct:.1f}%  `${fmt_price(last_price)}` → `${fmt_price(current_price)}`"
                    )

            if prefs.get("volume_spike") and last_volume > 0:
                volume_ratio = current_volume / last_volume if last_volume > 0 else 0
                if volume_ratio > VOLUME_SPIKE_RATIO:
                    alert_lines.append(
                        f"🔥 *Volume spike:* {volume_ratio:.1f}x  `${fmt_money(last_volume)}` → `${fmt_money(current_volume)}`"
                    )

            if prefs.get("liquidity_change") and last_liquidity > 0:
                liquidity_pct = ((current_liquidity - last_liquidity) / last_liquidity) * 100
                if abs(liquidity_pct) > LIQUIDITY_CHANGE_ALERT_PCT:
                    direction = "⬆️" if liquidity_pct > 0 else "⬇️"
                    sign = "+" if liquidity_pct > 0 else ""
                    alert_lines.append(
                        f"{direction} *Liquidity:* {sign}{liquidity_pct:.1f}%  `${fmt_money(last_liquidity)}` → `${fmt_money(current_liquidity)}`"
                    )

            if prefs.get("unusual_activity") and (current_buys > 0 or current_sells > 0):
                if current_buys >= 5 * max(current_sells, 1) and current_volume >= 10_000:
                    alert_lines.append(
                        f"🧪 *Unusual activity:* buy pressure detected ({current_buys} buys vs {current_sells} sells)"
                    )
                elif current_sells >= 5 * max(current_buys, 1) and current_volume >= 10_000:
                    alert_lines.append(
                        f"🧪 *Unusual activity:* sell pressure detected ({current_sells} sells vs {current_buys} buys)"
                    )

            if alert_lines:
                header = f"🚨 *Tracked Token Alert — {symbol}*\n"
                sub = f"🔗 *Chain:* {chain_display}\n\n"
                body = "\n".join(alert_lines)
                footer = f"\n\n[View on Dexscreener]({dex_url})" if dex_url else ""
                message = header + sub + body + footer

                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                    alerts_sent += 1
                except Exception as e:
                    err = str(e).lower()
                    log.warning(f"check_tracked_tokens: failed to send alert to {chat_id}: {e}")
                    if "blocked" in err or "chat not found" in err:
                        user["blocked"] = True

            record.update({
                "last_price": current_price,
                "last_volume": current_volume,
                "last_liquidity": current_liquidity,
                "last_checked": now_str,
            })

    db.save()
    log.info(
        f"check_tracked_tokens: completed. {api_call_count} API call(s), "
        f"{len(token_to_record_keys)} unique token(s), {alerts_sent} alert(s) sent."
    )


# ─────────────────────────────────────────────
# APP SETUP & RUN
# ─────────────────────────────────────────────

db.load()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("status", status_command))
app.add_handler(CommandHandler("analytics", analytics_command))
app.add_handler(CommandHandler("search", search_command))
app.add_handler(CommandHandler("mytokens", mytokens_command))
app.add_handler(CommandHandler("myid", myid_command))

app.add_handler(CommandHandler("analyze", analyze_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

try:
    if app.job_queue:
        app.job_queue.run_repeating(check_new, interval=CHECK_INTERVAL, first=10)
        app.job_queue.run_repeating(check_tracked_tokens, interval=300, first=30)
        log.info("Job queue started.")
    else:
        log.warning("job_queue is None — background jobs disabled. Install python-telegram-bot[job-queue].")
except Exception as e:
    log.warning(f"Could not start job queue: {e}")

log.info("Bot V2 production file running...")
app.run_polling()
