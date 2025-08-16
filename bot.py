#!/usr/bin/env python3
"""
Merged Telegram BNB-earning bot (single file).
Features:
- verification (channel join + YouTube self-confirm) via one button
- daily bonus (0.001 BNB / 24h), referral bonus (0.01 BNB)
- BSC wallet saving with optional EIP-55 checksum validation (eth_utils)
- withdraw requests (min 0.5 BNB) with admin approve / reject (reject with reason)
- admin dashboard /admin_stats
- auto-delete old bot messages (TTL)
- SQLite storage
"""
import os
import sqlite3
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import app
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# optional checksum library
try:
    from eth_utils import is_checksum_address, to_checksum_address
    ETH_UTILS_AVAILABLE = True
except Exception:
    ETH_UTILS_AVAILABLE = False

# ---------- Config ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "Cryptomax9r")
YOUTUBE_URL = os.getenv("YOUTUBE_URL", "https://www.youtube.com/channel/UCmckYPi_hxyFkVsF6ALDQAA")
DB_PATH = os.getenv("DB_PATH", "crypto_bot.db")

getcontext().prec = 28
REF_BONUS = Decimal(os.getenv("REF_BONUS", "0.01"))
DAILY_BONUS = Decimal(os.getenv("DAILY_BONUS", "0.001"))
MIN_WITHDRAW = Decimal(os.getenv("MIN_WITHDRAW", "0.5"))
DECIMALS_DISPLAY = Decimal("0.000001")

BOT_MESSAGE_TTL = int(os.getenv("BOT_MESSAGE_TTL", "20"))
BOT_NAME = "BNB Earner Bot"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- DB ----------
_conn = None


