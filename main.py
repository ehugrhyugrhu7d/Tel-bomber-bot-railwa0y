import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# Railway deployment notes
# ============================================================
# Required Railway Variables:
#   BOT_TOKEN          = Telegram Bot Token
#   ADMIN_ID           = Numeric Telegram ID of the main admin
# Optional Railway Variables:
#   ADMIN_USERNAME     = Telegram username without @
#   SUPPORT_USERNAME   = Support username without @
#   DATA_DIR           = Persistent data directory, e.g. /data on a Railway Volume
#   DB_NAME            = Database filename (default: bot.sqlite3)
#   BACKUP_MAX_MB      = Maximum uploaded backup size (default: 25)
#   EXPIRY_CHECK_SEC   = Expiry worker interval (default: 60)
#   LOG_LEVEL          = INFO / DEBUG / WARNING / ERROR
#
# Recommended Railway Start Command:
#   python main.py
#
# IMPORTANT:
# Railway Variables are the source of secrets/configuration.
# Do NOT hard-code BOT_TOKEN or any secret in this source file.
# SQLite survives Railway restarts only when DATA_DIR is on persistent storage.
# The bot still runs without a volume; data durability then depends on the runtime filesystem.
# ============================================================

APP_NAME = "Railway Telegram Bot"
SCHEMA_VERSION = 4
START_TIME = time.time()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", ADMIN_USERNAME).strip().lstrip("@")
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).expanduser().resolve()
DB_NAME = os.getenv("DB_NAME", "bot.sqlite3").strip() or "bot.sqlite3"
MAX_BACKUP_BYTES = int(float(os.getenv("BACKUP_MAX_MB", "25")) * 1024 * 1024)
EXPIRY_CHECK_SEC = max(30, int(os.getenv("EXPIRY_CHECK_SEC", "60")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / DB_NAME
TMP_DIR = DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram_bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN to Railway Variables.")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.")

if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is missing or invalid. Add a numeric ADMIN_ID to Railway Variables.")

# -----------------------------
# Defaults
# -----------------------------
DEFAULT_TEXTS = {
    "welcome": "👋 سلام {name}\n\nبه ربات خوش اومدی.",
    "no_subscription": "⛔️ شما در حال حاضر اشتراک فعالی ندارید.\nبرای دریافت تست یا پشتیبانی روی دکمه زیر کلیک کنید.",
    "active_welcome": "✅ اشتراک شما فعاله.\n\n⏳ زمان باقی‌مانده: {remaining}\n📅 انقضا: {expires}",
    "ask_phone": "📱 لطفاً شماره موبایلتان را ارسال کنید.\nمثال: +989123456789",
    "invalid_phone": "❌ شماره واردشده معتبر نیست.\nفرمت مجاز: +989xxxxxxxxx",
    "order_created": "✅ سفارش شما با موفقیت ثبت شد.\n🆔 شماره سفارش: {order_id}\n📌 وضعیت: در انتظار بررسی",
    "order_done": "✅ سفارش #{order_id} انجام شد.",
    "order_rejected": "❌ سفارش #{order_id} رد شد.\n\n📝 دلیل: {reason}",
    "order_cancelled": "🚫 سفارش #{order_id} لغو شد.",
    "order_in_progress": "🔄 سفارش #{order_id} وارد مرحله انجام شد.",
    "ticket_created": "🎫 تیکت شما با شماره #{ticket_id} ثبت شد.",
    "ticket_closed": "✅ تیکت #{ticket_id} بسته شد.",
    "subscription_added": "🎉 اشتراک شما فعال شد.\n📅 انقضا: {expires}",
    "subscription_renewed": "🔄 اشتراک شما تمدید شد.\n📅 انقضا: {expires}",
    "subscription_expired": "⛔️ اشتراک شما منقضی شده است. برای تمدید با پشتیبانی در ارتباط باشید.",
    "subscription_warning": "⚠️ اشتراک شما به‌زودی منقضی می‌شود.\n⏳ زمان باقی‌مانده: {remaining}",
    "blocked": "🚫 دسترسی شما به ربات مسدود شده است.",
    "daily_limit": "⚠️ سقف سفارش روزانه شما تکمیل شده است.",
    "unknown": "❓ این درخواست قابل پردازش نیست.",
}

DEFAULT_BUTTONS = [
    ("ثبت سفارش 📱", "order", 1, "active"),
    ("سفارش‌های من 📦", "my_orders", 2, "active"),
    ("حساب کاربری 👤", "account", 3, "active"),
    ("پشتیبانی 💬", "support", 4, "active"),
    ("تست / پشتیبانی 💬", "support", 1, "inactive"),
]

DEFAULT_SETTINGS = {
    "support_username": SUPPORT_USERNAME,
    "expiry_warning_hours": "24",
    "daily_order_limit": "5",
    "page_size": "8",
    "broadcast_delay": "0.08",
    "expiry_worker_enabled": "1",
}

# -----------------------------
# Database
# -----------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def human_dt(value: Optional[str]) -> str:
    dt = parse_dt(value)
    if not dt:
        return "-"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_remaining(expires_at: Optional[str]) -> str:
    dt = parse_dt(expires_at)
    if not dt:
        return "ندارد"
    seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "منقضی شده"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} روز و {hours} ساعت"
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def db_exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with closing(db_connect()) as conn:
        return conn.execute(sql, params)


def db_fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchone()


def db_fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchall()


def init_db() -> None:
    with closing(db_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                warning_sent INTEGER NOT NULL DEFAULT 0,
                expired_notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                name TEXT,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                admin_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                subject TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL DEFAULT 'active',
                label TEXT NOT NULL,
                action TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(scope, label)
            );

            CREATE TABLE IF NOT EXISTS texts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
            CREATE INDEX IF NOT EXISTS idx_admins_username ON admins(username);
            CREATE INDEX IF NOT EXISTS idx_subs_user_active ON subscriptions(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_subs_expire ON subscriptions(expires_at, active);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id);
            CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
            """
        )
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(buttons)")
        columns = [col[1] for col in cursor.fetchall()]
        if "scope" in columns:
            conn.execute("UPDATE buttons SET scope='active' WHERE scope='user'")
        
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version', ?) ",
            (str(SCHEMA_VERSION),),
        )
        for key, value in DEFAULT_TEXTS.items():
            conn.execute("INSERT OR IGNORE INTO texts(key,value) VALUES(?,?)", (key, value))
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        
        conn.execute(
            "INSERT OR IGNORE INTO admins(user_id, username, added_at) VALUES(?, ?, ?)",
            (ADMIN_ID, ADMIN_USERNAME, now_iso())
        )

        count_active = conn.execute("SELECT COUNT(*) FROM buttons WHERE scope='active'").fetchone()[0]
        if count_active == 0:
            conn.executemany(
                "INSERT INTO buttons(scope,label,action,enabled,sort_order) VALUES('active',?,?,1,?)",
                [(label, action, order) for label, action, order, scope in DEFAULT_BUTTONS if scope == 'active'],
            )
        count_inactive = conn.execute("SELECT COUNT(*) FROM buttons WHERE scope='inactive'").fetchone()[0]
        if count_inactive == 0:
            conn.executemany(
                "INSERT INTO buttons(scope,label,action,enabled,sort_order) VALUES('inactive',?,?,1,?)",
                [(label, action, order) for label, action, order, scope in DEFAULT_BUTTONS if scope == 'inactive'],
            )


def get_setting(key: str, default: str = "") -> str:
    row = db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_text(key: str) -> str:
    row = db_fetchone("SELECT value FROM texts WHERE key=?", (key,))
    return str(row["value"]) if row else DEFAULT_TEXTS.get(key, key)


def log_event(user_id: Optional[int], action: str, details: str = "") -> None:
    try:
        db_exec(
            "INSERT INTO logs(user_id,action,details,created_at) VALUES(?,?,?,?)",
            (user_id, action, details[:2000], now_iso()),
        )
    except Exception:
        logger.exception("Failed to write audit log")


def ensure_user(tg_user) -> None:
    uid = int(tg_user.id)
    now = now_iso()
    clean_username = (tg_user.username or "").strip().lstrip("@")
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO users(user_id,username,first_name,last_name,status,joined_at,last_seen)
            VALUES(?,?,?,?, 'active', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=excluded.last_seen
            """,
            (
                uid,
                clean_username,
                tg_user.first_name or "",
                tg_user.last_name or "",
                now,
                now,
            ),
        )


def ensure_user_id(user_id: int) -> None:
    now = now_iso()
    db_exec(
        "INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,status,joined_at,last_seen) VALUES(?,?,?,?,'active',?,?)",
        (user_id, "", "", "", now, now),
    )


def user_is_blocked(user_id: int) -> bool:
    row = db_fetchone("SELECT status FROM users WHERE user_id=?", (user_id,))
    return bool(row and row["status"] == "blocked")


def get_active_subscription(user_id: int) -> Optional[sqlite3.Row]:
    row = db_fetchone(
        """
        SELECT * FROM subscriptions
        WHERE user_id=? AND active=1 AND expires_at>?
        ORDER BY expires_at DESC LIMIT 1
        """,
        (user_id, now_iso()),
    )
    return row


def user_has_subscription(user_id: int) -> bool:
    return get_active_subscription(user_id) is not None


def deactivate_expired_subscriptions() -> list[int]:
    current = now_iso()
    rows = db_fetchall(
        "SELECT id,user_id FROM subscriptions WHERE active=1 AND expires_at<=?",
        (current,),
    )
    ids = [int(r["user_id"]) for r in rows]
    if rows:
        db_exec("UPDATE subscriptions SET active=0,updated_at=? WHERE active=1 AND expires_at<=?", (current, current))
    return ids


