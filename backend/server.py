import uuid
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from db import init_db, get_db, now_iso, today_str
from auth import code_to_openid, verify_token, make_token
from quota import check_quota, deduct_quota
from codes import generate_codes, redeem_code
from pipeline import (
    detect_platform, download_audio, slice_audio,
    transcribe_all, summarize_transcript, generate_card,
    update_task_status,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    os.makedirs("static/cards", exist_ok=True)
    yield


app = FastAPI(title="链接解牛 API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---- Auth ----
def get_openid(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    token = authorization.removeprefix("Bearer ")
    openid = verify_token(token)
    if openid is None:
        raise HTTPException(401, "令牌无效或过期")
    return openid


def _resolve_openid(header_val) -> str:
    if not header_val:
        raise HTTPException(401, "请先登录")
    if header_val.startswith("Bearer "):
        return get_openid(header_val)
    return header_val


# ---- Routes ----
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    js_code = body.get("code", "")
    openid = await code_to_openid(js_code)
    if openid is None:
        raise HTTPException(400, "微信登录失败，请重试")
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (openid) VALUES (?)", (openid,))
    conn.commit()
    conn.close()
    token = make_token(openid)
    return {"token": token, "openid": openid}


@app.get("/api/user")
async def get_user(openid: str = Header(None)):
    actual_openid = None
    if openid and not openid.startswith("Bearer "):
        actual_openid = openid
    else:
        try:
            actual_openid = get_openid(openid)
        except HTTPException:
            actual_openid = openid

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE openid = ?", (actual_openid,)).fetchone()
    conn.close()

    if user is None:
        return {"free_left": 1, "is_subscribed": False, "extra_uses": 0, "total_tasks": 0}

    free_left = 1
    if user["last_free_date"] == today_str() and user["free_uses_today"] >= 1:
        free_left = 0
    if user["subscription_expiry"] and user["subscription_expiry"] > now_iso():
        free_left = 999

    return {
        "free_left": free_left,
        "is_subscribed": bool(user["subscription_expiry"] and user["subscription_expiry"] > now_iso()),
        "subscription_expiry": user["subscription_expiry"],
        "extra_uses": user["extra_uses"],
        "total_tasks": user["total_tasks"],
    }


@app.post("/api/tasks")
async def create_task(request: Request, openid: str = Header(None)):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "请提供链接")

    actual_openid = _resolve_openid(openid)

    if not url.startswith("http"):
        raise HTTPException(400, "请提供有效链接")

    conn = get_db()

    # Deduplicate: same URL within 1 hour
    existing = conn.execute(
        "SELECT id FROM tasks WHERE user_openid = ? AND url = ? AND created_at > datetime('now', '-1 hour')",
        (actual_openid, url),
    ).fetchone()
    if existing:
        conn.close()
        return {"task_id": existing["id"], "status": "existing"}

    # Quota check
    can_use, reason = check_quota(conn, actual_openid)
    if not can_use:
        conn.close()
        raise HTTPException(402, reason)

    deduct_quota(conn, actual_openid)

    task_id = uuid.uuid4().hex[:12]
    platform = detect_platform(url)
    conn.execute(
        "INSERT INTO tasks (id, user_openid, url, platform) VALUES (?, ?, ?, ?)",
        (task_id, actual_openid, url, platform),
    )
    conn.commit()
    conn.close()

    import threading
    t = threading.Thread(target=_process_task, args=(task_id, url, platform))
    t.start()

    return {"task_id": task_id, "status": "pending"}


def _process_task(task_id: str, url: str, platform: str):
    try:
        update_task_status(task_id, "downloading")
        audio_path = download_audio(url, task_id)
        if not audio_path:
            update_task_status(task_id, "failed", "音频下载失败")
            return

        update_task_status(task_id, "transcribing")
        chunks = slice_audio(task_id)
        if not chunks:
            update_task_status(task_id, "failed", "音频切片失败")
            return
        full_text = transcribe_all(task_id, chunks)

        if not full_text or full_text.strip() == "":
            update_task_status(task_id, "failed", "转录结果为空")
            return

        update_task_status(task_id, "summarizing")
        summary = summarize_transcript(full_text)

        title = "未命名"
        source_name = platform
        for line in summary.split("\n"):
            if line.startswith("**标题**"):
                title = line.replace("**标题**", "").replace(":", "：").strip(":： ")[:50]
                break

        card_path = generate_card(task_id, title, source_name, summary)

        conn = get_db()
        conn.execute(
            "INSERT INTO summaries (task_id, title, source_name, full_text, card_image_path) VALUES (?, ?, ?, ?, ?)",
            (task_id, title, source_name, summary, card_path),
        )
        conn.commit()
        conn.close()

        update_task_status(task_id, "completed")
    except Exception as e:
        update_task_status(task_id, "failed", str(e)[:500])


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, openid: str = Header(None)):
    actual_openid = _resolve_openid(openid)
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        conn.close()
        raise HTTPException(404, "任务不存在")
    if task["user_openid"] != actual_openid:
        conn.close()
        raise HTTPException(403, "无权查看此任务")

    result = {
        "task_id": task["id"],
        "status": task["status"],
        "url": task["url"],
        "created_at": task["created_at"],
        "error_message": task["error_message"],
    }

    if task["status"] == "completed":
        summary = conn.execute(
            "SELECT * FROM summaries WHERE task_id = ?", (task_id,)
        ).fetchone()
        if summary:
            result["summary"] = {
                "title": summary["title"],
                "source_name": summary["source_name"],
                "full_text": summary["full_text"],
                "card_url": f"/static/cards/{task_id}.png",
            }

    conn.close()
    return result


@app.get("/api/tasks")
async def list_tasks(page: int = 1, openid: str = Header(None)):
    actual_openid = _resolve_openid(openid)
    conn = get_db()
    offset = (page - 1) * 20
    rows = conn.execute(
        "SELECT id, url, platform, status, created_at FROM tasks WHERE user_openid = ? ORDER BY created_at DESC LIMIT 21 OFFSET ?",
        (actual_openid, offset),
    ).fetchall()
    has_more = len(rows) > 20
    tasks = [dict(r) for r in rows[:20]]
    conn.close()
    return {"tasks": tasks, "has_more": has_more, "page": page}


@app.post("/api/codes/redeem")
async def redeem(request: Request, openid: str = Header(None)):
    actual_openid = _resolve_openid(openid)
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "请输入兑换码")

    conn = get_db()
    result = redeem_code(conn, code, actual_openid)
    conn.close()

    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result