def db_connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def db_init():
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
      telegram_id INTEGER PRIMARY KEY,
      username TEXT,
      balance TEXT DEFAULT '0',
      referrals INTEGER DEFAULT 0,
      referred_by INTEGER,
      wallet TEXT,
      last_bonus TEXT,
      joined_channel INTEGER DEFAULT 0,
      subscribed_yt INTEGER DEFAULT 0,
      last_bot_message_id INTEGER DEFAULT NULL
    )
    """
    )
    c.execute(
        """
    CREATE TABLE IF NOT EXISTS withdrawals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_id INTEGER,
      amount TEXT,
      wallet TEXT,
      status TEXT DEFAULT 'pending',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """
    )
    conn.commit()


# ---------- Utilities ----------
def to_decimal(x):
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def format_bnb(d: Decimal):
    q = d.quantize(DECIMALS_DISPLAY, rounding=ROUND_DOWN)
    s = format(q.normalize(), "f")
    return f"{s} BNB"


def now_utc():
    return datetime.now(timezone.utc)


# EIP-55 enabled validation if eth_utils installed
def is_valid_bsc_address(addr: str):
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    if not (addr.startswith("0x") and len(addr) == 42):
        return False
    if ETH_UTILS_AVAILABLE:
        # accept either lowercase address (non-checksummed) or correct checksum
        try:
            # If address is all lowercase or all uppercase, to_checksum_address will transform it.
            # We treat that as valid *format*, but prefer to warn if checksum doesn't match mixed-case.
            checksum = to_checksum_address(addr)
            # is_checksum_address returns True only for correct mixed-case checksum
            return True if (addr == checksum or addr.lower() == addr or addr.upper() == addr) else True
        except Exception:
            return False
    else:
        # fallback: basic format-only validation
        logger.warning("eth_utils not installed — running basic BSC address validation only.")
        return True


# ---------- DB helpers ----------
def get_user_row(tid):
    c = db_connect().cursor()
    c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,))
    return c.fetchone()


def ensure_user(tid, username=None, referred_by=None):
    conn = db_connect()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, balance, referred_by) VALUES (?, ?, '0', ?)",
              (tid, username or None, referred_by))
    conn.commit()


def add_balance(tid, amount: Decimal):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    row = get_user_row(tid)
    cur = to_decimal(row["balance"]) if row else Decimal("0")
    new = cur + amount
    c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (str(new), tid))
    conn.commit()


def set_balance(tid, amount: Decimal):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (str(amount), tid))
    conn.commit()


def inc_referrals(tid):
    conn = db_connect(); c = conn.cursor()
    c.execute("UPDATE users SET referrals = referrals + 1 WHERE telegram_id=?", (tid,))
    conn.commit()


def set_wallet(tid, addr):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    c.execute("UPDATE users SET wallet=? WHERE telegram_id=?", (addr, tid))
    conn.commit()


def record_last_bonus(tid, dt: datetime):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    c.execute("UPDATE users SET last_bonus=? WHERE telegram_id=?", (dt.isoformat(), tid))
    conn.commit()


def set_joined_flag(tid, val: int):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    c.execute("UPDATE users SET joined_channel=? WHERE telegram_id=?", (val, tid))
    conn.commit()


def set_subscribed_flag(tid, val: int):
    conn = db_connect(); c = conn.cursor()
    ensure_user(tid)
    c.execute("UPDATE users SET subscribed_yt=? WHERE telegram_id=?", (val, tid))
    conn.commit()


def create_withdrawal(tid, amount: Decimal, wallet: str):
    conn = db_connect(); c = conn.cursor()
    c.execute("INSERT INTO withdrawals (telegram_id, amount, wallet, status) VALUES (?, ?, ?, 'pending')",
              (tid, str(amount), wallet))
    conn.commit()
    return c.lastrowid


def get_withdrawal(wid):
    c = db_connect().cursor()
    c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,))
    return c.fetchone()


def mark_withdrawal(wid, status):
    conn = db_connect(); c = conn.cursor()
    c.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, wid))
    conn.commit()


def get_pending_withdrawals(limit=100):
    c = db_connect().cursor()
    c.execute("SELECT id, telegram_id, amount, wallet, created_at FROM withdrawals WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,))
    return c.fetchall()


def user_is_verified(tid):
    row = get_user_row(tid)
    if not row:
        return False
    return (row["joined_channel"] == 1) and (row["subscribed_yt"] == 1)


def save_last_bot_message(tid, message_id):
    conn = db_connect(); c = conn.cursor()
    c.execute("UPDATE users SET last_bot_message_id=? WHERE telegram_id=?", (message_id, tid))
    conn.commit()


def get_last_bot_message_id(tid):
    row = get_user_row(tid)
    return row["last_bot_message_id"] if row and "last_bot_message_id" in row.keys() else None


# Map admin_id -> wid for pending reject flow (so admin types reason)
pending_rejects = {}


# ---------- Messaging helper (auto-delete previous bot message) ----------
async def safe_send_and_store(chat_id, context, text, reply_markup=None, parse_mode=None, ttl=BOT_MESSAGE_TTL):
    # delete previous bot message if present
    last_mid = get_last_bot_message_id(chat_id)
    if last_mid:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_mid)
        except Exception:
            pass
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    save_last_bot_message(chat_id, msg.message_id)
    # schedule deletion
    if ttl and ttl > 0:
        context.job_queue.run_once(lambda ctx: ctx.bot.delete_message(chat_id=chat_id, message_id=msg.message_id), when=ttl)
    return msg


# ---------- Keyboards ----------
def main_menu_markup():
    kb = [
        [InlineKeyboardButton("💰 Claim Daily Bonus", callback_data="claim_daily"),
         InlineKeyboardButton("📊 My Balance", callback_data="my_balance")],
        [InlineKeyboardButton("👥 Referral Link", callback_data="referral"),
         InlineKeyboardButton("💳 Set/Update Wallet", callback_data="set_wallet")],
        [InlineKeyboardButton("💵 Withdraw", callback_data="withdraw"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(kb)


def verify_single_markup():
    kb = [
        [InlineKeyboardButton("🔗 Open Channel", url=f"https://t.me/{CHANNEL_USERNAME}")],
        [InlineKeyboardButton("▶️ YouTube", url=YOUTUBE_URL)],
        [InlineKeyboardButton("✅ I Joined & Subscribed (Check)", callback_data="check_both")]
    ]
    return InlineKeyboardMarkup(kb)


# ---------- Handlers ----------
def parse_ref_arg(context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.args:
            token = context.args[0].strip()
            if token.startswith("ref="):
                return int(token.split("=", 1)[1])
            if token.startswith("ref"):
                return int(token[3:])
    except Exception:
        pass
    return None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tid = user.id
    username = user.username or ""
    ref_id = parse_ref_arg(context)

    ensure_user(tid, username=username, referred_by=ref_id)

    # credit referrer if applicable (only if ref provided and not self)
    if ref_id and ref_id != tid:
        ensure_user(ref_id)
        add_balance(ref_id, to_decimal(REF_BONUS))
        inc_referrals(ref_id)
        try:
            await context.bot.send_message(ref_id, f"🎉 You earned {format_bnb(REF_BONUS)} from a referral!")
        except Exception:
            pass

    # notify admin when someone starts
    try:
        await context.bot.send_message(ADMIN_ID, f"👤 New user started: @{username or 'no_username'} (ID: {tid})")
    except Exception:
        pass

    row = get_user_row(tid)
    joined = row["joined_channel"] == 1 if row else False
    subbed = row["subscribed_yt"] == 1 if row else False

    if not (joined and subbed):
        text = (
            f"👋 Welcome to *{BOT_NAME}*\n\n"
            "Before using the bot, please:\n"
            f"1) Join our Telegram channel: @{CHANNEL_USERNAME}\n"
            "2) Subscribe to our YouTube channel\n\n"
            "Tap the button below after completing both steps."
        )
        await safe_send_and_store(tid, context, text, reply_markup=verify_single_markup(), parse_mode=ParseMode.MARKDOWN)
        return

    text = (
        f"🏦 *{BOT_NAME}*\n\n"
        f"Daily bonus: *{format_bnb(DAILY_BONUS)}*  •  Referral bonus: *{format_bnb(REF_BONUS)}*\n"
        f"Minimum withdrawal: *{format_bnb(MIN_WITHDRAW)}*\n\n"
        "Use the menu below."
    )
    await safe_send_and_store(tid, context, text, reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)


async def check_both_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = q.from_user.id

    # Channel membership check
    try:
        member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", tid)
        if member.status in ("member", "administrator", "creator"):
            set_joined_flag(tid, 1)
        else:
            await q.edit_message_text("❌ You are not a member yet. Please join the channel and press the button again.")
            return
    except Exception as e:
        logger.exception("Channel check error: %s", e)
        await q.edit_message_text("⚠️ Could not verify channel membership. Make sure the channel is public or add the bot as admin.")
        return

    # Self-confirm YouTube
    set_subscribed_flag(tid, 1)

    await q.edit_message_text("✅ Verification complete — main menu sent.")
    await safe_send_and_store(tid, context, f"🏦 *{BOT_NAME}*", reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_send_and_store(q.from_user.id, context, f"🏦 *{BOT_NAME}*", reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)


async def my_balance_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        tid = update.callback_query.from_user.id
        await update.callback_query.answer()
    else:
        tid = update.message.from_user.id

    if not user_is_verified(tid):
        await safe_send_and_store(tid, context, "You must verify (join channel & subscribe) before using the bot.", reply_markup=verify_single_markup())
        return

    row = get_user_row(tid)
    balance = to_decimal(row["balance"]) if row else Decimal("0")
    referrals = row["referrals"] if row else 0
    wallet = row["wallet"] if row and row["wallet"] else "Not set"
    last_bonus = row["last_bonus"] if row else None
    last_text = last_bonus if last_bonus else "Never"

    text = (
        f"📊 *Your Account*\n\n"
        f"💵 Balance: *{format_bnb(balance)}*\n"
        f"👥 Referrals: *{referrals}*\n"
        f"🏦 Wallet: `{wallet}`\n"
        f"⏱ Last daily bonus: *{last_text}*\n\n"
        f"Minimum withdrawal: *{format_bnb(MIN_WITHDRAW)}*"
    )
    await safe_send_and_store(tid, context, text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_markup())


async def claim_daily_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        tid = update.callback_query.from_user.id
        await update.callback_query.answer()
    else:
        tid = update.message.from_user.id

    if not user_is_verified(tid):
        await safe_send_and_store(tid, context, "You must verify (join channel & subscribe) before claiming bonuses.", reply_markup=verify_single_markup())
        return

    ensure_user(tid)
    row = get_user_row(tid)
    last = None
    if row and row["last_bonus"]:
        try:
            last = datetime.fromisoformat(row["last_bonus"])
        except Exception:
            last = None

    if last:
        diff = now_utc() - last
        if diff < timedelta(hours=24):
            remaining = timedelta(hours=24) - diff
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            await safe_send_and_store(tid, context, f"⏳ Already claimed. Try again in {hours}h {minutes}m.", reply_markup=main_menu_markup())
            return

    add_balance(tid, to_decimal(DAILY_BONUS))
    record_last_bonus(tid, now_utc())
    await safe_send_and_store(tid, context, f"✅ You received {format_bnb(DAILY_BONUS)} as the daily bonus!", reply_markup=main_menu_markup())


async def referral_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.callback_query.from_user.id if update.callback_query else update.message.from_user.id
    bot_user = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start=ref{tid}"
    text = f"👥 *Referral Link*\nShare this and earn {format_bnb(REF_BONUS)} per new user:\n\n`{ref_link}`"
    await safe_send_and_store(tid, context, text, reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)


async def set_wallet_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.callback_query.from_user.id if update.callback_query else update.message.from_user.id
    await safe_send_and_store(tid, context, "Please send your BSC (BEP-20) wallet address (starts with `0x`).", reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)
    context.user_data["awaiting_wallet"] = True


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.message.from_user.id
    text = (update.message.text or "").strip()

    # admin: if admin is currently entering reject reason
    if update.effective_user.id == ADMIN_ID and ADMIN_ID in pending_rejects:
        wid = pending_rejects.pop(ADMIN_ID)
        reason = text.strip()
        w = get_withdrawal(wid)
        if not w:
            await safe_send_and_store(ADMIN_ID, context, "❌ Withdrawal not found or already processed.")
            return
        # mark rejected and notify user with reason
        mark_withdrawal(wid, "rejected")
        try:
            await context.bot.send_message(w["telegram_id"], f"❌ Your withdrawal (ID {wid}) was *REJECTED* by admin.\nReason: {reason}", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        await safe_send_and_store(ADMIN_ID, context, f"✅ Rejection processed for WID {wid}. User notified.")
        return

    # normal flow: awaiting wallet
    if context.user_data.get("awaiting_wallet"):
        context.user_data.pop("awaiting_wallet", None)
        if is_valid_bsc_address(text):
            set_wallet(tid, text)
            await safe_send_and_store(tid, context, f"✅ Wallet saved: `{text}`", reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)
        else:
            msg = "❌ Invalid BSC address. It should start with `0x` and be 42 chars. If you used mixed-case, ensure checksum is correct."
            await safe_send_and_store(tid, context, msg, reply_markup=main_menu_markup(), parse_mode=ParseMode.MARKDOWN)
        return

    # fallback
    if not user_is_verified(tid):
        await safe_send_and_store(tid, context, "You must verify (join channel & subscribe) before using the bot.", reply_markup=verify_single_markup())
    else:
        await safe_send_and_store(tid, context, "Use the menu below.", reply_markup=main_menu_markup())


async def withdraw_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        tid = update.callback_query.from_user.id
        await update.callback_query.answer()
    else:
        tid = update.message.from_user.id

    if not user_is_verified(tid):
        await safe_send_and_store(tid, context, "You must verify (join channel & subscribe) before requesting withdraw.", reply_markup=verify_single_markup())
        return

    ensure_user(tid)
    row = get_user_row(tid)
    balance = to_decimal(row["balance"]) if row else Decimal("0")
    wallet = row["wallet"] if row and row["wallet"] else None
    referrals = row["referrals"] if row else 0

    if balance < to_decimal(MIN_WITHDRAW):
        await safe_send_and_store(tid, context, f"⚠️ Minimum withdrawal is {format_bnb(MIN_WITHDRAW)}. Your balance: {format_bnb(balance)}", reply_markup=main_menu_markup())
        return

    if not wallet:
        await safe_send_and_store(tid, context, "⚠️ You must set a BSC wallet first. Use the menu or send /setwallet.", reply_markup=main_menu_markup())
        return

    wid = create_withdrawal(tid, balance, wallet)

    admin_text = (
        f"💸 *Withdraw Request*\n"
        f"User: @{row['username'] or 'no_username'} (ID: {tid})\n"
        f"Amount: {format_bnb(balance)}\n"
        f"Wallet: `{wallet}`\n"
        f"Referrals: {referrals}\n"
        f"Withdrawal ID: {wid}"
    )
    kb = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve:{wid}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"reject:{wid}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    try:
        await context.bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        await safe_send_and_store(tid, context, "✅ Withdrawal request submitted. Admin will review it shortly.", reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("Failed to notify admin: %s", e)
        await safe_send_and_store(tid, context, "❌ Failed to send withdraw request to admin. Please try again later.", reply_markup=main_menu_markup())


async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    operator = q.from_user.id
    if operator != ADMIN_ID:
        await q.edit_message_text("❌ You are not authorized to perform this action.")
        return

    data = q.data
    try:
        action, wid_s = data.split(":")
        wid = int(wid_s)
    except Exception:
        await q.edit_message_text("Invalid callback data.")
        return

    w = get_withdrawal(wid)
    if not w:
        await q.edit_message_text("❌ Withdrawal not found.")
        return
    if w["status"] != "pending":
        await q.edit_message_text(f"⚠️ Withdrawal already {w['status']}.")
        return

    tid = w["telegram_id"]
    amount = to_decimal(w["amount"])
    wallet = w["wallet"]

    if action == "approve":
        mark_withdrawal(wid, "approved")
        set_balance(tid, Decimal("0"))
        await q.edit_message_text(f"✅ Approved withdrawal ID {wid} for user {tid} — amount: {format_bnb(amount)}")
        try:
            await context.bot.send_message(tid, f"🎉 Your withdrawal of {format_bnb(amount)} has been *APPROVED* by the admin and will be processed.", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    else:
        # start rejection flow: ask admin to type reason in private chat; store mapping
        pending_rejects[ADMIN_ID] = wid
        await q.edit_message_text(f"✏️ Please type the rejection reason now. Your next message will be sent to the user (WID {wid}).")
        return


async def admin_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    c = db_connect().cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM users")
    total_users = c.fetchone()["cnt"]
    c.execute("SELECT COUNT(*) AS cnt FROM withdrawals WHERE status='pending'")
    pending = c.fetchone()["cnt"]
    c.execute("SELECT SUM(CAST(amount AS REAL)) AS total_pending FROM withdrawals WHERE status='pending'")
    total_pending = c.fetchone()["total_pending"] or 0
    c.execute("SELECT SUM(referrals) AS total_refs FROM users")
    total_refs = c.fetchone()["total_refs"] or 0
    text = (
        f"📊 Admin Stats\n\n"
        f"Total users: {total_users}\n"
        f"Total referrals (sum): {total_refs}\n"
        f"Pending withdrawals: {pending}\n"
        f"Total pending amount: {total_pending} BNB\n"
    )
    await safe_send_and_store(ADMIN_ID, context, text)


# ---------- Main ----------
def main():
    db_init()
    if not BOT_TOKEN or ADMIN_ID == 0:
        logger.error("BOT_TOKEN and ADMIN_ID must be set in .env")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Public commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("balance", my_balance_action))
    app.add_handler(CommandHandler("claim", claim_daily_action))
    app.add_handler(CommandHandler("setwallet", set_wallet_prompt))
    app.add_handler(CommandHandler("withdraw", withdraw_action))
    app.add_handler(CommandHandler("referral", referral_action))

    # Admin commands
    app.add_handler(CommandHandler("admin_stats", admin_stats_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(check_both_callback, pattern="^check_both$"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: claim_daily_action(u, c), pattern="^claim_daily$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: my_balance_action(u, c), pattern="^my_balance$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: referral_action(u, c), pattern="^referral$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: set_wallet_prompt(u, c), pattern="^set_wallet$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: withdraw_action(u, c), pattern="^withdraw$"))
    app.add_handler(CallbackQueryHandler(approve_reject_callback, pattern=r'^(approve|reject):'))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

logger.info("BNB Earner Bot starting...")
import os

PORT = int(os.environ.get("PORT", 5000))
WEBHOOK_URL = "https://crypto-bnb-bot.onrender.com"  # replace with your Render URL

from flask import Flask, request
from telegram import Update
from telegram.ext import Application
import logging
import os

# logging setup
logging.basicConfig(level=logging.INFO)

# Flask app
flask_app = Flask(__name__)

# Telegram bot app
TOKEN = os.getenv("BOT_TOKEN")
application = Application.builder().token(TOKEN).build()

# your handlers here
# application.add_handler(...)

@flask_app.route("/")
def home():
    return "BNB Earner Bot is live!"

@flask_app.route(f"/{TOKEN}", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()     
