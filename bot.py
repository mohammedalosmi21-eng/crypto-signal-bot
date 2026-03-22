"""
Only Signals Bot — Version 2 (Production-ready single file)
Architecture: per-user state, token tracking, independent new-token alerts, smart-money alerts, and manipulation alerts, developer contact
Storage: JSON (schema ready for PostgreSQL migration)
Deployment: Railway / any Python host

FIXES vs previous version:
- token_detail callback: query.answer() is unconditional and first;
  UI response uses context.bot.send_message() as primary path,
  edit_message_text() is best-effort cleanup only.
- pref| callback: same pattern — answer first, send_message primary,
  no silent edit failures possible.
- Both handlers are wrapped so every code path produces a visible response.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.helpers import escape_markdown
import requests
from datetime import datetime, timedelta
import json
import os
import logging
import asyncio
from typing import Optional

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = 760930914

# Subscription / payments
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))
PREMIUM_DAYS = int(os.getenv("PREMIUM_DAYS", "30"))
STARS_PRICE = int(os.getenv("STARS_PRICE", "500"))
TRADER_STARS_PRICE = int(os.getenv("TRADER_STARS_PRICE", "150"))
PRO_STARS_PRICE = int(os.getenv("PRO_STARS_PRICE", "500"))
ELITE_STARS_PRICE = int(os.getenv("ELITE_STARS_PRICE", "1200"))
PAYMENT_WALLET = os.getenv("PAYMENT_WALLET", "0x73c95943191fddc3e44fff22749c4ccc1ccc8a08")
PAYMENT_NETWORK = os.getenv("PAYMENT_NETWORK", "BEP20 (BSC)")
SUBSCRIPTION_PRICE_USDT = float(os.getenv("SUBSCRIPTION_PRICE_USDT", "10"))

PLAN_CATALOG = {
    "trader": {"label": "Trader", "rank": 1, "stars": TRADER_STARS_PRICE, "usdt": 5, "days": 30, "headline": "Fast alerts + cleaner execution", "features": ["⚡ Fast Alerts mode", "📦 Up to 15 tracked tokens", "🔔 Basic alerts with faster cadence", "🧪 Alpha Preview"]},
    "pro": {"label": "Pro Alpha", "rank": 2, "stars": PRO_STARS_PRICE, "usdt": 10, "days": 30, "headline": "Decision edge for serious traders", "features": ["🐋 Smart Money Alerts", "⚠️ Manipulation Alerts", "📡 Real-time Signals", "🧬 Full Alpha Breakdown", "⚡/📊/📈 Alert modes"]},
    "elite": {"label": "Elite", "rank": 3, "stars": ELITE_STARS_PRICE, "usdt": 25, "days": 30, "headline": "Priority intelligence + custom control", "features": ["⚙️ Custom Filters", "🚨 Priority signal framing", "📦 Up to 100 tracked tokens", "💬 Priority support", "Everything in Pro Alpha"]},
}

CHAIN_FILTER = "bsc"
MIN_LIQUIDITY = 10000
MIN_VOLUME = 5000
CHECK_INTERVAL = 180
MAX_ALERTS_PER_CYCLE = 1
DATA_FILE = "bot_data.json"
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
SMART_MONEY_CHECK_INTERVAL = 180
SMART_MONEY_MIN_TOKEN_VALUE_USD = 1000.0
SMART_MONEY_WALLETS = [
    {"label": "Wallet 1", "address": "0x0000000000000000000000000000000000000001"},
    {"label": "Wallet 2", "address": "0x0000000000000000000000000000000000000002"},
    {"label": "Wallet 3", "address": "0x0000000000000000000000000000000000000003"},
]

SMART_MONEY_CLUSTER_WINDOW_SECONDS = 900
SMART_MONEY_MIN_CLUSTER_WALLETS = 2
SMART_MONEY_MAX_TOKEN_ALERTS_PER_CYCLE = 10

# Per-token alert thresholds
PRICE_CHANGE_ALERT_PCT = 10.0
VOLUME_SPIKE_RATIO = 3.0
LIQUIDITY_CHANGE_ALERT_PCT = 20.0
TRACKED_BATCH_SLEEP_EVERY = 10
TRACKED_BATCH_SLEEP_SECONDS = 2
TRACKED_CHECK_MIN_BASELINE_SECONDS = 240

# Market manipulation detection thresholds
MANIPULATION_PRICE_SPIKE_PCT = 8.0
MANIPULATION_VOLUME_SPIKE_RATIO = 3.0
MANIPULATION_MIN_LIQUIDITY_USD = 10000.0
MANIPULATION_MIN_VOLUME_USD = 25000.0
MANIPULATION_BUY_SELL_RATIO = 4.0

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
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "chat_id": chat_id,
        "state": "idle",
        "last_search_query": None,
        "token_alerts": False,
        "smart_money_alerts": False,
        "manipulation_alerts": False,
        "blocked": False,
        "first_seen": now_str,
        "last_active": now_str,
        "trial_start": now_str,
        "trial_end": (now_dt + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
        "is_paid": False,
        "paid_until": None,
        "subscription_plan": "free",
        "subscription_tier": "free",
        "payment_method": None,
        "alert_mode": "normal",
        "custom_filters": False,
    }


def default_tracked_token(chat_id: int, token_key: str, symbol: str, name: str, chain: str) -> dict:
    return {
        "chat_id": chat_id,
        "token_key": token_key,
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


def parse_dt(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def is_trial_active(chat_id: int) -> bool:
    user = db.get_user(chat_id) if "db" in globals() else None
    if not user:
        return False
    trial_end = parse_dt(user.get("trial_end"))
    return bool(trial_end and datetime.now() < trial_end)


def is_paid_active(chat_id: int) -> bool:
    user = db.get_user(chat_id) if "db" in globals() else None
    if not user or not user.get("is_paid"):
        return False
    paid_until = parse_dt(user.get("paid_until"))
    return bool(paid_until and datetime.now() < paid_until)


def has_premium_access(chat_id: int) -> bool:
    return is_trial_active(chat_id) or is_paid_active(chat_id)


def trial_days_left(chat_id: int) -> int:
    user = db.get_user(chat_id) if "db" in globals() else None
    if not user:
        return 0
    trial_end = parse_dt(user.get("trial_end"))
    if not trial_end:
        return 0
    delta = trial_end - datetime.now()
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def build_payment_message() -> str:
    return (
        "💳 *Upgrade to Premium*\n\n"
        f"After your *{TRIAL_DAYS}-day free trial*, premium access costs *{SUBSCRIPTION_PRICE_USDT:.0f} USDT / {PREMIUM_DAYS} days* or *{STARS_PRICE} Telegram Stars*.\n\n"
        "*Premium unlocks:*\n"
        "• 🐋 Smart Money Alerts\n"
        "• ⚠️ Manipulation Alerts\n"
        "• Priority intelligence layer\n\n"
        f"*USDT option:*\nNetwork: {PAYMENT_NETWORK}\nAddress: `" + PAYMENT_WALLET + "`\n\n"
        "⚠️ Only send USDT on the specified network.\n"
        "If you prefer the easiest in-app payment, use Telegram Stars below."
    )


def trial_or_subscription_status(chat_id: int) -> str:
    user = db.get_user(chat_id)
    tier = user.get("subscription_tier", "free")
    label = "Free" if tier == "free" else PLAN_CATALOG.get(tier, {}).get("label", tier.title())
    if is_paid_active(chat_id):
        return f"💎 {label} active until {user.get('paid_until')}"
    if is_trial_active(chat_id):
        return f"🆓 Trial active — {trial_days_left(chat_id)} day(s) left"
    return "⛔ Trial ended — premium required for Smart Money and Manipulation"


def current_user_tier(chat_id: int) -> str:
    user = db.get_user(chat_id)
    if is_paid_active(chat_id):
        return user.get("subscription_tier", "pro")
    if is_trial_active(chat_id):
        return "trial"
    return "free"


def tier_rank_value(tier: str) -> int:
    if tier == "trial":
        return PLAN_CATALOG["pro"]["rank"]
    if tier == "free":
        return 0
    return PLAN_CATALOG.get(tier, {}).get("rank", 0)


def feature_allowed(chat_id: int, feature: str) -> bool:
    tier = current_user_tier(chat_id)
    rank = tier_rank_value(tier)
    if feature in {"search", "basic_alerts", "my_tokens", "status", "alpha_preview", "alert_mode_basic"}:
        return True
    if feature == "fast_mode":
        return rank >= 1
    if feature in {"smart_money", "manipulation", "signals_realtime", "alpha_full"}:
        return rank >= 2
    if feature == "custom_filters":
        return rank >= 3
    return False


def tracked_token_limit_for(chat_id: int) -> int:
    tier = current_user_tier(chat_id)
    if tier == "trial":
        return 50
    if tier == "elite":
        return 100
    if tier == "pro":
        return 50
    if tier == "trader":
        return 15
    return 5


def alert_mode_label(mode: str) -> str:
    return {"fast": "⚡ Fast", "normal": "📊 Normal", "long": "📈 Long-term"}.get(mode, "📊 Normal")


def alert_check_interval_seconds(chat_id: int) -> int:
    mode = db.get_user(chat_id).get("alert_mode", "normal")
    if mode == "fast":
        return 300
    if mode == "long":
        return 86400
    return 10800


def premium_plan_card(plan_key: str) -> str:
    plan = PLAN_CATALOG[plan_key]
    body = "\n".join(f"• {item}" for item in plan["features"])
    return f"*{plan['label']}*\n{plan['headline']}\nStars: *{plan['stars']}*  |  USDT: *{plan['usdt']}*  |  Days: *{plan['days']}*\n\n{body}"


def build_subscription_hub(chat_id: int) -> str:
    tier = current_user_tier(chat_id)
    tier_label = "Free" if tier == "free" else ("Trial" if tier == "trial" else PLAN_CATALOG[tier]["label"])
    return (
        f"💎 *Quantara Subscription Hub*\n\nCurrent access: *{tier_label}*\nStatus: {trial_or_subscription_status(chat_id)}\n\n"
        f"*Free keeps the bot useful:*\n• 📊 Prices / Search\n• 🔔 Basic Alerts\n• 📦 My Tokens\n\n"
        f"*Premium sells decisions, not raw data:*\n• 🐋 Smart Money\n• ⚠️ Manipulation\n• 📡 Real-time Signals\n• ⚙️ Custom Filters\n• 🧬 Full Alpha Lab\n\n"
        f"Choose the plan that matches your speed and edge."
    )


def alpha_components(pair: dict) -> dict:
    liquidity = safe_float((pair.get("liquidity") or {}).get("usd"))
    volume = safe_float((pair.get("volume") or {}).get("h24"))
    buys = int(((pair.get("txns") or {}).get("h24") or {}).get("buys") or 0)
    sells = int(((pair.get("txns") or {}).get("h24") or {}).get("sells") or 0)
    price_change = safe_float((pair.get("priceChange") or {}).get("h24"))
    score = 0
    risk = 35
    notes = []
    if liquidity >= 100_000:
        score += 28; notes.append("deep liquidity")
    elif liquidity >= 30_000:
        score += 18; notes.append("usable liquidity")
    else:
        risk += 15; notes.append("thin liquidity")
    if volume >= 100_000:
        score += 24; notes.append("strong volume")
    elif volume >= 20_000:
        score += 14; notes.append("moderate volume")
    else:
        risk += 10; notes.append("weak volume")
    flow_ratio = buys / max(sells, 1)
    if flow_ratio >= 1.8:
        score += 18; notes.append("buy pressure")
    elif flow_ratio < 0.8:
        risk += 12; notes.append("sell pressure")
    if -8 <= price_change <= 18:
        score += 14; notes.append("healthy momentum")
    elif price_change > 35:
        risk += 18; notes.append("overextended move")
    elif price_change < -20:
        risk += 10; notes.append("heavy downside")
    if liquidity > 0 and volume / max(liquidity, 1) > 3:
        risk += 12; notes.append("volume/liquidity imbalance")
    alpha = max(5, min(95, score))
    risk = max(5, min(95, risk))
    if alpha >= 75 and risk <= 45:
        grade, verdict, action = "A", "Offensive", "Act fast if confirmation aligns"
    elif alpha >= 60 and risk <= 60:
        grade, verdict, action = "B", "Constructive", "Watch closely / size selectively"
    elif alpha >= 45:
        grade, verdict, action = "C", "Mixed", "Observe, do not chase"
    else:
        grade, verdict, action = "D", "Weak", "Avoid until structure improves"
    probability = min(90, max(20, int(alpha - (risk * 0.35) + 20)))
    return {"alpha": alpha, "risk": risk, "grade": grade, "verdict": verdict, "action": action, "probability": probability, "notes": notes, "price_change": price_change, "buys": buys, "sells": sells}


def build_alpha_summary(pair: dict, premium: bool = False) -> str:
    c = alpha_components(pair)
    notes = ", ".join(c["notes"][:3]) if c["notes"] else "no clear edge"
    text = (
        f"🧬 *Alpha Summary*\n"
        f"Alpha Score: *{c['alpha']}*/95\n"
        f"Risk Score: *{c['risk']}*/95\n"
        f"Grade: *{c['grade']}*  |  Probability: *{c['probability']}%*\n"
        f"Verdict: *{c['verdict']}*\n"
        f"Driver: {escape_markdown(notes, version=1)}\n"
        f"Action: *{escape_markdown(c['action'], version=1)}*"
    )
    if premium:
        text += f"\n\nBuy/Sell Flow: *{c['buys']} / {c['sells']}*\n24h Change: *{c['price_change']:.2f}%*"
    return text


# ─────────────────────────────────────────────
# RUNTIME STATE
# ─────────────────────────────────────────────
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
        self.smart_money_last_check_time: str = "Not started yet"
        self.manipulation_last_check_time: str = "Not started yet"
        self.smart_money_seen_hashes: dict = {}

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

    def token_alerts_enabled(self, chat_id: int) -> bool:
        return self.get_user(chat_id).get("token_alerts", False)

    def set_token_alerts(self, chat_id: int, enabled: bool):
        self.get_user(chat_id)["token_alerts"] = enabled

    def token_alert_subscribers(self) -> list:
        return [
            int(uid) for uid, u in self.users.items()
            if u.get("token_alerts") and not u.get("blocked")
        ]

    def smart_money_alerts_enabled(self, chat_id: int) -> bool:
        return self.get_user(chat_id).get("smart_money_alerts", False)

    def set_smart_money_alerts(self, chat_id: int, enabled: bool):
        self.get_user(chat_id)["smart_money_alerts"] = enabled

    def smart_money_subscribers(self) -> list:
        return [
            int(uid) for uid, u in self.users.items()
            if u.get("smart_money_alerts") and not u.get("blocked")
        ]

    def manipulation_alerts_enabled(self, chat_id: int) -> bool:
        return self.get_user(chat_id).get("manipulation_alerts", False)

    def set_manipulation_alerts(self, chat_id: int, enabled: bool):
        self.get_user(chat_id)["manipulation_alerts"] = enabled

    def manipulation_subscribers(self) -> list:
        return [
            int(uid) for uid, u in self.users.items()
            if u.get("manipulation_alerts") and not u.get("blocked")
        ]

    def premium_subscribers(self) -> list:
        return [
            int(uid) for uid, u in self.users.items()
            if u.get("is_paid") and not u.get("blocked")
        ]

    def premium_active_count(self) -> int:
        now = datetime.now()
        count = 0
        for u in self.users.values():
            if not u.get("is_paid"):
                continue
            try:
                paid_until = datetime.strptime(u.get("paid_until") or "", "%Y-%m-%d %H:%M:%S")
                if paid_until > now:
                    count += 1
            except Exception:
                continue
        return count

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
                for u in self.users.values():
                    if "global_alerts" in u and "token_alerts" not in u:
                        u["token_alerts"] = u.pop("global_alerts")
                    if "smart_money_alerts" not in u:
                        u["smart_money_alerts"] = False
                    if "manipulation_alerts" not in u:
                        u["manipulation_alerts"] = False
                    if "trial_start" not in u:
                        u["trial_start"] = _now()
                    if "trial_end" not in u:
                        u["trial_end"] = (datetime.now() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                    if "is_paid" not in u:
                        u["is_paid"] = False
                    if "paid_until" not in u:
                        u["paid_until"] = None
                    if "subscription_plan" not in u:
                        u["subscription_plan"] = "free"
                    if "subscription_tier" not in u:
                        u["subscription_tier"] = "free"
                    if "payment_method" not in u:
                        u["payment_method"] = None
                    if "alert_mode" not in u:
                        u["alert_mode"] = "normal"
                    if "custom_filters" not in u:
                        u["custom_filters"] = False
                self.tracked_tokens = raw.get("tracked_tokens", {})
                self.search_history = raw.get("search_history", [])
                self.search_counts = raw.get("search_counts", {})
                self.analyze_count = raw.get("analyze_count", 0)
                self.last_alert_message = raw.get("last_alert_message", "No alerts yet")
                self.last_check_time = raw.get("last_check_time", "Not started yet")
                self.smart_money_last_check_time = raw.get("smart_money_last_check_time", "Not started yet")
                self.manipulation_last_check_time = raw.get("manipulation_last_check_time", "Not started yet")
                self.smart_money_seen_hashes = raw.get("smart_money_seen_hashes", {})
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
                        u["token_alerts"] = True
                    if cid in old_blocked:
                        u["blocked"] = True

                for cid in old_subs:
                    u = self.get_user(cid)
                    u["token_alerts"] = True

                self.search_history = raw.get("search_history", [])
                self.search_counts = raw.get("search_counts", {})
                self.analyze_count = raw.get("analyze_count", 0)
                self.last_alert_message = raw.get("last_alert_message", "No alerts yet")
                self.smart_money_last_check_time = raw.get("smart_money_last_check_time", "Not started yet")
                self.manipulation_last_check_time = raw.get("manipulation_last_check_time", "Not started yet")
                self.smart_money_seen_hashes = raw.get("smart_money_seen_hashes", {})
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
                    "smart_money_last_check_time": self.smart_money_last_check_time,
                    "manipulation_last_check_time": self.manipulation_last_check_time,
                    "smart_money_seen_hashes": self.smart_money_seen_hashes,
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
    enabled = db.token_alerts_enabled(chat_id)
    toggle_label = "🔔 Basic Alerts: ON ✅" if enabled else "🔔 Basic Alerts: OFF ❌"
    toggle_data = "token_alerts_off" if enabled else "token_alerts_on"
    sm_enabled = db.smart_money_alerts_enabled(chat_id)
    sm_label = "🐋 Smart Money: ON ✅" if sm_enabled else "🐋 Smart Money: OFF ❌"
    sm_data = "smart_money_off" if sm_enabled else "smart_money_on"
    manip_enabled = db.manipulation_alerts_enabled(chat_id)
    manip_label = "⚠️ Manipulation: ON ✅" if manip_enabled else "⚠️ Manipulation: OFF ❌"
    manip_data = "manipulation_off" if manip_enabled else "manipulation_on"
    mode_label = alert_mode_label(db.get_user(chat_id).get("alert_mode", "normal"))

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Prices / Search", callback_data="search_prompt")],
        [InlineKeyboardButton(toggle_label, callback_data=toggle_data), InlineKeyboardButton("📦 My Tokens", callback_data="my_tokens")],
        [InlineKeyboardButton(f"⏱ Alert Mode: {mode_label}", callback_data="alert_mode_menu")],
        [InlineKeyboardButton(sm_label, callback_data=sm_data)],
        [InlineKeyboardButton(manip_label, callback_data=manip_data)],
        [InlineKeyboardButton("🧬 Alpha Lab", callback_data="alpha_lab"), InlineKeyboardButton("⚙️ Custom Filters", callback_data="custom_filters")],
        [InlineKeyboardButton("💎 Upgrade", callback_data="subscribe_info"), InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("💬 Contact Developer", callback_data="contact_prompt"), InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


def track_prompt_menu(token_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Track this token", callback_data=f"track_add:{token_key}"),
            InlineKeyboardButton("❌ No thanks", callback_data=f"track_skip:{token_key}"),
        ]
    ])


def alert_prefs_menu(chat_id: int, token_key: str) -> InlineKeyboardMarkup:
    entry = db.get_tracked_token(chat_id, token_key)
    if not entry:
        return InlineKeyboardMarkup([])

    alerts = entry["alerts"]

    def label(pref: str, display: str) -> str:
        return f"{'✅' if alerts.get(pref) else '❌'} {display}"

    # ── FIX: callback_data uses pipe separator ─────────────────────────────
    # Format: pref|<pref_name>|<token_key>
    # token_key already contains a colon (chain:address), so we MUST use a
    # different separator between pref_name and token_key to avoid ambiguity.
    # Pipe (|) is safe: it does not appear in chain IDs, addresses, or symbols.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("price_change", "Price Change"),
                              callback_data=f"pref|price_change|{token_key}")],
        [InlineKeyboardButton(label("volume_spike", "Volume Spike"),
                              callback_data=f"pref|volume_spike|{token_key}")],
        [InlineKeyboardButton(label("liquidity_change", "Liquidity Change"),
                              callback_data=f"pref|liquidity_change|{token_key}")],
        [InlineKeyboardButton(label("unusual_activity", "Unusual Activity"),
                              callback_data=f"pref|unusual_activity|{token_key}")],
        [InlineKeyboardButton("🗑 Stop Tracking", callback_data=f"track_remove:{token_key}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="my_tokens")],
    ])


def tracked_token_action_menu(token_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Manage Alerts", callback_data=f"manage_alerts:{token_key}")],
        [InlineKeyboardButton("🗑 Remove from List", callback_data=f"track_remove_confirm:{token_key}")],
        [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
    ])


def token_delete_confirm_menu(token_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Manage Alerts", callback_data=f"manage_alerts:{token_key}")],
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"track_remove:{token_key}"),
            InlineKeyboardButton("❌ No", callback_data="back_main"),
        ],
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
        rows.append([InlineKeyboardButton(label, callback_data=f"token_detail|{t['token_key']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def payment_options_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🟡 Trader — {PLAN_CATALOG['trader']['stars']}⭐", callback_data="plan_trader")],
        [InlineKeyboardButton(f"🔵 Pro Alpha — {PLAN_CATALOG['pro']['stars']}⭐", callback_data="plan_pro")],
        [InlineKeyboardButton(f"🔴 Elite — {PLAN_CATALOG['elite']['stars']}⭐", callback_data="plan_elite")],
        [InlineKeyboardButton("💸 Show USDT Payment Details", callback_data="subscribe_usdt")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
    ])


def alert_mode_menu(chat_id: int) -> InlineKeyboardMarkup:
    current = db.get_user(chat_id).get("alert_mode", "normal")
    def row(mode: str, label: str):
        prefix = "✅ " if current == mode else ""
        return [InlineKeyboardButton(prefix + label, callback_data=f"mode_{mode}")]
    return InlineKeyboardMarkup([
        row("fast", "⚡ Fast Alerts"),
        row("normal", "📊 Normal"),
        row("long", "📈 Long-term"),
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
    ])


def premium_gate_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Upgrade to Pro Alpha", callback_data="subscribe_info")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
    ])


# ─────────────────────────────────────────────
# SMART MONEY LAYER# ─────────────────────────────────────────────
# SMART MONEY LAYER (BSC via BscScan)
# ─────────────────────────────────────────────

def normalize_address(addr: str) -> str:
    return (addr or "").strip().lower()


def smart_wallets() -> list:
    cleaned = []
    for item in SMART_MONEY_WALLETS:
        addr = normalize_address(item.get("address", ""))
        if addr.startswith("0x") and len(addr) == 42:
            cleaned.append({"label": item.get("label", addr[-6:]), "address": addr})
    return cleaned


def bscscan_get(params: dict, timeout: int = 20):
    if not BSCSCAN_API_KEY:
        return None
    full_params = dict(params)
    full_params["apikey"] = BSCSCAN_API_KEY
    try:
        r = requests.get("https://api.bscscan.com/api", params=full_params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        status = str(data.get("status", ""))
        message = str(data.get("message", ""))
        result = data.get("result")
        if status == "1" and isinstance(result, list):
            return result
        if isinstance(result, str) and "No transactions found" in result:
            return []
        if message.upper() == "OK" and isinstance(result, list):
            return result
        log.warning(f"bscscan_get: unexpected response: {data}")
    except Exception as e:
        log.warning(f"bscscan_get failed: {e}")
    return None


def get_wallet_token_transfers(address: str, offset: int = 10) -> list:
    return bscscan_get({
        "module": "account",
        "action": "tokentx",
        "address": address,
        "sort": "desc",
        "page": 1,
        "offset": offset,
    }) or []


def infer_tx_side(wallet_address: str, tx: dict) -> str:
    wallet = normalize_address(wallet_address)
    from_addr = normalize_address(tx.get("from", ""))
    to_addr = normalize_address(tx.get("to", ""))
    if to_addr == wallet and from_addr != wallet:
        return "buy"
    if from_addr == wallet and to_addr != wallet:
        return "sell"
    return "transfer"


def tx_token_amount(tx: dict) -> float:
    try:
        value = float(tx.get("value") or 0)
        decimals = int(tx.get("tokenDecimal") or 0)
        if decimals >= 0:
            return value / (10 ** decimals) if decimals <= 30 else 0.0
    except Exception:
        pass
    return 0.0


def approximate_tx_usd_value(tx: dict) -> float:
    amount = tx_token_amount(tx)
    token_symbol = str(tx.get("tokenSymbol") or "").upper()
    if token_symbol in {"USDT", "USDC", "BUSD", "DAI"}:
        return amount
    return 0.0


def get_smart_money_token_key(tx: dict) -> str:
    contract = normalize_address(tx.get("contractAddress", ""))
    symbol = str(tx.get("tokenSymbol") or "?").upper().strip()
    if contract.startswith("0x") and len(contract) == 42:
        return f"bsc:{contract}"
    return f"bsc:{symbol}"


def classify_cluster_strength(wallet_count: int, unique_wallet_count: int) -> tuple[str, int]:
    base_count = max(wallet_count, unique_wallet_count)
    if base_count >= 6:
        return "Very High", 90
    if base_count >= 4:
        return "High", 78
    if base_count >= 3:
        return "Medium", 66
    return "Low", 52


def build_smart_money_alert(wallet_label: str, wallet_address: str, tx: dict, cluster_info: Optional[dict] = None) -> Optional[str]:
    side = infer_tx_side(wallet_address, tx)
    if side == "transfer":
        return None

    token_symbol = escape_markdown(str(tx.get("tokenSymbol") or "?"), version=1)
    token_name = escape_markdown(str(tx.get("tokenName") or "?"), version=1)
    contract = str(tx.get("contractAddress") or "")
    amount = tx_token_amount(tx)
    approx_usd = approximate_tx_usd_value(tx)
    if approx_usd and approx_usd < SMART_MONEY_MIN_TOKEN_VALUE_USD:
        return None

    side_label = "🟢 BUY" if side == "buy" else "🔴 SELL"
    wallet_label_safe = escape_markdown(wallet_label, version=1)
    wallet_short = escape_markdown(wallet_address[:6] + "..." + wallet_address[-4:], version=1)
    amount_text = f"{amount:,.4f}".rstrip("0").rstrip(".") if amount else "N/A"
    usd_text = f"~${fmt_money(approx_usd)}" if approx_usd > 0 else "N/A"
    bscscan_link = f"https://bscscan.com/tx/{tx.get('hash','')}" if tx.get("hash") else ""
    token_link = f"https://dexscreener.com/bsc/{contract}" if contract else ""
    links = []
    if token_link:
        links.append(f"[Chart]({token_link})")
    if bscscan_link:
        links.append(f"[Tx]({bscscan_link})")
    links_line = " | ".join(links)
    links_line = f"\n\n{links_line}" if links_line else ""

    return (
        f"🐋 *Smart Money Alert*\n\n"
        f"Wallet: *{wallet_label_safe}* (`{wallet_short}`)\n"
        f"Action: *{side_label}*\n"
        f"Token: *{token_symbol}* — {token_name}\n"
        f"Amount: `{escape_markdown(amount_text, version=1)}`\n"
        f"Approx Value: `{escape_markdown(usd_text, version=1)}`\n"
        f"Chain: *BSC*"
        f"{links_line}"
    )


async def smart_money_check(token_key: str) -> dict:
    # kept for compatibility with older code paths
    return {
        "status": "bscscan_ready" if BSCSCAN_API_KEY else "disabled",
        "tracked_wallets": len(smart_wallets()),
        "confidence": 60 if BSCSCAN_API_KEY else 0,
    }


# ─────────────────────────────────────────────
# MANIPULATION DETECTION LAYER
# ─────────────────────────────────────────────

def detect_manipulation_signal(last_price: float, current_price: float, last_volume: float, current_volume: float, current_liquidity: float, current_buys: int, current_sells: int) -> Optional[dict]:
    if last_price <= 0 or last_volume <= 0:
        return None
    if current_liquidity < MANIPULATION_MIN_LIQUIDITY_USD or current_volume < MANIPULATION_MIN_VOLUME_USD:
        return None

    price_pct = ((current_price - last_price) / last_price) * 100 if last_price > 0 else 0.0
    volume_ratio = current_volume / last_volume if last_volume > 0 else 0.0
    buy_sell_ratio = current_buys / max(current_sells, 1)

    if price_pct < MANIPULATION_PRICE_SPIKE_PCT:
        return None
    if volume_ratio < MANIPULATION_VOLUME_SPIKE_RATIO:
        return None
    if buy_sell_ratio < MANIPULATION_BUY_SELL_RATIO:
        return None

    risk_score = 55
    risk_score += min(20, int((price_pct - MANIPULATION_PRICE_SPIKE_PCT) * 1.5))
    risk_score += min(15, int((volume_ratio - MANIPULATION_VOLUME_SPIKE_RATIO) * 5))
    if current_sells == 0:
        risk_score += 10
    risk_score = min(risk_score, 99)

    return {
        "price_pct": price_pct,
        "volume_ratio": volume_ratio,
        "buy_sell_ratio": buy_sell_ratio,
        "risk_score": risk_score,
    }


def build_manipulation_alert(symbol: str, chain_display: str, dex_url: str, signal: dict, last_price: float, current_price: float, last_volume: float, current_volume: float, current_buys: int, current_sells: int) -> str:
    footer = f"\n\n[View on Dexscreener]({dex_url})" if dex_url else ""
    return (
        f"⚠️ *Market Manipulation Warning — {symbol}*\n\n"
        f"🔗 *Chain:* {chain_display}\n"
        f"📈 *Price Spike:* +{signal['price_pct']:.1f}%  `${fmt_price(last_price)}` → `${fmt_price(current_price)}`\n"
        f"🔥 *Volume Spike:* {signal['volume_ratio']:.1f}x  `${fmt_money(last_volume)}` → `${fmt_money(current_volume)}`\n"
        f"🧪 *Buy Pressure:* {current_buys} buys vs {current_sells} sells\n"
        f"🎯 *Risk Score:* {signal['risk_score']}/100\n\n"
        f"*Interpretation:* sudden price and volume acceleration with concentrated buy pressure.\n"
        f"*Recommendation:* avoid chasing the move until the token stabilizes."
        f"{footer}"
    )


# ─────────────────────────────────────────────
# INTERNAL HELPER — send alert settings message
# ─────────────────────────────────────────────

async def _send_alert_settings(context, chat_id: int, token_key: str) -> bool:
    """
    Sends a fresh alert-settings message to the user via send_message().
    Returns True on success, False on failure.
    This is the single authoritative function for opening the alert prefs UI.
    Using send_message() (not edit) guarantees delivery even when the
    original callback message is no longer editable.
    """
    entry = db.get_tracked_token(chat_id, token_key)
    if not entry:
        log.warning(f"_send_alert_settings: token_key={token_key} not found for chat_id={chat_id}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Token not found in your tracked list.\n"
                    "It may have been removed. Use *My Tracked Tokens* to check."
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")],
                    [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
                ]),
            )
        except Exception as e:
            log.error(f"_send_alert_settings: could not send 'not found' message to {chat_id}: {e}")
        return False

    symbol_safe = escape_markdown(str(entry.get("symbol", "?")), version=1)
    chain_safe = escape_markdown(str(entry.get("chain", "?")).upper(), version=1)
    added_safe = escape_markdown(str(entry.get("added_at", "?")), version=1)

    text = (
        f"⚙️ *Alert Settings — {symbol_safe}*\n"
        f"Chain: {chain_safe}\n"
        f"Added: {added_safe}\n\n"
        "Toggle the alerts you want below:"
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=alert_prefs_menu(chat_id, token_key),
        )
        return True
    except Exception as e:
        log.error(f"_send_alert_settings: send_message failed for {chat_id}: {e}")
        return False


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.get_user(cid)
    db.touch_user(cid)
    db.save()

    text = (
        "🚀 *Quantara — Crypto Decision Engine*\n\n"
        "Built to sell *decisions*, not raw data.\n\n"
        "*Free Layer*\n"
        "• 📊 Prices / Search\n"
        "• 🔔 Basic Alerts\n"
        "• 📦 My Tokens\n\n"
        "*Premium Layer*\n"
        "• 🐋 Smart Money\n"
        "• ⚠️ Manipulation Detection\n"
        "• 📡 Real-time Signals\n"
        "• 🧬 Alpha Lab\n"
        "• ⚙️ Custom Filters\n\n"
        "Choose an option below:"
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
        "*Alert Modes:*\n"
        "🔥 *New Token Alerts* — optional broadcast of fresh tokens that pass the filter.\n"
        "🐋 *Smart Money Alerts* — optional alerts from tracked smart-money wallets on BSC, including smart-wallet count and cluster strength.\n"
        "⚠️ *Manipulation Alerts* — optional warnings for tracked tokens showing pump-style conditions.\n"
        "✅ You can enable any one of them *or all three together* from the main menu.\n\n"
        f"🆓 Smart Money and Manipulation are included during your first *{TRIAL_DAYS}-day trial*. After that, premium payment is required.\n\n"
        "⚠️ Signal scores are filters, not financial advice."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.touch_user(cid)

    new_tokens_on = "Enabled ✅" if db.token_alerts_enabled(cid) else "Disabled ❌"
    smart_money_on = "Enabled ✅" if db.smart_money_alerts_enabled(cid) else "Disabled ❌"
    manipulation_on = "Enabled ✅" if db.manipulation_alerts_enabled(cid) else "Disabled ❌"
    tracked_count = len(db.get_tracked(cid))

    manipulation_note = "\nℹ️ Manipulation alerts need at least one tracked token." if db.manipulation_alerts_enabled(cid) and tracked_count == 0 else ""

    text = (
        "📊 *Your Status*\n\n"
        f"🔥 New Token Alerts: {new_tokens_on}\n"
        f"🐋 Smart Money Alerts: {smart_money_on}\n"
        f"⚠️ Manipulation Alerts: {manipulation_on}\n"
        f"📌 Tokens Tracked: {tracked_count}{manipulation_note}\n\n"
        f"*Bot Filters*\n"
        f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
        f"💧 Min Liquidity: ${MIN_LIQUIDITY:,}\n"
        f"📊 Min 24h Volume: ${MIN_VOLUME:,}\n"
        f"⏱ New Token Last Check: {db.last_check_time}\n"
        f"⏱ Smart Money Last Check: {db.smart_money_last_check_time}\n"
        f"⏱ Manipulation Last Check: {db.manipulation_last_check_time}"
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))


async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    if cid != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    global_subs = len(db.token_alert_subscribers())
    smart_money_subs = len(db.smart_money_subscribers())
    manipulation_subs = len(db.manipulation_subscribers())
    total_users = len(db.users)
    blocked_count = sum(1 for u in db.users.values() if u.get("blocked"))
    tracked_total = len(db.tracked_tokens)
    premium_active = db.premium_active_count()

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
        f"🔥 New Token Alert Subscribers: {global_subs}\n"
        f"🐋 Smart Money Subscribers: {smart_money_subs}\n"
        f"⚠️ Manipulation Alert Subscribers: {manipulation_subs}\n"
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


async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.set_state(cid, "awaiting_feedback")
    db.touch_user(cid)
    db.save()
    await update.message.reply_text(
        "💬 Send your message for the developer.\n\n"
        "You can send:\n"
        "- bug reports\n"
        "- feature requests\n"
        "- feedback\n\n"
        "Type your message in the next reply."
    )


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /reply <chat_id> <message>")
        return

    target_raw = context.args[0].strip()
    if not target_raw.isdigit():
        await update.message.reply_text("Invalid chat_id.")
        return

    target_chat_id = int(target_raw)
    reply_text = " ".join(context.args[1:]).strip()
    if not reply_text:
        await update.message.reply_text("Message cannot be empty.")
        return

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"💬 *Developer Reply*\n\n{escape_markdown(reply_text, version=1)}",
            parse_mode="Markdown",
        )
        await update.message.reply_text("✅ Reply sent.")
    except Exception as e:
        log.warning(f"reply_command failed for {target_chat_id}: {e}")
        await update.message.reply_text(f"❌ Failed to send reply: {e}")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Chat ID: `{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


async def activatepaid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /activatepaid <chat_id> <days>")
        return

    try:
        target_chat_id = int(context.args[0])
        days = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid chat_id or days.")
        return

    user = db.get_user(target_chat_id)
    user["is_paid"] = True
    user["paid_until"] = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    user["subscription_plan"] = f"manual_{days}d"
    user["subscription_tier"] = "pro"
    user["payment_method"] = "usdt_manual"
    db.save()

    await update.message.reply_text("✅ Premium activated manually.")
    try:
        await context.bot.send_message(
            target_chat_id,
            f"✅ *Payment confirmed*\n\nPremium access is now active for *{days} days*.",
            parse_mode="Markdown",
            reply_markup=main_menu_for(target_chat_id),
        )
    except Exception:
        pass


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await search_command(update, context)


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.touch_user(cid)
    await update.message.reply_text(
        build_subscription_hub(cid),
        parse_mode="Markdown",
        reply_markup=payment_options_menu(),
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    db.set_token_alerts(cid, False)
    db.touch_user(cid)
    db.save()
    await update.message.reply_text(
        "🔥 New token alerts disabled. Smart Money and Manipulation modes stay exactly as you set them.",
        reply_markup=main_menu_for(cid),
    )


# ─────────────────────────────────────────────
# MESSAGE HANDLER — search flow
# ─────────────────────────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    state = db.get_state(cid)

    if state == "awaiting_feedback":
        feedback_text = (update.message.text or "").strip()
        if not feedback_text:
            await update.message.reply_text("Please send a non-empty message.")
            return

        db.set_state(cid, "idle")
        db.touch_user(cid)
        db.save()

        user = update.effective_user
        username = f"@{user.username}" if user and user.username else "No username"
        full_name = user.full_name if user else "Unknown user"

        owner_message = (
            "💬 *User Feedback / Support Message*\n\n"
            f"*From:* {escape_markdown(full_name, version=1)}\n"
            f"*Username:* {escape_markdown(username, version=1)}\n"
            f"*Chat ID:* `{cid}`\n\n"
            f"*Message:*\n{escape_markdown(feedback_text, version=1)}\n\n"
            "Reply with:\n"
            f"`/reply {cid} your message`"
        )

        delivery_ok = True
        try:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=owner_message,
                parse_mode="Markdown",
            )
        except Exception as e:
            delivery_ok = False
            log.warning(f"Failed to forward user feedback from {cid}: {e}")

        if delivery_ok:
            await update.message.reply_text(
                "✅ Your message was sent to the developer.\n\n"
                "You should receive a reply here if needed.",
                reply_markup=main_menu_for(cid),
            )
        else:
            await update.message.reply_text(
                "❌ Failed to send your message right now. Please try again later.",
                reply_markup=main_menu_for(cid),
            )
        return

    if state != "awaiting_search":
        await update.message.reply_text(
            "Use the menu or /search to look up a token, or use Contact Developer if you want support.",
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
    context.user_data["last_pair"] = best
    await update.message.reply_text(scan_text, parse_mode="Markdown", disable_web_page_preview=True)
    await update.message.reply_text(build_alpha_summary(best, premium=feature_allowed(cid, "alpha_full")), parse_mode="Markdown")

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
            f"📌 Do you want to add *{escape_markdown(symbol, version=1)}* to your tracked list?",
            parse_mode="Markdown",
            reply_markup=track_prompt_menu(token_key),
        )
    else:
        await update.message.reply_text(
            f"✅ You're already tracking *{escape_markdown(symbol, version=1)}*.\n\nManage alerts from *My Tracked Tokens*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
            ]),
        )


# ─────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cid = query.message.chat_id
    data = query.data
    db.touch_user(cid)

    # ── STEP 1: Always answer the callback query immediately ───────────────
    # This MUST happen before any other logic to prevent "button loading"
    # spinner from freezing in the Telegram client.
    # Never skip this — even if we're about to return early.
    try:
        await query.answer()
    except Exception as e:
        # answer() can fail if the callback is too old (>10 min).
        # Log and continue — we still want to deliver a UI response.
        log.warning(f"button_handler: query.answer() failed for data={data!r}: {e}")

    # ── STEP 2: Route to the correct handler ──────────────────────────────

    if data == "search_prompt":
        db.set_state(cid, "awaiting_search")
        try:
            await query.edit_message_text("🔍 Send a token name, symbol, or contract address:")
        except Exception:
            await context.bot.send_message(cid, "🔍 Send a token name, symbol, or contract address:")
        return

    if data == "alert_mode_menu":
        text = "⏱ *Alert Speed*\n\n⚡ *Fast Alerts* — best for active traders\n📊 *Normal* — balanced default\n📈 *Long-term* — low-noise monitoring\n\nChoose the mode that fits your style."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=alert_mode_menu(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=alert_mode_menu(cid))
        return

    if data in {"mode_fast", "mode_normal", "mode_long"}:
        mode = data.split("_", 1)[1]
        if mode == "fast" and not feature_allowed(cid, "fast_mode"):
            await context.bot.send_message(cid, "⚡ *Fast Alerts* is a paid feature in Trader and above.", parse_mode="Markdown", reply_markup=premium_gate_menu())
            return
        db.get_user(cid)["alert_mode"] = mode
        db.save()
        text = f"✅ Alert mode set to *{alert_mode_label(mode)}*\n\nCurrent cadence target: ~{alert_check_interval_seconds(cid)//60} minutes."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "alpha_lab":
        last_pair = context.user_data.get("last_pair")
        if not last_pair:
            await context.bot.send_message(cid, "🧬 Alpha Lab needs a recent token scan first. Use *Prices / Search* and scan a token.", parse_mode="Markdown", reply_markup=main_menu_for(cid))
            return
        premium = feature_allowed(cid, "alpha_full")
        text = build_alpha_summary(last_pair, premium=premium)
        if not premium:
            text += "\n\n🔒 Full Alpha Breakdown is available in *Pro Alpha* and above."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=(premium_gate_menu() if not premium else main_menu_for(cid)))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=(premium_gate_menu() if not premium else main_menu_for(cid)))
        return

    if data == "custom_filters":
        if not feature_allowed(cid, "custom_filters"):
            await context.bot.send_message(cid, "⚙️ *Custom Filters* are reserved for *Elite*.", parse_mode="Markdown", reply_markup=premium_gate_menu())
            return
        db.get_user(cid)["custom_filters"] = True
        db.save()
        text = "⚙️ *Custom Filters*\n\nElite mode unlocked. In the next phase we can wire bespoke liquidity / volume / signal thresholds for your workflow."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "subscribe_info":
        text = build_subscription_hub(cid) + "\n\n" + premium_plan_card("trader") + "\n\n" + premium_plan_card("pro") + "\n\n" + premium_plan_card("elite")
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=payment_options_menu())
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=payment_options_menu())
        return

    if data == "subscribe_usdt":
        text = build_subscription_hub(cid) + "\n\n*USDT settlement*\n" + "\n".join(f"• {PLAN_CATALOG[k]['label']}: {PLAN_CATALOG[k]['usdt']} USDT / {PLAN_CATALOG[k]['days']} days" for k in ["trader", "pro", "elite"]) + f"\n\nNetwork: {PAYMENT_NETWORK}\nAddress: `{PAYMENT_WALLET}`\n\nAfter payment, send proof to the developer for activation."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=payment_options_menu())
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=payment_options_menu())
        return

    if data in {"plan_trader", "plan_pro", "plan_elite"}:
        plan_key = data.split("_", 1)[1]
        plan = PLAN_CATALOG[plan_key]
        context.user_data["pending_plan"] = plan_key
        prices = [LabeledPrice(f"{plan['label']} - {plan['days']} days", plan["stars"])]
        await context.bot.send_invoice(chat_id=cid, title=f"Quantara {plan['label']}", description=plan["headline"], payload=f"{plan_key}_{cid}_{int(datetime.now().timestamp())}", currency="XTR", prices=prices)
        return

    if data == "token_alerts_on":
        db.set_token_alerts(cid, True)
        db.save()
        text = (
            "🔥 *New Token Alerts Enabled*\n\n"
            "You'll receive alerts when strong new tokens pass the filter.\n"
            "🐋 Smart Money Alerts and ⚠️ Manipulation Alerts remain unchanged, so you can run any one mode or all three together."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "token_alerts_off":
        db.set_token_alerts(cid, False)
        db.save()
        text = (
            "🔥 *New Token Alerts Disabled*\n\n"
            "You won't receive alerts about strong new tokens.\n"
            "🐋 Smart Money Alerts and your tracked tokens are unaffected."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "smart_money_on":
        if not has_premium_access(cid):
            text = build_payment_message()
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=payment_options_menu())
            return
        db.set_smart_money_alerts(cid, True)
        db.save()
        text = (
            "🐋 *Smart Money Alerts Enabled*\n\n"
            "You'll receive alerts when the configured smart-money wallets buy or sell on BSC.\n"
            "🔥 New Token Alerts and ⚠️ Manipulation Alerts remain unchanged, so you can run any one mode or all three together."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "smart_money_off":
        db.set_smart_money_alerts(cid, False)
        db.save()
        text = (
            "🐋 *Smart Money Alerts Disabled*\n\n"
            "You won't receive smart-money wallet alerts.\n"
            "🔥 New Token Alerts and your tracked tokens are unaffected."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "manipulation_on":
        if not has_premium_access(cid):
            text = build_payment_message()
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=payment_options_menu())
            return
        db.set_manipulation_alerts(cid, True)
        db.save()
        text = (
            "⚠️ *Manipulation Alerts Enabled*\n\n"
            "You'll receive warnings when a tracked token shows sudden price/volume spikes and pump-style behavior.\n"
            "🔥 New Token Alerts and 🐋 Smart Money Alerts remain unchanged."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "manipulation_off":
        db.set_manipulation_alerts(cid, False)
        db.save()
        text = (
            "⚠️ *Manipulation Alerts Disabled*\n\n"
            "You won't receive pump-and-dump style warning alerts.\n"
            "🔥 New Token Alerts and 🐋 Smart Money Alerts remain unchanged."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "status":
        new_tokens_on = "Enabled ✅" if db.token_alerts_enabled(cid) else "Disabled ❌"
        smart_money_on = "Enabled ✅" if db.smart_money_alerts_enabled(cid) else "Disabled ❌"
        manipulation_on = "Enabled ✅" if db.manipulation_alerts_enabled(cid) else "Disabled ❌"
        tracked_count = len(db.get_tracked(cid))
        manipulation_note = "\nℹ️ Manipulation alerts need at least one tracked token." if db.manipulation_alerts_enabled(cid) and tracked_count == 0 else ""
        text = (
            "📊 *Your Status*\n\n"
            f"🔥 New Token Alerts: {new_tokens_on}\n"
            f"🐋 Smart Money Alerts: {smart_money_on}\n"
            f"⚠️ Manipulation Alerts: {manipulation_on}\n"
            f"📌 Tokens Tracked: {tracked_count}{manipulation_note}\n\n"
            f"*Bot Filters*\n"
            f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
            f"💧 Min Liquidity: ${MIN_LIQUIDITY:,}\n"
            f"📊 Min 24h Volume: ${MIN_VOLUME:,}\n"
            f"⏱ New Token Last Check: {db.last_check_time}\n"
            f"⏱ Smart Money Last Check: {db.smart_money_last_check_time}\n"
            f"⏱ Manipulation Last Check: {db.manipulation_last_check_time}"
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "help":
        text = (
            "❓ *Help*\n\n"
            "Use the *Search Token* button or `/search` to look up any token.\n"
            "After searching, you can choose to track it and set alert preferences.\n\n"
            "*New Token Alerts* — optional broadcast of new tokens that pass the filter.\n"
            "*Smart Money Alerts* — optional alerts from tracked smart-money wallets on BSC, including smart-wallet count and cluster strength.\n"
            "*Manipulation Alerts* — warnings for tracked tokens showing pump-style conditions.\n"
            "✅ You can enable any one of them or run all modes together from the main menu.\n"
            "*Tracked Tokens* — your personal watchlist with custom alert settings."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "contact_prompt":
        db.set_state(cid, "awaiting_feedback")
        db.save()
        text = (
            "💬 *Contact Developer*\n\nSend your message in the next reply.\n\n"
            "Use this for feedback, bug reports, or feature requests."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown")
        return

    if data == "back_main":
        text = "🚀 *Only Signals — V2*\nChoose an option below:"
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    if data == "my_tokens":
        tracked = db.get_tracked(cid)
        if not tracked:
            text = "📋 You have no tracked tokens.\n\nSearch a token to start tracking."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Token", callback_data="search_prompt")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
            ])
        else:
            text = "📋 *Your Tracked Tokens*\nTap a token to manage its alert settings."
            kb = my_tokens_menu(cid)
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=kb)
        return

    # ── token_detail — FIX ─────────────────────────────────────────────────
    # ROOT CAUSE (old code): the entire handler was wrapped in a single try
    # block. If edit_message_text failed, execution jumped to the except block
    # which also used send_message — BUT that except was catching the answer()
    # call above AND the log.info, so send_message was never guaranteed to run.
    # In some Telegram client versions, the inline button message is not
    # editable (e.g. it's a photo, or the message is too old), causing a
    # silent failure with no visible response.
    #
    # FIX: query.answer() is already done above (unconditionally).
    # _send_alert_settings() uses send_message() as the PRIMARY delivery path.
    # edit_message_text() is attempted AFTER as a best-effort cosmetic cleanup
    # (to remove the "tap a token" list message), isolated in its own try block
    # that CANNOT affect the main response delivery.
    if data.startswith("token_detail|"):
        token_key = data.split("|", 1)[1]
        log.info(f"token_detail: cid={cid}, token_key={token_key!r}")
        entry = db.get_tracked_token(cid, token_key)
        if not entry:
            await context.bot.send_message(
                chat_id=cid,
                text="⚠️ Token not found in your tracked list.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")],
                    [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
                ]),
            )
            return

        symbol_safe = escape_markdown(str(entry.get("symbol", "?")), version=1)
        chain_safe = escape_markdown(str(entry.get("chain", "?")).upper(), version=1)
        text = (
            f"📌 *{symbol_safe}* — {chain_safe}\n\n"
            "Do you want to delete this token from your tracked list?\n\n"
            "You can also open alert settings before deciding."
        )

        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=token_delete_confirm_menu(token_key))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=token_delete_confirm_menu(token_key))
        return

    if data.startswith("manage_alerts:"):
        token_key = data.split(":", 1)[1]
        delivered = await _send_alert_settings(context, cid, token_key)
        if delivered:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                log.debug(f"manage_alerts: could not remove previous keyboard (non-fatal): {e}")
        return

    if data.startswith("track_remove_confirm:"):
        token_key = data.split(":", 1)[1]
        entry = db.get_tracked_token(cid, token_key)
        symbol_safe = escape_markdown(str((entry or {}).get("symbol", token_key)), version=1)
        text = (
            f"🗑 *Delete {symbol_safe}?*\n\n"
            "Press *Yes* to remove it from your tracked list.\n"
            "Press *No* to return to the main menu."
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=token_delete_confirm_menu(token_key))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=token_delete_confirm_menu(token_key))
        return

    # ── track_add ──────────────────────────────────────────────────────────
    if data.startswith("track_add:"):
        token_key = data.split(":", 1)[1]
        pending = context.user_data.get("pending_track") or {}
        symbol = pending.get("symbol", token_key)
        name = pending.get("name", symbol)
        chain = pending.get("chain", parse_token_key(token_key)[0] or "bsc")
        if db.get_tracked_token(cid, token_key):
            text = f"✅ *{escape_markdown(symbol, version=1)}* is already in your tracked list."
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=tracked_token_action_menu(token_key))
            except Exception:
                await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=tracked_token_action_menu(token_key))
            return
        if len(db.get_tracked(cid)) >= tracked_token_limit_for(cid):
            text = f"⛔ Tracking limit reached. Your current tier allows *{tracked_token_limit_for(cid)}* tracked tokens."
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=premium_gate_menu())
            return
        db.track_token(cid, token_key, symbol, name, chain)
        db.save()
        context.user_data.pop("pending_track", None)
        text = f"📦 *{escape_markdown(symbol, version=1)}* added to your tracked list.\n\nManage alerts below."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=tracked_token_action_menu(token_key))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=tracked_token_action_menu(token_key))
        return

    # ── track_skip ─────────────────────────────────────────────────────────
    if data.startswith("track_skip:"):
        token_key = data.split(":", 1)[1]
        entry = db.get_tracked_token(cid, token_key)

        if entry:
            safe_symbol = escape_markdown(entry.get("symbol", token_key), version=1)
            text = (
                f"ℹ️ *{safe_symbol}* is already in your tracked list.\n\n"
                "Manage alerts from *My Tracked Tokens*, or remove it below:"
            )
            try:
                await query.edit_message_text(text, parse_mode="Markdown",
                                               reply_markup=tracked_token_action_menu(token_key))
            except Exception:
                await context.bot.send_message(cid, text, parse_mode="Markdown",
                                                reply_markup=tracked_token_action_menu(token_key))
        else:
            context.user_data.pop("pending_track", None)
            text = "👍 No problem. This token was not added to your tracked list."
            try:
                await query.edit_message_text(text, reply_markup=main_menu_for(cid))
            except Exception:
                await context.bot.send_message(cid, text, reply_markup=main_menu_for(cid))
        return

    # ── track_remove ───────────────────────────────────────────────────────
    if data.startswith("track_remove:"):
        token_key = data.split(":", 1)[1]
        entry = db.get_tracked_token(cid, token_key)
        symbol = entry["symbol"] if entry else token_key
        db.untrack_token(cid, token_key)
        db.save()
        context.user_data.pop("pending_track", None)
        text = f"🗑 *{escape_markdown(symbol, version=1)}* was removed from your tracked list."
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        except Exception:
            await context.bot.send_message(cid, text, parse_mode="Markdown", reply_markup=main_menu_for(cid))
        return

    # ── pref toggle — FIX ──────────────────────────────────────────────────
    # ROOT CAUSE (old code): callback_data is "pref|<pref_name>|<token_key>".
    # token_key itself contains a colon (e.g. "bsc:0xABC..."), so splitting on
    # "|" with maxsplit=2 is safe — but the old code called query.edit_message_text
    # then ALSO tried to send via context.bot.send_message in a fragile nested
    # try/except where an early exception could prevent any message from sending.
    #
    # FIX: parse callback_data cleanly (maxsplit=2 on "|"), call answer() first
    # (already done above), use send_message() as the primary UI response,
    # and wrap the optional edit attempt in its own isolated try block.
    if data.startswith("pref|"):
        parts = data.split("|", 2)   # ["pref", "<pref_name>", "<token_key>"]

        if len(parts) != 3:
            log.warning(f"pref: malformed callback_data={data!r} from cid={cid}")
            await context.bot.send_message(
                chat_id=cid,
                text="⚠️ Something went wrong with that button. Please try again from My Tracked Tokens.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")]
                ]),
            )
            return

        _, pref, token_key = parts
        log.info(f"pref toggle: cid={cid}, pref={pref!r}, token_key={token_key!r}")

        entry = db.get_tracked_token(cid, token_key)
        if not entry:
            log.warning(f"pref: token_key={token_key!r} not found for cid={cid}")
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    "⚠️ Token not found in your tracked list.\n"
                    "It may have been removed."
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tracked Tokens", callback_data="my_tokens")],
                    [InlineKeyboardButton("⬅️ Main Menu", callback_data="back_main")],
                ]),
            )
            return

        # Toggle the preference
        current_val = entry["alerts"].get(pref, False)
        db.set_alert_pref(cid, token_key, pref, not current_val)
        db.save()

        refreshed = db.get_tracked_token(cid, token_key)
        symbol_safe = escape_markdown(str(refreshed.get("symbol", "?")), version=1)
        chain_safe = escape_markdown(str(refreshed.get("chain", "?")).upper(), version=1)

        settings_text = (
            f"⚙️ *Alert Settings — {symbol_safe}*\n"
            f"Chain: {chain_safe}\n\n"
            "Toggle the alerts you want below:"
        )

        # Primary: always send a fresh message — no dependency on editability
        await context.bot.send_message(
            chat_id=cid,
            text=settings_text,
            parse_mode="Markdown",
            reply_markup=alert_prefs_menu(cid, token_key),
        )

        # Best-effort: remove the keyboard from the previous settings message
        # so the chat doesn't accumulate stale keyboards. Isolated — cannot
        # affect the delivery above.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug(f"pref: edit_message_reply_markup non-fatal: {e}")
        return

    # ── Catch-all for any unrecognised callback_data ───────────────────────
    log.warning(f"button_handler: unrecognised callback data={data!r} from cid={cid}")
    try:
        await query.edit_message_text("Unknown option.", reply_markup=main_menu_for(cid))
    except Exception:
        await context.bot.send_message(cid, "Unknown option.", reply_markup=main_menu_for(cid))


# ─────────────────────────────────────────────
# BACKGROUND JOB — new token alerts
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
    subscribers = db.token_alert_subscribers()

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
                        db.get_user(cid)["token_alerts"] = False

    db.save()


# ─────────────────────────────────────────────
# BACKGROUND JOB — tracked token alerts
# ─────────────────────────────────────────────

async def check_tracked_tokens(context: ContextTypes.DEFAULT_TYPE):
    """
    Checks all user-tracked tokens for material changes and sends alerts
    according to each user's token-specific preferences.
    """
    log.info("check_tracked_tokens STARTED")
    db.manipulation_last_check_time = _now()
    if not db.tracked_tokens:
        log.info("check_tracked_tokens: no tracked tokens.")
        return

    token_to_record_keys: dict[str, list[str]] = {}

    for record_key, record in db.tracked_tokens.items():
        token_key = record.get("token_key", "")
        if not can_query_token_pairs(token_key):
            log.debug(f"check_tracked_tokens: skipping non-address token_key {token_key}")
            continue
        token_to_record_keys.setdefault(token_key, []).append(record_key)

    if not token_to_record_keys:
        log.info("check_tracked_tokens: no trackable address-based tokens.")
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
                        f"{direction} *Price:* {sign}{price_pct:.1f}%"
                        f"  `${fmt_price(last_price)}` → `${fmt_price(current_price)}`"
                    )

            if prefs.get("volume_spike") and last_volume > 0:
                volume_ratio = current_volume / last_volume if last_volume > 0 else 0
                if volume_ratio > VOLUME_SPIKE_RATIO:
                    alert_lines.append(
                        f"🔥 *Volume spike:* {volume_ratio:.1f}x"
                        f"  `${fmt_money(last_volume)}` → `${fmt_money(current_volume)}`"
                    )

            if prefs.get("liquidity_change") and last_liquidity > 0:
                liquidity_pct = ((current_liquidity - last_liquidity) / last_liquidity) * 100
                if abs(liquidity_pct) > LIQUIDITY_CHANGE_ALERT_PCT:
                    direction = "⬆️" if liquidity_pct > 0 else "⬇️"
                    sign = "+" if liquidity_pct > 0 else ""
                    alert_lines.append(
                        f"{direction} *Liquidity:* {sign}{liquidity_pct:.1f}%"
                        f"  `${fmt_money(last_liquidity)}` → `${fmt_money(current_liquidity)}`"
                    )

            if prefs.get("unusual_activity") and (current_buys > 0 or current_sells > 0):
                if current_buys >= 5 * max(current_sells, 1) and current_volume >= 10_000:
                    alert_lines.append(
                        f"🧪 *Unusual activity:* buy pressure detected"
                        f" ({current_buys} buys vs {current_sells} sells)"
                    )
                elif current_sells >= 5 * max(current_buys, 1) and current_volume >= 10_000:
                    alert_lines.append(
                        f"🧪 *Unusual activity:* sell pressure detected"
                        f" ({current_sells} sells vs {current_buys} buys)"
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

            if db.manipulation_alerts_enabled(chat_id):
                signal = detect_manipulation_signal(
                    last_price=last_price,
                    current_price=current_price,
                    last_volume=last_volume,
                    current_volume=current_volume,
                    current_liquidity=current_liquidity,
                    current_buys=current_buys,
                    current_sells=current_sells,
                )
                if signal:
                    message = build_manipulation_alert(
                        symbol=symbol,
                        chain_display=chain_display,
                        dex_url=dex_url,
                        signal=signal,
                        last_price=last_price,
                        current_price=current_price,
                        last_volume=last_volume,
                        current_volume=current_volume,
                        current_buys=current_buys,
                        current_sells=current_sells,
                    )
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
                        log.warning(f"check_tracked_tokens: failed to send manipulation alert to {chat_id}: {e}")
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
# BACKGROUND JOB — smart money alerts
# ─────────────────────────────────────────────

async def check_smart_money(context: ContextTypes.DEFAULT_TYPE):
    db.smart_money_last_check_time = _now()

    subscribers = db.smart_money_subscribers()
    wallets = smart_wallets()
    if not subscribers or not wallets or not BSCSCAN_API_KEY:
        db.save()
        return

    alerts = []

    for wallet in wallets:
        wallet_addr = wallet["address"]
        wallet_label = wallet["label"]
        seen_hashes = set(db.smart_money_seen_hashes.get(wallet_addr, []))
        txs = get_wallet_token_transfers(wallet_addr, offset=10)
        if txs is None:
            continue

        fresh_seen = set(seen_hashes)
        wallet_alerts = []
        for tx in txs:
            tx_hash = str(tx.get("hash") or "").lower()
            if not tx_hash:
                continue
            if tx_hash in seen_hashes:
                continue
            fresh_seen.add(tx_hash)
            message = build_smart_money_alert(wallet_label, wallet_addr, tx)
            if message:
                wallet_alerts.append((int(tx.get("timeStamp") or 0), message))

        wallet_alerts.sort(key=lambda x: x[0])
        alerts.extend([msg for _, msg in wallet_alerts])
        db.smart_money_seen_hashes[wallet_addr] = list(fresh_seen)[-200:]

    for msg in alerts[:10]:
        for cid in subscribers:
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                err = str(e).lower()
                log.warning(f"check_smart_money: failed to send alert to {cid}: {e}")
                if "blocked" in err or "chat not found" in err:
                    user = db.get_user(cid)
                    user["blocked"] = True
                    user["smart_money_alerts"] = False

    db.save()


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except Exception as e:
        log.warning(f"precheckout_callback failed: {e}")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.successful_payment:
        return

    cid = message.chat_id
    user = db.get_user(cid)
    user["is_paid"] = True
    user["paid_until"] = (datetime.now() + timedelta(days=PREMIUM_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    user["subscription_plan"] = f"premium_{PREMIUM_DAYS}d"
    user["payment_method"] = "telegram_stars"
    db.save()

    await message.reply_text(
        f"✅ *Payment received*\n\nPremium access is now active for *{PREMIUM_DAYS} days*.\n\nYou can now enable 🐋 Smart Money Alerts and ⚠️ Manipulation Alerts.",
        parse_mode="Markdown",
        reply_markup=main_menu_for(cid),
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
app.add_handler(CommandHandler("contact", contact_command))
app.add_handler(CommandHandler("reply", reply_command))
app.add_handler(CommandHandler("myid", myid_command))
app.add_handler(CommandHandler("activatepaid", activatepaid_command))
app.add_handler(CommandHandler("analyze", analyze_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

try:
    if app.job_queue:
        app.job_queue.run_repeating(check_new, interval=CHECK_INTERVAL, first=10)
        app.job_queue.run_repeating(check_tracked_tokens, interval=300, first=30)
        app.job_queue.run_repeating(check_smart_money, interval=SMART_MONEY_CHECK_INTERVAL, first=45)
        log.info("Job queue started.")
    else:
        log.warning("Job queue NOT available — background jobs DISABLED")
except Exception as e:
    log.warning(f"Could not start job queue: {e}")

log.info("=== DEPLOY MARKER V6-UI-PREMIUM-ALPHA ===")
log.info("Quantara UI premium alpha running...")
app.run_polling()
