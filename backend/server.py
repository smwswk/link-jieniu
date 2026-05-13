import uuid
import os
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
import hashlib, time as time_module, secrets as secrets_module
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


def _resolve_openid_from_request(request: Request) -> str:
    """Extract openid from Authorization header or cookie."""
    # Try Authorization header first
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ")
        openid = verify_token(token)
        if openid:
            return openid
    # Try cookie
    token = request.cookies.get("token")
    if token:
        openid = verify_token(token)
        if openid:
            return openid
    raise HTTPException(401, "请先登录")


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


@app.get("/api/debug/ytdlp")
async def debug_ytdlp():
    import subprocess, shutil
    results = {}
    results["which"] = shutil.which("yt-dlp") or "NOT FOUND"
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=15)
        results["version"] = r.stdout.strip() or r.stderr.strip()
        results["returncode"] = r.returncode
    except Exception as e:
        results["version_error"] = str(e)
    return results


@app.get("/api/user")
async def get_user(request: Request):
    try:
        actual_openid = _resolve_openid_from_request(request)
    except HTTPException:
        actual_openid = "anonymous"

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
async def create_task(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "请提供链接")

    actual_openid = _resolve_openid_from_request(request)

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
            update_task_status(task_id, "failed", "音频下载失败(API和yt-dlp均失败)")
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
        summary = asyncio.run(summarize_transcript(full_text))

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
async def get_task(task_id: str, request: Request):
    actual_openid = _resolve_openid_from_request(request)
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
async def list_tasks(request: Request, page: int = 1):
    actual_openid = _resolve_openid_from_request(request)
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
async def redeem(request: Request):
    actual_openid = _resolve_openid_from_request(request)
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


# ---- JSSDK ----
_jsapi_ticket: dict = {"value": "", "expires_at": 0}


@app.get("/api/jsapi/config")
async def jsapi_config(url: str = ""):
    """Return wx.config parameters for WeChat JSSDK."""
    import httpx as _httpx
    now_ts = int(time_module.time())

    # Refresh ticket if needed (cache 7000s)
    if not _jsapi_ticket["value"] or now_ts > _jsapi_ticket["expires_at"]:
        # First get access_token
        token_url = "https://api.weixin.qq.com/cgi-bin/token"
        from auth import WX_OA_APPID as appid, WX_OA_SECRET as secret
        async with _httpx.AsyncClient() as client:
            resp = await client.get(token_url, params={
                "grant_type": "client_credential",
                "appid": appid,
                "secret": secret,
            }, timeout=10)
            data = resp.json()
        access_token = data.get("access_token", "")

        if access_token:
            ticket_url = "https://api.weixin.qq.com/cgi-bin/ticket/getticket"
            async with _httpx.AsyncClient() as client:
                resp = await client.get(ticket_url, params={
                    "type": "jsapi",
                    "access_token": access_token,
                }, timeout=10)
                data = resp.json()
            _jsapi_ticket["value"] = data.get("ticket", "")
            _jsapi_ticket["expires_at"] = now_ts + 7000

    noncestr = secrets_module.token_hex(16)
    timestamp = now_ts
    ticket = _jsapi_ticket["value"]

    sign_str = f"jsapi_ticket={ticket}&noncestr={noncestr}&timestamp={timestamp}&url={url}"
    signature = hashlib.sha1(sign_str.encode()).hexdigest()

    from auth import WX_OA_APPID as appid
    return {
        "appId": appid,
        "timestamp": timestamp,
        "nonceStr": noncestr,
        "signature": signature,
    }


# ---- H5 OAuth ----
@app.get("/login")
async def h5_login(request: Request):
    """Redirect to WeChat OAuth page."""
    from auth import get_oauth_url, WX_OA_APPID
    next_path = request.query_params.get("next", "/app")
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("Host", request.base_url.hostname or "localhost:8000")
    if WX_OA_APPID:
        # Production: relay OAuth through GitHub Pages (verified domain)
        redirect_uri = "https://smwswk.github.io/callback.html"
        tunnel_callback = f"{scheme}://{host}/callback"
        state = f"{tunnel_callback}::{next_path}"
    else:
        # Dev mode: callback directly to local server, state = next_path
        redirect_uri = f"{scheme}://{host}/callback"
        state = next_path
    oauth_url = get_oauth_url(redirect_uri, state)
    return RedirectResponse(oauth_url)