def set_subscription(user_id: int, days: int, renew: bool = False) -> sqlite3.Row:
    ensure_user_id(user_id)
    current = datetime.now(timezone.utc)
    active = get_active_subscription(user_id)
    if renew and active:
        start = parse_dt(active["expires_at"]) or current
        if start < current:
            start = current
    else:
        start = current
        if active and renew:
            start = parse_dt(active["expires_at"]) or current
    expires = start + timedelta(days=max(1, days))
    now = now_iso()
    with closing(db_connect()) as conn:
        conn.execute("UPDATE subscriptions SET active=0,updated_at=? WHERE user_id=? AND active=1", (now, user_id))
        conn.execute(
            """
            INSERT INTO subscriptions(user_id,starts_at,expires_at,active,warning_sent,expired_notified,created_at,updated_at)
            VALUES(?,?,?,?,0,0,?,?)
            """,
            (user_id, start.isoformat(), expires.isoformat(), 1, now, now),
        )
    return get_active_subscription(user_id)


def remove_subscription(user_id: int) -> None:
    db_exec("UPDATE subscriptions SET active=0,updated_at=? WHERE user_id=? AND active=1", (now_iso(), user_id))


def subscription_status_text(user_id: int) -> str:
    row = get_active_subscription(user_id)
    if not row:
        return "بدون اشتراک"
    return f"فعال تا {human_dt(row['expires_at'])}\n⏳ {human_remaining(row['expires_at'])}"


def get_page_size() -> int:
    try:
        return max(3, min(20, int(get_setting("page_size", "8"))))
    except ValueError:
        return 8

# -----------------------------
# UI helpers
# -----------------------------

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def back_button(target: str = "user:menu") -> InlineKeyboardButton:
    return btn("↩️ بازگشت", target)


def admin_menu_markup() -> InlineKeyboardMarkup:
    rows = [
        [btn("👥 کاربران", "admin:users:0"), btn("➕ افزودن کاربر", "admin:adduser")],
        [btn("🛡 ادمین‌ها", "admin:admins:0"), btn("➕ افزودن ادمین", "admin:addadmin")],
        [btn("💳 اشتراک‌ها", "admin:subs:0")],
        [btn("📦 سفارش‌ها", "admin:orders:0"), btn("🎫 تیکت‌ها", "admin:tickets:0")],
        [btn("📊 آمار", "admin:stats"), btn("🧾 لاگ‌ها", "admin:logs:0")],
        [btn("🔘 دکمه‌ها", "admin:buttons") , btn("✏️ متن‌ها", "admin:texts")],
        [btn("📢 Broadcast", "admin:broadcast"), btn("💾 Backup", "admin:backup")],
        [btn("⚙️ تنظیمات", "admin:settings"), btn("🩺 وضعیت", "admin:health")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_user_actions(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("➕ اشتراک", f"admin:addsub:{user_id}"), btn("🔄 تمدید", f"admin:renew:{user_id}")],
        [btn("❌ حذف اشتراک", f"admin:remsub:{user_id}"), btn("💬 پیام", f"admin:msg:{user_id}")],
        [btn("📦 سفارش‌ها", f"admin:userorders:{user_id}:0"), btn("🎫 تیکت‌ها", f"admin:usertickets:{user_id}:0")],
        [btn("🚫/✅ تغییر بلاک", f"admin:toggleblock:{user_id}")],
        [back_button("admin:users:0")],
    ])


def user_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    has_sub = user_has_subscription(user_id)
    scope = "active" if has_sub else "inactive"
    rows = []
    buttons = db_fetchall(
        "SELECT id,label,action FROM buttons WHERE scope=? AND enabled=1 ORDER BY sort_order,id",
        (scope,)
    )
    username = get_setting("support_username", SUPPORT_USERNAME).strip().lstrip("@")
    
    current = []
    for row in buttons:
        # اگر دکمه مربوط به پشتیبانی/تست بود و کاربر اشتراک نداشت، آن را به عنوان URL دکمه شیشه ای قرار بده تا مستقیم به آیدی برود
        if not has_sub and row["action"] == "support" and username:
            current.append(InlineKeyboardButton(row["label"], url=f"https://t.me/{username}"))
        else:
            current.append(btn(row["label"], f"user:action:{row['action']}"))
            
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
        
    if not has_sub:
        if username and not any(b["action"] == "support" for b in buttons):
            rows.append([InlineKeyboardButton("💬 تست / پشتیبانی", url=f"https://t.me/{username}")])
    return InlineKeyboardMarkup(rows)


def support_markup() -> InlineKeyboardMarkup:
    username = get_setting("support_username", SUPPORT_USERNAME).strip().lstrip("@")
    if username:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💬 تست / پشتیبانی", url=f"https://t.me/{username}")], [back_button("user:menu")]])
    return InlineKeyboardMarkup([[back_button("user:menu")]])


def safe_format(template: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except Exception:
        return template


def display_name(row: sqlite3.Row | dict) -> str:
    name = (row.get("first_name") if hasattr(row, "get") else row["first_name"]) or ""
    last = (row.get("last_name") if hasattr(row, "get") else row["last_name"]) or ""
    full = f"{name} {last}".strip()
    return full or "بدون نام"

# -----------------------------
# Auth / middleware-like checks
# -----------------------------

def is_admin_user(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    uid = int(user.id)
    if uid == ADMIN_ID:
        return True
    row = db_fetchone("SELECT user_id FROM admins WHERE user_id=?", (uid,))
    return bool(row)


def require_not_blocked(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user_is_blocked(int(user.id)))

# -----------------------------
# Commands
# -----------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    ensure_user(user)
    if user_is_blocked(user.id) and not is_admin_user(update):
        await update.effective_message.reply_text(get_text("blocked"))
        return
    context.user_data.clear()
    if is_admin_user(update):
        await update.effective_message.reply_text("🛠 پنل مدیریت آماده است.", reply_markup=admin_menu_markup())
        log_event(user.id, "admin_start")
        return
    sub = get_active_subscription(user.id)
    if not sub:
        await update.effective_message.reply_text(
            safe_format(get_text("welcome"), name=user.first_name or "دوست عزیز") + "\n\n" + get_text("no_subscription"),
            reply_markup=user_menu_markup(user.id),
        )
        return
    await update.effective_message.reply_text(
        safe_format(
            get_text("active_welcome"),
            remaining=human_remaining(sub["expires_at"]),
            expires=human_dt(sub["expires_at"]),
        ),
        reply_markup=user_menu_markup(user.id),
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin_user(update):
        return
    ensure_user(update.effective_user)
    context.user_data.clear()
    await update.effective_message.reply_text("🛠 پنل مدیریت", reply_markup=admin_menu_markup())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text("✅ عملیات لغو شد.")
    if is_admin_user(update):
        await update.effective_message.reply_text("🛠 پنل مدیریت", reply_markup=admin_menu_markup())
    elif update.effective_user:
        await update.effective_message.reply_text("🏠 منوی اصلی", reply_markup=user_menu_markup(update.effective_user.id))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"🆔 ID شما:\n`{user.id}`", parse_mode=ParseMode.MARKDOWN_V2)

# -----------------------------
# User actions
# -----------------------------

async def show_account(update: Update) -> None:
    user = update.effective_user
    row = db_fetchone("SELECT * FROM users WHERE user_id=?", (user.id,))
    sub = get_active_subscription(user.id)
    orders = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE user_id=?", (user.id,))["c"]
    if row:
        username = f"@{row['username']}" if row["username"] else "ندارد"
        status = "✅ فعال" if sub else "❌ بدون اشتراک"
        expires = human_dt(sub["expires_at"]) if sub else "-"
        remaining = human_remaining(sub["expires_at"]) if sub else "-"
        text = (
            "👤 *حساب کاربری*\n\n"
            f"🆔 `{row['user_id']}`\n"
            f"👤 نام: {display_name(row)}\n"
            f"🔗 Username: {username}\n"
            f"📌 وضعیت: {status}\n"
            f"📅 انقضا: {expires}\n"
            f"⏳ باقی‌مانده: {remaining}\n"
            f"📦 تعداد سفارش‌ها: {orders}\n"
            f"🗓 عضویت: {human_dt(row['joined_at'])}"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button("user:menu")]]))


