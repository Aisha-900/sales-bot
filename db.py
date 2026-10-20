import sqlite3
from contextlib import closing

import config


def _conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with closing(_conn()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                role TEXT, content TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS ai_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                cost_rub REAL, ts TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                username TEXT, product TEXT, amount INTEGER,
                status TEXT DEFAULT 'new',
                created TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT);
            """
        )
        c.commit()


def add_message(user_id, role, content):
    with closing(_conn()) as c:
        c.execute("INSERT INTO messages(user_id, role, content) VALUES (?,?,?)",
                  (user_id, role, content))
        c.commit()


def get_history(user_id, limit):
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()[::-1]
    msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def add_ai_cost(user_id, cost_rub):
    with closing(_conn()) as c:
        c.execute("INSERT INTO ai_usage(user_id, cost_rub) VALUES (?,?)", (user_id, cost_rub))
        c.commit()


def create_order(user_id, username, product, amount):
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO orders(user_id, username, product, amount) VALUES (?,?,?,?)",
            (user_id, username, product, amount),
        )
        c.commit()
        return cur.lastrowid


def mark_paid(order_id):
    """Возвращает заказ, если он только что оплачен; None, если уже был оплачен или не найден."""
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] == "paid":
            return None
        c.execute("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
        c.commit()
        return row


def stats(days=7):
    since = f"-{days} days"
    with closing(_conn()) as c:
        by_product = c.execute(
            "SELECT product, COUNT(*) AS n, SUM(amount) AS s FROM orders "
            "WHERE status='paid' AND paid_at >= datetime('now', ?) GROUP BY product",
            (since,),
        ).fetchall()
        cost = c.execute(
            "SELECT COALESCE(SUM(cost_rub), 0) FROM ai_usage WHERE ts >= datetime('now', ?)",
            (since,),
        ).fetchone()[0]
        dialogs = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM messages WHERE ts >= datetime('now', ?)",
            (since,),
        ).fetchone()[0]
    return by_product, cost, dialogs
