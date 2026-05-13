import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota import check_quota, deduct_quota
from db import init_db, get_db


def setup_function():
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()


def test_new_user_has_free_use():
    # New user should have 1 free use today, no subscription
    can_use, reason = check_quota(get_db(), "user_new")
    assert can_use is True
    deduct_quota(get_db(), "user_new")
    # After using the free one, should be out
    can_use2, reason2 = check_quota(get_db(), "user_new")
    assert can_use2 is False
    assert "today" in reason2.lower() or "兑换" in reason2


def test_subscribed_user_unlimited():
    from db import now_iso
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO users (openid, subscription_expiry) VALUES (?, ?)",
        ("user_sub", "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    for _ in range(100):
        can_use, _ = check_quota(get_db(), "user_sub")
        assert can_use is True
        deduct_quota(get_db(), "user_sub")


def test_extra_uses_deducted_first():
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO users (openid, extra_uses) VALUES (?, 5)",
        ("user_extra",),
    )
    conn.commit()
    conn.close()
    for i in range(5):
        can_use, _ = check_quota(get_db(), "user_extra")
        assert can_use is True
        deduct_quota(get_db(), "user_extra")
    # 6th use should use the free daily
    can_use, _ = check_quota(get_db(), "user_extra")
    assert can_use is True  # still has free today
    deduct_quota(get_db(), "user_extra")
    # 7th use - has 0 extra + 1/1 free -> out
    can_use, reason = check_quota(get_db(), "user_extra")
    assert can_use is False