async def show_my_orders(update: Update, page: int = 0, edit: bool = False) -> None:
    user_id = update.effective_user.id
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE user_id=?", (user_id,))["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, size, page * size),
    )
    if rows:
        text = "📦 *سفارش‌های من*\n\n" + "\n".join(
            f"• `#{r['id']}` — {r['status']} — {human_dt(r['created_at'])}" for r in rows
        )
    else:
        text = "📦 هنوز سفارشی ندارید."
    keyboard = []
    for r in rows:
        keyboard.append([btn(f"#{r['id']}", f"user:order:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", f"user:orders:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "user:noop"))
    if page < pages - 1:
        nav.append(btn("بعدی ▶️", f"user:orders:{page+1}"))
    keyboard.append(nav)
    keyboard.append([back_button("user:menu")])
    markup = InlineKeyboardMarkup(keyboard)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def show_order_detail(update: Update, order_id: str, admin: bool = False) -> None:
    row = db_fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await update.effective_message.reply_text("❌ سفارش پیدا نشد.")
        return
    if not admin and row["user_id"] != update.effective_user.id:
        return
    text = (
        f"📦 *جزئیات سفارش #{row['id']}*\n\n"
        f"👤 کاربر: `{row['user_id']}`\n"
        f"📱 شماره: `{row['phone']}`\n"
        f"📌 وضعیت: `{row['status']}`\n"
        f"🕐 ثبت: {human_dt(row['created_at'])}\n"
        f"🔄 بروزرسانی: {human_dt(row['updated_at'])}"
    )
    if row["reason"]:
        text += f"\n📝 دلیل: {row['reason']}"
    if row["admin_note"]:
        text += f"\n💬 یادداشت ادمین: {row['admin_note']}"
    if admin:
        markup = InlineKeyboardMarkup([
            [btn("🔄 در حال انجام", f"admin:orderstatus:{order_id}:in_progress"), btn("✅ انجام شد", f"admin:orderstatus:{order_id}:done")],
            [btn("❌ رد", f"admin:reject:{order_id}"), btn("🚫 لغو", f"admin:orderstatus:{order_id}:cancelled")],
            [btn("💬 پیام به کاربر", f"admin:ordermsg:{order_id}")],
            [back_button("admin:orders:0")],
        ])
    else:
        markup = InlineKeyboardMarkup([[back_button("user:orders:0")]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def begin_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_has_subscription(update.effective_user.id):
        await update.effective_message.reply_text(get_text("no_subscription"), reply_markup=support_markup())
        return
    today = datetime.now(timezone.utc).date().isoformat()
    limit = int(get_setting("daily_order_limit", "5") or 5)
    count = db_fetchone(
        "SELECT COUNT(*) AS c FROM orders WHERE user_id=? AND substr(created_at,1,10)=?",
        (update.effective_user.id, today),
    )["c"]
    if count >= limit:
        await update.effective_message.reply_text(get_text("daily_limit"))
        return
    context.user_data["state"] = "awaiting_phone"
    await update.effective_message.reply_text(get_text("ask_phone"), reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "user:menu")]]))


def normalize_digits(text: str) -> str:
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return text.translate(translation)


def normalize_phone(text: str) -> Optional[str]:
    value = normalize_digits(text).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if value.startswith("0098"):
        value = "+98" + value[4:]
    elif value.startswith("98"):
        value = "+" + value
    elif value.startswith("09"):
        value = "+98" + value[1:]
    if not re.fullmatch(r"\+989\d{9}", value):
        return None
    return value


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("state") != "awaiting_phone":
        return
    if not user_has_subscription(update.effective_user.id):
        context.user_data.clear()
        await update.effective_message.reply_text(get_text("no_subscription"), reply_markup=support_markup())
        return
    phone = normalize_phone(update.effective_message.text or "")
    if not phone:
        await update.effective_message.reply_text(get_text("invalid_phone"))
        return
    user_id = update.effective_user.id
    daily = int(get_setting("daily_order_limit", "5") or 5)
    today = datetime.now(timezone.utc).date().isoformat()
    count = db_fetchone(
        "SELECT COUNT(*) AS c FROM orders WHERE user_id=? AND substr(created_at,1,10)=?",
        (user_id, today),
    )["c"]
    if count >= daily:
        context.user_data.clear()
        await update.effective_message.reply_text(get_text("daily_limit"))
        return
    order_id = uuid.uuid4().hex[:10].upper()
    user = db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    current = now_iso()
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO orders(id,user_id,username,name,phone,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
            (order_id, user_id, user["username"], display_name(user), phone, current, current),
        )
    context.user_data.clear()
    await update.effective_message.reply_text(
        safe_format(get_text("order_created"), order_id=order_id),
        reply_markup=InlineKeyboardMarkup([[back_button("user:menu")]]),
    )
    log_event(user_id, "order_created", f"order_id={order_id}")
    admin_text = (
        "📥 *سفارش جدید*\n\n"
        f"🆔 Order: `{order_id}`\n"
        f"👤 User ID: `{user_id}`\n"
        f"🔗 Username: @{user['username']}\n" if user["username"] else
        ""
    )
    admin_text += (
        f"👤 Name: {display_name(user)}\n"
        f"📱 Phone: `{phone}`\n"
        f"🕐 Time: {human_dt(current)}\n"
        "📌 Status: `pending`"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("🔄 در حال انجام", f"admin:orderstatus:{order_id}:in_progress"), btn("✅ انجام شد", f"admin:orderstatus:{order_id}:done")],
                [btn("❌ رد", f"admin:reject:{order_id}"), btn("🚫 لغو", f"admin:orderstatus:{order_id}:cancelled")],
            ]),
        )
    except TelegramError:
        logger.exception("Could not notify admin about order")
        log_event(user_id, "admin_notification_failed", f"order_id={order_id}")

# -----------------------------
# Tickets
# -----------------------------

async def create_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = "awaiting_ticket"
    await update.effective_message.reply_text(
        "🎫 پیام خود را برای پشتیبانی بنویسید:",
        reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "user:menu")]]),
    )


async def receive_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("state") != "awaiting_ticket":
        return
    text = (update.effective_message.text or "").strip()
    if not text or len(text) > 4000:
        await update.effective_message.reply_text("❌ متن تیکت باید بین 1 تا 4000 کاراکتر باشد.")
        return
    user_id = update.effective_user.id
    ticket_id = uuid.uuid4().hex[:10].upper()
    current = now_iso()
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO tickets(id,user_id,status,subject,created_at,updated_at) VALUES(?,?, 'open',?,?,?)",
            (ticket_id, user_id, "پشتیبانی", current, current),
        )
        conn.execute(
            "INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) VALUES(?,?,?,?,?)",
            (ticket_id, user_id, "user", text, current),
        )
    context.user_data.clear()
    await update.effective_message.reply_text(
        safe_format(get_text("ticket_created"), ticket_id=ticket_id),
        reply_markup=InlineKeyboardMarkup([[back_button("user:menu")]]),
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"🎫 *تیکت جدید #{ticket_id}*\n\n👤 User ID: `{user_id}`\n📝 {text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("💬 پاسخ", f"admin:ticketreply:{ticket_id}"), btn("✅ بستن", f"admin:ticketclose:{ticket_id}")],
            ]),
        )
    except TelegramError:
        logger.exception("Could not notify admin of ticket")
    log_event(user_id, "ticket_created", f"ticket_id={ticket_id}")

# -----------------------------
# Generic text state handler
# -----------------------------

async def handle_text_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state")
    if state in {"awaiting_phone", "awaiting_ticket"}:
        if state == "awaiting_phone":
            await receive_phone(update, context)
        else:
            await receive_ticket_message(update, context)
        return
    if not is_admin_user(update):
        return
    text = (update.effective_message.text or "").strip()
    if state == "add_user":
        await admin_add_user_from_text(update, context, text)
    elif state == "add_admin":
        await admin_add_admin_from_text(update, context, text)
    elif state in {"addsub", "renewsub"}:
        await admin_subscription_from_text(update, context, text)
    elif state == "admin_message":
        await admin_send_user_message(update, context, text)
    elif state == "reject_order":
        await admin_reject_order_from_text(update, context, text)
    elif state == "order_message":
        await admin_order_message_from_text(update, context, text)
    elif state == "ticket_reply":
        await admin_ticket_reply_from_text(update, context, text)
    elif state == "add_button":
        await admin_add_button_from_text(update, context, text)
    elif state == "edit_button":
        await admin_edit_button_from_text(update, context, text)
    elif state == "delete_button":
        await admin_delete_button_from_text(update, context, text)
    elif state == "reorder_button":
        await admin_reorder_button_from_text(update, context, text)
    elif state == "edit_text":
        await admin_edit_text_from_text(update, context, text)
    elif state == "set_setting":
        await admin_set_setting_from_text(update, context, text)
    elif state == "broadcast":
        await admin_broadcast_from_text(update, context, text)
    elif state == "search_user":
        await admin_search_user_from_text(update, context, text)
    elif state == "search_order":
        await admin_search_order_from_text(update, context, text)

# -----------------------------
# Admin helpers/actions
# -----------------------------

async def admin_add_user_from_text(update, context, text):
    query = text.strip().lstrip("@")
    user_row = None
    if query.isdigit():
        user_row = db_fetchone("SELECT * FROM users WHERE user_id=?", (int(query),))
        uid = int(query)
        if not user_row:
            ensure_user_id(uid)
    else:
        user_row = db_fetchone("SELECT * FROM users WHERE lower(username)=?", (query.lower(),))
        if user_row:
            uid = int(user_row["user_id"])
        else:
            await update.effective_message.reply_text(
                "❌ کاربری با این Username در دیتابیس یافت نشد.\nتوجه: کاربر باید قبلاً حداقل یک‌بار ربات را Start کرده باشد تا اطلاعاتش ثبت شود."
            )
            return

    context.user_data["target_user_id"] = uid
    context.user_data["state"] = "addsub"
    await update.effective_message.reply_text(f"✅ کاربر `{uid}` شناسایی شد. حالا تعداد روز اشتراک را وارد کن.", parse_mode=ParseMode.MARKDOWN)


