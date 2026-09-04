import asyncio
import html
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

APP_NAME = "Railway Telegram Bot"
SCHEMA_VERSION = 5
START_TIME = time.time()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@").lower()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", ADMIN_USERNAME).strip().lstrip("@").lower()
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).expanduser().resolve()
DB_NAME = os.getenv("DB_NAME", "bot.sqlite3").strip() or "bot.sqlite3"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
try:
    MAX_BACKUP_BYTES = max(1, int(float(os.getenv("BACKUP_MAX_MB", "25")) * 1024 * 1024))
except ValueError:
    MAX_BACKUP_BYTES = 25 * 1024 * 1024
try:
    EXPIRY_CHECK_SEC = max(30, int(os.getenv("EXPIRY_CHECK_SEC", "60")))
except ValueError:
    EXPIRY_CHECK_SEC = 60
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be numeric") from exc
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is missing or invalid. Set it in Railway Variables.")

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR = DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / DB_NAME

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("telegram_bot")

ALLOWED_ACTIONS = {
    "order", "my_orders", "my_tickets", "account", "support", "support_link",
    "users", "adduser", "admins", "addadmin", "subs", "orders", "tickets",
    "stats", "logs", "buttons", "texts", "broadcast", "backup", "settings", "health",
}
SCOPES = {"active", "guest", "admin"}
ORDER_STATUSES = {"pending", "in_progress", "done", "rejected", "cancelled"}
TICKET_STATUSES = {"open", "waiting", "closed"}
ADMIN_ROLES = {"owner", "admin"}

DEFAULT_TEXTS = {
    "welcome": "👋 سلام {name}\n\nبه ربات خوش اومدی.",
    "no_subscription": "⛔️ شما اشتراک فعالی ندارید.\nبرای دریافت راهنمایی با پشتیبانی در ارتباط باشید.",
    "active_welcome": "✅ اشتراک شما فعاله.\n\n⏳ زمان باقی‌مانده: {remaining}\n📅 انقضا: {expires}",
    "ask_phone": "📱 شماره موبایل ایران را ارسال کنید.\nمثال: +989123456789",
    "invalid_phone": "❌ شماره واردشده معتبر نیست.\nفرمت‌های مجاز: 09xxxxxxxxx / 98xxxxxxxxxx / 0098xxxxxxxxxx / +98xxxxxxxxxx",
    "order_created": "✅ سفارش ثبت شد.\n🆔 شماره سفارش: {order_id}\n📌 وضعیت: pending",
    "order_done": "✅ سفارش #{order_id} انجام شد.",
    "order_rejected": "❌ سفارش #{order_id} رد شد.\n\n📝 دلیل: {reason}",
    "order_cancelled": "🚫 سفارش #{order_id} لغو شد.",
    "order_in_progress": "🔄 سفارش #{order_id} وارد مرحله انجام شد.",
    "ticket_created": "🎫 تیکت #{ticket_id} ثبت شد.",
    "ticket_closed": "✅ تیکت #{ticket_id} بسته شد.",
    "subscription_added": "🎉 اشتراک فعال شد.\n📅 انقضا: {expires}",
    "subscription_renewed": "🔄 اشتراک تمدید شد.\n📅 انقضا: {expires}",
    "subscription_expired": "⛔️ اشتراک شما منقضی شده است.",
    "subscription_warning": "⚠️ اشتراک شما به‌زودی منقضی می‌شود.\n⏳ زمان باقی‌مانده: {remaining}",
    "blocked": "🚫 دسترسی شما به ربات مسدود شده است.",
    "daily_limit": "⚠️ سقف سفارش روزانه شما تکمیل شده است.",
    "unknown": "❓ این درخواست قابل پردازش نیست.",
}