@app.get("/callback")
async def h5_callback(code: str = "", state: str = "", next: str = ""):
    """OAuth callback - exchange code for openid, set cookie, redirect to app."""
    from auth import code_to_openid_web
    openid = await code_to_openid_web(code)
    if openid is None:
        return HTMLResponse("<h2>登录失败</h2><p>请重新打开链接</p>", status_code=400)

    # Ensure user exists
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (openid) VALUES (?)", (openid,))
    conn.commit()
    conn.close()

    token = make_token(openid)
    # next param from GitHub Pages relay takes precedence; fall back to state (dev mode)
    next_path = next if next and next.startswith("/") else (state if state and state.startswith("/") else "/app")
    response = RedirectResponse(next_path)
    response.set_cookie(key="token", value=token, httponly=True, max_age=86400 * 30, path="/")
    return response


# ---- H5 Pages ----
@app.get("/")
@app.get("/app")
async def h5_index(request: Request):
    """H5 main page - requires auth, redirects to OAuth if not logged in."""
    try:
        _resolve_openid_from_request(request)
    except HTTPException:
        # Preserve query string (e.g. ?share=TASK_ID) through OAuth
        qs = request.url.query
        next_path = f"/?{qs}" if qs else "/app"
        return RedirectResponse(f"/login?next={quote(next_path, safe='/?=&')}")
    return HTMLResponse(_index_html())