async def admin_add_admin_from_text(update, context, text):
    query = text.strip().lstrip("@")
    user_row = None
    if query.isdigit():
        uid = int(query)
        user_row = db_fetchone("SELECT * FROM users WHERE user_id=?", (uid,))
    else:
        user_row = db_fetchone("SELECT * FROM users WHERE lower(username)=?", (query.lower(),))
        if user_row:
            uid = int(user_row["user_id"])
        else:
            uid = None

    username = user_row["username"] if user_row and user_row["username"] else (query if not query.isdigit() else "")
    
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admins(user_id, username, added_at) VALUES(?, ?, ?)",
            (uid if uid else 0, username, now_iso())
        )
    context.user_data.clear()
    log_event(ADMIN_ID, "admin_added", f"query={query} uid={uid}")
    await update.effective_message.reply_text(
        f"✅ ادمین جدید با موفقیت اضافه شد.\nتوجه: اگر کاربر هنوز ربات را استارت نکرده باشد، به محض استارت کردن ربات دسترسی ادمین برای او فعال می‌شود.",
        reply_markup=admin_menu_markup()
    )


async def admin_subscription_from_text(update, context, text):
    if not re.fullmatch(r"\d{1,5}", text):
        await update.effective_message.reply_text("❌ تعداد روز باید یک عدد باشد.")
        return
    days = int(text)
    uid = int(context.user_data["target_user_id"])
    renew = context.user_data.get("state") == "renewsub"
    sub = set_subscription(uid, days, renew=renew)
    context.user_data.clear()
    log_event(ADMIN_ID, "subscription_changed", f"user_id={uid} days={days} renew={renew}")
    msg_key = "subscription_renewed" if renew else "subscription_added"
    try:
        await context.bot.send_message(uid, safe_format(get_text(msg_key), expires=human_dt(sub["expires_at"])), reply_markup=user_menu_markup(uid))
    except TelegramError:
        pass
    await update.effective_message.reply_text(
        f"✅ اشتراک کاربر `{uid}` تا {human_dt(sub['expires_at'])} فعال شد.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu_markup(),
    )


async def admin_send_user_message(update, context, text):
    uid = int(context.user_data["target_user_id"])
    context.user_data.clear()
    try:
        await context.bot.send_message(uid, text)
        result = "✅ پیام ارسال شد."
    except TelegramError as exc:
        result = f"❌ ارسال ناموفق بود: {type(exc).__name__}"
    log_event(ADMIN_ID, "admin_message_user", f"user_id={uid}")
    await update.effective_message.reply_text(result, reply_markup=admin_menu_markup())


async def admin_reject_order_from_text(update, context, text):
    order_id = context.user_data["order_id"]
    row = db_fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        context.user_data.clear()
        await update.effective_message.reply_text("❌ سفارش پیدا نشد.")
        return
    current = now_iso()
    db_exec("UPDATE orders SET status='rejected',reason=?,updated_at=?,closed_at=? WHERE id=?", (text[:2000], current, current, order_id))
    context.user_data.clear()
    try:
        await context.bot.send_message(row["user_id"], safe_format(get_text("order_rejected"), order_id=order_id, reason=text[:2000]))
    except TelegramError:
        pass
    log_event(ADMIN_ID, "order_rejected", f"order_id={order_id}")
    await update.effective_message.reply_text("✅ سفارش رد شد.", reply_markup=admin_menu_markup())


async def admin_order_message_from_text(update, context, text):
    order_id = context.user_data["order_id"]
    row = db_fetchone("SELECT * FROM orders WHERE id=?", (order_id,))
    context.user_data.clear()
    if not row:
        await update.effective_message.reply_text("❌ سفارش پیدا نشد.")
        return
    try:
        await context.bot.send_message(row["user_id"], f"💬 پیام درباره سفارش #{order_id}:\n\n{text}")
        result = "✅ پیام ارسال شد."
    except TelegramError as exc:
        result = f"❌ ارسال ناموفق بود: {type(exc).__name__}"
    log_event(ADMIN_ID, "order_message", f"order_id={order_id}")
    await update.effective_message.reply_text(result, reply_markup=admin_menu_markup())


async def admin_ticket_reply_from_text(update, context, text):
    ticket_id = context.user_data["ticket_id"]
    row = db_fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not row:
        context.user_data.clear()
        await update.effective_message.reply_text("❌ تیکت پیدا نشد.")
        return
    current = now_iso()
    with closing(db_connect()) as conn:
        conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) VALUES(?,?,?,?,?)", (ticket_id, ADMIN_ID, "admin", text[:4000], current))
        conn.execute("UPDATE tickets SET status='waiting',updated_at=? WHERE id=?", (current, ticket_id))
    context.user_data.clear()
    try:
        await context.bot.send_message(row["user_id"], f"💬 پاسخ پشتیبانی برای تیکت #{ticket_id}:\n\n{text[:4000]}")
    except TelegramError:
        pass
    log_event(ADMIN_ID, "ticket_replied", f"ticket_id={ticket_id}")
    await update.effective_message.reply_text("✅ پاسخ ارسال شد.", reply_markup=admin_menu_markup())


async def admin_add_button_from_text(update, context, text):
    parts = [p.strip() for p in text.split("|")]
    if len(parts) == 2:
        scope = context.user_data.get("button_scope", "active")
        label, action = parts[0], parts[1]
    elif len(parts) == 3 and parts[0] in {"active", "inactive"}:
        scope, label, action = parts[0], parts[1], parts[2]
    else:
        await update.effective_message.reply_text("❌ فرمت صحیح:\n[active/inactive] | نام دکمه | action\nیا فقط: نام دکمه | action (برای منوی فعال پیش‌فرض)")
        return
    
    with closing(db_connect()) as conn:
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0) FROM buttons WHERE scope=?", (scope,)).fetchone()[0]
        conn.execute("INSERT INTO buttons(scope,label,action,enabled,sort_order) VALUES(?,?,?,1,?)", (scope, label[:100], action[:100], max_order + 1))
    context.user_data.clear()
    log_event(ADMIN_ID, "button_added", f"scope={scope};label={label};action={action}")
    await update.effective_message.reply_text("✅ دکمه اضافه شد.", reply_markup=admin_menu_markup())


async def admin_edit_button_from_text(update, context, text):
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 5 or not parts[0].isdigit() or parts[1] not in {"active", "inactive"} or parts[4] not in {"0", "1"}:
        await update.effective_message.reply_text("❌ فرمت: ID | scope(active/inactive) | label | action | enabled(0/1)")
        return
    bid = int(parts[0])
    row = db_fetchone("SELECT id FROM buttons WHERE id=?", (bid,))
    if not row:
        await update.effective_message.reply_text("❌ دکمه پیدا نشد.")
        return
    db_exec("UPDATE buttons SET scope=?,label=?,action=?,enabled=? WHERE id=?", (parts[1], parts[2][:100], parts[3][:100], int(parts[4]), bid))
    context.user_data.clear()
    log_event(ADMIN_ID, "button_edited", f"button_id={bid}")
    await update.effective_message.reply_text("✅ دکمه ویرایش شد.", reply_markup=admin_menu_markup())


async def admin_delete_button_from_text(update, context, text):
    if not text.isdigit():
        await update.effective_message.reply_text("❌ ID صحیح وارد کن.")
        return
    bid = int(text)
    db_exec("DELETE FROM buttons WHERE id=?", (bid,))
    context.user_data.clear()
    log_event(ADMIN_ID, "button_deleted", f"button_id={bid}")
    await update.effective_message.reply_text("✅ عملیات حذف انجام شد.", reply_markup=admin_menu_markup())


async def admin_reorder_button_from_text(update, context, text):
    parts = [p.strip() for p in text.split("|", 1)]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await update.effective_message.reply_text("❌ فرمت: ID | sort")
        return
    bid, order = map(int, parts)
    db_exec("UPDATE buttons SET sort_order=? WHERE id=?", (order, bid))
    context.user_data.clear()
    log_event(ADMIN_ID, "button_reordered", f"button_id={bid};sort={order}")
    await update.effective_message.reply_text("✅ ترتیب تغییر کرد.", reply_markup=admin_menu_markup())


