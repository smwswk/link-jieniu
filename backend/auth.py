from itsdangerous import URLSafeTimedSerializer
import httpx
import os

SECRET_KEY = os.environ.get("APP_SECRET_KEY", os.urandom(32).hex())
WX_APPID = os.environ.get("WX_APPID", "")
WX_SECRET = os.environ.get("WX_SECRET", "")

serializer = URLSafeTimedSerializer(SECRET_KEY)


async def code_to_openid(js_code: str) -> str | None:
    """Exchange WeChat js_code for openid."""
    if not WX_APPID or not WX_SECRET:
        # Dev mode: accept any code as openid directly
        return js_code if len(js_code) > 4 else None

    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": WX_APPID,
        "secret": WX_SECRET,
        "js_code": js_code,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10)
        data = resp.json()
    if "openid" in data:
        return data["openid"]
    return None


def make_token(openid: str) -> str:
    return serializer.dumps(openid)


def verify_token(token: str) -> str | None:
    try:
        return serializer.loads(token, max_age=86400 * 30)
    except Exception:
        return None
