import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codes import generate_codes, redeem_code
from db import init_db, get_db


def _clean_db():
    """Open a fresh connection, clean test data, close it."""
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM codes")
    conn.commit()
    conn.close()


def setup_function():
    _clean_db()


def test_generate_month_codes():
    _clean_db()
    conn = get_db()
    codes = generate_codes(conn, "month", 5)
    assert len(codes) == 5
    for c in codes:
        assert c.startswith("M-")
        assert len(c) == 10  # M- + 8 chars

    conn2 = get_db()
    count = conn2.execute("SELECT COUNT(*) FROM codes WHERE redeemed_by IS NULL").fetchone()[0]
    conn2.close()
    assert count == 5
    conn.close()


def test_redeem_month_code():
    _clean_db()
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (openid) VALUES ('user1')")
    codes = generate_codes(conn, "month", 1)
    conn.commit()

    result = redeem_code(conn, codes[0], "user1")
    assert result["success"] is True
    assert result["type"] == "month"

    # Double redeem should fail
    result2 = redeem_code(conn, codes[0], "user2")
    assert result2["success"] is False
    conn.close()


def test_redeem_10pack():
    _clean_db()
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (openid) VALUES ('user2')")
    codes = generate_codes(conn, "10pack", 1)
    conn.commit()

    result = redeem_code(conn, codes[0], "user2")
    assert result["success"] is True
    assert result["type"] == "10pack"

    user = conn.execute("SELECT * FROM users WHERE openid = 'user2'").fetchone()
    assert user["extra_uses"] == 10
    conn.close()