async def admin_edit_text_from_text(update, context, text):
    key = context.user_data["text_key"]
    if len(text) > 4000:
        await update.effective_message.reply_text("❌ متن خیلی طولانی است.")
        return
    db_exec("INSERT INTO texts(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, text))
    context.user_data.clear()
    log_event(ADMIN_ID, "text_edited", f"key={key}")
    await update.effective_message.reply_text("✅ متن ذخیره شد.", reply_markup=admin_menu_markup())


async def admin_set_setting_from_text(update, context, text):
    key = context.user_data["setting_key"]
    allowed = {"support_username", "expiry_warning_hours", "daily_order_limit", "page_size", "broadcast_delay", "expiry_worker_enabled"}
    if key not in allowed:
        context.user_data.clear()
        await update.effective_message.reply_text("❌ تنظیمات نامعتبر است.")
        return
    if key != "support_username" and key != "broadcast_delay" and not re.fullmatch(r"\d+(\.\d+)?", text):
        await update.effective_message.reply_text("❌ مقدار عددی معتبر وارد کن.")
        return
    set_setting(key, text.strip().lstrip("@"))
    context.user_data.clear()
    log_event(ADMIN_ID, "setting_changed", f"key={key}")
    await update.effective_message.reply_text("✅ تنظیمات ذخیره شد.", reply_markup=admin_menu_markup())


async def admin_broadcast_from_text(update, context, text):
    if len(text) > 4000:
        await update.effective_message.reply_text("❌ متن Broadcast خیلی طولانی است.")
        return
    context.user_data["state"] = "broadcast_confirm"
    context.user_data["broadcast_text"] = text
    await update.effective_message.reply_text(
        f"📢 متن آماده ارسال:\n\n{text}\n\nآیا ارسال شود؟",
        reply_markup=InlineKeyboardMarkup([[btn("✅ بله", "admin:broadcast:confirm"), btn("❌ لغو", "admin:menu")]]),
    )


async def admin_search_user_from_text(update, context, text):
    context.user_data.clear()
    query = text.strip().lstrip("@")
    if query.isdigit():
        row = db_fetchone("SELECT * FROM users WHERE user_id=?", (int(query),))
    else:
        row = db_fetchone("SELECT * FROM users WHERE lower(username)=?", (query.lower(),))
    if not row:
        await update.effective_message.reply_text("❌ کاربر پیدا نشد.", reply_markup=admin_menu_markup())
        return
    await update.effective_message.reply_text(admin_user_detail_text(row), reply_markup=admin_user_actions(row["user_id"]))


async def admin_search_order_from_text(update, context, text):
    context.user_data.clear()
    row = db_fetchone("SELECT * FROM orders WHERE id=?", (text.upper(),))
    if not row:
        await update.effective_message.reply_text("❌ سفارش پیدا نشد.", reply_markup=admin_menu_markup())
        return
    await update.effective_message.reply_text("📦 سفارش پیدا شد.", reply_markup=InlineKeyboardMarkup([[btn("مشاهده", f"admin:order:{row['id']}")], [back_button("admin:orders:0")]]))


def admin_user_detail_text(row: sqlite3.Row) -> str:
    sub = get_active_subscription(row["user_id"])
    orders = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE user_id=?", (row["user_id"],))["c"]
    tickets = db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE user_id=?", (row["user_id"],))["c"]
    return (
        "👤 *مشخصات کاربر*\n\n"
        f"🆔 `{row['user_id']}`\n"
        f"🔗 @{row['username'] or '-'}\n"
        f"👤 {display_name(row)}\n"
        f"📌 وضعیت: {row['status']}\n"
        f"💳 اشتراک: {'فعال' if sub else 'ندارد'}\n"
        f"📅 انقضا: {human_dt(sub['expires_at']) if sub else '-'}\n"
        f"📦 سفارش‌ها: {orders}\n"
        f"🎫 تیکت‌ها: {tickets}\n"
        f"🕐 آخرین فعالیت: {human_dt(row['last_seen'])}"
    )

# -----------------------------
# Admin list pages
# -----------------------------

async def edit_or_reply(update: Update, text: str, markup=None, parse_mode=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)


async def admin_users(update: Update, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM users")["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (size, page * size))
    text = f"👥 *کاربران*\n\nتعداد کل: {total}\n\n" + "\n".join(
        f"• `{r['user_id']}` — @{r['username'] or '-'} — {r['status']}" for r in rows
    )
    kb = [[btn(f"{r['user_id']} (@{r['username'] or 'no_user'})", f"admin:user:{r['user_id']}")] for r in rows]
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:users:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:users:{page+1}"))
    kb.append(nav)
    kb.append([btn("🔎 جستجوی کاربر", "admin:usersearch"), back_button("admin:menu")])
    await edit_or_reply(update, text, InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_admins(update: Update, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM admins")["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall("SELECT * FROM admins ORDER BY added_at DESC LIMIT ? OFFSET ?", (size, page * size))
    text = f"🛡 *مدیریت ادمین‌ها*\n\nتعداد کل: {total}\n\n" + "\n".join(
        f"• `{r['user_id']}` — @{r['username'] or '-'}" for r in rows
    )
    kb = [[btn(f"حذف ادمین {r['user_id']}", f"admin:deladmin:{r['user_id']}")] for r in rows if r['user_id'] != ADMIN_ID]
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:admins:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:admins:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([btn("➕ افزودن ادمین جدید", "admin:addadmin"), back_button("admin:menu")])
    await edit_or_reply(update, text, InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_subscriptions(update: Update, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM subscriptions")["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall("SELECT * FROM subscriptions ORDER BY expires_at DESC LIMIT ? OFFSET ?", (size, page * size))
    text = "💳 *اشتراک‌ها*\n\n" + "\n".join(
        f"• `{r['user_id']}` — {'فعال' if r['active'] and parse_dt(r['expires_at']) and parse_dt(r['expires_at']) > datetime.now(timezone.utc) else 'منقضی'} — {human_dt(r['expires_at'])}" for r in rows
    )
    kb = [[btn(f"کاربر {r['user_id']}", f"admin:user:{r['user_id']}")] for r in rows]
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:subs:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:subs:{page+1}"))
    kb.append(nav)
    kb.append([back_button("admin:menu")])
    await edit_or_reply(update, text, InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_orders(update: Update, page: int, status: Optional[str] = None):
    size = get_page_size()
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status=?"
        params.append(status)
    total = db_fetchone(f"SELECT COUNT(*) AS c FROM orders {where}", tuple(params))["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall(
        f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params + [size, page * size]),
    )
    text = f"📦 *سفارش‌ها* {status or 'همه'}\n\n" + "\n".join(
        f"• `#{r['id']}` — `{r['status']}` — `{r['user_id']}`" for r in rows
    )
    kb = [[btn(f"#{r['id']} | {r['status']}", f"admin:order:{r['id']}")] for r in rows]
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:orders:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:orders:{page+1}"))
    kb.append(nav)
    kb.append([btn("⏳ Pending", "admin:ordersfilter:pending"), btn("🔄 In Progress", "admin:ordersfilter:in_progress")])
    kb.append([btn("✅ Done", "admin:ordersfilter:done"), btn("❌ Rejected", "admin:ordersfilter:rejected")])
    kb.append([btn("📋 همه", "admin:ordersfilter:all"), back_button("admin:menu")])
    await edit_or_reply(update, text or "📦 سفارشی نیست.", InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_tickets(update: Update, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM tickets")["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ? OFFSET ?", (size, page * size))
    text = "🎫 *تیکت‌ها*\n\n" + "\n".join(f"• `#{r['id']}` — `{r['status']}` — `{r['user_id']}`" for r in rows)
    kb = [[btn(f"#{r['id']}", f"admin:ticket:{r['id']}")] for r in rows]
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:tickets:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:tickets:{page+1}"))
    kb.append(nav)
    kb.append([back_button("admin:menu")])
    await edit_or_reply(update, text or "🎫 تیکتی نیست.", InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_logs(update: Update, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) AS c FROM logs")["c"]
    pages = max(1, (total + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = db_fetchall("SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (size, page * size))
    text = "🧾 *لاگ‌ها*\n\n" + "\n".join(f"• {human_dt(r['created_at'])} — `{r['action']}` — {r['details'] or '-'}" for r in rows)
    nav = []
    if page > 0: nav.append(btn("◀️", f"admin:logs:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page < pages - 1: nav.append(btn("▶️", f"admin:logs:{page+1}"))
    kb = [nav, [back_button("admin:menu")]]
    await edit_or_reply(update, text or "🧾 لاگی نیست.", InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)

# -----------------------------
# Stats / settings / buttons / texts
# -----------------------------

async def admin_stats(update: Update):
    total_users = db_fetchone("SELECT COUNT(*) c FROM users")["c"]
    active_users = db_fetchone("SELECT COUNT(*) c FROM users WHERE status='active'")["c"]
    blocked_users = db_fetchone("SELECT COUNT(*) c FROM users WHERE status='blocked'")["c"]
    active_subs = db_fetchone("SELECT COUNT(*) c FROM subscriptions WHERE active=1 AND expires_at>?", (now_iso(),))["c"]
    expired_subs = db_fetchone("SELECT COUNT(*) c FROM subscriptions WHERE expires_at<=?", (now_iso(),))["c"]
    orders = db_fetchone("SELECT COUNT(*) c FROM orders")["c"]
    pending = db_fetchone("SELECT COUNT(*) c FROM orders WHERE status='pending'")["c"]
    done = db_fetchone("SELECT COUNT(*) c FROM orders WHERE status='done'")["c"]
    rejected = db_fetchone("SELECT COUNT(*) c FROM orders WHERE status='rejected'")["c"]
    tickets = db_fetchone("SELECT COUNT(*) c FROM tickets")["c"]
    today = datetime.now(timezone.utc).date().isoformat()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    users_today = db_fetchone("SELECT COUNT(*) c FROM users WHERE substr(joined_at,1,10)=?", (today,))["c"]
    users_week = db_fetchone("SELECT COUNT(*) c FROM users WHERE joined_at>=?", (week,))["c"]
    orders_today = db_fetchone("SELECT COUNT(*) c FROM orders WHERE substr(created_at,1,10)=?", (today,))["c"]
    orders_week = db_fetchone("SELECT COUNT(*) c FROM orders WHERE created_at>=?", (week,))["c"]
    up = int(time.time() - START_TIME)
    uptime = f"{up//86400}d {(up%86400)//3600}h {(up%3600)//60}m"
    text = (
        "📊 *آمار ربات*\n\n"
        f"👥 کاربران: {total_users}\n✅ فعال: {active_users}\n🚫 بلاک: {blocked_users}\n"
        f"💳 اشتراک فعال: {active_subs}\n⛔️ اشتراک منقضی: {expired_subs}\n"
        f"📦 سفارش‌ها: {orders}\n⏳ Pending: {pending}\n✅ Done: {done}\n❌ Rejected: {rejected}\n"
        f"🎫 تیکت‌ها: {tickets}\n\n"
        f"🆕 کاربران امروز: {users_today}\n🗓 کاربران 7 روز اخیر: {users_week}\n"
        f"📦 سفارش امروز: {orders_today}\n📦 سفارش 7 روز اخیر: {orders_week}\n"
        f"⏱ Uptime: {uptime}"
    )
    await edit_or_reply(update, text, InlineKeyboardMarkup([[back_button("admin:menu")]]), ParseMode.MARKDOWN)


async def admin_buttons(update: Update):
    rows = db_fetchall("SELECT * FROM buttons ORDER BY scope, sort_order, id")
    text = "🔘 *مدیریت دکمه‌های ربات*\n\n" + "\n".join(
        f"• ID {r['id']} | [{r['scope']}] | {r['label']} | `{r['action']}` | {'فعال' if r['enabled'] else 'خاموش'} | sort={r['sort_order']}" for r in rows
    )
    kb = [
        [btn("➕ افزودن دکمه", "admin:button:add"), btn("✏️ ویرایش", "admin:button:edit")],
        [btn("🗑 حذف", "admin:button:delete"), btn("↕️ ترتیب", "admin:button:reorder")],
        [back_button("admin:menu")],
    ]
    await edit_or_reply(update, text or "هیچ دکمه‌ای نیست.", InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_texts(update: Update):
    rows = db_fetchall("SELECT key,value FROM texts ORDER BY key")
    text = "✏️ *متن‌ها*\n\n" + "\n".join(f"• `{r['key']}`" for r in rows)
    kb = [[btn(r["key"], f"admin:text:{r['key']}")] for r in rows]
    kb.append([back_button("admin:menu")])
    await edit_or_reply(update, text, InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_settings(update: Update):
    rows = db_fetchall("SELECT key,value FROM settings ORDER BY key")
    text = "⚙️ *تنظیمات*\n\n" + "\n".join(f"• `{r['key']}` = `{r['value']}`" for r in rows)
    kb = [[btn(r["key"], f"admin:setting:{r['key']}")] for r in rows]
    kb.append([back_button("admin:menu")])
    await edit_or_reply(update, text, InlineKeyboardMarkup(kb), ParseMode.MARKDOWN)


async def admin_health(update: Update):
    started = human_dt(datetime.fromtimestamp(START_TIME, tz=timezone.utc).isoformat())
    try:
        integrity = db_fetchone("PRAGMA integrity_check")[0]
        db_ok = integrity == "ok"
    except Exception:
        db_ok = False
        integrity = "error"
    text = (
        "🩺 *وضعیت ربات*\n\n"
        f"🤖 Process: ✅ فعال\n"
        f"💾 SQLite: {'✅' if db_ok else '❌'} ({integrity})\n"
        f"🕐 Start: {started}\n"
        f"⏱ Uptime: {int(time.time()-START_TIME)} sec\n"
        f"🚂 Railway Service: {os.getenv('RAILWAY_SERVICE_NAME','-')}\n"
        f"🌱 Environment: {os.getenv('RAILWAY_ENVIRONMENT_NAME','-')}"
    )
    await edit_or_reply(update, text, InlineKeyboardMarkup([[back_button("admin:menu")]]), ParseMode.MARKDOWN)

# -----------------------------
# Backup / restore
# -----------------------------

def sqlite_checkpoint() -> None:
    with closing(db_connect()) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup() -> Path:
    sqlite_checkpoint()
    created = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = TMP_DIR / f"backup_{created}_{uuid.uuid4().hex[:6]}.zip"
    db_hash = sha256_file(DB_PATH)
    metadata = {
        "app": APP_NAME,
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "database_sha256": db_hash,
    }
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(DB_PATH, arcname="bot.sqlite3")
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return out


def validate_backup_bytes(data: bytes) -> tuple[bool, str]:
    if len(data) > MAX_BACKUP_BYTES:
        return False, "فایل بکاپ بزرگ‌تر از حد مجاز است."
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "bot.sqlite3" not in names or "metadata.json" not in names:
                return False, "ساختار بکاپ ناقص است."
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                return False, "مسیر غیرمجاز داخل ZIP شناسایی شد."
            metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
            if int(metadata.get("schema_version", 0)) > SCHEMA_VERSION:
                return False, "نسخه بکاپ از نسخه این ربات جدیدتر است."
            db_bytes = zf.read("bot.sqlite3")
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, KeyError, ValueError) as exc:
        return False, f"بکاپ نامعتبر است: {type(exc).__name__}"
    temp = TMP_DIR / f"validate_{uuid.uuid4().hex}.sqlite3"
    try:
        temp.write_bytes(db_bytes)
        conn = sqlite3.connect(temp)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        required = {"users", "subscriptions", "orders", "tickets", "ticket_messages", "logs", "buttons", "texts", "settings", "admins"}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        if result != "ok":
            return False, "Database integrity check شکست خورد."
        if not required.issubset(tables):
            return False, "جدول‌های ضروری داخل بکاپ وجود ندارند."
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    expected = metadata.get("database_sha256")
    actual = hashlib.sha256(db_bytes).hexdigest()
    if expected and expected != actual:
        return False, "SHA256 دیتابیس با metadata مطابقت ندارد."
    return True, "OK"


def restore_backup_bytes(data: bytes) -> tuple[bool, str]:
    valid, reason = validate_backup_bytes(data)
    if not valid:
        return False, reason
    extract_dir = Path(tempfile.mkdtemp(prefix="restore_", dir=TMP_DIR))
    emergency = DATA_DIR / f"emergency_before_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.sqlite3"
    old_wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    old_shm = DB_PATH.with_name(DB_PATH.name + "-shm")
    new_db = extract_dir / "bot.sqlite3"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with zf.open("bot.sqlite3") as src, new_db.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        sqlite_checkpoint()
        if DB_PATH.exists():
            shutil.copy2(DB_PATH, emergency)
        for sidecar in (old_wal, old_shm):
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        conn = sqlite3.connect(new_db)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA integrity_check")
        conn.close()
        shutil.copy2(new_db, DB_PATH)
        for sidecar in (DB_PATH.with_name(DB_PATH.name + "-wal"), DB_PATH.with_name(DB_PATH.name + "-shm")):
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        init_db()
        return True, str(emergency) if emergency.exists() else ""
    except Exception as exc:
        logger.exception("Restore failed")
        if emergency.exists():
            try:
                shutil.copy2(emergency, DB_PATH)
                init_db()
            except Exception:
                logger.exception("Emergency restore failed")
        return False, f"Restore failed: {type(exc).__name__}"
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

# -----------------------------
# Callback router
# -----------------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    user_id = q.from_user.id

    if not is_admin_user(update) and user_is_blocked(user_id):
        await q.answer("دسترسی شما مسدود است.", show_alert=True)
        return

    if data == "user:menu":
        context.user_data.clear()
        if user_has_subscription(user_id):
            sub = get_active_subscription(user_id)
            await q.edit_message_text(
                safe_format(
                    get_text("active_welcome"),
                    remaining=human_remaining(sub["expires_at"]),
                    expires=human_dt(sub["expires_at"]),
                ),
                reply_markup=user_menu_markup(user_id)
            )
        else:
            await q.edit_message_text(get_text("no_subscription"), reply_markup=user_menu_markup(user_id))
        return

    if data == "user:noop":
        return

    if data.startswith("user:action:"):
        action = data.split(":", 2)[2]
        if action == "order":
            await begin_order(update, context)
        elif action == "my_orders":
            await show_my_orders(update, 0, edit=True)
        elif action == "account":
            await show_account(update)
        elif action == "support":
            await create_ticket(update, context)
        else:
            await q.edit_message_text(get_text("unknown"), reply_markup=user_menu_markup(user_id))
        return

    if data.startswith("user:orders:"):
        page = int(data.split(":")[2])
        await show_my_orders(update, page, edit=True)
        return

    if data.startswith("user:order:"):
        await show_order_detail(update, data.split(":", 2)[2], admin=False)
        return

    if data.startswith("admin:"):
        if not is_admin_user(update):
            await q.answer("دسترسی غیرمجاز", show_alert=True)
            return
        await admin_callback(update, context, data)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    q = update.callback_query
    parts = data.split(":")
    cmd = parts[1] if len(parts) > 1 else ""

    if data == "admin:menu":
        context.user_data.clear()
        await q.edit_message_text("🛠 *پنل مدیریت*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_markup())
        return
    if data == "admin:noop":
        return
    if data == "admin:stats":
        await admin_stats(update)
        return
    if data == "admin:health":
        await admin_health(update)
        return
    if data == "admin:buttons":
        await admin_buttons(update)
        return
    if data == "admin:texts":
        await admin_texts(update)
        return
    if data == "admin:settings":
        await admin_settings(update)
        return
    if data == "admin:backup":
        await q.edit_message_text(
            "💾 *مدیریت Backup*\n\nدانلود: از دیتابیس فعلی ZIP ساخته می‌شود.\nآپلود: پس از Validation و تأیید نهایی Restore می‌شود.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("⬇️ دانلود بکاپ", "admin:backup:download")],
                [btn("⬆️ آپلود بکاپ", "admin:backup:upload")],
                [back_button("admin:menu")],
            ]),
        )
        return
    if data == "admin:backup:download":
        await q.edit_message_text("⏳ در حال ساخت بکاپ...")
        path = None
        try:
            path = await asyncio.to_thread(create_backup)
            with path.open("rb") as f:
                await context.bot.send_document(
                    ADMIN_ID,
                    document=f,
                    filename=path.name,
                    caption="💾 بکاپ کامل دیتابیس و تنظیمات ربات",
                )
            log_event(ADMIN_ID, "backup_created", path.name)
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
        await q.edit_message_text("✅ بکاپ ارسال شد.", reply_markup=InlineKeyboardMarkup([[back_button("admin:backup")]]))
        return
    if data == "admin:backup:upload":
        context.user_data["state"] = "awaiting_backup_document"
        await q.edit_message_text("📤 فایل ZIP بکاپ را به صورت Document ارسال کن.\n\nحجم مجاز محدود است.", reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "admin:menu")]]))
        return

    if data == "admin:adduser":
        context.user_data["state"] = "add_user"
        await q.edit_message_text("➕ Username یا ID عددی کاربر را ارسال کن:", reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "admin:menu")]]))
        return

    if data == "admin:addadmin":
        context.user_data["state"] = "add_admin"
        await q.edit_message_text("🛡 Username یا ID عددی شخص موردنظر را جهت افزودن به ادمین‌ها ارسال کن:", reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "admin:menu")]]))
        return

    if data.startswith("admin:deladmin:"):
        target_admin_id = int(parts[2])
        if target_admin_id == ADMIN_ID:
            await q.answer("امکان حذف ادمین اصلی وجود ندارد.", show_alert=True)
            return
        db_exec("DELETE FROM admins WHERE user_id=?", (target_admin_id,))
        log_event(ADMIN_ID, "admin_removed", f"admin_id={target_admin_id}")
        await admin_admins(update, 0)
        return

    if data == "admin:restore:confirm":
        path = context.user_data.get("restore_path")
        context.user_data.clear()
        if not path or not Path(path).exists():
            await q.edit_message_text("❌ فایل موقت Restore پیدا نشد.", reply_markup=admin_menu_markup())
            return
        try:
            data_bytes = Path(path).read_bytes()
            ok, info = await asyncio.to_thread(restore_backup_bytes, data_bytes)
            if ok:
                log_event(ADMIN_ID, "backup_restored", "restore completed")
                await q.edit_message_text("✅ Restore با موفقیت انجام شد.", reply_markup=admin_menu_markup())
            else:
                log_event(ADMIN_ID, "backup_restore_failed", info)
                await q.edit_message_text(f"❌ Restore ناموفق بود.\n{info}", reply_markup=admin_menu_markup())
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        return

    if data == "admin:restore:cancel":
        path = context.user_data.pop("restore_path", None)
        context.user_data.clear()
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        await q.edit_message_text("✅ Restore لغو شد.", reply_markup=admin_menu_markup())
        return

    if data == "admin:broadcast":
        context.user_data["state"] = "broadcast"
        await q.edit_message_text("📢 متن Broadcast را ارسال کن:", reply_markup=InlineKeyboardMarkup([[btn("❌ لغو", "admin:menu")]]))
        return
    if data == "admin:broadcast:confirm":
        await run_broadcast(update, context)
        return

    if data == "admin:users:0" or (cmd == "users" and len(parts) == 3):
        await admin_users(update, int(parts[2]))
        return
    if data == "admin:admins:0" or (cmd == "admins" and len(parts) == 3):
        await admin_admins(update, int(parts[2]))
        return
    if cmd == "usersearch":
        context.user_data["state"] = "search_user"
        await q.edit_message_text("🔎 ID یا Username کاربر را ارسال کن:")
        return
    if cmd == "user" and len(parts) == 3:
        row = db_fetchone("SELECT * FROM users WHERE user_id=?", (int(parts[2]),))
        if not row:
            await q.edit_message_text("❌ کاربر پیدا نشد.", reply_markup=InlineKeyboardMarkup([[back_button("admin:users:0")]]))
            return
        await q.edit_message_text(admin_user_detail_text(row), parse_mode=ParseMode.MARKDOWN, reply_markup=admin_user_actions(row["user_id"]))
        return
    if cmd in {"addsub", "renew"}:
        uid = int(parts[2])
        ensure_user_id(uid)
        context.user_data["target_user_id"] = uid
        context.user_data["state"] = "addsub" if cmd == "addsub" else "renewsub"
        await q.edit_message_text("📅 تعداد روز اشتراک را وارد کن:")
        return
    if cmd == "remsub":
        uid = int(parts[2])
        remove_subscription(uid)
        log_event(ADMIN_ID, "subscription_removed", f"user_id={uid}")
        try:
            await context.bot.send_message(uid, get_text("subscription_expired"), reply_markup=user_menu_markup(uid))
        except TelegramError:
            pass
        await q.edit_message_text("✅ اشتراک حذف شد.", reply_markup=admin_user_actions(uid))
        return
    if cmd == "msg":
        uid = int(parts[2])
        context.user_data["target_user_id"] = uid
        context.user_data["state"] = "admin_message"
        await q.edit_message_text("💬 متن پیام را ارسال کن:")
        return
    if cmd == "toggleblock":
        uid = int(parts[2])
        row = db_fetchone("SELECT status FROM users WHERE user_id=?", (uid,))
        if not row:
            await q.edit_message_text("❌ کاربر پیدا نشد.")
            return
        new_status = "blocked" if row["status"] != "blocked" else "active"
        db_exec("UPDATE users SET status=?,last_seen=? WHERE user_id=?", (new_status, now_iso(), uid))
        log_event(ADMIN_ID, "user_status_changed", f"user_id={uid};status={new_status}")
        row2 = db_fetchone("SELECT * FROM users WHERE user_id=?", (uid,))
        await q.edit_message_text(admin_user_detail_text(row2), parse_mode=ParseMode.MARKDOWN, reply_markup=admin_user_actions(uid))
        return
    if cmd == "userorders":
        uid, page = int(parts[2]), int(parts[3])
        await admin_user_orders(update, uid, page)
        return
    if cmd == "usertickets":
        uid, page = int(parts[2]), int(parts[3])
        await admin_user_tickets(update, uid, page)
        return
    if cmd == "subs":
        await admin_subscriptions(update, int(parts[2]))
        return
    if cmd == "orders":
        await admin_orders(update, int(parts[2]))
        return
    if cmd == "ordersfilter":
        status = None if parts[2] == "all" else parts[2]
        await admin_orders(update, 0, status)
        return
    if cmd == "order":
        await show_order_detail(update, parts[2], admin=True)
        return
    if cmd == "orderstatus":
        order_id, status = parts[2], parts[3]
        await change_order_status(update, context, order_id, status)
        return
    if cmd == "reject":
        order_id = parts[2]
        context.user_data["order_id"] = order_id
        context.user_data["state"] = "reject_order"
        await q.edit_message_text("📝 دلیل رد سفارش را ارسال کن:")
        return
    if cmd == "ordermsg":
        context.user_data["order_id"] = parts[2]
        context.user_data["state"] = "order_message"
        await q.edit_message_text("💬 پیام مربوط به سفارش را ارسال کن:")
        return
    if cmd == "tickets":
        await admin_tickets(update, int(parts[2]))
        return
    if cmd == "ticket":
        await admin_ticket_detail(update, parts[2])
        return
    if cmd == "ticketreply":
        context.user_data["ticket_id"] = parts[2]
        context.user_data["state"] = "ticket_reply"
        await q.edit_message_text("💬 پاسخ تیکت را ارسال کن:")
        return
    if cmd == "ticketclose":
        await close_ticket(update, context, parts[2])
        return
    if cmd == "logs":
        await admin_logs(update, int(parts[2]))
        return
    if cmd == "button":
        action = parts[2]
        if action == "add":
            context.user_data["state"] = "add_button"
            await q.edit_message_text("➕ فرمت:\n[active/inactive] | نام دکمه | action\n(پیش‌فرض منوی فعال است)")
        elif action == "edit":
            context.user_data["state"] = "edit_button"
            await q.edit_message_text("✏️ فرمت: ID | scope(active/inactive) | label | action | enabled(0/1)")
        elif action == "delete":
            context.user_data["state"] = "delete_button"
            await q.edit_message_text("🗑 ID دکمه را ارسال کن:")
        elif action == "reorder":
            context.user_data["state"] = "reorder_button"
            await q.edit_message_text("↕️ فرمت: ID | sort")
        return
    if cmd == "text":
        key = ":".join(parts[2:])
        current = get_text(key)
        context.user_data["text_key"] = key
        context.user_data["state"] = "edit_text"
        await q.edit_message_text(f"✏️ متن فعلی `{key}`:\n\n{current}\n\nمتن جدید را ارسال کن.", parse_mode=ParseMode.MARKDOWN)
        return
    if cmd == "setting":
        key = ":".join(parts[2:])
        context.user_data["setting_key"] = key
        context.user_data["state"] = "set_setting"
        await q.edit_message_text(f"⚙️ مقدار جدید برای `{key}` را ارسال کن.\nمقدار فعلی: `{get_setting(key)}`", parse_mode=ParseMode.MARKDOWN)
        return
    await q.edit_message_text("❓ عملیات ناشناخته است.", reply_markup=admin_menu_markup())

# -----------------------------
# Admin additional pages
# -----------------------------

async def admin_user_orders(update, user_id: int, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=?", (user_id,))["c"]
    pages = max(1, (total + size - 1)//size)
    page = max(0, min(page, pages-1))
    rows = db_fetchall("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (user_id,size,page*size))
    text = f"📦 سفارش‌های کاربر `{user_id}`\n\n" + "\n".join(f"• #{r['id']} — {r['status']}" for r in rows)
    kb = [[btn(f"#{r['id']}", f"admin:order:{r['id']}")] for r in rows]
    nav=[]
    if page>0: nav.append(btn("◀️", f"admin:userorders:{user_id}:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}","admin:noop"))
    if page<pages-1: nav.append(btn("▶️", f"admin:userorders:{user_id}:{page+1}"))
    kb.append(nav); kb.append([back_button(f"admin:user:{user_id}")])
    await edit_or_reply(update,text,InlineKeyboardMarkup(kb),ParseMode.MARKDOWN)


async def admin_user_tickets(update, user_id: int, page: int):
    size = get_page_size()
    total = db_fetchone("SELECT COUNT(*) c FROM tickets WHERE user_id=?", (user_id,))["c"]
    pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1))
    rows=db_fetchall("SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(user_id,size,page*size))
    text=f"🎫 تیکت‌های کاربر `{user_id}`\n\n"+"\n".join(f"• #{r['id']} — {r['status']}" for r in rows)
    kb=[[btn(f"#{r['id']}",f"admin:ticket:{r['id']}")] for r in rows]
    nav=[]
    if page>0: nav.append(btn("◀️",f"admin:usertickets:{user_id}:{page-1}"))
    nav.append(btn(f"{page+1}/{pages}","admin:noop"))
    if page<pages-1: nav.append(btn("▶️",f"admin:usertickets:{user_id}:{page+1}"))
    kb.append(nav); kb.append([back_button(f"admin:user:{user_id}")])
    await edit_or_reply(update,text,InlineKeyboardMarkup(kb),ParseMode.MARKDOWN)


async def admin_ticket_detail(update, ticket_id: str):
    row = db_fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not row:
        await update.callback_query.edit_message_text("❌ تیکت پیدا نشد.", reply_markup=InlineKeyboardMarkup([[back_button("admin:tickets:0")]]))
        return
    msgs=db_fetchall("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC LIMIT 30",(ticket_id,))
    text=f"🎫 *تیکت #{ticket_id}*\n\n👤 User: `{row['user_id']}`\n📌 Status: `{row['status']}`\n\n"
    for m in msgs:
        who="کاربر" if m["sender_role"]=="user" else "ادمین"
        text += f"*{who}* — {human_dt(m['created_at'])}\n{m['text']}\n\n"
    kb=[[btn("💬 پاسخ",f"admin:ticketreply:{ticket_id}")]]
    if row["status"] != "closed": kb.append([btn("✅ بستن",f"admin:ticketclose:{ticket_id}")])
    kb.append([back_button("admin:tickets:0")])
    await update.callback_query.edit_message_text(text,parse_mode=ParseMode.MARKDOWN,reply_markup=InlineKeyboardMarkup(kb))


async def close_ticket(update, context, ticket_id: str):
    row=db_fetchone("SELECT * FROM tickets WHERE id=?",(ticket_id,))
    if not row:
        await update.callback_query.edit_message_text("❌ تیکت پیدا نشد.")
        return
    current=now_iso()
    db_exec("UPDATE tickets SET status='closed',updated_at=?,closed_at=? WHERE id=?",(current,current,ticket_id))
    try:
        await context.bot.send_message(row["user_id"],safe_format(get_text("ticket_closed"),ticket_id=ticket_id))
    except TelegramError:
        pass
    log_event(ADMIN_ID,"ticket_closed",f"ticket_id={ticket_id}")
    await update.callback_query.edit_message_text("✅ تیکت بسته شد.",reply_markup=InlineKeyboardMarkup([[back_button("admin:tickets:0")]]))


async def change_order_status(update, context, order_id: str, status: str):
    allowed={"in_progress":"order_in_progress","done":"order_done","cancelled":"order_cancelled"}
    if status not in allowed:
        return
    row=db_fetchone("SELECT * FROM orders WHERE id=?",(order_id,))
    if not row:
        await update.callback_query.edit_message_text("❌ سفارش پیدا نشد.")
        return
    current=now_iso(); closed=current if status in {"done","cancelled"} else None
    db_exec("UPDATE orders SET status=?,updated_at=?,closed_at=? WHERE id=?",(status,current,closed,order_id))
    try:
        await context.bot.send_message(row["user_id"],safe_format(get_text(allowed[status]),order_id=order_id))
    except TelegramError:
        pass
    log_event(ADMIN_ID,"order_status_changed",f"order_id={order_id};status={status}")
    await update.callback_query.edit_message_text("✅ وضعیت سفارش تغییر کرد.",reply_markup=InlineKeyboardMarkup([[back_button("admin:orders:0")]]))

# -----------------------------
# Backup document handler
# -----------------------------

async def backup_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin_user(update):
        return
    if context.user_data.get("state") != "awaiting_backup_document":
        return
    document = update.effective_message.document
    if not document:
        return
    if document.file_size and document.file_size > MAX_BACKUP_BYTES:
        await update.effective_message.reply_text("❌ فایل بکاپ بیش از حد مجاز بزرگ است.")
        return
    tg_file = await document.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    valid, reason = await asyncio.to_thread(validate_backup_bytes, data)
    if not valid:
        context.user_data.clear()
        await update.effective_message.reply_text(f"❌ بکاپ رد شد.\n{reason}", reply_markup=admin_menu_markup())
        return
    tmp_path = TMP_DIR / f"uploaded_{uuid.uuid4().hex}.zip"
    tmp_path.write_bytes(data)
    context.user_data["state"] = "restore_confirm"
    context.user_data["restore_path"] = str(tmp_path)
    await update.effective_message.reply_text(
        "⚠️ *تأیید Restore*\n\nاین عملیات دیتابیس فعلی را با نسخه بکاپ جایگزین می‌کند.\nیک Emergency Backup از دیتابیس فعلی قبل از Restore ساخته می‌شود.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[btn("✅ تأیید Restore", "admin:restore:confirm"), btn("❌ لغو", "admin:restore:cancel")]]),
    )

# -----------------------------
# Broadcast
# -----------------------------

async def run_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text=context.user_data.get("broadcast_text")
    if not text:
        context.user_data.clear()
        await update.callback_query.edit_message_text("❌ متنی برای ارسال وجود ندارد.",reply_markup=admin_menu_markup())
        return
    users=db_fetchall("SELECT user_id FROM users WHERE status='active' ORDER BY user_id")
    sent=0; failed=0
    delay=float(get_setting("broadcast_delay","0.08") or 0.08)
    await update.callback_query.edit_message_text("📢 Broadcast شروع شد...")
    for row in users:
        try:
            await context.bot.send_message(row["user_id"],text[:4000])
            sent+=1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after)+0.2)
            try:
                await context.bot.send_message(row["user_id"],text[:4000]); sent+=1
            except TelegramError: failed+=1
        except (Forbidden, BadRequest, TelegramError):
            failed+=1
        if delay>0:
            await asyncio.sleep(delay)
    context.user_data.clear()
    log_event(ADMIN_ID,"broadcast",f"sent={sent};failed={failed}")
    await update.callback_query.edit_message_text(f"✅ Broadcast تمام شد.\n\nارسال موفق: {sent}\nناموفق: {failed}",reply_markup=admin_menu_markup())

# -----------------------------
# Expiry worker
# -----------------------------

async def expiry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_setting("expiry_worker_enabled", "1") != "1":
        return
    now = datetime.now(timezone.utc)
    expired_ids = deactivate_expired_subscriptions()
    for uid in expired_ids:
        if user_has_subscription(uid):
            continue
        expired = db_fetchall("SELECT * FROM subscriptions WHERE user_id=? AND active=0 AND expires_at<=? AND expired_notified=0 ORDER BY expires_at DESC", (uid, now_iso()))
        if not expired:
            continue
        target = expired[0]
        updated = db_exec("UPDATE subscriptions SET expired_notified=1 WHERE id=? AND expired_notified=0", (target["id"],))
        if updated.rowcount != 1:
            continue
        try:
            await context.bot.send_message(uid, get_text("subscription_expired"), reply_markup=user_menu_markup(uid))
        except TelegramError:
            pass
        log_event(uid, "subscription_expired")
    try:
        warn_hours = int(get_setting("expiry_warning_hours", "24") or 24)
    except ValueError:
        warn_hours = 24
    horizon = (now + timedelta(hours=max(1, warn_hours))).isoformat()
    rows = db_fetchall(
        """
        SELECT * FROM subscriptions
        WHERE active=1 AND warning_sent=0 AND expires_at>? AND expires_at<=?
        """,
        (now.isoformat(), horizon),
    )
    for row in rows:
        updated = db_exec("UPDATE subscriptions SET warning_sent=1,updated_at=? WHERE id=? AND warning_sent=0", (now_iso(), row["id"]))
        if updated.rowcount != 1:
            continue
        try:
            await context.bot.send_message(uid := row["user_id"], safe_format(get_text("subscription_warning"), remaining=human_remaining(row["expires_at"])))
        except TelegramError:
            pass
        log_event(uid, "subscription_warning")

# -----------------------------
# Error handler
# -----------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)
    if isinstance(context.error, RetryAfter):
        return
    try:
        if isinstance(update, Update) and update.effective_message and update.effective_user and not is_admin_user(update):
            await update.effective_message.reply_text("⚠️ یک خطای موقت رخ داد. دوباره تلاش کنید.")
    except Exception:
        pass

# -----------------------------
# Main
# -----------------------------

async def post_init(app: Application) -> None:
    init_db()
    if app.job_queue is not None:
        app.job_queue.run_repeating(expiry_job, interval=EXPIRY_CHECK_SEC, first=5, name="expiry_worker")
    logger.info("Bot initialized. DB=%s", DB_PATH)


async def post_shutdown(app: Application) -> None:
    logger.info("Bot shutdown complete")


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, backup_document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_state))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    init_db()
    logger.info("Starting %s on Railway-compatible polling runtime", APP_NAME)
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, close_loop=False)


if __name__ == "__main__":
    main()
