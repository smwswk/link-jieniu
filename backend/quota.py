import sqlite3
from db import now_iso, today_str


def ensure_user(conn: sqlite3.Connection, openid: str):
    conn.execute(
        "INSERT OR IGNORE INTO users (openid) VALUES (?)", (openid,)
    )
    conn.commit()


def check_quota(conn: sqlite3.Connection, openid: str) -> tuple[bool, str]:
    """Return (can_use, reason)."""
    ensure_user(conn, openid)
    user = conn.execute(
        "SELECT * FROM users WHERE openid = ?", (openid,)
    ).fetchone()

    if user is None:
        return False, "user not found"

    # 1. Subscription active?
    if user["subscription_expiry"] and user["subscription_expiry"] > now_iso():
        return True, "subscribed"

    # 2. Extra uses (次卡)?
    if user["extra_uses"] > 0:
        return True, "extra_use"

    # 3. New day -> reset free
    if user["last_free_date"] != today_str():
        return True, "free_daily_new"

    # 4. Today's free use still available?
    if user["free_uses_today"] < 1:
        return True, "free_daily"

    return False, "今日免费次数已用完，兑换码获取更多次数"


def deduct_quota(conn: sqlite3.Connection, openid: str):
    """Deduct quota after check passed. Must call check_quota first."""
    user = conn.execute(
        "SELECT * FROM users WHERE openid = ?", (openid,)
    ).fetchone()

    if user["subscription_expiry"] and user["subscription_expiry"] > now_iso():
        return  # subscribed, no deduction needed

    if user["extra_uses"] > 0:
        conn.execute(
            "UPDATE users SET extra_uses = extra_uses - 1 WHERE openid = ?",
            (openid,),
        )
    elif user["last_free_date"] != today_str():
        conn.execute(
            "UPDATE users SET free_uses_today = 1, last_free_date = ? WHERE openid = ?",
            (today_str(), openid),
        )
    elif user["free_uses_today"] < 1:
        conn.execute(
            "UPDATE users SET free_uses_today = free_uses_today + 1 WHERE openid = ?",
            (openid,),
        )

    conn.execute(
        "UPDATE users SET total_tasks = total_tasks + 1 WHERE openid = ?",
        (openid,),
    )
    conn.commit()
