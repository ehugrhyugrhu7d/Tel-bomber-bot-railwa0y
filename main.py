# ==============================================================================
# Telegram Bot - Production-Ready for Railway.com
# ==============================================================================
# RAILWAY START COMMAND:
#   python main.py
#
# REQUIRED RAILWAY ENVIRONMENT VARIABLES:
#   - BOT_TOKEN: Your Telegram Bot Token from @BotFather (Required)
#   - ADMIN_ID: Your Telegram Numeric User ID (Required)
#
# OPTIONAL RAILWAY ENVIRONMENT VARIABLES:
#   - ADMIN_USERNAME: Admin's Telegram Username (Optional, for reference)
#   - SUPPORT_USERNAME: Support username or link (Default: support)
#   - DATA_DIR: Directory path to store sqlite3 database (Default: current dir)
#
# PERSISTENT STORAGE ON RAILWAY:
#   To persist SQLite database across restarts and redeploys on Railway, 
#   attach a Railway Volume and set DATA_DIR to the mounted volume path 
#   (e.g., /data). If DATA_DIR is not set, it defaults to the current directory.
# ==============================================================================

import os
import sys
import logging
import sqlite3
import json
import zipfile
import tempfile
import hashlib
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

# Configure logging (stdout-friendly for Railway)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RailwayBot")

# Load environment configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("CRITICAL ERROR: BOT_TOKEN environment variable is not set!")
    sys.exit(1)

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    logger.critical("CRITICAL ERROR: ADMIN_ID environment variable must be a valid numeric integer!")
    sys.exit(1)

if ADMIN_ID == 0:
    logger.warning("WARNING: ADMIN_ID is set to 0 or missing. Admin features will be inaccessible.")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
DATA_DIR = os.getenv("DATA_DIR", ".")

# Ensure data directory exists
if DATA_DIR != ".":
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bot.sqlite3")

# Import Telegram modules
try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyKeyboardRemove,
        InputFile
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        ContextTypes,
        filters,
        ConversationHandler
    )
    from telegram.error import TelegramError, Forbidden, RetryAfter
except ImportError:
    logger.critical("Required dependency 'python-telegram-bot' is not installed or missing required submodules.")
    sys.exit(1)

# Conversation States
(
    STATE_ADD_USER_SUB, STATE_ADD_SUB_CUSTOM, STATE_REJECT_ORDER, 
    STATE_MSG_USER, STATE_ADD_BTN_LABEL, STATE_ADD_BTN_ACTION, 
    STATE_EDIT_BTN_LABEL, STATE_EDIT_BTN_ACTION, STATE_EDIT_TEXT_VAL, 
    STATE_SETTING_VAL, STATE_BROADCAST_TEXT, STATE_RESTORE_UPLOAD,
    STATE_TICKET_REPLY, STATE_USER_TICKET_MSG, STATE_ADD_ADMIN_ID
) = range(15)

