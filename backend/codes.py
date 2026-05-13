import sqlite3
import secrets
import string
from db import now_iso


def _gen_code(prefix: str) -> str:
    chars = string.ascii_uppercase + string.digits
    # Remove ambiguous chars: 0/O, 1/I/L
    chars = chars.translate(str.maketrans("", "", "0O1IL"))
    rand = "".join(secrets.choice(chars) for _ in range(8))
    return f"{prefix}-{rand}"


def generate_codes(conn: sqlite3.Connection, code_type: str, count: int) -> list[str]:
    """Generate unused redemption codes. code_type: 'month' or '10pack'."""
    prefix = "M" if code_type == "month" else "C"
    value = "30days" if code_type == "month" else "10uses"
    codes = []
    attempts = 0
    while len(codes) < count and attempts < count * 10:
        code = _gen_code(prefix)
        try:
            conn.execute(
                "INSERT INTO codes (code, type, value) VALUES (?, ?, ?)",
                (code, code_type, value),
            )
            conn.commit()
            codes.append(code)
        except sqlite3.IntegrityError:
            attempts += 1
            continue
    return codes


def redeem_code(conn: sqlite3.Connection, code: str, openid: str) -> dict:
    """Redeem a code. Returns {success, type?, error?}."""
    code = code.upper().strip()
    row = conn.execute(
        "SELECT * FROM codes WHERE code = ?", (code,)
    ).fetchone()

    if row is None:
        return {"success": False, "error": "兑换码不存在"}
    if row["redeemed_by"] is not None:
        return {"success": False, "error": "兑换码已被使用"}

    now = now_iso()
    if row["type"] == "month":
        user = conn.execute(
            "SELECT subscription_expiry FROM users WHERE openid = ?", (openid,)
        ).fetchone()
        if user is None:
            conn.execute("INSERT OR IGNORE INTO users (openid) VALUES (?)", (openid,))

        current = user["subscription_expiry"] if user and user["subscription_expiry"] else ""
        if current and current > now:
            # Extend from current expiry
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(current)
            new_expiry = (dt + timedelta(days=30)).isoformat()
        else:
            from datetime import datetime, timezone, timedelta
            new_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

        conn.execute(
            "UPDATE users SET subscription_expiry = ? WHERE openid = ?",
            (new_expiry, openid),
        )
    elif row["type"] == "10pack":
        conn.execute("INSERT OR IGNORE INTO users (openid) VALUES (?)", (openid,))
        conn.execute(
            "UPDATE users SET extra_uses = extra_uses + 10 WHERE openid = ?",
            (openid,),
        )
    else:
        return {"success": False, "error": "未知兑换码类型"}

    conn.execute(
        "UPDATE codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ?",
        (openid, now, code),
    )
    conn.commit()
    return {"success": True, "type": row["type"]}
