import aiosqlite
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict

DB_PATH = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                price_rub REAL,
                price_stars INTEGER,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT,
                plan_id INTEGER,
                payment_method TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_user_id INTEGER,
                sender_name TEXT,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                caption TEXT,
                event_type TEXT NOT NULL,
                original_text TEXT,
                saved_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS business_message_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                sender_id INTEGER,
                sender_name TEXT,
                media_type TEXT,
                file_id TEXT,
                text TEXT,
                caption TEXT,
                cached_at TEXT NOT NULL,
                UNIQUE(connection_id, chat_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_emails (
                user_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                is_enabled INTEGER DEFAULT 1,
                stored_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                registered_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                is_admin INTEGER DEFAULT 0,
                text TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('subscription_enabled', '0')
        """)
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_ids', '')
        """)
        await db.commit()

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()

async def is_subscription_enabled() -> bool:
    val = await get_setting("subscription_enabled")
    return val == "1"

async def get_admin_ids() -> List[int]:
    val = await get_setting("admin_ids")
    if not val:
        return []
    return [int(x) for x in val.split(",") if x.strip().isdigit()]

async def add_admin(user_id: int):
    admins = await get_admin_ids()
    if user_id not in admins:
        admins.append(user_id)
        await set_setting("admin_ids", ",".join(str(a) for a in admins))

async def remove_admin(user_id: int):
    admins = await get_admin_ids()
    admins = [a for a in admins if a != user_id]
    await set_setting("admin_ids", ",".join(str(a) for a in admins))

async def get_plans(active_only=True) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM subscription_plans"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY duration_days"
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_plan(plan_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM subscription_plans WHERE id = ?", (plan_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def add_plan(name: str, duration_days: int, price_rub: Optional[float], price_stars: Optional[int]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO subscription_plans (name, duration_days, price_rub, price_stars) VALUES (?, ?, ?, ?)",
            (name, duration_days, price_rub, price_stars)
        )
        await db.commit()
        return cur.lastrowid

async def delete_plan(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscription_plans WHERE id = ?", (plan_id,))
        await db.commit()

async def toggle_plan(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscription_plans SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id = ?",
            (plan_id,)
        )
        await db.commit()

async def get_user_subscription(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_subscriptions WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def has_active_subscription(user_id: int) -> bool:
    sub = await get_user_subscription(user_id)
    if not sub:
        return False
    expires_at = datetime.fromisoformat(sub["expires_at"])
    return expires_at > datetime.now()

async def grant_subscription(user_id: int, plan_id: int, payment_method: str):
    plan = await get_plan(plan_id)
    if not plan:
        return
    sub = await get_user_subscription(user_id)
    if sub:
        current_expires = datetime.fromisoformat(sub["expires_at"])
        base = max(current_expires, datetime.now())
    else:
        base = datetime.now()
    new_expires = base + timedelta(days=plan["duration_days"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_subscriptions (user_id, expires_at, plan_id, payment_method) VALUES (?, ?, ?, ?)",
            (user_id, new_expires.isoformat(), plan_id, payment_method)
        )
        await db.commit()

async def save_pending_payment(payment_id: str, user_id: int, plan_id: int, amount: float, currency: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_payments (payment_id, user_id, plan_id, amount, currency, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (payment_id, user_id, plan_id, amount, currency, datetime.now().isoformat())
        )
        await db.commit()

async def get_pending_payment(payment_id: str) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pending_payments WHERE payment_id = ?", (payment_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_last_pending_payment(user_id: int, plan_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_payments WHERE user_id = ? AND plan_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (user_id, plan_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def complete_payment(payment_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_payments SET status = 'completed' WHERE payment_id = ?",
            (payment_id,)
        )
        await db.commit()

async def save_media_event(
    owner_user_id: int, chat_id: int, sender_user_id: Optional[int],
    sender_name: str, media_type: str, file_id: str,
    caption: Optional[str], event_type: str, original_text: Optional[str] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO saved_media
               (owner_user_id, chat_id, sender_user_id, sender_name, media_type, file_id, caption, event_type, original_text, saved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, chat_id, sender_user_id, sender_name, media_type, file_id,
             caption, event_type, original_text, datetime.now().isoformat())
        )
        await db.commit()

async def cache_business_message(
    connection_id: str, chat_id: int, message_id: int, owner_id: int,
    sender_id: Optional[int], sender_name: str,
    media_type: Optional[str], file_id: Optional[str],
    text: Optional[str], caption: Optional[str]
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO business_message_cache
               (connection_id, chat_id, message_id, owner_id, sender_id, sender_name,
                media_type, file_id, text, caption, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (connection_id, chat_id, message_id, owner_id, sender_id, sender_name,
             media_type, file_id, text, caption, datetime.now().isoformat())
        )
        await db.commit()

async def get_cached_message(connection_id: str, chat_id: int, message_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM business_message_cache WHERE connection_id=? AND chat_id=? AND message_id=?",
            (connection_id, chat_id, message_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def update_cached_message_text(connection_id: str, chat_id: int, message_id: int, new_text: Optional[str], new_caption: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE business_message_cache SET text=?, caption=? WHERE connection_id=? AND chat_id=? AND message_id=?",
            (new_text, new_caption, connection_id, chat_id, message_id)
        )
        await db.commit()

async def store_business_connection(connection_id: str, owner_id: int, is_enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO business_connections (connection_id, owner_id, is_enabled, stored_at) VALUES (?, ?, ?, ?)",
            (connection_id, owner_id, 1 if is_enabled else 0, datetime.now().isoformat())
        )
        await db.commit()

async def get_connection_owner(connection_id: str) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT owner_id FROM business_connections WHERE connection_id = ?",
            (connection_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def grant_subscription_days(user_id: int, days: int):
    sub = await get_user_subscription(user_id)
    if sub:
        current_expires = datetime.fromisoformat(sub["expires_at"])
        base = max(current_expires, datetime.now())
    else:
        base = datetime.now()
    new_expires = base + timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_subscriptions (user_id, expires_at, plan_id, payment_method) VALUES (?, ?, ?, ?)",
            (user_id, new_expires.isoformat(), None, "Администратор")
        )
        await db.commit()


async def revoke_subscription(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_subscriptions WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_user_email(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT email FROM user_emails WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def save_user_email(user_id: int, email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_emails (user_id, email, updated_at) VALUES (?, ?, ?)",
            (user_id, email, datetime.now().isoformat())
        )
        await db.commit()

async def register_user(user_id: int, first_name: Optional[str], last_name: Optional[str], username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, first_name, last_name, username, registered_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,
               last_name=excluded.last_name, username=excluded.username""",
            (user_id, first_name, last_name, username, datetime.now().isoformat())
        )
        await db.commit()

async def get_total_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            return (await cur.fetchone())[0]

async def get_all_users_detailed(offset: int = 0, limit: int = 20) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.user_id, u.first_name, u.last_name, u.username,
                   s.expires_at
            FROM users u
            LEFT JOIN user_subscriptions s ON u.user_id = s.user_id
            ORDER BY u.registered_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_users() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def get_stats() -> Dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM user_subscriptions WHERE expires_at > ?",
            (datetime.now().isoformat(),)
        ) as cur:
            active_subs = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM saved_media WHERE media_type IN ('photo', 'video', 'voice', 'video_note')"
        ) as cur:
            total_saved = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM pending_payments WHERE status = 'completed'"
        ) as cur:
            total_payments = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM business_message_cache") as cur:
            cached_msgs = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'") as cur:
            open_tickets = (await cur.fetchone())[0]
    return {
        "total_users": total_users,
        "active_subs": active_subs,
        "total_saved": total_saved,
        "total_payments": total_payments,
        "cached_msgs": cached_msgs,
        "open_tickets": open_tickets,
    }

async def create_ticket(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO support_tickets (user_id, status, created_at) VALUES (?, 'open', ?)",
            (user_id, datetime.now().isoformat())
        )
        await db.commit()
        return cur.lastrowid

async def get_user_open_ticket(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM support_tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_ticket(ticket_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def add_ticket_message(ticket_id: int, sender_id: int, is_admin: bool, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, text, sent_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, sender_id, 1 if is_admin else 0, text, datetime.now().isoformat())
        )
        await db.commit()
        return cur.lastrowid

async def close_ticket(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE support_tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), ticket_id)
        )
        await db.commit()

async def get_ticket_messages(ticket_id: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY sent_at ASC",
            (ticket_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_open_tickets(offset: int = 0, limit: int = 10) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT t.*, u.first_name, u.last_name, u.username
            FROM support_tickets t
            LEFT JOIN users u ON t.user_id = u.user_id
            WHERE t.status = 'open'
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_tickets_count(status: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if status:
            async with db.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE status = ?", (status,)
            ) as cur:
                return (await cur.fetchone())[0]
        else:
            async with db.execute("SELECT COUNT(*) FROM support_tickets") as cur:
                return (await cur.fetchone())[0]