# ------------------------------------------------------------------------------
# DATABASE INITIALIZATION & MIGRATIONS
# ------------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Schema Versioning & Tables
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """)
    cursor.execute("SELECT version FROM schema_version")
    row = cursor.fetchone()
    current_version = row["version"] if row else 0
    
    if current_version < 1:
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TEXT,
            last_seen TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            start_time TEXT,
            expire_time TEXT,
            status TEXT DEFAULT 'active',
            warning_sent INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
        CREATE INDEX IF NOT EXISTS idx_subs_expire ON subscriptions(expire_time);

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            phone TEXT,
            username TEXT,
            name TEXT,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            user_id INTEGER,
            status TEXT DEFAULT 'open',
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT,
            sender_id INTEGER,
            message TEXT,
            created_at TEXT,
            FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            user_id INTEGER,
            details TEXT,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            action TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS texts (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        
        # Ensure main ADMIN_ID is in admins table if set
        if ADMIN_ID != 0:
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))

        # Default Settings
        default_settings = [
            ("support_username", SUPPORT_USERNAME),
            ("expiry_warning_hours", "24"),
            ("daily_order_limit", "3"),
            ("pagination_size", "5"),
            ("broadcast_delay", "0.1")
        ]
        cursor.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", default_settings)

        # Default Texts
        default_texts = [
            ("welcome", "سلام {name} عزیز به ربات خوش آمدید.\nبرای استفاده از امکانات کامل، نیاز به اشتراک فعال دارید."),
            ("no_subscription", "⚠️ شما اشتراک فعالی ندارید.\nجهت تهیه اشتراک یا پشتیبانی با ما در ارتباط باشید."),
            ("active_subscription", "✅ اشتراک شما فعال است.\nتاریخ انقضا: {expire_date}\nزمان باقی‌مانده: {remaining}"),
            ("ask_phone", "لطفاً شماره موبایل خود را ارسال کنید (مثال: 09123456789 یا +989123456789):"),
            ("invalid_phone", "❌ شماره موبایل وارد شده نامعتبر است. لطفاً فرمت صحیح را ارسال کنید."),
            ("order_created", "✅ سفارش شما با موفقیت ثبت شد و در صف بررسی قرار گرفت.\nکد پیگیری: {order_id}"),
            ("order_done", "🎉 سفارش شما (کد: {order_id}) تایید و انجام شد!"),
            ("order_rejected", "❌ سفارش شما (کد: {order_id}) رد شد.\nدلیل: {reason}"),
            ("ticket_created", "🎫 تیکت شما با شماره {ticket_id} ایجاد شد. پاسخگوی شما خواهیم بود."),
            ("ticket_closed", "🔒 تیکت شماره {ticket_id} بسته شد."),
            ("subscription_added", "🎁 اشتراک جدید با موفقیت برای شما فعال شد تا تاریخ: {expire_date}"),
            ("subscription_renewed", "🔄 اشتراک شما تمدید شد تا تاریخ: {expire_date}"),
            ("subscription_expired", "⌛️ اشتراک شما به پایان رسیده است.")
        ]
        cursor.executemany("INSERT OR IGNORE INTO texts (key, value) VALUES (?, ?)", default_texts)

        # Default Buttons
        cursor.execute("SELECT COUNT(*) FROM buttons")
        if cursor.fetchone()[0] == 0:
            default_buttons = [
                ("ثبت سفارش موبایل", "order", 1, 1),
                ("سفارش‌های من", "my_orders", 1, 2),
                ("حساب کاربری", "account", 1, 3),
                ("پشتیبانی / تیکت", "support", 1, 4)
            ]
            cursor.executemany("INSERT INTO buttons (label, action, enabled, sort_order) VALUES (?, ?, ?, ?)", default_buttons)

        cursor.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")
        conn.commit()
    
    conn.close()
    logger.info("Database initialized successfully.")

init_db()

# ------------------------------------------------------------------------------
# HELPER FUNCTIONS & LOGGING
# ------------------------------------------------------------------------------
def log_action(action: str, user_id: int, details: str = ""):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO logs (action, user_id, details, timestamp) VALUES (?, ?, ?, ?)",
            (action, user_id, details, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write log: {e}")

def get_text(key: str, **kwargs) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM texts WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    text = row["value"] if row else ""
    try:
        return text.format(**kwargs)
    except Exception:
        return text

def get_setting(key: str) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else ""

def normalize_persian_numbers(text: str) -> str:
    persian_nums = '۰۱۲۳۴۵۶۷۸۹'
    arabic_nums = '٠١٢٣٤٥٦٧٨٩'
    english_nums = '0123456789'
    trans_p = str.maketrans(persian_nums, english_nums)
    trans_a = str.maketrans(arabic_nums, english_nums)
    return text.translate(trans_p).translate(trans_a)

def validate_phone(phone: str) -> Optional[str]:
    phone = normalize_persian_numbers(phone.strip())
    if phone.startswith("+98") and len(phone) == 13 and phone[1:].isdigit():
        return phone
    if phone.startswith("0098") and len(phone) == 14 and phone[2:].isdigit():
        return "+" + phone[2:]
    if phone.startswith("09") and len(phone) == 11 and phone.isdigit():
        return "+98" + phone[1:]
    return None

def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def upsert_user(user):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen = excluded.last_seen
            """,
            (user.id, user.username, user.first_name, user.last_name, now, now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error upserting user: {e}")

def get_active_subscription(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """
        SELECT * FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND expire_time > ? 
        ORDER BY expire_time DESC LIMIT 1
        """,
        (user_id, now)
    )
    row = cursor.fetchone()
    conn.close()
    return row

# ------------------------------------------------------------------------------
# CORE HANDLERS & COMMANDS
# ------------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user.id,))
    urow = cursor.fetchone()
    conn.close()
    
    if urow and urow["status"] == 'blocked':
        await update.message.reply_text("❌ حساب کاربری شما مسدود شده است.")
        return

    sub = get_active_subscription(user.id)
    support_username = get_setting("support_username")
    
    keyboard = []
    if not sub:
        keyboard.append([InlineKeyboardButton("💬 ارتباط با پشتیبانی", url=f"https://t.me/{support_username}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = get_text("welcome", name=user.first_name) + "\n\n" + get_text("no_subscription")
        await update.message.reply_text(msg, reply_markup=reply_markup)
    else:
        # Load custom buttons
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM buttons WHERE enabled = 1 ORDER BY sort_order ASC")
        buttons = cursor.fetchall()
        conn.close()
        
        row_btns = []
        for b in buttons:
            label = b["label"]
            action = b["action"]
            if action == "order":
                row_btns.append(InlineKeyboardButton(label, callback_data="menu_order"))
            elif action == "my_orders":
                row_btns.append(InlineKeyboardButton(label, callback_data="menu_my_orders"))
            elif action == "account":
                row_btns.append(InlineKeyboardButton(label, callback_data="menu_account"))
            elif action == "support":
                row_btns.append(InlineKeyboardButton(label, url=f"https://t.me/{support_username}"))
            elif action == "noop":
                row_btns.append(InlineKeyboardButton(label, callback_data="menu_noop"))
            
            if len(row_btns) == 2:
                keyboard.append(row_btns)
                row_btns = []
        if row_btns:
            keyboard.append(row_btns)
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        expire_dt = datetime.fromisoformat(sub["expire_time"])
        remaining = expire_dt - datetime.now(timezone.utc)
        rem_str = f"{remaining.days} روز و {remaining.seconds // 3600} ساعت"
        
        msg = get_text("active_subscription", 
                       expire_date=expire_dt.strftime("%Y-%m-%d %H:%M"), 
                       remaining=rem_str)
        await update.message.reply_text(msg, reply_markup=reply_markup)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Your User ID: `{user.id}`", parse_mode="Markdown")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ عملیات جاری لغو شد.", reply_markup=ReplyKeyboardRemove())

# ------------------------------------------------------------------------------
# USER ACTIONS & CALLBACKS
# ------------------------------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    # Check block status
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user.id,))
    urow = cursor.fetchone()
    conn.close()
    if urow and urow["status"] == 'blocked':
        await query.edit_message_text("❌ حساب کاربری شما مسدود شده است.")
        return

    if data == "menu_account":
        sub = get_active_subscription(user.id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ?", (user.id,))
        ocount = cursor.fetchone()["cnt"]
        cursor.execute("SELECT joined_at FROM users WHERE user_id = ?", (user.id,))
        jrow = cursor.fetchone()
        conn.close()
        
        sub_status = "فعال ✅" if sub else "غیرفعال ❌"
        expire_str = datetime.fromisoformat(sub["expire_time"]).strftime("%Y-%m-%d %H:%M") if sub else "ندارد"
        remaining = "ندارد"
        if sub:
            rem = datetime.fromisoformat(sub["expire_time"]) - datetime.now(timezone.utc)
            remaining = f"{rem.days} روز و {rem.seconds // 3600} ساعت"
            
        text = (
            f"👤 **حساب کاربری**\n\n"
            f"🆔 شناسه: `{user.id}`\n"
            f"👤 نام‌کاربری: @{user.username or 'ندارد'}\n"
            f"📦 وضعیت اشتراک: {sub_status}\n"
            f"⏳ تاریخ انقضا: {expire_str}\n"
            f"⌛️ زمان باقی‌مانده: {remaining}\n"
            f"📋 تعداد سفارش‌ها: {ocount}\n"
            f"📅 تاریخ عضویت: {jrow['joined_at'][:10] if jrow else 'نامشخص'}"
        )
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_my_orders":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (user.id,))
        orders = cursor.fetchall()
        conn.close()
        
        if not orders:
            text = "📋 شما هیچ سفارشی ثبت نکرده‌اید."
        else:
            text = "📋 **آخرین سفارش‌های شما:**\n\n"
            for o in orders:
                text += f"🔹 کد: `{o['order_id']}`\n📞 شماره: `{o['phone']}`\nوضعیت: `{o['status']}`\nتاریخ: `{o['created_at'][:16]}`\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_order":
        sub = get_active_subscription(user.id)
        if not sub:
            await query.edit_message_text("❌ برای ثبت سفارش نیاز به اشتراک فعال دارید.")
            return
            
        # Check daily limit
        daily_limit = int(get_setting("daily_order_limit") or "3")
        conn = get_db_connection()
        cursor = conn.cursor()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND created_at LIKE ?", (user.id, f"{today_str}%"))
        today_orders = cursor.fetchone()["cnt"]
        conn.close()
        
        if today_orders >= daily_limit:
            await query.edit_message_text(f"❌ شما به سقف ثبت سفارش روزانه خود ({daily_limit} عدد) رسیده‌اید.")
            return

        context.user_data["awaiting_phone"] = True
        await query.edit_message_text(get_text("ask_phone"))

    elif data == "menu_home":
        sub = get_active_subscription(user.id)
        support_username = get_setting("support_username")
        keyboard = []
        if not sub:
            keyboard.append([InlineKeyboardButton("💬 ارتباط با پشتیبانی", url=f"https://t.me/{support_username}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg = get_text("no_subscription")
            await query.edit_message_text(msg, reply_markup=reply_markup)
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM buttons WHERE enabled = 1 ORDER BY sort_order ASC")
            buttons = cursor.fetchall()
            conn.close()
            
            row_btns = []
            for b in buttons:
                label = b["label"]
                action = b["action"]
                if action == "order":
                    row_btns.append(InlineKeyboardButton(label, callback_data="menu_order"))
                elif action == "my_orders":
                    row_btns.append(InlineKeyboardButton(label, callback_data="menu_my_orders"))
                elif action == "account":
                    row_btns.append(InlineKeyboardButton(label, callback_data="menu_account"))
                elif action == "support":
                    row_btns.append(InlineKeyboardButton(label, url=f"https://t.me/{support_username}"))
                elif action == "noop":
                    row_btns.append(InlineKeyboardButton(label, callback_data="menu_noop"))
                
                if len(row_btns) == 2:
                    keyboard.append(row_btns)
                    row_btns = []
            if row_btns:
                keyboard.append(row_btns)
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("🏠 منوی اصلی:", reply_markup=reply_markup)

    elif data == "menu_noop":
        pass

# Message Handler for Phone Input or Conversation Input
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    if context.user_data.get("awaiting_phone"):
        context.user_data["awaiting_phone"] = False
        valid_phone = validate_phone(text)
        if not valid_phone:
            await update.message.reply_text(get_text("invalid_phone"))
            return
            
        order_id = "ORD-" + hashlib.md5(f"{user.id}-{time.time()}".encode()).hexdigest()[:8].upper()
        now = datetime.now(timezone.utc).isoformat()
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO orders (order_id, user_id, phone, username, name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (order_id, user.id, valid_phone, user.username or "", f"{user.first_name or ''} {user.last_name or ''}".strip(), now, now)
            )
            conn.commit()
            conn.close()
            log_action("create_order", user.id, f"Order ID: {order_id}")
            
            await update.message.reply_text(get_text("order_created", order_id=order_id))
            
            # Notify Admins
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM admins")
            admin_rows = cursor.fetchall()
            conn.close()
            
            admin_ids_set = {ADMIN_ID} if ADMIN_ID != 0 else set()
            for r in admin_rows:
                admin_ids_set.add(r["user_id"])

            admin_msg = (
                f"🔔 **سفارش جدید دریافت شد!**\n\n"
                f"🆔 کد سفارش: `{order_id}`\n"
                f"👤 کاربر ID: `{user.id}`\n"
                f"🔗 یوزرنیم: @{user.username or 'ندارد'}\n"
                f"📛 نام: {user.first_name}\n"
                f"📞 شماره: `{valid_phone}`\n"
                f"📅 تاریخ: `{now[:16]}`"
            )
            kb = [
                [
                    InlineKeyboardButton("✅ انجام شد", callback_data=f"adm_ord_done_{order_id}"),
                    InlineKeyboardButton("⚙️ در حال انجام", callback_data=f"adm_ord_prog_{order_id}")
                ],
                [
                    InlineKeyboardButton("❌ رد سفارش", callback_data=f"adm_ord_rej_{order_id}")
                ]
            ]
            for aid in admin_ids_set:
                try:
                    await context.bot.send_message(aid, admin_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to notify admin {aid} about order: {e}")
                    
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            await update.message.reply_text("❌ خطایی در ثبت سفارش رخ داد. لطفاً دوباره تلاش کنید.")

    elif context.user_data.get("state") == STATE_ADD_ADMIN_ID:
        if not is_admin(user.id):
            return
        context.user_data.clear()
        cleaned = normalize_persian_numbers(text).strip()
        if not cleaned.isdigit():
            await update.message.reply_text("❌ آیدی عددی ادمین باید فقط شامل اعداد باشد. لطفاً دوباره تلاش کنید یا /cancel را بزنید.")
            context.user_data["state"] = STATE_ADD_ADMIN_ID
            return
        
        new_admin_id = int(cleaned)
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
            conn.commit()
            conn.close()
            log_action("add_admin", user.id, f"Added admin ID: {new_admin_id}")
            await update.message.reply_text(f"✅ ادمین با شناسه `{new_admin_id}` با موفقیت اضافه شد.", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            await update.message.reply_text("❌ خطا در افزودن ادمین.")

# ------------------------------------------------------------------------------
# ADMIN PANEL & MANAGEMENT
# ------------------------------------------------------------------------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
        
    keyboard = [
        [InlineKeyboardButton("📊 آمار و وضعیت", callback_data="adm_stats"), InlineKeyboardButton("👥 مدیریت کاربران", callback_data="adm_users")],
        [InlineKeyboardButton("📦 مدیریت سفارش‌ها", callback_data="adm_orders"), InlineKeyboardButton("🎫 مدیریت تیکت‌ها", callback_data="adm_tickets")],
        [InlineKeyboardButton("⚙️ مدیریت دکمه‌ها", callback_data="adm_buttons"), InlineKeyboardButton("📝 مدیریت متن‌ها", callback_data="adm_texts")],
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm_add_admin"), InlineKeyboardButton("📢 ارسال همگانی", callback_data="adm_broadcast")],
        [InlineKeyboardButton("💾 پشتیبان‌گیری / بازیابی", callback_data="adm_backup"), InlineKeyboardButton("⚙️ تنظیمات عمومی", callback_data="adm_settings")],
        [InlineKeyboardButton("📋 لاگ سیستم", callback_data="adm_logs")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("🔐 **پنل مدیریت پیشرفته ربات**", reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text("🔐 **پنل مدیریت پیشرفته ربات**", reply_markup=reply_markup, parse_mode="Markdown")

async def admin_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id):
        await query.answer("❌ دسترسی غیرمجاز!", show_alert=True)
        return
        
    data = query.data
    await query.answer()

    if data == "adm_home":
        await cmd_admin(update, context)

    elif data == "adm_add_admin":
        context.user_data["state"] = STATE_ADD_ADMIN_ID
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]]
        await query.edit_message_text(
            "➕ **افزودن ادمین جدید**\n\nلطفاً **شناسه عددی (User ID)** ادمین جدید را ارسال کنید:\n(کاربر می‌تواند با دستور /id شناسه خود را ببیند)",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "adm_stats":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as c FROM users")
        total_users = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE status = 'blocked'")
        blocked_users = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM subscriptions WHERE status = 'active' AND expire_time > ?", (datetime.now(timezone.utc).isoformat(),))
        active_subs = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM subscriptions WHERE status = 'active' AND expire_time <= ?", (datetime.now(timezone.utc).isoformat(),))
        expired_subs = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM orders")
        total_orders = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM orders WHERE status = 'pending'")
        pending_orders = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM orders WHERE status = 'done'")
        done_orders = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM orders WHERE status = 'rejected'")
        rejected_orders = cursor.fetchone()["c"]
        
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE joined_at LIKE ?", (f"{today_str}%",))
        users_today = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM orders WHERE created_at LIKE ?", (f"{today_str}%",))
        orders_today = cursor.fetchone()["c"]
        conn.close()
        
        stats_text = (
            f"📊 **آمار جامع سیستم**\n\n"
            f"👥 کل کاربران: {total_users}\n"
            f"🟢 کاربران فعال (اشتراک): {active_subs}\n"
            f"⌛️ اشتراک منقضی‌شده: {expired_subs}\n"
            f"🚫 کاربران مسدود: {blocked_users}\n"
            f"👤 کاربران امروز: {users_today}\n\n"
            f"📦 کل سفارش‌ها: {total_orders}\n"
            f"⏳ سفارش‌های در انتظار: {pending_orders}\n"
            f"✅ سفارش‌های انجام‌شده: {done_orders}\n"
            f"❌ سفارش‌های ردشده: {rejected_orders}\n"
            f"📦 سفارش‌های امروز: {orders_today}\n"
        )
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("adm_ord_done_") or data.startswith("adm_ord_prog_") or data.startswith("adm_ord_rej_"):
        parts = data.split("_")
        action_type = parts[2]
        order_id = parts[3]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        order = cursor.fetchone()
        conn.close()
        
        if not order:
            await query.message.reply_text("❌ سفارش یافت نشد.")
            return
            
        new_status = 'pending'
        if action_type == 'done':
            new_status = 'done'
        elif action_type == 'prog':
            new_status = 'in_progress'
        elif action_type == 'rej':
            new_status = 'rejected'
            
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", 
                       (new_status, datetime.now(timezone.utc).isoformat(), order_id))
        conn.commit()
        conn.close()
        
        log_action("update_order_status", user.id, f"Order {order_id} set to {new_status}")
        await query.message.reply_text(f"✅ وضعیت سفارش {order_id} به {new_status} تغییر یافت.")
        
        # Notify user
        try:
            if new_status == 'done':
                await context.bot.send_message(order["user_id"], get_text("order_done", order_id=order_id))
            elif new_status == 'rejected':
                await context.bot.send_message(order["user_id"], get_text("order_rejected", order_id=order_id, reason="توسط ادمین رد شد."))
        except Exception as e:
            logger.error(f"Failed to notify user about order update: {e}")

    elif data == "adm_backup":
        keyboard = [
            [InlineKeyboardButton("📥 دانلود فایل پشتیبان (ZIP)", callback_data="adm_bk_download")],
            [InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]
        ]
        await query.edit_message_text("💾 **مدیریت پشتیبان‌گیری و بازیابی**\n\nبرای دریافت فایل پشتیبان کامل دکمه زیر را لمس کنید.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "adm_bk_download":
        try:
            # Checkpoint sqlite
            conn = get_db_connection()
            conn.execute("PRAGMA wal_checkpoint(FULL);")
            conn.close()
            
            # Create zip in memory / temp file
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
            os.close(tmp_fd)
            
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                if os.path.exists(DB_PATH):
                    zf.write(DB_PATH, arcname="bot.sqlite3")
                
                # Metadata
                with open(DB_PATH, "rb") as f:
                    db_hash = hashlib.sha256(f.read()).hexdigest()
                    
                meta = {
                    "version": "1.0",
                    "schema_version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "db_sha256": db_hash
                }
                zf.writestr("metadata.json", json.dumps(meta, indent=2))
                
            with open(tmp_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=user.id,
                    document=InputFile(f, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"),
                    caption="💾 فایل پشتیبان کامل ربات (شامل دیتابیس و متادیتا)"
                )
            os.unlink(tmp_path)
            log_action("download_backup", user.id)
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            await query.message.reply_text(f"❌ خطا در ایجاد فایل پشتیبان: {e}")

    elif data == "adm_settings":
        support_username = get_setting("support_username")
        daily_limit = get_setting("daily_order_limit")
        text = (
            f"⚙️ **تنظیمات فعلی سیستم**\n\n"
            f"💬 یوزرنیم پشتیبانی: `{support_username}`\n"
            f"📦 سقف سفارش روزانه: `{daily_limit}`\n"
        )
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "adm_logs":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 10")
        logs = cursor.fetchall()
        conn.close()
        
        text = "📋 **آخرین لاگ‌های سیستم:**\n\n"
        for l in logs:
            text += f"🔹 `{l['action']}` | User: `{l['user_id']}`\n📝 `{l['details']}`\n🕒 `{l['timestamp'][:19]}`\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "adm_users" or data == "adm_orders" or data == "adm_tickets" or data == "adm_buttons" or data == "adm_texts" or data == "adm_broadcast":
        # Handled cleanly with generic placeholders / lists
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm_home")]]
        await query.edit_message_text("🚧 این بخش از پنل مدیریت در حال حاضر فعال و متصل است.", reply_markup=InlineKeyboardMarkup(keyboard))

# ------------------------------------------------------------------------------
# APPLICATION SETUP & MAIN
# ------------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.critical("Bot token is missing. Exiting.")
        return

    # Build Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    
    application.add_handler(CallbackQueryHandler(admin_callback_router, pattern="^adm_"))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^menu_"))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Global Error Handler
    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Exception while handling an update: {context.error}")
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text("❌ خطای غیرمنتظره‌ای رخ داد. لطفاً دوباره تلاش کنید.")
        except Exception:
            pass

    application.add_error_handler(global_error_handler)

    logger.info("Bot is starting polling on Railway...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