def _index_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>链接解牛</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;color:#333;padding:16px;max-width:500px;margin:0 auto}
.hero{text-align:center;padding:40px 0 30px}
.hero-icon{font-size:48px}
.hero-title{font-size:28px;font-weight:800;color:#1a1a2e;margin-top:10px}
.hero-sub{font-size:14px;color:#999;margin-top:6px}
.input-box{background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 12px rgba(0,0,0,0.06)}
.url-input{width:100%;height:48px;padding:0 12px;background:#f5f5f5;border:1px solid #eee;border-radius:10px;font-size:15px;outline:none}
.submit-btn{width:100%;height:48px;margin-top:12px;background:linear-gradient(135deg,#e94560,#c23152);color:#fff;font-size:17px;font-weight:600;border-radius:10px;border:none;cursor:pointer}
.submit-btn:disabled{opacity:0.6}
.quota{text-align:center;margin-top:14px;font-size:13px;color:#999}
.sub-badge{background:#e94560;color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;margin-left:6px}
.error-msg{background:#fff3f3;color:#e94560;padding:12px;margin-top:12px;border-radius:10px;font-size:13px;display:none}
.error-msg.show{display:block}
.section-title{font-size:18px;font-weight:700;margin:24px 0 12px}
.task-item{display:flex;align-items:center;gap:10px;background:#fff;padding:14px 16px;margin-bottom:8px;border-radius:12px;cursor:pointer}
.task-dot{width:10px;height:10px;border-radius:5px;flex-shrink:0}
.task-dot.completed{background:#4caf50}
.task-dot.failed{background:#e94560}
.task-dot.pending,.task-dot.downloading,.task-dot.transcribing,.task-dot.summarizing{background:#ff9800}
.task-info{flex:1;min-width:0}
.task-url{font-size:14px;color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-time{font-size:11px;color:#ccc;margin-top:4px}
.empty{text-align:center;color:#ccc;padding:40px 0;font-size:14px}
</style>
</head>
<body>
<div class="hero">
  <div class="hero-icon">🎧</div>
  <div class="hero-title">链接解牛</div>
  <div class="hero-sub">粘贴播客链接，秒懂长内容</div>
</div>
<div class="input-box">
  <input class="url-input" id="urlInput" placeholder="粘贴小宇宙/播客链接..." />
  <button class="submit-btn" id="submitBtn" onclick="submit()">开始解牛</button>
</div>
<div id="quotaInfo" class="quota">今日免费 1 次</div>
<div id="errorMsg" class="error-msg"></div>
<div>
  <div class="section-title">最近任务</div>
  <div id="taskList"></div>
</div>
<script>
const sp=new URLSearchParams(location.search);if(sp.has('share')){location.replace('/result?taskId='+sp.get('share'))}
const API = '';
function showError(msg){const e=document.getElementById('errorMsg');e.textContent=msg;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),4000)}
function fmtTime(ts){return ts?ts.slice(0,16).replace('T',' '):''}
function statusDot(s){return '<div class="task-dot '+s+'"></div>'}
function statusEmoji(s){return s==='completed'?'✅':s==='failed'?'❌':'⏳'}
function loadQuota(){fetch(API+'/api/user',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
  let txt='今日免费 '+(d.freeLeft>0?1:0)+' 次';
  if(d.is_subscribed)txt+=' <span class="sub-badge">订阅中</span>';
  if(d.extra_uses>0)txt+=' · 剩余 '+d.extra_uses+' 次';
  document.getElementById('quotaInfo').innerHTML=txt
}).catch(()=>{})}
function loadTasks(){fetch(API+'/api/tasks?page=1',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
  const list=(d.tasks||[]).map(t=>'<div class="task-item" onclick="location=\\'/result?taskId='+t.id+'\\'">'+statusDot(t.status)+'<div class="task-info"><div class="task-url">'+t.url+'</div><div class="task-time">'+fmtTime(t.created_at)+'</div></div>'+statusEmoji(t.status)+'</div>').join('');
  document.getElementById('taskList').innerHTML=list||'<div class="empty">还没有任务，粘贴链接开始吧</div>'
}).catch(()=>{})}
function submit(){
  const url=document.getElementById('urlInput').value.trim();
  if(!url){showError('请粘贴链接');return}
  if(!url.startsWith('http')){showError('请粘贴有效链接（以http开头）');return}
  const btn=document.getElementById('submitBtn');
  btn.disabled=true;btn.textContent='处理中...';
  fetch(API+'/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({url})}).then(r=>{
    if(r.status===402)return r.json().then(d=>{throw new Error(d.detail||'额度不足')});
    if(!r.ok)return r.json().then(d=>{throw new Error(d.detail||'提交失败')});
    return r.json()
  }).then(d=>{
    btn.disabled=false;btn.textContent='开始解牛';
    document.getElementById('urlInput').value='';
    location.href='/result?taskId='+d.task_id
  }).catch(err=>{
    btn.disabled=false;btn.textContent='开始解牛';
    showError(err.message)
  })
}
loadQuota();loadTasks();
</script>
</body>
</html>"""


@app.get("/result")
async def h5_result(request: Request, taskId: str = ""):
    try:
        _resolve_openid_from_request(request)
    except HTTPException:
        return RedirectResponse(f"/login?next={quote(f'/result?taskId={taskId}', safe='/?=&')}")
    if not taskId:
        return HTMLResponse("<h2>缺少任务ID</h2>", status_code=400)
    return HTMLResponse(_result_html(taskId))


def _result_html(task_id: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>摘要 - 链接解牛</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;color:#333;padding:16px;max-width:500px;margin:0 auto}}
.loading{{display:flex;flex-direction:column;align-items:center;padding-top:120px}}
.spinner{{width:48px;height:48px;border:4px solid #eee;border-top-color:#e94560;border-radius:50%;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.loading-text{{margin-top:20px;font-size:16px;color:#333}}
.loading-hint{{margin-top:8px;font-size:13px;color:#999}}
.error-box{{display:flex;flex-direction:column;align-items:center;padding-top:100px;display:none}}
.error-box.show{{display:flex}}
.error-icon{{font-size:48px;margin-bottom:16px}}
.error-title{{font-size:20px;font-weight:700;margin-bottom:8px}}
.error-detail{{font-size:14px;color:#999;text-align:center;padding:0 20px}}
.retry-btn{{margin-top:24px;background:#e94560;color:#fff;border:none;border-radius:10px;padding:10px 40px;font-size:15px;cursor:pointer}}
.result{{display:none}}
.result.show{{display:block}}
.card-image{{width:100%;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,0.1);margin-bottom:16px}}
.summary-box{{background:#fff;border-radius:14px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
.source-tag{{background:#f0f0f0;color:#e94560;padding:4px 14px;border-radius:12px;font-size:12px}}
.summary-title{{font-size:20px;font-weight:800;color:#1a1a2e;display:block;margin-top:10px}}
.summary-body{{font-size:15px;line-height:1.8;color:#555;margin-top:16px}}
.summary-body h1,.summary-body h2,.summary-body h3{{color:#1a1a2e;margin:16px 0 8px;font-size:17px}}
.summary-body li{{margin-left:20px;margin-bottom:6px}}
.actions{{display:flex;gap:12px;margin-top:20px}}
.action-btn{{flex:1;height:44px;border-radius:10px;font-size:15px;font-weight:600;text-align:center;line-height:44px;cursor:pointer}}
.share-btn{{background:#e94560;color:#fff;border:none}}
.save-btn{{background:#fff;color:#e94560;border:2px solid #e94560}}
</style>
</head>
<body>
<div id="loading" class="loading">
  <div class="spinner"></div>
  <div class="loading-text" id="statusText">加载中...</div>
  <div class="loading-hint">音频较长的可能需要几分钟</div>
</div>
<div id="errorBox" class="error-box">
  <div class="error-icon">😞</div>
  <div class="error-title">处理失败</div>
  <div class="error-detail" id="errorDetail"></div>
  <button class="retry-btn" onclick="history.back()">返回</button>
</div>
<div id="resultBox" class="result">
  <img id="cardImg" class="card-image" style="display:none" onclick="previewCard()" />
  <div class="summary-box">
    <span id="sourceTag" class="source-tag"></span>
    <span id="summaryTitle" class="summary-title"></span>
    <div id="summaryBody" class="summary-body"></div>
  </div>
  <div class="actions">
    <button class="action-btn share-btn" id="shareBtn" onclick="shareCard()">分享卡片给朋友</button>
    <button class="action-btn save-btn" id="saveBtn" onclick="saveCard()">保存卡片</button>
  </div>
</div>
<script>
var API='',TASK_ID='{task_id}',CARD_URL='';
var statusMap={{pending:'排队中...',downloading:'正在下载音频...',transcribing:'正在语音转文字...',summarizing:'AI 正在生成摘要...'}};
function simpleMd(md){{
  return md.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>').replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>').replace(/^# (.+)$/gm,'<h1>$1</h1>').replace(/^- (.+)$/gm,'<li>$1</li>').replace(/\\n/g,'<br/>')
}}
function poll(){{
  fetch(API+'/api/tasks/'+TASK_ID,{{credentials:'same-origin'}}).then(r=>r.json()).then(d=>{{
    if(d.status==='completed'){{
      document.getElementById('loading').style.display='none';
      CARD_URL=API+d.summary.card_url;
      document.getElementById('cardImg').src=CARD_URL;
      document.getElementById('cardImg').style.display='block';
      document.getElementById('sourceTag').textContent=d.summary.source_name;
      document.getElementById('summaryTitle').textContent=d.summary.title;
      document.getElementById('summaryBody').innerHTML=simpleMd(d.summary.full_text);
      document.getElementById('resultBox').classList.add('show');
      // Configure WeChat share
      setupWxShare(d.summary.title, CARD_URL);
    }}else if(d.status==='failed'){{
      document.getElementById('loading').style.display='none';
      document.getElementById('errorDetail').textContent=d.error_message||'处理失败';
      document.getElementById('errorBox').classList.add('show');
    }}else{{
      document.getElementById('statusText').textContent=statusMap[d.status]||'处理中...';
      setTimeout(poll,3000)
    }}
  }}).catch(err=>{{
    document.getElementById('loading').style.display='none';
    document.getElementById('errorDetail').textContent='网络错误';
    document.getElementById('errorBox').classList.add('show')
  }})
}}
function previewCard(){{window.open(CARD_URL)}}
function saveCard(){{
  var a=document.createElement('a');a.href=CARD_URL;a.download='card.png';a.click()
}}
function shareCard(){{
  // Fallback: use Web Share API if available
  if(navigator.share){{navigator.share({{title:'AI 替我读了这篇内容',url:location.href}})}}else{{alert('请长按卡片图片保存后分享')}}
}}
function setupWxShare(title,imgUrl){{}}
// WeChat JSSDK setup
var url=location.href.split('#')[0];
fetch(API+'/api/jsapi/config?url='+encodeURIComponent(url),{{credentials:'same-origin'}}).then(r=>r.json()).then(cfg=>{{
  if(!window.wx)return;
  wx.config({{debug:false,appId:cfg.appId,timestamp:cfg.timestamp,nonceStr:cfg.nonceStr,signature:cfg.signature,jsApiList:['updateAppMessageShareData','updateTimelineShareData']}});
  wx.ready(function(){{
    var share={{title:'AI 替你读了《'+title+'》',desc:'粘贴链接，秒懂长内容',link:location.origin+'/?share='+TASK_ID,imgUrl:imgUrl}};
    wx.updateAppMessageShareData(share);
    wx.updateTimelineShareData(share);
  }})
}});
poll();
</script>
<script src="https://res.wx.qq.com/open/js/jweixin-1.6.0.js"></script>
</body>
</html>"""


@app.get("/mine")
async def h5_mine(request: Request):
    try:
        _resolve_openid_from_request(request)
    except HTTPException:
        return RedirectResponse("/login?next=/mine")
    return HTMLResponse(_mine_html())


def _mine_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>我的 - 链接解牛</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;color:#333;padding:16px;max-width:500px;margin:0 auto}
.section-title{font-size:16px;font-weight:700;color:#333;margin-bottom:12px}
.quota-card,.redeem-card{background:#fff;border-radius:14px;padding:18px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,0.06)}
.quota-row{display:flex}
.quota-item{flex:1;text-align:center}
.quota-num{font-size:32px;font-weight:800;color:#e94560}
.quota-label{font-size:13px;color:#999;display:block;margin-top:4px}
.sub-status{margin-top:12px;text-align:center;background:#f0fff0;color:#4caf50;padding:8px;border-radius:8px;font-size:13px;display:none}
.sub-status.show{display:block}
.redeem-row{display:flex;gap:10px}
.code-input{flex:1;height:44px;padding:0 12px;background:#f5f5f5;border:1px solid #eee;border-radius:8px;font-size:15px;outline:none;text-transform:uppercase}
.redeem-btn{width:80px;height:44px;background:#e94560;color:#fff;font-size:15px;border-radius:8px;border:none;cursor:pointer}
.redeem-btn:disabled{opacity:0.6}
.redeem-msg{margin-top:10px;font-size:13px;padding:8px;border-radius:8px;display:none}
.redeem-msg.show{display:block}
.redeem-msg.success{background:#f0fff0;color:#4caf50}
.redeem-msg.fail{background:#fff3f3;color:#e94560}
.price-info{display:flex;justify-content:space-between;margin-top:10px;font-size:12px;color:#ccc}
.buy-hint{color:#e94560}
.task-item{display:flex;align-items:center;gap:10px;background:#fff;padding:14px 16px;margin-bottom:8px;border-radius:12px;cursor:pointer}
.task-dot{width:10px;height:10px;border-radius:5px;flex-shrink:0}
.task-dot.completed{background:#4caf50}.task-dot.failed{background:#e94560}
.task-dot.pending,.task-dot.downloading,.task-dot.transcribing,.task-dot.summarizing{background:#ff9800}
.task-info{flex:1;min-width:0}
.task-url{font-size:14px;color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-time{font-size:11px;color:#ccc;margin-top:4px}
.empty{text-align:center;color:#ccc;padding:40px 0;font-size:14px}
</style>
</head>
<body>
<div class="quota-card">
  <div class="section-title">我的额度</div>
  <div class="quota-row">
    <div class="quota-item"><div class="quota-num" id="freeNum">1</div><div class="quota-label">今日免费</div></div>
    <div class="quota-item"><div class="quota-num" id="extraNum">0</div><div class="quota-label">剩余次数</div></div>
    <div class="quota-item"><div class="quota-num" id="totalNum">0</div><div class="quota-label">累计使用</div></div>
  </div>
  <div class="sub-status" id="subStatus">✅ 订阅中 · 不限次数</div>
</div>
<div class="redeem-card">
  <div class="section-title">兑换码</div>
  <div class="redeem-row">
    <input class="code-input" id="codeInput" placeholder="输入兑换码" maxlength="12" />
    <button class="redeem-btn" id="redeemBtn" onclick="redeem()">兑换</button>
  </div>
  <div class="redeem-msg" id="redeemMsg"></div>
  <div class="price-info">
    <span>月卡 ¥9.9/30天 · 次卡 ¥2.9/10次</span>
    <span class="buy-hint">联系客服购买</span>
  </div>
</div>
<div>
  <div class="section-title">历史任务</div>
  <div id="taskList"></div>
</div>
<script>
var API='';
function fmtTime(ts){return ts?ts.slice(0,16).replace('T',' '):''}
function statusDot(s){return '<div class="task-dot '+s+'"></div>'}
function statusEmoji(s){return s==='completed'?'✅':s==='failed'?'❌':'⏳'}
function loadQuota(){fetch(API+'/api/user',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
  document.getElementById('freeNum').textContent=d.freeLeft>0?1:0;
  document.getElementById('extraNum').textContent=d.extra_uses||0;
  document.getElementById('totalNum').textContent=d.total_tasks||0;
  var sub=document.getElementById('subStatus');
  if(d.is_subscribed)sub.classList.add('show');else sub.classList.remove('show')
}).catch(()=>{})}
function loadTasks(){fetch(API+'/api/tasks?page=1',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
  var list=(d.tasks||[]).map(t=>'<div class="task-item" onclick="location=\\'/result?taskId='+t.id+'\\'">'+statusDot(t.status)+'<div class="task-info"><div class="task-url">'+t.url+'</div><div class="task-time">'+fmtTime(t.created_at)+'</div></div>'+statusEmoji(t.status)+'</div>').join('');
  document.getElementById('taskList').innerHTML=list||'<div class="empty">暂无任务</div>'
}).catch(()=>{})}
function redeem(){
  var code=document.getElementById('codeInput').value.trim().toUpperCase();
  if(!code){showRedeemMsg('请输入兑换码',false);return}
  var btn=document.getElementById('redeemBtn');
  btn.disabled=true;btn.textContent='兑换中';
  fetch(API+'/api/codes/redeem',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({code})}).then(r=>r.json()).then(d=>{
    btn.disabled=false;btn.textContent='兑换';
    document.getElementById('codeInput').value='';
    showRedeemMsg(d.type==='month'?'月卡兑换成功！已延长30天 🎉':'次卡兑换成功！已增加10次 🎉',true);
    loadQuota()
  }).catch(err=>{
    btn.disabled=false;btn.textContent='兑换';
    err.json().then(d=>showRedeemMsg(d.detail||'兑换失败',false)).catch(()=>showRedeemMsg('兑换失败',false))
  })
}
function showRedeemMsg(msg,success){
  var el=document.getElementById('redeemMsg');
  el.textContent=msg;el.className='redeem-msg show '+(success?'success':'fail')
}
loadQuota();loadTasks();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