DEFAULT_BUTTONS = [
    ("active", "🛒 خرید", "order", 1),
    ("active", "📦 سفارش‌های من", "my_orders", 2),
    ("active", "👤 حساب من", "account", 3),
    ("active", "💬 پشتیبانی", "support", 4),
    ("active", "🎫 تیکت‌های من", "my_tickets", 5),
    ("guest", "💬 پشتیبانی", "support", 1),
    ("admin", "👥 کاربران", "users", 1),
    ("admin", "➕ افزودن کاربر", "adduser", 2),
    ("admin", "👑 ادمین‌ها", "admins", 3),
    ("admin", "➕ افزودن ادمین", "addadmin", 4),
    ("admin", "📦 اشتراک‌ها", "subs", 5),
    ("admin", "🛒 سفارش‌ها", "orders", 6),
    ("admin", "🎫 تیکت‌ها", "tickets", 7),
    ("admin", "📊 آمار", "stats", 8),
    ("admin", "📜 لاگ‌ها", "logs", 9),
    ("admin", "🔘 مدیریت دکمه‌ها", "buttons", 10),
    ("admin", "📝 مدیریت متن‌ها", "texts", 11),
    ("admin", "📢 ارسال همگانی", "broadcast", 12),
    ("admin", "💾 Backup", "backup", 13),
    ("admin", "⚙️ تنظیمات", "settings", 14),
    ("admin", "❤️ Health", "health", 15),
]
DEFAULT_SETTINGS = {
    "support_username": SUPPORT_USERNAME,
    "expiry_warning_hours": "24",
    "daily_order_limit": "5",
    "page_size": "8",
    "broadcast_delay": "0.08",
    "expiry_worker_enabled": "1",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def human_dt(value: Optional[str]) -> str:
    dt = parse_dt(value)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


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


def normalize_username(value: str) -> Optional[str]:
    value = (value or "").strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", value):
        return None
    return value


def normalize_digits(value: str) -> str:
    return (value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def normalize_phone(value: str) -> Optional[str]:
    value = normalize_digits(value).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if value.startswith("0098"):
        value = "+98" + value[4:]
    elif value.startswith("98"):
        value = "+" + value
    elif value.startswith("09"):
        value = "+98" + value[1:]
    return value if re.fullmatch(r"\+989\d{9}", value) else None


def safe_format(template: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except Exception:
        return template


def display_name(row: sqlite3.Row | dict) -> str:
    first = (row["first_name"] if isinstance(row, sqlite3.Row) else row.get("first_name")) or ""
    last = (row["last_name"] if isinstance(row, sqlite3.Row) else row.get("last_name")) or ""
    return f"{first} {last}".strip() or "بدون نام"


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_exec(sql: str, params: tuple = ()) -> dict[str, Any]:
    """Execute a mutation while keeping cursor lifetime inside the open connection."""
    with closing(db_connect()) as conn:
        cur = conn.execute(sql, params)
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}


def db_fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchone()


def db_fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(db_connect()) as conn:
        return conn.execute(sql, params).fetchall()


def init_db() -> None:
    with closing(db_connect()) as conn:
        old_version = int((conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() or [0])[0]) if _table_exists(conn, "meta") else 0
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            status TEXT NOT NULL DEFAULT 'active', joined_at TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_user_grants(
            username TEXT PRIMARY KEY, days INTEGER NOT NULL, created_at TEXT NOT NULL, created_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY, username TEXT, role TEXT NOT NULL DEFAULT 'admin',
            enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_admin_grants(
            username TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, created_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, warning_sent INTEGER NOT NULL DEFAULT 0,
            expired_notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT, name TEXT, phone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', reason TEXT, admin_note TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, closed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS tickets(
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', subject TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS ticket_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, sender_id INTEGER NOT NULL,
            sender_role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buttons(
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL DEFAULT 'guest', label TEXT NOT NULL,
            action TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(scope,label)
        );
        CREATE TABLE IF NOT EXISTS texts(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
        CREATE INDEX IF NOT EXISTS idx_subs_user_active ON subscriptions(user_id,active);
        CREATE INDEX IF NOT EXISTS idx_subs_expire ON subscriptions(expires_at,active);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
        """)
        _ensure_column(conn, "users", "last_seen", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "buttons", "scope", "TEXT NOT NULL DEFAULT 'guest'")
        if old_version < 4:
            conn.execute("UPDATE buttons SET scope='active' WHERE scope='user'")
        for key, value in DEFAULT_TEXTS.items():
            conn.execute("INSERT OR IGNORE INTO texts(key,value) VALUES(?,?)", (key, value))
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        for scope, label, action, order in DEFAULT_BUTTONS:
            if action in ALLOWED_ACTIONS:
                conn.execute("INSERT OR IGNORE INTO buttons(scope,label,action,enabled,sort_order) VALUES(?,?,?,1,?)", (scope,label,action,order))
        # Sanitize legacy rows without deleting them.
        conn.execute("UPDATE buttons SET enabled=0 WHERE action NOT IN ({})".format(",".join("?" * len(ALLOWED_ACTIONS))), tuple(ALLOWED_ACTIONS))
        # Owner bootstrap is deterministic and cannot be deleted through normal admin UI.
        now = now_iso()
        conn.execute("""INSERT INTO admins(user_id,username,role,enabled,created_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET role='owner',enabled=1,username=COALESCE(NULLIF(excluded.username,''),admins.username)""",
                     (ADMIN_ID, ADMIN_USERNAME, "owner", 1, now))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_setting(key: str, default: str = "") -> str:
    row = db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    db_exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_text(key: str) -> str:
    row = db_fetchone("SELECT value FROM texts WHERE key=?", (key,))
    return str(row["value"]) if row else DEFAULT_TEXTS.get(key, key)


def log_event(user_id: Optional[int], action: str, details: str = "") -> None:
    # Never accept or intentionally log secrets. Callers pass IDs/statuses only.
    try:
        db_exec("INSERT INTO logs(user_id,action,details,created_at) VALUES(?,?,?,?)", (user_id, action, str(details)[:2000], now_iso()))
    except Exception:
        logger.exception("Audit log write failed")


def ensure_user(tg_user) -> None:
    now = now_iso()
    username = normalize_username(tg_user.username or "") or ""
    with closing(db_connect()) as conn:
        conn.execute("""INSERT INTO users(user_id,username,first_name,last_name,status,joined_at,last_seen)
                       VALUES(?,?,?,?,'active',?,?)
                       ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,
                       last_name=excluded.last_name,last_seen=excluded.last_seen""",
                     (int(tg_user.id), username, tg_user.first_name or "", tg_user.last_name or "", now, now))
    apply_pending_grants(int(tg_user.id), username)
    apply_pending_admin_grant(int(tg_user.id), username)


def ensure_user_id(user_id: int, username: str = "") -> None:
    now = now_iso()
    db_exec("INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,status,joined_at,last_seen) VALUES(?,?,?,'','active',?,?)", (int(user_id), normalize_username(username or "") or "", "", now, now))


def apply_pending_grants(user_id: int, username: str) -> None:
    if not username:
        return
    row = db_fetchone("SELECT days FROM pending_user_grants WHERE username=?", (username,))
    if not row:
        return
    days = max(1, int(row["days"]))
    set_subscription(user_id, days, renew=False)
    db_exec("DELETE FROM pending_user_grants WHERE username=?", (username,))
    log_event(user_id, "pending_subscription_applied", f"days={days}")


def apply_pending_admin_grant(user_id: int, username: str) -> None:
    if not username:
        return
    row = db_fetchone("SELECT role FROM pending_admin_grants WHERE username=?", (username,))
    if not row:
        return
    role = row["role"] if row["role"] in ADMIN_ROLES else "admin"
    db_exec("INSERT INTO admins(user_id,username,role,enabled,created_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,role=excluded.role,enabled=1", (user_id, username, role, 1, now_iso()))
    db_exec("DELETE FROM pending_admin_grants WHERE username=?", (username,))
    log_event(user_id, "pending_admin_applied", f"role={role}")


def user_is_blocked(user_id: int) -> bool:
    row = db_fetchone("SELECT status FROM users WHERE user_id=?", (int(user_id),))
    return bool(row and row["status"] == "blocked")


def is_admin_id(user_id: int) -> bool:
    if int(user_id) == ADMIN_ID:
        return True
    row = db_fetchone("SELECT 1 FROM admins WHERE user_id=? AND enabled=1 AND role IN ('owner','admin')", (int(user_id),))
    return bool(row)


def is_admin_user(update: Update) -> bool:
    user = update.effective_user
    return bool(user and is_admin_id(user.id))


def can_manage_admins(user_id: int) -> bool:
    row = db_fetchone("SELECT role FROM admins WHERE user_id=? AND enabled=1", (int(user_id),))
    return int(user_id) == ADMIN_ID or bool(row and row["role"] in ADMIN_ROLES)


def get_active_subscription(user_id: int) -> Optional[sqlite3.Row]:
    return db_fetchone("SELECT * FROM subscriptions WHERE user_id=? AND active=1 AND expires_at>? ORDER BY expires_at DESC LIMIT 1", (int(user_id), now_iso()))


def user_has_subscription(user_id: int) -> bool:
    return get_active_subscription(user_id) is not None


def deactivate_expired_subscriptions() -> list[int]:
    current = now_iso()
    rows = db_fetchall("SELECT id,user_id FROM subscriptions WHERE active=1 AND expires_at<=?", (current,))
    if rows:
        db_exec("UPDATE subscriptions SET active=0,updated_at=? WHERE active=1 AND expires_at<=?", (current,current))
    return [int(r["user_id"]) for r in rows]


def set_subscription(user_id: int, days: int, renew: bool = False) -> sqlite3.Row:
    days = max(1, int(days))
    ensure_user_id(user_id)
    current = datetime.now(timezone.utc)
    active = get_active_subscription(user_id)
    if renew and active:
        base = parse_dt(active["expires_at"]) or current
        start = max(base, current)
    else:
        start = current
    expires = start + timedelta(days=days)
    now = now_iso()
    with closing(db_connect()) as conn:
        conn.execute("UPDATE subscriptions SET active=0,updated_at=? WHERE user_id=? AND active=1", (now,user_id))
        conn.execute("INSERT INTO subscriptions(user_id,starts_at,expires_at,active,warning_sent,expired_notified,created_at,updated_at) VALUES(?,?,?,1,0,0,?,?)", (user_id,start.isoformat(),expires.isoformat(),now,now))
    return get_active_subscription(user_id)


def remove_subscription(user_id: int) -> None:
    db_exec("UPDATE subscriptions SET active=0,updated_at=? WHERE user_id=? AND active=1", (now_iso(),int(user_id)))


def get_page_size() -> int:
    try:
        return max(3, min(20, int(get_setting("page_size", "8"))))
    except ValueError:
        return 8


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data[:64])


def back_button(target: str = "user:menu") -> InlineKeyboardButton:
    return btn("↩️ بازگشت", target)


def scope_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("All", "admin:buttons_scope:all"), btn("Active", "admin:buttons_scope:active")],
        [btn("Guest", "admin:buttons_scope:guest"), btn("Admin", "admin:buttons_scope:admin")],
        [btn("➕ افزودن", "admin:button:add"), btn("✏️ ویرایش", "admin:button:edit")],
        [btn("🗑 حذف", "admin:button:delete"), btn("↕️ ترتیب", "admin:button:reorder")],
        [back_button("admin:menu")],
    ])


def admin_menu_markup() -> InlineKeyboardMarkup:
    rows = db_fetchall("SELECT label,action FROM buttons WHERE scope='admin' AND enabled=1 AND action IN ({}) ORDER BY sort_order,id".format(",".join("?"*len(ALLOWED_ACTIONS))), tuple(ALLOWED_ACTIONS))
    if not rows:
        rows = [sqlite3.Row]  # sentinel; fallback below
    fallback = [
        ("👥 کاربران", "users"), ("➕ افزودن کاربر", "adduser"), ("👑 ادمین‌ها", "admins"), ("➕ افزودن ادمین", "addadmin"),
        ("📦 اشتراک‌ها", "subs"), ("🛒 سفارش‌ها", "orders"), ("🎫 تیکت‌ها", "tickets"), ("📊 آمار", "stats"),
        ("📜 لاگ‌ها", "logs"), ("🔘 مدیریت دکمه‌ها", "buttons"), ("📝 مدیریت متن‌ها", "texts"), ("📢 ارسال همگانی", "broadcast"),
        ("💾 Backup", "backup"), ("⚙️ تنظیمات", "settings"), ("❤️ Health", "health"),
    ]
    pairs = fallback if rows and rows[0] is sqlite3.Row else [(r["label"], r["action"]) for r in rows]
    out = []
    current = []
    for label, action in pairs:
        current.append(btn(label, f"admin:menuaction:{action}"))
        if len(current) == 2:
            out.append(current); current=[]
    if current: out.append(current)
    return InlineKeyboardMarkup(out)


def user_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    scope = "active" if user_has_subscription(user_id) else "guest"
    rows = db_fetchall("SELECT label,action FROM buttons WHERE scope=? AND enabled=1 AND action IN ({}) ORDER BY sort_order,id".format(",".join("?"*len(ALLOWED_ACTIONS))), (scope,*ALLOWED_ACTIONS))
    if not rows:
        fallback = [("🛒 خرید","order"),("📦 سفارش‌های من","my_orders"),("👤 حساب من","account"),("💬 پشتیبانی","support"),("🎫 تیکت‌های من","my_tickets")] if scope=="active" else [("💬 پشتیبانی","support")]
        rows = [{"label":a,"action":b} for a,b in fallback]
    out=[]; cur=[]
    for r in rows:
        cur.append(btn(r["label"], f"user:action:{r['action']}"))
        if len(cur)==2: out.append(cur); cur=[]
    if cur: out.append(cur)
    return InlineKeyboardMarkup(out)


def support_markup() -> InlineKeyboardMarkup:
    username = get_setting("support_username", SUPPORT_USERNAME).strip().lstrip("@")
    if username:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💬 تماس با پشتیبانی", url=f"https://t.me/{username}")]])
    return InlineKeyboardMarkup([[back_button("user:menu")]])


def admin_user_actions(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("➕ اشتراک", f"admin:addsub:{user_id}"), btn("🔄 تمدید", f"admin:renewsub:{user_id}")],
        [btn("❌ حذف اشتراک", f"admin:remsub:{user_id}"), btn("💬 پیام", f"admin:msg:{user_id}")],
        [btn("📦 Orders", f"admin:userorders:{user_id}:0"), btn("🎫 Tickets", f"admin:usertickets:{user_id}:0")],
        [btn("🚫/✅ Block", f"admin:toggleblock:{user_id}")],
        [back_button("admin:users:0")],
    ])


def pagination(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row=[]
    if page>0: row.append(btn("◀️ قبلی", f"{prefix}:{page-1}"))
    row.append(btn(f"{page+1}/{pages}", "admin:noop"))
    if page<pages-1: row.append(btn("بعدی ▶️", f"{prefix}:{page+1}"))
    return row


def format_plain_user(row: sqlite3.Row) -> str:
    username = f"@{row['username']}" if row["username"] else "-"
    sub = get_active_subscription(row["user_id"])
    admin = db_fetchone("SELECT role,enabled FROM admins WHERE user_id=?", (row["user_id"],))
    return (f"👤 {display_name(row)}\nID: {row['user_id']}\nUsername: {username}\n"
            f"Block: {'بله' if row['status']=='blocked' else 'خیر'}\n"
            f"Subscription: {'فعال' if sub else 'ندارد'}\nExpiry: {human_dt(sub['expires_at']) if sub else '-'}\n"
            f"Admin: {admin['role'] if admin and admin['enabled'] else 'خیر'}\nLast activity: {human_dt(row['last_seen'])}")


async def edit_or_reply(update: Update, text: str, markup=None, parse_mode=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)


async def safe_send(bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return True
        except TelegramError:
            return False
    except (Forbidden, BadRequest, TelegramError):
        return False


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user: return
    ensure_user(user)
    context.user_data.clear()
    if user_is_blocked(user.id) and not is_admin_user(update):
        await update.effective_message.reply_text(get_text("blocked")); return
    if is_admin_user(update):
        await update.effective_message.reply_text("🛠 پنل مدیریت آماده است.", reply_markup=admin_menu_markup()); return
    sub = get_active_subscription(user.id)
    if sub:
        await update.effective_message.reply_text(safe_format(get_text("active_welcome"), remaining=human_remaining(sub["expires_at"]), expires=human_dt(sub["expires_at"])), reply_markup=user_menu_markup(user.id))
    else:
        await update.effective_message.reply_text(safe_format(get_text("welcome"), name=html.escape(user.first_name or "دوست عزیز")), reply_markup=user_menu_markup(user.id))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin_user(update): return
    ensure_user(update.effective_user); context.user_data.clear()
    await update.effective_message.reply_text("🛠 پنل مدیریت", reply_markup=admin_menu_markup())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    if is_admin_user(update):
        await update.effective_message.reply_text("✅ لغو شد.", reply_markup=admin_menu_markup())
    elif update.effective_user:
        await update.effective_message.reply_text("✅ لغو شد.", reply_markup=user_menu_markup(update.effective_user.id))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        await update.effective_message.reply_text(f"🆔 ID شما: {update.effective_user.id}")


async def show_account(update: Update) -> None:
    uid=update.effective_user.id
    row=db_fetchone("SELECT * FROM users WHERE user_id=?",(uid,))
    if not row: return
    sub=get_active_subscription(uid)
    orders=db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=?",(uid,))["c"]
    text=(f"👤 حساب کاربری\n\nID: {row['user_id']}\nنام: {display_name(row)}\nUsername: @{row['username'] or '-'}\n"
          f"Subscription: {'فعال' if sub else 'ندارد'}\nانقضا: {human_dt(sub['expires_at']) if sub else '-'}\nباقی‌مانده: {human_remaining(sub['expires_at']) if sub else '-'}\nسفارش‌ها: {orders}")
    await edit_or_reply(update,text,InlineKeyboardMarkup([[back_button("user:menu")]]))


async def show_my_orders(update: Update, page:int=0, edit:bool=False) -> None:
    uid=update.effective_user.id; size=get_page_size()
    total=db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=?",(uid,))["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1))
    rows=db_fetchall("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(uid,size,page*size))
    text="📦 سفارش‌های من\n\n"+"\n".join(f"• #{r['id']} — {r['status']} — {human_dt(r['created_at'])}" for r in rows) if rows else "📦 سفارشی ندارید."
    kb=[[btn(f"#{r['id']}",f"user:order:{r['id']}")] for r in rows]
    kb.append(pagination("user:orders",page,pages)); kb.append([back_button("user:menu")])
    await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def show_order_detail(update: Update, order_id:str, admin:bool=False) -> None:
    row=db_fetchone("SELECT * FROM orders WHERE id=?",(order_id,))
    if not row:
        await edit_or_reply(update,"❌ سفارش پیدا نشد.",InlineKeyboardMarkup([[back_button("admin:orders:0" if admin else "user:orders:0")]])); return
    if not admin and int(row["user_id"])!=int(update.effective_user.id): return
    text=(f"📦 سفارش #{row['id']}\n\nUser: {row['user_id']}\nUsername: @{row['username'] or '-'}\n"
          f"Phone: {row['phone']}\nStatus: {row['status']}\nCreated: {human_dt(row['created_at'])}\nUpdated: {human_dt(row['updated_at'])}")
    if row["reason"]: text += f"\nReason: {row['reason']}"
    if row["admin_note"]: text += f"\nNote: {row['admin_note']}"
    if admin:
        kb=[[btn("🔄 In Progress",f"admin:orderstatus:{order_id}:in_progress"),btn("✅ Done",f"admin:orderstatus:{order_id}:done")],[btn("❌ Reject",f"admin:reject:{order_id}"),btn("🚫 Cancel",f"admin:orderstatus:{order_id}:cancelled")],[btn("💬 پیام",f"admin:ordermsg:{order_id}")],[back_button("admin:orders:0")]]
    else: kb=[[back_button("user:orders:0")]]
    await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def show_my_tickets(update:Update,page:int=0)->None:
    uid=update.effective_user.id; size=get_page_size()
    total=db_fetchone("SELECT COUNT(*) c FROM tickets WHERE user_id=?",(uid,))["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1))
    rows=db_fetchall("SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(uid,size,page*size))
    text="🎫 تیکت‌های من\n\n"+"\n".join(f"• #{r['id']} | {r['status']} | {human_dt(r['created_at'])}" for r in rows) if rows else "🎫 تیکتی ندارید."
    kb=[[btn(f"#{r['id']}",f"user:ticket:{r['id']}")] for r in rows]; kb.append(pagination("user:tickets",page,pages)); kb.append([back_button("user:menu")])
    await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def user_ticket_detail(update:Update,ticket_id:str)->None:
    row=db_fetchone("SELECT * FROM tickets WHERE id=? AND user_id=?",(ticket_id,update.effective_user.id))
    if not row: await edit_or_reply(update,"❌ تیکت پیدا نشد.",InlineKeyboardMarkup([[back_button("user:tickets:0")]])); return
    msgs=db_fetchall("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC LIMIT 50",(ticket_id,))
    text=f"🎫 تیکت #{ticket_id}\nوضعیت: {row['status']}\n\n"+"\n\n".join(("پشتیبانی" if m['sender_role']=="admin" else "شما")+f" | {human_dt(m['created_at'])}\n{m['text']}" for m in msgs)
    kb=[]
    if row["status"]!="closed": kb.append([btn("💬 ارسال پیام",f"user:ticketreply:{ticket_id}")])
    kb.append([back_button("user:tickets:0")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def user_ticket_reply_prompt(update,context,ticket_id:str)->None:
    row=db_fetchone("SELECT id FROM tickets WHERE id=? AND user_id=? AND status!='closed'",(ticket_id,update.effective_user.id))
    if not row: await edit_or_reply(update,"❌ تیکت پیدا نشد یا بسته است.",InlineKeyboardMarkup([[back_button("user:tickets:0")]])); return
    context.user_data.update(state="user_ticket_reply",ticket_id=ticket_id); await update.callback_query.edit_message_text("💬 پیام جدید برای این تیکت را ارسال کن:",reply_markup=InlineKeyboardMarkup([[back_button(f"user:ticket:{ticket_id}")]]))


async def receive_user_ticket_reply(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if context.user_data.get("state")!="user_ticket_reply": return
    text=(update.effective_message.text or "").strip(); tid=context.user_data.get("ticket_id"); uid=update.effective_user.id
    if not tid or not text or len(text)>4000: await update.effective_message.reply_text("❌ پیام نامعتبر است."); return
    row=db_fetchone("SELECT * FROM tickets WHERE id=? AND user_id=? AND status!='closed'",(tid,uid))
    if not row: context.user_data.clear(); await update.effective_message.reply_text("❌ تیکت پیدا نشد.",reply_markup=user_menu_markup(uid)); return
    current=now_iso()
    with closing(db_connect()) as conn:
        conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) VALUES(?,?,?,?,?)",(tid,uid,"user",text[:4000],current))
        conn.execute("UPDATE tickets SET status='open',updated_at=? WHERE id=?",(current,tid))
    context.user_data.clear(); await update.effective_message.reply_text("✅ پیام تیکت ثبت شد.",reply_markup=InlineKeyboardMarkup([[back_button(f"user:ticket:{tid}")]]))
    await safe_send(context.bot,ADMIN_ID,f"💬 پیام جدید در تیکت #{tid}\nUser: {uid}\n\n{text}",reply_markup=InlineKeyboardMarkup([[btn("💬 پاسخ",f"admin:ticketreply:{tid}")],[btn("✅ بستن",f"admin:ticketclose:{tid}")]]))
    log_event(uid,"Ticket Action",f"ticket_id={tid};action=user_reply")


async def begin_order(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    uid=update.effective_user.id
    if not user_has_subscription(uid):
        await update.effective_message.reply_text(get_text("no_subscription"),reply_markup=support_markup()); return
    try: limit=max(1,int(get_setting("daily_order_limit","5")))
    except ValueError: limit=5
    today=datetime.now(timezone.utc).date().isoformat()
    count=db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=? AND substr(created_at,1,10)=?",(uid,today))["c"]
    if count>=limit:
        await update.effective_message.reply_text(get_text("daily_limit")); return
    context.user_data["state"]="awaiting_phone"
    await update.effective_message.reply_text(get_text("ask_phone"),reply_markup=InlineKeyboardMarkup([[btn("❌ لغو","user:menu")]]))


async def receive_phone(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if context.user_data.get("state")!="awaiting_phone": return
    uid=update.effective_user.id
    if user_is_blocked(uid): context.user_data.clear(); await update.effective_message.reply_text(get_text("blocked")); return
    if not user_has_subscription(uid): context.user_data.clear(); await update.effective_message.reply_text(get_text("no_subscription"),reply_markup=support_markup()); return
    phone=normalize_phone(update.effective_message.text or "")
    if not phone: await update.effective_message.reply_text(get_text("invalid_phone")); return
    try: limit=max(1,int(get_setting("daily_order_limit","5")))
    except ValueError: limit=5
    today=datetime.now(timezone.utc).date().isoformat(); count=db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=? AND substr(created_at,1,10)=?",(uid,today))["c"]
    if count>=limit: context.user_data.clear(); await update.effective_message.reply_text(get_text("daily_limit")); return
    order_id=uuid.uuid4().hex[:10].upper(); user=db_fetchone("SELECT * FROM users WHERE user_id=?",(uid,)); current=now_iso()
    db_exec("INSERT INTO orders(id,user_id,username,name,phone,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",(order_id,uid,user["username"],display_name(user),phone,current,current))
    context.user_data.clear(); await update.effective_message.reply_text(safe_format(get_text("order_created"),order_id=order_id),reply_markup=InlineKeyboardMarkup([[back_button("user:menu")]]))
    log_event(uid,"add_order",f"order_id={order_id}")
    text=f"📥 سفارش جدید\n\nID: {order_id}\nUser: {uid}\nUsername: @{user['username'] or '-'}\nPhone: {phone}\nStatus: pending"
    await safe_send(context.bot,ADMIN_ID,text,reply_markup=InlineKeyboardMarkup([[btn("🔄 In Progress",f"admin:orderstatus:{order_id}:in_progress"),btn("✅ Done",f"admin:orderstatus:{order_id}:done")],[btn("❌ Reject",f"admin:reject:{order_id}")]]))


async def create_ticket(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    uid=update.effective_user.id
    if user_is_blocked(uid): await update.effective_message.reply_text(get_text("blocked")); return
    context.user_data["state"]="awaiting_ticket"
    await update.effective_message.reply_text("🎫 پیام خود را برای پشتیبانی بنویسید:",reply_markup=InlineKeyboardMarkup([[btn("❌ لغو","user:menu")]]))


async def receive_ticket_message(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if context.user_data.get("state")!="awaiting_ticket": return
    text=(update.effective_message.text or "").strip(); uid=update.effective_user.id
    if not text or len(text)>4000: await update.effective_message.reply_text("❌ متن باید بین 1 تا 4000 کاراکتر باشد."); return
    ticket_id=uuid.uuid4().hex[:10].upper(); current=now_iso()
    with closing(db_connect()) as conn:
        conn.execute("INSERT INTO tickets(id,user_id,status,subject,created_at,updated_at) VALUES(?,?, 'open',?,?,?)",(ticket_id,uid,"پشتیبانی",current,current))
        conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) VALUES(?,?,?,?,?)",(ticket_id,uid,"user",text,current))
    context.user_data.clear(); await update.effective_message.reply_text(safe_format(get_text("ticket_created"),ticket_id=ticket_id),reply_markup=InlineKeyboardMarkup([[back_button("user:menu")]]))
    await safe_send(context.bot,ADMIN_ID,f"🎫 تیکت جدید #{ticket_id}\nUser: {uid}\n\n{text}",reply_markup=InlineKeyboardMarkup([[btn("💬 پاسخ",f"admin:ticketreply:{ticket_id}"),btn("✅ بستن",f"admin:ticketclose:{ticket_id}")]]))
    log_event(uid,"ticket_created",f"ticket_id={ticket_id}")


async def handle_text_state(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    user=update.effective_user
    if not user: return
    if user_is_blocked(user.id) and not is_admin_user(update):
        await update.effective_message.reply_text(get_text("blocked")); return
    state=context.user_data.get("state")
    if state=="awaiting_phone": await receive_phone(update,context); return
    if state=="awaiting_ticket": await receive_ticket_message(update,context); return
    if state=="user_ticket_reply": await receive_user_ticket_reply(update,context); return
    if not is_admin_user(update): return
    text=(update.effective_message.text or "").strip()
    dispatch={
        "add_user":admin_add_user_from_text,"add_admin":admin_add_admin_from_text,"addsub":admin_subscription_from_text,"renewsub":admin_subscription_from_text,
        "admin_message":admin_send_user_message,"reject_order":admin_reject_order_from_text,"order_message":admin_order_message_from_text,
        "ticket_reply":admin_ticket_reply_from_text,"add_button":admin_add_button_from_text,"edit_button":admin_edit_button_from_text,"delete_button":admin_delete_button_from_text,
        "reorder_button":admin_reorder_button_from_text,"edit_text":admin_edit_text_from_text,"set_setting":admin_set_setting_from_text,"broadcast":admin_broadcast_from_text,
        "search_user":admin_search_user_from_text,"search_order":admin_search_order_from_text,
    }
    fn=dispatch.get(state)
    if fn: await fn(update,context,text)


async def admin_add_user_from_text(update,context,text):
    username=normalize_username(text)
    if not username: await update.effective_message.reply_text("❌ Username معتبر نیست. مثال: @Mmdm_mk"); return
    row=db_fetchone("SELECT user_id FROM users WHERE lower(username)=?",(username,))
    context.user_data["state"]="addsub"
    if row:
        context.user_data["target_user_id"]=int(row["user_id"])
        await update.effective_message.reply_text(f"✅ کاربر @{username} پیدا شد. تعداد روز اشتراک را وارد کن:")
    else:
        context.user_data["pending_username"]=username
        log_event(ADMIN_ID,"Add User",f"pending_username={username}")
        await update.effective_message.reply_text(f"ℹ️ @{username} هنوز /start نزده. مدت Subscription را انتخاب/وارد کن تا Pending Grant ثبت شود:")


async def admin_subscription_from_text(update,context,text):
    if not re.fullmatch(r"\d{1,5}",text): await update.effective_message.reply_text("❌ تعداد روز باید عدد باشد."); return
    days=int(text)
    if not 1<=days<=36500: await update.effective_message.reply_text("❌ تعداد روز نامعتبر است."); return
    username=context.user_data.get("pending_username")
    if username:
        db_exec("INSERT INTO pending_user_grants(username,days,created_at,created_by) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET days=excluded.days,created_at=excluded.created_at,created_by=excluded.created_by",(username,days,now_iso(),ADMIN_ID))
        context.user_data.clear(); log_event(ADMIN_ID,"Add Subscription","pending_username=%s;days=%s"%(username,days)); await update.effective_message.reply_text(f"✅ Pending Subscription برای @{username} ثبت شد.",reply_markup=admin_menu_markup()); return
    uid=context.user_data.get("target_user_id")
    if not isinstance(uid,int): context.user_data.clear(); await update.effective_message.reply_text("❌ کاربر هدف نامعتبر است.",reply_markup=admin_menu_markup()); return
    renew=context.user_data.get("state")=='renewsub'
    sub=set_subscription(uid,days,renew=renew); context.user_data.clear()
    log_event(ADMIN_ID,"Renew Subscription" if renew else "Add Subscription",f"user_id={uid};days={days}")
    await safe_send(context.bot,uid,safe_format(get_text("subscription_renewed" if renew else "subscription_added"),expires=human_dt(sub["expires_at"])))
    await update.effective_message.reply_text(f"✅ اشتراک کاربر تا {human_dt(sub['expires_at'])} فعال شد.",reply_markup=admin_menu_markup())


async def admin_add_admin_from_text(update,context,text):
    if not can_manage_admins(update.effective_user.id): await update.effective_message.reply_text("❌ مجوز ندارید."); return
    username=normalize_username(text)
    if not username: await update.effective_message.reply_text("❌ Username معتبر نیست."); return
    row=db_fetchone("SELECT user_id FROM users WHERE lower(username)=?",(username,))
    if row:
        db_exec("INSERT INTO admins(user_id,username,role,enabled,created_at) VALUES(?,?, 'admin',1,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,enabled=1",(int(row["user_id"]),username,now_iso()))
        log_event(ADMIN_ID,"Add Admin",f"user_id={row['user_id']};username={username}")
        context.user_data.clear(); await update.effective_message.reply_text(f"✅ @{username} ادمین شد.",reply_markup=admin_menu_markup()); return
    db_exec("INSERT INTO pending_admin_grants(username,role,created_at,created_by) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET role=excluded.role,created_at=excluded.created_at,created_by=excluded.created_by",(username,"admin",now_iso(),ADMIN_ID))
    context.user_data.clear(); log_event(ADMIN_ID,"Add Admin",f"pending_username={username}"); await update.effective_message.reply_text(f"ℹ️ @{username} هنوز /start نزده؛ Pending Admin ثبت شد.",reply_markup=admin_menu_markup())


async def admin_send_user_message(update,context,text):
    uid=context.user_data.get("target_user_id")
    context.user_data.clear()
    if not isinstance(uid,int) or not text or len(text)>4000: await update.effective_message.reply_text("❌ ورودی نامعتبر است.",reply_markup=admin_menu_markup()); return
    ok=await safe_send(context.bot,uid,text); log_event(ADMIN_ID,"Message User",f"user_id={uid};sent={ok}")
    await update.effective_message.reply_text("✅ پیام ارسال شد." if ok else "❌ ارسال ناموفق بود؛ کاربر ممکن است Bot را Block کرده باشد.",reply_markup=admin_menu_markup())


async def admin_reject_order_from_text(update,context,text):
    oid=context.user_data.get("order_id"); context.user_data.clear()
    if not oid or not text or len(text)>2000: await update.effective_message.reply_text("❌ ورودی نامعتبر است.",reply_markup=admin_menu_markup()); return
    row=db_fetchone("SELECT * FROM orders WHERE id=?",(oid,))
    if not row: await update.effective_message.reply_text("❌ سفارش پیدا نشد.",reply_markup=admin_menu_markup()); return
    current=now_iso(); db_exec("UPDATE orders SET status='rejected',reason=?,updated_at=?,closed_at=? WHERE id=?",(text,current,current,oid))
    await safe_send(context.bot,row["user_id"],safe_format(get_text("order_rejected"),order_id=oid,reason=text)); log_event(ADMIN_ID,"Order Status Change",f"order_id={oid};status=rejected")
    await update.effective_message.reply_text("✅ سفارش رد شد.",reply_markup=admin_menu_markup())


async def admin_order_message_from_text(update,context,text):
    oid=context.user_data.get("order_id"); context.user_data.clear(); row=db_fetchone("SELECT * FROM orders WHERE id=?",(oid,)) if oid else None
    if not row: await update.effective_message.reply_text("❌ سفارش پیدا نشد.",reply_markup=admin_menu_markup()); return
    ok=await safe_send(context.bot,row["user_id"],f"💬 پیام درباره سفارش #{oid}:\n\n{text[:4000]}"); log_event(ADMIN_ID,"Order Message",f"order_id={oid};sent={ok}")
    await update.effective_message.reply_text("✅ پیام ارسال شد." if ok else "❌ ارسال ناموفق بود.",reply_markup=admin_menu_markup())


async def admin_ticket_reply_from_text(update,context,text):
    tid=context.user_data.get("ticket_id"); context.user_data.clear(); row=db_fetchone("SELECT * FROM tickets WHERE id=?",(tid,)) if tid else None
    if not row: await update.effective_message.reply_text("❌ تیکت پیدا نشد.",reply_markup=admin_menu_markup()); return
    current=now_iso()
    with closing(db_connect()) as conn:
        conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) VALUES(?,?,?,?,?)",(tid,ADMIN_ID,"admin",text[:4000],current))
        conn.execute("UPDATE tickets SET status='waiting',updated_at=? WHERE id=?",(current,tid))
    ok=await safe_send(context.bot,row["user_id"],f"💬 پاسخ پشتیبانی برای تیکت #{tid}:\n\n{text[:4000]}"); log_event(ADMIN_ID,"Ticket Action",f"ticket_id={tid};action=reply;sent={ok}")
    await update.effective_message.reply_text("✅ پاسخ ثبت شد.",reply_markup=admin_menu_markup())


async def admin_add_button_from_text(update,context,text):
    # scope | label | action | enabled | sort
    parts=[x.strip() for x in text.split("|")]
    if len(parts)!=5 or parts[0] not in SCOPES or parts[2] not in ALLOWED_ACTIONS or parts[3] not in {"0","1"} or not parts[4].isdigit() or not parts[1]:
        await update.effective_message.reply_text("❌ فرمت: scope | label | action | enabled(0/1) | sort"); return
    scope,label,action,en,sort=parts
    db_exec("INSERT INTO buttons(scope,label,action,enabled,sort_order) VALUES(?,?,?,?,?)",(scope,label[:100],action,int(en),int(sort)))
    context.user_data.clear(); log_event(ADMIN_ID,"Button Add",f"scope={scope};action={action}"); await update.effective_message.reply_text("✅ دکمه اضافه شد.",reply_markup=admin_menu_markup())


async def admin_edit_button_from_text(update,context,text):
    parts=[x.strip() for x in text.split("|")]
    if len(parts)!=6 or not parts[0].isdigit() or parts[1] not in SCOPES or parts[3] not in ALLOWED_ACTIONS or parts[4] not in {"0","1"} or not parts[5].isdigit():
        await update.effective_message.reply_text("❌ فرمت: ID | scope | label | action | enabled | sort"); return
    bid,scope,label,action,en,sort=int(parts[0]),parts[1],parts[2],parts[3],int(parts[4]),int(parts[5])
    if not label: await update.effective_message.reply_text("❌ label خالی است."); return
    result=db_exec("UPDATE buttons SET scope=?,label=?,action=?,enabled=?,sort_order=? WHERE id=?",(scope,label[:100],action,en,sort,bid))
    context.user_data.clear(); log_event(ADMIN_ID,"Button Edit",f"button_id={bid}"); await update.effective_message.reply_text("✅ ذخیره شد." if result["rowcount"] else "❌ دکمه پیدا نشد.",reply_markup=admin_menu_markup())


async def admin_delete_button_from_text(update,context,text):
    if not text.isdigit(): await update.effective_message.reply_text("❌ ID صحیح نیست."); return
    result=db_exec("DELETE FROM buttons WHERE id=?",(int(text),)); context.user_data.clear(); log_event(ADMIN_ID,"Button Delete",f"button_id={text}"); await update.effective_message.reply_text("✅ حذف شد." if result["rowcount"] else "❌ پیدا نشد.",reply_markup=admin_menu_markup())


async def admin_reorder_button_from_text(update,context,text):
    parts=[x.strip() for x in text.split("|",1)]
    if len(parts)!=2 or not all(x.isdigit() for x in parts): await update.effective_message.reply_text("❌ فرمت: ID | sort"); return
    result=db_exec("UPDATE buttons SET sort_order=? WHERE id=?",(int(parts[1]),int(parts[0]))); context.user_data.clear(); log_event(ADMIN_ID,"Button Reorder",f"button_id={parts[0]};sort={parts[1]}"); await update.effective_message.reply_text("✅ ترتیب تغییر کرد." if result["rowcount"] else "❌ پیدا نشد.",reply_markup=admin_menu_markup())


async def admin_edit_text_from_text(update,context,text):
    key=context.user_data.get("text_key"); context.user_data.clear()
    if not key or len(text)>4000: await update.effective_message.reply_text("❌ ورودی نامعتبر است.",reply_markup=admin_menu_markup()); return
    db_exec("INSERT INTO texts(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,text)); log_event(ADMIN_ID,"Text Edit",f"key={key}"); await update.effective_message.reply_text("✅ متن ذخیره شد.",reply_markup=admin_menu_markup())


async def admin_set_setting_from_text(update,context,text):
    key=context.user_data.get("setting_key"); context.user_data.clear()
    allowed=set(DEFAULT_SETTINGS)
    if key not in allowed: await update.effective_message.reply_text("❌ تنظیم نامعتبر است.",reply_markup=admin_menu_markup()); return
    if key=="support_username" and text and normalize_username(text) is None: await update.effective_message.reply_text("❌ Username معتبر نیست.",reply_markup=admin_menu_markup()); return
    if key in {"expiry_warning_hours","daily_order_limit","page_size","expiry_worker_enabled"} and not re.fullmatch(r"\d+",text): await update.effective_message.reply_text("❌ مقدار عددی معتبر نیست.",reply_markup=admin_menu_markup()); return
    if key=="broadcast_delay":
        try:
            if float(text)<0: raise ValueError
        except ValueError: await update.effective_message.reply_text("❌ delay نامعتبر است.",reply_markup=admin_menu_markup()); return
    value=normalize_username(text) if key=="support_username" else text.strip(); set_setting(key,value or ""); log_event(ADMIN_ID,"Setting Change",f"key={key}"); await update.effective_message.reply_text("✅ تنظیم ذخیره شد.",reply_markup=admin_menu_markup())


async def admin_broadcast_from_text(update,context,text):
    if not text or len(text)>4000: await update.effective_message.reply_text("❌ متن نامعتبر است."); return
    context.user_data["broadcast_text"]=text; context.user_data["state"]="broadcast_confirm"
    await update.effective_message.reply_text(f"📢 متن آماده ارسال:\n\n{text}\n\nارسال شود؟",reply_markup=InlineKeyboardMarkup([[btn("✅ بله","admin:broadcast:confirm"),btn("❌ لغو","admin:menu")]]))


async def admin_search_user_from_text(update,context,text):
    context.user_data.clear(); row=db_fetchone("SELECT * FROM users WHERE user_id=?",(int(text),)) if text.isdigit() else db_fetchone("SELECT * FROM users WHERE lower(username)=?",(normalize_username(text) or "!",))
    if not row: await update.effective_message.reply_text("❌ کاربر پیدا نشد.",reply_markup=admin_menu_markup()); return
    await update.effective_message.reply_text(format_plain_user(row),reply_markup=admin_user_actions(row["user_id"]))


async def admin_search_order_from_text(update,context,text):
    context.user_data.clear(); oid=text.strip().upper(); row=db_fetchone("SELECT * FROM orders WHERE id=?",(oid,))
    if not row: await update.effective_message.reply_text("❌ سفارش پیدا نشد.",reply_markup=admin_menu_markup()); return
    await update.effective_message.reply_text("📦 سفارش پیدا شد.",reply_markup=InlineKeyboardMarkup([[btn("مشاهده",f"admin:order:{oid}")],[back_button("admin:orders:0")]]))


async def admin_users(update:Update,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM users")["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",(size,page*size))
    text="👥 کاربران\n\n"+"\n".join(f"• {display_name(r)} | @{r['username'] or '-'} | Block={'بله' if r['status']=='blocked' else 'خیر'} | Sub={'فعال' if get_active_subscription(r['user_id']) else 'خیر'}" for r in rows) if rows else "👥 کاربری نیست."
    kb=[[btn(f"{display_name(r)[:25]}",f"admin:user:{r['user_id']}")] for r in rows]; kb.append(pagination("admin:users",page,pages)); kb.append([btn("🔎 جستجو","admin:usersearch"),back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_subscriptions(update:Update,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM subscriptions")["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM subscriptions ORDER BY expires_at DESC LIMIT ? OFFSET ?",(size,page*size))
    text="💳 اشتراک‌ها\n\n"+"\n".join(f"• {r['user_id']} | {'فعال' if get_active_subscription(r['user_id']) else 'منقضی'} | {human_dt(r['expires_at'])}" for r in rows) if rows else "💳 اشتراکی نیست."
    kb=[[btn(f"کاربر {r['user_id']}",f"admin:user:{r['user_id']}")] for r in rows]; kb.append(pagination("admin:subs",page,pages)); kb.append([back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_orders(update:Update,page:int,status:Optional[str]=None):
    size=get_page_size(); params=[]; where=""
    if status and status in ORDER_STATUSES: where="WHERE status=?"; params=[status]
    total=db_fetchone(f"SELECT COUNT(*) c FROM orders {where}",tuple(params))["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall(f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",tuple(params+[size,page*size]))
    text=f"🛒 سفارش‌ها | {status or 'all'}\n\n"+"\n".join(f"• #{r['id']} | {r['status']} | {r['user_id']}" for r in rows) if rows else "🛒 سفارشی نیست."
    kb=[[btn(f"#{r['id']} | {r['status']}",f"admin:order:{r['id']}")] for r in rows]; kb.append(pagination("admin:orders",page,pages)); kb.append([btn("pending","admin:ordersfilter:pending"),btn("in_progress","admin:ordersfilter:in_progress")]); kb.append([btn("done","admin:ordersfilter:done"),btn("rejected","admin:ordersfilter:rejected")]); kb.append([btn("all","admin:ordersfilter:all"),back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_tickets(update:Update,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM tickets")["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ? OFFSET ?",(size,page*size)); text="🎫 تیکت‌ها\n\n"+"\n".join(f"• #{r['id']} | {r['status']} | {r['user_id']}" for r in rows) if rows else "🎫 تیکتی نیست."; kb=[[btn(f"#{r['id']}",f"admin:ticket:{r['id']}")] for r in rows]; kb.append(pagination("admin:tickets",page,pages)); kb.append([back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_logs(update:Update,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM logs")["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?",(size,page*size)); text="📜 لاگ‌ها\n\n"+"\n".join(f"• {human_dt(r['created_at'])} | {r['action']} | {r['details'] or '-'}" for r in rows) if rows else "📜 لاگی نیست."; kb=[pagination("admin:logs",page,pages),[back_button("admin:menu")]]; await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_stats(update:Update):
    now=now_iso(); today=datetime.now(timezone.utc).date().isoformat(); week=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    vals={
        "users":db_fetchone("SELECT COUNT(*) c FROM users")["c"],"active":db_fetchone("SELECT COUNT(*) c FROM users WHERE status='active'")["c"],"blocked":db_fetchone("SELECT COUNT(*) c FROM users WHERE status='blocked'")["c"],
        "active_sub":db_fetchone("SELECT COUNT(*) c FROM subscriptions WHERE active=1 AND expires_at>?",(now,))["c"],"expired_sub":db_fetchone("SELECT COUNT(*) c FROM subscriptions WHERE expires_at<=?",(now,))["c"],
        "orders":db_fetchone("SELECT COUNT(*) c FROM orders")["c"],"tickets":db_fetchone("SELECT COUNT(*) c FROM tickets")["c"],"today_users":db_fetchone("SELECT COUNT(*) c FROM users WHERE substr(joined_at,1,10)=?",(today,))["c"],"week_users":db_fetchone("SELECT COUNT(*) c FROM users WHERE joined_at>=?",(week,))["c"],"today_orders":db_fetchone("SELECT COUNT(*) c FROM orders WHERE substr(created_at,1,10)=?",(today,))["c"],"week_orders":db_fetchone("SELECT COUNT(*) c FROM orders WHERE created_at>=?",(week,))["c"]}
    text=(f"📊 آمار\n\nکاربران: {vals['users']}\nفعال: {vals['active']}\nبلاک: {vals['blocked']}\nاشتراک فعال: {vals['active_sub']}\nاشتراک منقضی: {vals['expired_sub']}\nOrders: {vals['orders']}\nTickets: {vals['tickets']}\n\nامروز کاربران: {vals['today_users']}\n۷ روز کاربران: {vals['week_users']}\nامروز Orders: {vals['today_orders']}\n۷ روز Orders: {vals['week_orders']}")
    await edit_or_reply(update,text,InlineKeyboardMarkup([[back_button("admin:menu")]]))


async def admin_buttons(update:Update,scope_filter:str="all"):
    if scope_filter not in {"all",*SCOPES}: scope_filter="all"
    if scope_filter=="all": rows=db_fetchall("SELECT * FROM buttons ORDER BY scope,sort_order,id")
    else: rows=db_fetchall("SELECT * FROM buttons WHERE scope=? ORDER BY sort_order,id",(scope_filter,))
    text=f"🔘 Buttons | Scope={scope_filter}\n\n"+"\n".join(f"ID {r['id']} | {r['scope']} | {r['label']} | {r['action']} | {'ON' if r['enabled'] else 'OFF'} | sort={r['sort_order']}" for r in rows) if rows else "🔘 دکمه‌ای نیست."
    await edit_or_reply(update,text,scope_menu_markup())


async def admin_texts(update:Update):
    rows=db_fetchall("SELECT key,value FROM texts ORDER BY key"); text="📝 Texts\n\n"+"\n".join(f"• {r['key']}" for r in rows); kb=[[btn(r['key'][:30],f"admin:text:{r['key']}")] for r in rows]; kb.append([back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_settings(update:Update):
    rows=db_fetchall("SELECT key,value FROM settings ORDER BY key"); text="⚙️ Settings\n\n"+"\n".join(f"• {r['key']} = {r['value']}" for r in rows); kb=[[btn(r['key'],f"admin:setting:{r['key']}")] for r in rows]; kb.append([back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_health(update:Update):
    try: integrity=db_fetchone("PRAGMA integrity_check")[0]; db_ok=integrity=="ok"
    except Exception: integrity="error"; db_ok=False
    text=(f"❤️ Health\n\nBot: ✅\nDatabase: {'✅' if db_ok else '❌'} ({integrity})\nUptime: {int(time.time()-START_TIME)} sec\n"
          f"Railway service: {os.getenv('RAILWAY_SERVICE_NAME','-')}\nEnvironment: {os.getenv('RAILWAY_ENVIRONMENT_NAME','-')}\nDB: {DB_PATH}")
    await edit_or_reply(update,text,InlineKeyboardMarkup([[back_button("admin:menu")]]))


async def admin_admins(update:Update,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM admins")["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM admins ORDER BY role DESC,created_at LIMIT ? OFFSET ?",(size,page*size)); text="👑 Admins\n\n"+"\n".join(f"• {r['user_id']} | @{r['username'] or '-'} | {r['role']} | {'ON' if r['enabled'] else 'OFF'}" for r in rows) if rows else "👑 ادمینی نیست."; kb=[[btn(f"{r['user_id']} | {r['role']}",f"admin:admindetail:{r['user_id']}")] for r in rows]; kb.append(pagination("admin:admins",page,pages)); kb.append([btn("➕ افزودن ادمین","admin:addadmin"),back_button("admin:menu")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_user_detail(update:Update,uid:int):
    row=db_fetchone("SELECT * FROM users WHERE user_id=?",(uid,))
    if not row: await edit_or_reply(update,"❌ کاربر پیدا نشد.",InlineKeyboardMarkup([[back_button("admin:users:0")]])); return
    await edit_or_reply(update,format_plain_user(row),admin_user_actions(uid))


async def admin_user_orders(update:Update,uid:int,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=?",(uid,))["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(uid,size,page*size)); text=f"📦 Orders user {uid}\n\n"+"\n".join(f"• #{r['id']} | {r['status']}" for r in rows) if rows else "بدون Order."; kb=[[btn(f"#{r['id']}",f"admin:order:{r['id']}")] for r in rows]; kb.append(pagination(f"admin:userorders:{uid}",page,pages)); kb.append([back_button(f"admin:user:{uid}")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_user_tickets(update:Update,uid:int,page:int):
    size=get_page_size(); total=db_fetchone("SELECT COUNT(*) c FROM tickets WHERE user_id=?",(uid,))["c"]; pages=max(1,(total+size-1)//size); page=max(0,min(page,pages-1)); rows=db_fetchall("SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(uid,size,page*size)); text=f"🎫 Tickets user {uid}\n\n"+"\n".join(f"• #{r['id']} | {r['status']}" for r in rows) if rows else "بدون Ticket."; kb=[[btn(f"#{r['id']}",f"admin:ticket:{r['id']}")] for r in rows]; kb.append(pagination(f"admin:usertickets:{uid}",page,pages)); kb.append([back_button(f"admin:user:{uid}")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def admin_ticket_detail(update:Update,ticket_id:str):
    row=db_fetchone("SELECT * FROM tickets WHERE id=?",(ticket_id,))
    if not row: await edit_or_reply(update,"❌ تیکت پیدا نشد.",InlineKeyboardMarkup([[back_button("admin:tickets:0")]])); return
    msgs=db_fetchall("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC LIMIT 50",(ticket_id,)); text=f"🎫 Ticket #{ticket_id}\nUser: {row['user_id']}\nStatus: {row['status']}\n\n"+"\n\n".join(("ادمین" if m["sender_role"]=="admin" else "کاربر")+f" | {human_dt(m['created_at'])}\n{m['text']}" for m in msgs)
    kb=[[btn("💬 پاسخ",f"admin:ticketreply:{ticket_id}")]]
    if row["status"]=="closed": kb.append([btn("🔓 Reopen",f"admin:ticketreopen:{ticket_id}")])
    else: kb.append([btn("✅ Close",f"admin:ticketclose:{ticket_id}")])
    kb.append([back_button("admin:tickets:0")]); await edit_or_reply(update,text,InlineKeyboardMarkup(kb))


async def close_ticket(update,context,ticket_id:str,reopen:bool=False):
    row=db_fetchone("SELECT * FROM tickets WHERE id=?",(ticket_id,))
    if not row: await edit_or_reply(update,"❌ تیکت پیدا نشد.",InlineKeyboardMarkup([[back_button("admin:tickets:0")]])); return
    status="open" if reopen else "closed"; current=now_iso(); db_exec("UPDATE tickets SET status=?,updated_at=?,closed_at=? WHERE id=?",(status,current,None if reopen else current,ticket_id));
    if not reopen: await safe_send(context.bot,row["user_id"],safe_format(get_text("ticket_closed"),ticket_id=ticket_id))
    log_event(ADMIN_ID,"Ticket Action",f"ticket_id={ticket_id};action={'reopen' if reopen else 'close'}")
    await edit_or_reply(update,"🔓 تیکت دوباره باز شد." if reopen else "✅ تیکت بسته شد.",InlineKeyboardMarkup([[back_button("admin:tickets:0")]]))


async def change_order_status(update,context,order_id:str,status:str):
    if status not in {"in_progress","done","cancelled"}: await edit_or_reply(update,"❌ وضعیت نامعتبر."); return
    row=db_fetchone("SELECT * FROM orders WHERE id=?",(order_id,));
    if not row: await edit_or_reply(update,"❌ سفارش پیدا نشد."); return
    current_status=row["status"]
    valid_transition=(status=="in_progress" and current_status=="pending") or (status=="done" and current_status=="in_progress") or (status=="cancelled" and current_status in {"pending","in_progress"})
    if not valid_transition:
        await edit_or_reply(update,f"❌ انتقال وضعیت از {current_status} به {status} مجاز نیست.",InlineKeyboardMarkup([[back_button("admin:orders:0")]])); return
    current=now_iso(); closed=current if status in {"done","cancelled"} else None; db_exec("UPDATE orders SET status=?,updated_at=?,closed_at=? WHERE id=?",(status,current,closed,order_id));
    key={"in_progress":"order_in_progress","done":"order_done","cancelled":"order_cancelled"}[status]; await safe_send(context.bot,row["user_id"],safe_format(get_text(key),order_id=order_id)); log_event(ADMIN_ID,"Order Status Change",f"order_id={order_id};status={status}"); await edit_or_reply(update,"✅ وضعیت سفارش تغییر کرد.",InlineKeyboardMarkup([[back_button("admin:orders:0")]]))


async def remove_admin(update,uid:int):
    if uid==ADMIN_ID:
        await edit_or_reply(update,"❌ Owner قابل حذف نیست.",InlineKeyboardMarkup([[back_button("admin:admins:0")]])); return
    row=db_fetchone("SELECT role FROM admins WHERE user_id=?",(uid,));
    if not row: await edit_or_reply(update,"❌ Admin پیدا نشد.",InlineKeyboardMarkup([[back_button("admin:admins:0")]])); return
    db_exec("DELETE FROM admins WHERE user_id=?",(uid,)); log_event(ADMIN_ID,"Remove Admin",f"user_id={uid}"); await edit_or_reply(update,"✅ Admin حذف شد.",InlineKeyboardMarkup([[back_button("admin:admins:0")]]))


# ---------- Backup / Restore ----------

def sha256_bytes(data:bytes)->str: return __import__('hashlib').sha256(data).hexdigest()


def sqlite_snapshot_bytes()->bytes:
    with closing(db_connect()) as src:
        tmp=Path(tempfile.mkstemp(prefix="snapshot_",suffix=".sqlite3",dir=TMP_DIR)[1])
        try:
            with closing(sqlite3.connect(tmp)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.commit()
            return tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)


def create_backup()->Path:
    data=sqlite_snapshot_bytes(); created=now_iso(); metadata={"app":APP_NAME,"schema_version":SCHEMA_VERSION,"timestamp":created,"database_sha256":sha256_bytes(data),"required_tables":["meta","users","pending_user_grants","admins","pending_admin_grants","subscriptions","orders","tickets","ticket_messages","logs","buttons","texts","settings"]}
    out=TMP_DIR/f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bot.sqlite3",data); zf.writestr("metadata.json",json.dumps(metadata,ensure_ascii=False,indent=2))
    return out


def validate_backup_bytes(data:bytes)->tuple[bool,str]:
    if len(data)>MAX_BACKUP_BYTES: return False,"فایل از BACKUP_MAX_MB بزرگ‌تر است."
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if zf.testzip() is not None: return False,"ZIP خراب است."
            names=set(zf.namelist())
            if names!={"bot.sqlite3","metadata.json"}: return False,"ساختار ZIP نامعتبر یا ناقص است."
            metadata=json.loads(zf.read("metadata.json").decode("utf-8")); db_bytes=zf.read("bot.sqlite3")
            if int(metadata.get("schema_version",0))>SCHEMA_VERSION: return False,"Schema بکاپ جدیدتر از این Bot است."
            expected=metadata.get("database_sha256")
            if expected and expected!=sha256_bytes(db_bytes): return False,"SHA256 دیتابیس با metadata برابر نیست."
    except (zipfile.BadZipFile,json.JSONDecodeError,UnicodeDecodeError,ValueError,KeyError): return False,"Backup نامعتبر است."
    tmp=Path(tempfile.mkstemp(prefix="validate_",suffix=".sqlite3",dir=TMP_DIR)[1])
    try:
        tmp.write_bytes(db_bytes)
        with closing(sqlite3.connect(tmp)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok": return False,"SQLite integrity_check شکست خورد."
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required={"meta","users","pending_user_grants","admins","pending_admin_grants","subscriptions","orders","tickets","ticket_messages","logs","buttons","texts","settings"}
        if not required.issubset(tables): return False,"جدول‌های ضروری کامل نیستند."
    finally: tmp.unlink(missing_ok=True)
    return True,"OK"


def restore_backup_bytes(data:bytes)->tuple[bool,str]:
    valid,reason=validate_backup_bytes(data)
    if not valid: return False,reason
    emergency=TMP_DIR/f"emergency_before_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    try:
        current=create_backup(); shutil.copy2(current,emergency); current.unlink(missing_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf: incoming=zf.read("bot.sqlite3")
        new=TMP_DIR/f"restore_{uuid.uuid4().hex}.sqlite3"; new.write_bytes(incoming)
        with closing(sqlite3.connect(new)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE"); conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise RuntimeError("integrity")
        sqlite_checkpoint()
        for sidecar in (DB_PATH.with_name(DB_PATH.name+"-wal"),DB_PATH.with_name(DB_PATH.name+"-shm")): sidecar.unlink(missing_ok=True)
        shutil.copy2(new,DB_PATH); new.unlink(missing_ok=True); init_db(); return True,"Emergency backup: "+str(emergency)
    except Exception as exc:
        logger.exception("Restore failed")
        return False,f"Restore failed: {type(exc).__name__}"


def sqlite_checkpoint()->None:
    with closing(db_connect()) as conn: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def backup_document_handler(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if not is_admin_user(update) or context.user_data.get("state")!="awaiting_backup_document": return
    doc=update.effective_message.document
    if not doc: return
    if doc.file_size and doc.file_size>MAX_BACKUP_BYTES: await update.effective_message.reply_text("❌ فایل بزرگ است."); return
    tg_file=await doc.get_file(); data=bytes(await tg_file.download_as_bytearray()); ok,reason=await asyncio.to_thread(validate_backup_bytes,data)
    if not ok: context.user_data.clear(); await update.effective_message.reply_text(f"❌ Backup رد شد.\n{reason}",reply_markup=admin_menu_markup()); return
    path=TMP_DIR/f"uploaded_{uuid.uuid4().hex}.zip"; path.write_bytes(data); context.user_data.update(state="restore_confirm",restore_path=str(path)); await update.effective_message.reply_text("⚠️ قبل از Restore یک Emergency Backup گرفته می‌شود. تأیید؟",reply_markup=InlineKeyboardMarkup([[btn("✅ تأیید","admin:restore:confirm"),btn("❌ لغو","admin:restore:cancel")]]))


async def run_broadcast(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    text=context.user_data.get("broadcast_text")
    if not text: context.user_data.clear(); await edit_or_reply(update,"❌ متن Broadcast موجود نیست.",admin_menu_markup()); return
    users=db_fetchall("SELECT user_id FROM users WHERE status!='blocked' ORDER BY user_id"); delay=max(0.0,float(get_setting("broadcast_delay","0.08") or 0.08)); sent=failed=0; await edit_or_reply(update,"📢 Broadcast شروع شد...")
    for row in users:
        ok=await safe_send(context.bot,int(row["user_id"]),text[:4000]); sent+=int(ok); failed+=int(not ok)
        if delay: await asyncio.sleep(delay)
    context.user_data.clear(); log_event(ADMIN_ID,"Broadcast",f"sent={sent};failed={failed}"); await edit_or_reply(update,f"✅ Broadcast تمام شد.\nموفق: {sent}\nناموفق: {failed}",admin_menu_markup())


# ---------- Callback router ----------

async def callback_handler(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    q=update.callback_query
    if not q: return
    data=(q.data or "").strip()
    try: await q.answer()
    except TelegramError: pass
    uid=q.from_user.id
    if user_is_blocked(uid) and not is_admin_id(uid):
        await q.answer("دسترسی شما مسدود است.",show_alert=True); return
    try:
        if data=="user:menu":
            context.user_data.clear(); await q.edit_message_text("🏠 منوی اصلی",reply_markup=user_menu_markup(uid)); return
        if data=="user:noop": return
        if data.startswith("user:action:"):
            action=data.split(":",2)[2]
            if action not in ALLOWED_ACTIONS: await q.edit_message_text(get_text("unknown")); return
            if action=="order": await begin_order(update,context)
            elif action=="my_orders": await show_my_orders(update,0,True)
            elif action=="account": await show_account(update)
            elif action=="my_tickets": await show_my_tickets(update,0)
            elif action in {"support","support_link"}: await create_ticket(update,context)
            else: await q.edit_message_text(get_text("unknown"))
            return
        if data.startswith("user:orders:"):
            try: page=int(data.split(":")[2]); await show_my_orders(update,page,True)
            except (ValueError,IndexError): await q.answer("صفحه نامعتبر",show_alert=True)
            return
        if data.startswith("user:order:"):
            await show_order_detail(update,data.split(":",2)[2],False); return
        if data.startswith("user:tickets:"):
            try: await show_my_tickets(update,int(data.split(":")[2]))
            except (ValueError,IndexError): await q.answer("صفحه نامعتبر",show_alert=True)
            return
        if data.startswith("user:ticketreply:"):
            await user_ticket_reply_prompt(update,context,data.split(":",2)[2]); return
        if data.startswith("user:ticket:"):
            await user_ticket_detail(update,data.split(":",2)[2]); return
        if data.startswith("admin:"):
            if not is_admin_id(uid): await q.answer("دسترسی غیرمجاز",show_alert=True); return
            await admin_callback(update,context,data); return
    except (ValueError,IndexError,KeyError,TypeError,BadRequest,TelegramError) as exc:
        logger.exception("Callback failed: %s",type(exc).__name__)
        try: await q.edit_message_text("⚠️ این عملیات قابل پردازش نیست.",reply_markup=admin_menu_markup() if is_admin_id(uid) else user_menu_markup(uid))
        except Exception: pass


async def admin_callback(update:Update,context:ContextTypes.DEFAULT_TYPE,data:str)->None:
    q=update.callback_query; p=data.split(":")
    if data=="admin:menu": context.user_data.clear(); await q.edit_message_text("🛠 پنل مدیریت",reply_markup=admin_menu_markup()); return
    if data=="admin:noop": return
    if data.startswith("admin:menuaction:"):
        action=data.split(":",2)[2]
        if action not in ALLOWED_ACTIONS: await q.answer("Action نامعتبر",show_alert=True); return
        routes={"users":lambda:admin_users(update,0),"adduser":lambda:admin_adduser_prompt(update,context),"admins":lambda:admin_admins(update,0),"addadmin":lambda:admin_addadmin_prompt(update,context),"subs":lambda:admin_subscriptions(update,0),"orders":lambda:admin_orders(update,0),"tickets":lambda:admin_tickets(update,0),"stats":lambda:admin_stats(update),"logs":lambda:admin_logs(update,0),"buttons":lambda:admin_buttons(update,"all"),"texts":lambda:admin_texts(update),"broadcast":lambda:admin_broadcast_prompt(update,context),"backup":lambda:admin_backup_menu(update),"settings":lambda:admin_settings(update),"health":lambda:admin_health(update)}
        fn=routes.get(action)
        if fn: await fn()
        return
    if data=="admin:users:0" or (p[1]=="users" and len(p)==3): await admin_users(update,int(p[2])); return
    if p[1]=="usersearch": context.user_data["state"]="search_user"; await q.edit_message_text("🔎 ID یا @username را ارسال کن:"); return
    if p[1]=="user" and len(p)==3: await admin_user_detail(update,int(p[2])); return
    if p[1] in {"addsub","renewsub"}:
        try: uid2=int(p[2])
        except (ValueError,IndexError): await q.answer("ID نامعتبر",show_alert=True); return
        ensure_user_id(uid2)
        context.user_data.update(target_user_id=uid2,state=p[1])
        await q.edit_message_text(
            "📅 مدت اشتراک را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup([
                [btn("1 روز","admin:subdays:1"),btn("3 روز","admin:subdays:3"),btn("7 روز","admin:subdays:7")],
                [btn("30 روز","admin:subdays:30"),btn("90 روز","admin:subdays:90")],
                [btn("✏️ Custom Days","admin:subdays:custom")],
                [back_button(f"admin:user:{uid2}")],
            ]),
        )
        return
    if p[1]=="subdays":
        if p[2]=="custom":
            if not context.user_data.get("target_user_id"):
                await q.answer("کاربر هدف وجود ندارد",show_alert=True); return
            await q.edit_message_text("✏️ تعداد روز دلخواه را وارد کن:")
            return
        try: days=int(p[2])
        except (ValueError,IndexError): await q.answer("مدت نامعتبر",show_alert=True); return
        uid2=context.user_data.get("target_user_id")
        state=context.user_data.get("state")
        if not isinstance(uid2,int) or state not in {"addsub","renewsub"} or days not in {1,3,7,30,90}:
            await q.answer("عملیات نامعتبر",show_alert=True); return
        renew=state=="renewsub"; sub=set_subscription(uid2,days,renew=renew); context.user_data.clear()
        log_event(ADMIN_ID,"Renew Subscription" if renew else "Add Subscription",f"user_id={uid2};days={days}")
        await safe_send(context.bot,uid2,safe_format(get_text("subscription_renewed" if renew else "subscription_added"),expires=human_dt(sub["expires_at"])))
        await q.edit_message_text(f"✅ اشتراک تا {human_dt(sub['expires_at'])} فعال شد.",reply_markup=admin_user_actions(uid2))
        return
    if p[1]=="remsub":
        uid2=int(p[2]); remove_subscription(uid2); log_event(ADMIN_ID,"Remove Subscription",f"user_id={uid2}"); await safe_send(context.bot,uid2,get_text("subscription_expired")); await q.edit_message_text("✅ اشتراک حذف شد.",reply_markup=admin_user_actions(uid2)); return
    if p[1]=="msg": context.user_data.update(target_user_id=int(p[2]),state="admin_message"); await q.edit_message_text("💬 پیام را ارسال کن:"); return
    if p[1]=="toggleblock":
        uid2=int(p[2]); row=db_fetchone("SELECT status FROM users WHERE user_id=?",(uid2,));
        if not row: await q.edit_message_text("❌ کاربر پیدا نشد."); return
        new="active" if row["status"]=="blocked" else "blocked"; db_exec("UPDATE users SET status=?,last_seen=? WHERE user_id=?",(new,now_iso(),uid2)); log_event(ADMIN_ID,"Unblock User" if new=="active" else "Block User",f"user_id={uid2}"); await admin_user_detail(update,uid2); return
    if p[1]=="userorders": await admin_user_orders(update,int(p[2]),int(p[3])); return
    if p[1]=="usertickets": await admin_user_tickets(update,int(p[2]),int(p[3])); return
    if p[1]=="subs": await admin_subscriptions(update,int(p[2])); return
    if p[1]=="orders": await admin_orders(update,int(p[2])); return
    if p[1]=="ordersfilter": await admin_orders(update,0,None if p[2]=="all" else p[2]); return
    if p[1]=="order": await show_order_detail(update,p[2],True); return
    if p[1]=="orderstatus": await change_order_status(update,context,p[2],p[3]); return
    if p[1]=="reject": context.user_data.update(order_id=p[2],state="reject_order"); await q.edit_message_text("📝 دلیل رد را ارسال کن:"); return
    if p[1]=="ordermsg": context.user_data.update(order_id=p[2],state="order_message"); await q.edit_message_text("💬 پیام را ارسال کن:"); return
    if p[1]=="tickets": await admin_tickets(update,int(p[2])); return
    if p[1]=="ticket": await admin_ticket_detail(update,p[2]); return
    if p[1]=="ticketreply": context.user_data.update(ticket_id=p[2],state="ticket_reply"); await q.edit_message_text("💬 پاسخ را ارسال کن:"); return
    if p[1]=="ticketclose": await close_ticket(update,context,p[2],False); return
    if p[1]=="ticketreopen": await close_ticket(update,context,p[2],True); return
    if p[1]=="logs": await admin_logs(update,int(p[2])); return
    if p[1]=="admins": await admin_admins(update,int(p[2])); return
    if p[1]=="admindetail":
        uid2=int(p[2]); row=db_fetchone("SELECT * FROM admins WHERE user_id=?",(uid2,));
        if not row: await q.edit_message_text("❌ Admin پیدا نشد."); return
        kb=[[btn("🗑 حذف Admin",f"admin:removeadmin:{uid2}")]] if uid2!=ADMIN_ID else []; kb.append([back_button("admin:admins:0")]); await q.edit_message_text(f"👑 Admin {uid2}\nUsername: @{row['username'] or '-'}\nRole: {row['role']}",reply_markup=InlineKeyboardMarkup(kb)); return
    if p[1]=="removeadmin": await remove_admin(update,int(p[2])); return
    if p[1]=="button":
        action=p[2] if len(p)>2 else ""
        states={"add":"add_button","edit":"edit_button","delete":"delete_button","reorder":"reorder_button"}
        if action in states: context.user_data["state"]=states[action]; await q.edit_message_text({"add":"➕ scope | label | action | enabled(0/1) | sort","edit":"✏️ ID | scope | label | action | enabled | sort","delete":"🗑 ID","reorder":"↕️ ID | sort"}[action])
        return
    if p[1]=="buttons_scope": await admin_buttons(update,p[2] if len(p)>2 else "all"); return
    if p[1]=="text":
        key=":".join(p[2:]); context.user_data.update(text_key=key,state="edit_text"); await q.edit_message_text(f"📝 متن فعلی {key}:\n\n{get_text(key)}\n\nمتن جدید را بفرست:"); return
    if p[1]=="setting":
        key=":".join(p[2:]); context.user_data.update(setting_key=key,state="set_setting"); await q.edit_message_text(f"⚙️ مقدار جدید {key} را بفرست.\nفعلی: {get_setting(key)}"); return
    if data=="admin:adduser": await admin_adduser_prompt(update,context); return
    if data=="admin:addadmin": await admin_addadmin_prompt(update,context); return
    if data=="admin:broadcast": await admin_broadcast_prompt(update,context); return
    if data=="admin:backup": await admin_backup_menu(update); return
    if data=="admin:backup:download": await admin_backup_download(update,context); return
    if data=="admin:backup:upload": context.user_data["state"]="awaiting_backup_document"; await q.edit_message_text("📤 ZIP Backup را به صورت Document ارسال کن.",reply_markup=InlineKeyboardMarkup([[back_button("admin:menu")]])); return
    if data=="admin:restore:confirm":
        path=context.user_data.get("restore_path"); context.user_data.clear()
        if not path or not Path(path).exists(): await q.edit_message_text("❌ فایل Restore پیدا نشد.",reply_markup=admin_menu_markup()); return
        ok,info=await asyncio.to_thread(restore_backup_bytes,Path(path).read_bytes()); Path(path).unlink(missing_ok=True); log_event(ADMIN_ID,"Restore",info); await q.edit_message_text(("✅ Restore شد.\n"+info) if ok else ("❌ Restore ناموفق.\n"+info),reply_markup=admin_menu_markup()); return
    if data=="admin:restore:cancel":
        path=context.user_data.pop("restore_path",None); context.user_data.clear(); Path(path).unlink(missing_ok=True) if path else None; await q.edit_message_text("✅ Restore لغو شد.",reply_markup=admin_menu_markup()); return
    if data=="admin:broadcast:confirm": await run_broadcast(update,context); return
    await q.edit_message_text("❓ عملیات ناشناخته است.",reply_markup=admin_menu_markup())


async def admin_adduser_prompt(update,context): context.user_data["state"]="add_user"; await update.callback_query.edit_message_text("➕ Username کاربر را وارد کن: @username")
async def admin_addadmin_prompt(update,context):
    if not can_manage_admins(update.effective_user.id): await update.callback_query.edit_message_text("❌ مجوز ندارید.",reply_markup=admin_menu_markup()); return
    context.user_data["state"]="add_admin"; await update.callback_query.edit_message_text("👑 Username ادمین را وارد کن: @username")
async def admin_broadcast_prompt(update,context): context.user_data["state"]="broadcast"; await update.callback_query.edit_message_text("📢 متن Broadcast را ارسال کن:",reply_markup=InlineKeyboardMarkup([[back_button("admin:menu")]]))
async def admin_backup_menu(update): await update.callback_query.edit_message_text("💾 Backup / Restore",reply_markup=InlineKeyboardMarkup([[btn("⬇️ Backup","admin:backup:download")],[btn("⬆️ Restore","admin:backup:upload")],[back_button("admin:menu")]]))
async def admin_backup_download(update,context):
    path=None
    try:
        path=await asyncio.to_thread(create_backup)
        with path.open("rb") as f: await context.bot.send_document(ADMIN_ID,f,filename=path.name,caption="💾 Backup شامل DB و metadata")
        log_event(ADMIN_ID,"Backup",path.name); await update.callback_query.edit_message_text("✅ Backup ارسال شد.",reply_markup=InlineKeyboardMarkup([[back_button("admin:backup")]]))
    except TelegramError: await update.callback_query.edit_message_text("❌ ارسال Backup ناموفق بود.",reply_markup=admin_menu_markup())
    finally:
        if path: path.unlink(missing_ok=True)


async def expiry_job(context:ContextTypes.DEFAULT_TYPE)->None:
    if get_setting("expiry_worker_enabled","1")!="1": return
    expired=deactivate_expired_subscriptions()
    for uid in expired:
        row=db_fetchone("SELECT id,expired_notified,expires_at FROM subscriptions WHERE user_id=? AND active=0 ORDER BY expires_at DESC LIMIT 1",(uid,))
        if row and not row["expired_notified"] and not user_has_subscription(uid):
            db_exec("UPDATE subscriptions SET expired_notified=1 WHERE id=?",(row["id"],)); await safe_send(context.bot,uid,get_text("subscription_expired"),reply_markup=support_markup()); log_event(uid,"Subscription Expired")
    try: warn=max(1,int(get_setting("expiry_warning_hours","24")))
    except ValueError: warn=24
    horizon=(datetime.now(timezone.utc)+timedelta(hours=warn)).isoformat(); rows=db_fetchall("SELECT * FROM subscriptions WHERE active=1 AND warning_sent=0 AND expires_at>? AND expires_at<=?",(now_iso(),horizon))
    for row in rows:
        if db_exec("UPDATE subscriptions SET warning_sent=1,updated_at=? WHERE id=? AND warning_sent=0",(now_iso(),row["id"]))["rowcount"]!=1: continue
        await safe_send(context.bot,row["user_id"],safe_format(get_text("subscription_warning"),remaining=human_remaining(row["expires_at"]))); log_event(row["user_id"],"Subscription Warning")


async def error_handler(update:object,context:ContextTypes.DEFAULT_TYPE)->None:
    err=context.error; logger.error("Unhandled error: %s",type(err).__name__,exc_info=err)
    if isinstance(update,Update) and update.effective_message and update.effective_user and not is_admin_id(update.effective_user.id):
        try: await update.effective_message.reply_text("⚠️ یک خطای موقت رخ داد. دوباره تلاش کنید.")
        except Exception: pass


async def post_init(app:Application)->None:
    init_db()
    if app.job_queue is not None: app.job_queue.run_repeating(expiry_job,interval=EXPIRY_CHECK_SEC,first=5,name="expiry_worker")
    logger.info("Initialized DB=%s",DB_PATH)

async def post_shutdown(app:Application)->None: logger.info("Shutdown complete")


def build_application()->Application:
    app=(ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start",start_cmd)); app.add_handler(CommandHandler("admin",admin_cmd)); app.add_handler(CommandHandler("cancel",cancel_cmd)); app.add_handler(CommandHandler("id",id_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler)); app.add_handler(MessageHandler(filters.Document.ALL,backup_document_handler)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text_state)); app.add_error_handler(error_handler); return app


def main()->None:
    init_db(); logger.info("Starting %s with Railway polling",APP_NAME); build_application().run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False,close_loop=False)


if __name__=="__main__": main()
