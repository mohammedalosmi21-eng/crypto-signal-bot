from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import requests
from datetime import datetime, timedelta
import json
import os

TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = 760930914

CHAIN_FILTER = "bsc"
MIN_LIQUIDITY = 10000
MIN_VOLUME = 5000
CHECK_INTERVAL = 180
MAX_ALERTS_PER_CYCLE = 1
DATA_FILE = "bot_data.json"

subscribers = set()
seen_tokens = set()
waiting_for_analysis = set()
blocked_users = set()
user_activity = {}
analyze_count = 0
last_check_time = "Not started yet"
last_alert_message = "No alerts yet"
search_history = []
search_counts = {}


def load_data():
    global analyze_count, last_alert_message, search_history, search_counts

    if not os.path.exists(DATA_FILE):
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        subscribers.update(data.get("subscribers", []))
        seen_tokens.update(data.get("seen_tokens", []))
        blocked_users.update(data.get("blocked_users", []))
        user_activity.update(data.get("user_activity", {}))
        analyze_count = data.get("analyze_count", 0)
        last_alert_message = data.get("last_alert_message", "No alerts yet")
        search_history = data.get("search_history", [])
        search_counts = data.get("search_counts", {})
    except Exception as e:
        print("Error loading data:", e)


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "subscribers": list(subscribers),
                "seen_tokens": list(seen_tokens),
                "blocked_users": list(blocked_users),
                "user_activity": user_activity,
                "analyze_count": analyze_count,
                "last_alert_message": last_alert_message,
                "search_history": search_history,
                "search_counts": search_counts,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Error saving data:", e)