# ---- Admin ----
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "password123")


@app.get("/admin")
async def admin_page(request: Request):
    auth = request.headers.get("Authorization", "")
    if "Basic" in auth:
        import base64
        try:
            decoded = base64.b64decode(auth.replace("Basic ", "")).decode()
            user, pwd = decoded.split(":", 1)
            if user == ADMIN_USER and pwd == ADMIN_PASS:
                return HTMLResponse(_admin_html())
        except Exception:
            pass

    return HTMLResponse(
        '<html><head><meta charset="UTF-8"></head><body style="font-family:sans-serif;padding:40px">'
        '<h2>链接解牛 管理后台</h2><p>请输入用户名和密码</p></body></html>',
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=\"Admin\""},
    )


def _admin_html() -> str:
    conn = get_db()
    today = today_str()
    new_users_today = conn.execute("SELECT COUNT(*) FROM users WHERE date(created_at) = ?", (today,)).fetchone()[0]
    tasks_today = conn.execute("SELECT COUNT(*) FROM tasks WHERE date(created_at) = ?", (today,)).fetchone()[0]
    codes_redeemed_today = conn.execute("SELECT COUNT(*) FROM codes WHERE date(redeemed_at) = ?", (today,)).fetchone()[0]
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    active_subs = conn.execute("SELECT COUNT(*) FROM users WHERE subscription_expiry > datetime('now')").fetchone()[0]
    total_redeemed = conn.execute("SELECT COUNT(*) FROM codes WHERE redeemed_by IS NOT NULL").fetchone()[0]

    recent = conn.execute(
        "SELECT id, user_openid, platform, status, created_at, completed_at, error_message FROM tasks ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    conn.close()

    rows_html = ""
    for r in recent:
        status_color = {"completed": "green", "failed": "red", "pending": "gray"}.get(r["status"], "black")
        rows_html += f"<tr><td>{r['id']}</td><td>{r['user_openid'][:8]}..</td><td>{r['platform']}</td><td style='color:{status_color}'>{r['status']}</td><td>{r['created_at'][:19]}</td><td>{r.get('completed_at','')[:19] if r.get('completed_at') else ''}</td><td style='color:red;max-width:200px;overflow:hidden'>{r.get('error_message','')[:100]}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><title>链接解牛 - 管理</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 24px; background: #f5f5f5; }}
h1 {{ color: #1a1a2e; }}
.stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
.card {{ background: #fff; border-radius: 12px; padding: 20px; min-width: 140px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
.card .num {{ font-size: 36px; font-weight: 800; color: #e94560; }}
.card .label {{ font-size: 14px; color: #888; margin-top: 4px; }}
.gen-section {{ background: #fff; border-radius: 12px; padding: 24px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
.gen-section button {{ background: #e94560; color: #fff; border: none; padding: 8px 20px; border-radius: 8px; font-size: 16px; cursor: pointer; margin-right: 10px; }}
.gen-section input, .gen-section select {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }}
.code-list {{ margin-top: 12px; font-family: monospace; white-space: pre; background: #f9f9f9; padding: 12px; border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
th {{ background: #1a1a2e; color: #fff; padding: 12px; text-align: left; }}
td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
</style></head>
<body>
<h1>链接解牛 管理后台</h1>
<div class="stats">
  <div class="card"><div class="num">{new_users_today}</div><div class="label">今日新增用户</div></div>
  <div class="card"><div class="num">{tasks_today}</div><div class="label">今日调用次数</div></div>
  <div class="card"><div class="num">{codes_redeemed_today}</div><div class="label">今日兑换数</div></div>
  <div class="card"><div class="num">{total_users}</div><div class="label">总用户</div></div>
  <div class="card"><div class="num">{total_tasks}</div><div class="label">总任务</div></div>
  <div class="card"><div class="num">{active_subs}</div><div class="label">活跃订阅</div></div>
  <div class="card"><div class="num">{total_redeemed}</div><div class="label">已兑换码数</div></div>
</div>
<div class="gen-section">
  <h3>生成兑换码</h3>
  <select id="codeType">
    <option value="month">月卡 (30天)</option>
    <option value="10pack">次卡 (10次)</option>
  </select>
  <input id="codeCount" type="number" value="10" min="1" max="100" style="width:80px">
  <button onclick="generateCodes()">生成</button>
  <div class="code-list" id="codeList"></div>
</div>
<h3>最近任务</h3>
<table>
<tr><th>ID</th><th>用户</th><th>平台</th><th>状态</th><th>创建时间</th><th>完成时间</th><th>错误</th></tr>
{rows_html}
</table>
<script>
async function generateCodes() {{
  const type = document.getElementById('codeType').value;
  const count = document.getElementById('codeCount').value;
  const resp = await fetch('/admin/api/codes/generate?type=' + type + '&count=' + count);
  const data = await resp.json();
  document.getElementById('codeList').textContent = data.codes.map((c, i) => (i+1) + '. ' + c).join('\\n');
  document.getElementById('codeList').style.display = 'block';
}}
</script>
</body></html>"""


@app.get("/admin/api/codes/generate")
async def admin_generate_codes(type: str = "month", count: int = 10):
    conn = get_db()
    codes = generate_codes(conn, type, count)
    conn.close()
    return {"codes": codes, "count": len(codes)}


@app.get("/admin/api/stats")
async def admin_stats():
    conn = get_db()
    today = today_str()
    stats = {
        "new_users_today": conn.execute("SELECT COUNT(*) FROM users WHERE date(created_at) = ?", (today,)).fetchone()[0],
        "tasks_today": conn.execute("SELECT COUNT(*) FROM tasks WHERE date(created_at) = ?", (today,)).fetchone()[0],
        "total_users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "total_tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
    }
    conn.close()
    return stats


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