def track_user(chat_id):
    user_activity[str(chat_id)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data()


def get_active_users(days=1):
    now = datetime.now()
    count = 0
    for ts in user_activity.values():
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if now - dt <= timedelta(days=days):
                count += 1
        except Exception:
            pass
    return count


def normalize_search_term(term: str) -> str:
    return term.strip().lower()


def record_search(chat_id, query):
    global search_history, search_counts

    cleaned = normalize_search_term(query)
    if not cleaned:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    search_history.append({
        "chat_id": str(chat_id),
        "query": cleaned,
        "timestamp": now_str
    })

    search_counts[cleaned] = search_counts.get(cleaned, 0) + 1

    if len(search_history) > 5000:
        search_history = search_history[-5000:]

    save_data()


def get_top_searches(limit=5):
    items = sorted(search_counts.items(), key=lambda x: x[1], reverse=True)
    return items[:limit]


def get_top_searches_recent(days=1, limit=5):
    now = datetime.now()
    recent_counts = {}

    for item in search_history:
        try:
            dt = datetime.strptime(item["timestamp"], "%Y-%m-%d %H:%M:%S")
            if now - dt <= timedelta(days=days):
                q = item["query"]
                recent_counts[q] = recent_counts.get(q, 0) + 1
        except Exception:
            continue

    items = sorted(recent_counts.items(), key=lambda x: x[1], reverse=True)
    return items[:limit]


def main_menu():
    keyboard = [
        [InlineKeyboardButton("✅ Enable Alerts", callback_data="alerts_on")],
        [InlineKeyboardButton("⛔ Disable Alerts", callback_data="alerts_off")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("🧠 Analyze Token", callback_data="analyze_prompt")],
        [InlineKeyboardButton("🧪 Last Alert", callback_data="last_alert")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def fmt_money(v):
    try:
        v = float(v)
    except Exception:
        return "N/A"

    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


def get_latest_profiles():
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print("Error fetching latest profiles:", e)
        return []


def search_pairs(q):
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": q},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data.get("pairs", []) if isinstance(data, dict) else []
    except Exception as e:
        print("Error searching pairs:", e)
        return []


def get_token_pairs(chain, addr):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/token-pairs/v1/{chain}/{addr}",
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error fetching token pairs for {chain}/{addr}:", e)
        return []


def choose_best_pair(pairs):
    if not pairs:
        return None
    return max(
        pairs,
        key=lambda p: ((p.get("liquidity", {}) or {}).get("usd", 0) or 0)
    )


def analyze_pair(pair):
    liquidity = (pair.get("liquidity", {}) or {}).get("usd", 0) or 0
    volume = (pair.get("volume", {}) or {}).get("h24", 0) or 0
    buys = ((pair.get("txns", {}) or {}).get("h24", {}) or {}).get("buys", 0) or 0
    sells = ((pair.get("txns", {}) or {}).get("h24", {}) or {}).get("sells", 0) or 0

    score = 0
    notes = []

    if liquidity > 100000:
        score += 3
        notes.append("very strong liquidity")
    elif liquidity > 50000:
        score += 2
        notes.append("strong liquidity")
    elif liquidity > 10000:
        score += 1
        notes.append("acceptable liquidity")
    else:
        notes.append("weak liquidity")

    if volume > 100000:
        score += 3
        notes.append("very strong volume")
    elif volume > 50000:
        score += 2
        notes.append("good volume")
    elif volume > 10000:
        score += 1
        notes.append("moderate volume")
    else:
        notes.append("weak volume")

    if buys > sells:
        score += 1
        notes.append("buy pressure detected")
    elif sells > buys * 2 and sells > 20:
        notes.append("high sell pressure")

    score_percent = min(score * 12, 100)

    if score >= 7:
        verdict = "🟢 Strong"
    elif score >= 4:
        verdict = "🟡 Medium"
    else:
        verdict = "🔴 Risk"

    return verdict, notes, score_percent


def build_msg(pair):
    base = pair.get("baseToken", {}) or {}
    verdict, notes, score_percent = analyze_pair(pair)

    return (
        f"🧠 *Token Scan*\n\n"
        f"🪙 *Token:* {base.get('symbol', 'N/A')}\n"
        f"📛 *Name:* {base.get('name', 'N/A')}\n"
        f"🔗 *Chain:* {str(pair.get('chainId', 'N/A')).upper()}\n"
        f"💧 *Liquidity:* ${fmt_money((pair.get('liquidity') or {}).get('usd'))}\n"
        f"📊 *Volume:* ${fmt_money((pair.get('volume') or {}).get('h24'))}\n"
        f"🎯 *Signal Score:* {score_percent}/100\n\n"
        f"*Verdict:* {verdict}\n"
        f"*Why:* {' | '.join(notes)}"
    )


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Chat ID: {update.effective_chat.id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    subscribers.add(cid)
    track_user(cid)
    save_data()

    text = (
        "🚀 *Only Signals*\n\n"
        "This bot does two things:\n"
        "1) Detects new tokens that pass the basic filter\n"
        "2) Analyzes any token on demand\n\n"
        f"📌 *Current Filters*\n"
        f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
        f"💧 Min Liquidity: ${MIN_LIQUIDITY}\n"
        f"📊 Min 24h Volume: ${MIN_VOLUME}\n"
        f"⏱ Check Interval: {CHECK_INTERVAL} sec\n\n"
        "Use the menu below or type:\n"
        "`/analyze`\n"
        "Then send a token name, symbol, or contract address."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    track_user(cid)

    text = (
        "❓ *Help*\n\n"
        "*Commands:*\n"
        "/start - launch the bot\n"
        "/status - current bot status\n"
        "/alerts_on - enable alerts\n"
        "/alerts_off - disable alerts\n"
        "/analyze - start token analysis flow\n"
        "/analytics - owner-only metrics\n"
        "/myid - show your chat id\n\n"
        "*How analysis works:*\n"
        "1) Send `/analyze`\n"
        "2) Send token name / symbol / contract\n"
        "3) Receive a quick signal scan\n\n"
        "⚠️ This is an early filter, not financial advice."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    track_user(cid)
    is_subscribed = "Enabled ✅" if cid in subscribers else "Disabled ❌"

    text = (
        "📊 *Bot Status*\n\n"
        f"🔔 Alerts: {is_subscribed}\n"
        f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
        f"💧 Min Liquidity: ${MIN_LIQUIDITY}\n"
        f"📊 Min 24h Volume: ${MIN_VOLUME}\n"
        f"⏱ Last Check: {last_check_time}\n"
        f"📨 Last Alert: {'Available' if last_alert_message != 'No alerts yet' else 'None'}"
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())


async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    if cid != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    top_all = get_top_searches(limit=5)
    top_24h = get_top_searches_recent(days=1, limit=5)

    top_all_text = "\n".join([f"- {q}: {c}" for q, c in top_all]) if top_all else "No searches yet"
    top_24h_text = "\n".join([f"- {q}: {c}" for q, c in top_24h]) if top_24h else "No searches in last 24h"

    text = (
        "📈 *Analytics*\n\n"
        f"👥 Subscribers: {len(subscribers)}\n"
        f"🚫 Blocked: {len(blocked_users)}\n"
        f"🧠 Analyses: {analyze_count}\n"
        f"🔥 Active 24h: {get_active_users(1)}\n"
        f"📆 Active 7d: {get_active_users(7)}\n\n"
        f"🔎 *Top Searched Tokens (All Time)*\n{top_all_text}\n\n"
        f"⚡ *Top Searched Tokens (24h)*\n{top_24h_text}"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    subscribers.add(cid)
    track_user(cid)
    save_data()
    await update.message.reply_text("✅ Alerts enabled for your account.", reply_markup=main_menu())


async def alerts_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    subscribers.discard(cid)
    track_user(cid)
    save_data()
    await update.message.reply_text("⛔ Alerts disabled for your account.", reply_markup=main_menu())


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    waiting_for_analysis.add(cid)
    track_user(cid)
    await update.message.reply_text("Send token name, symbol, or contract address.")


async def auto_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global analyze_count

    cid = update.effective_chat.id
    if cid not in waiting_for_analysis:
        return

    query_text = update.message.text.strip()

    waiting_for_analysis.discard(cid)
    track_user(cid)
    analyze_count += 1

    record_search(cid, query_text)
    save_data()

    pairs = search_pairs(query_text)

    if not pairs:
        await update.message.reply_text("No result found.")
        return

    filtered = [p for p in pairs if p.get("chainId", "").lower() == CHAIN_FILTER]
    if filtered:
        pairs = filtered

    best = choose_best_pair(pairs)

    if not best:
        await update.message.reply_text("No result found.")
        return

    await update.message.reply_text(build_msg(best), parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = query.message.chat_id
    track_user(cid)

    if query.data == "alerts_on":
        subscribers.add(cid)
        save_data()
        text = "✅ Alerts enabled."
    elif query.data == "alerts_off":
        subscribers.discard(cid)
        save_data()
        text = "⛔ Alerts disabled."
    elif query.data == "status":
        is_subscribed = "Enabled ✅" if cid in subscribers else "Disabled ❌"
        text = (
            "📊 *Bot Status*\n\n"
            f"🔔 Alerts: {is_subscribed}\n"
            f"🔗 Chain: {CHAIN_FILTER.upper()}\n"
            f"💧 Min Liquidity: ${MIN_LIQUIDITY}\n"
            f"📊 Min 24h Volume: ${MIN_VOLUME}\n"
            f"⏱ Last Check: {last_check_time}"
        )
    elif query.data == "last_alert":
        text = last_alert_message
    elif query.data == "help":
        text = (
            "❓ *Help*\n\n"
            "Use `/analyze`, then send the token name, symbol, or contract.\n"
            "The bot only sends alerts when a new token passes the current filters."
        )
    elif query.data == "analyze_prompt":
        waiting_for_analysis.add(cid)
        text = "Send token name, symbol, or contract address."
    else:
        text = "Unknown option."

    await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=main_menu())


async def check_new(context: ContextTypes.DEFAULT_TYPE):
    global last_alert_message, last_check_time

    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    profiles = get_latest_profiles()
    if not profiles:
        print("No profiles returned.")
        return

    if not seen_tokens:
        for item in profiles:
            chain = item.get("chainId")
            addr = item.get("tokenAddress")
            if chain and addr:
                seen_tokens.add(f"{chain}:{addr}")
        save_data()
        print(f"Initialized with {len(seen_tokens)} tokens.")
        return

    alerts = []

    for item in profiles:
        chain = item.get("chainId")
        addr = item.get("tokenAddress")

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

        liquidity = (best.get("liquidity", {}) or {}).get("usd", 0) or 0
        volume = (best.get("volume", {}) or {}).get("h24", 0) or 0

        if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
            continue

        msg = "🚨 *New Token Alert*\n\n" + build_msg(best).replace("🧠 *Token Scan*\n\n", "")
        alerts.append(msg)

    alerts = alerts[:MAX_ALERTS_PER_CYCLE]

    if alerts and subscribers:
        for msg in alerts:
            last_alert_message = msg
            for cid in list(subscribers):
                try:
                    await context.bot.send_message(cid, msg, parse_mode="Markdown")
                except Exception as e:
                    err = str(e).lower()
                    print(f"Send error to {cid}: {e}")
                    if "blocked" in err or "chat not found" in err:
                        blocked_users.add(cid)
                        subscribers.discard(cid)
            save_data()
    else:
        save_data()


load_data()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("status", status_command))
app.add_handler(CommandHandler("analytics", analytics_command))
app.add_handler(CommandHandler("analyze", analyze_command))
app.add_handler(CommandHandler("alerts_on", alerts_on_command))
app.add_handler(CommandHandler("alerts_off", alerts_off_command))
app.add_handler(CommandHandler("myid", myid_command))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_analyze))

app.job_queue.run_repeating(check_new, interval=CHECK_INTERVAL, first=10)

print("Bot is running...")
app.run_polling()
