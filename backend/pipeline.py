import subprocess
import os
import uuid
import re
import httpx
from db import get_db, now_iso

WORK_DIR = "/tmp/summary_miniapp"


def task_dir(task_id: str) -> str:
    d = os.path.join(WORK_DIR, task_id)
    os.makedirs(d, exist_ok=True)
    return d


def detect_platform(url: str) -> str:
    if "xiaoyuzhoufm.com" in url:
        return "xiaoyuzhou"
    if any(kw in url for kw in ["podcast", "rss", "feed"]):
        return "podcast_rss"
    return "other"


def download_audio(url: str, task_id: str) -> str | None:
    """Download audio, return path to m4a file or None on failure."""
    d = task_dir(task_id)
    outpath = os.path.join(d, "audio.m4a")

    if "xiaoyuzhoufm.com" in url:
        return _download_xiaoyuzhou(url, outpath)
    else:
        return _download_ytdlp(url, outpath)


def _download_xiaoyuzhou(url: str, outpath: str) -> str | None:
    """Parse xiaoyuzhou page, find audio URL, download."""
    import re
    # Try to extract episode ID from URL
    eid_match = re.search(r'/episode/([a-zA-Z0-9]+)', url)
    if not eid_match:
        return None
    eid = eid_match[0].split('/')[-1]

    # Use the xiaoyuzhou API endpoint
    api_url = f"https://www.xiaoyuzhoufm.com/api/v1/episode/{eid}"
    try:
        r = httpx.get(api_url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        }, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            return None
        data = r.json()
        audio_url = data.get("data", {}).get("enclosure_url", "")
        if not audio_url:
            return None

        # Download the audio
        audio_r = httpx.get(audio_url, timeout=600, follow_redirects=True)
        if audio_r.status_code != 200:
            return None
        with open(outpath, "wb") as f:
            f.write(audio_r.content)
        return outpath
    except Exception:
        return None


def _download_ytdlp(url: str, outpath: str) -> str | None:
    """Use yt-dlp to download audio."""
    d = os.path.dirname(outpath)
    try:
        subprocess.run(
            [
                "yt-dlp", "-f", "bestaudio", "--extract-audio",
                "--audio-format", "m4a", "-o", outpath,
                "--no-playlist", "--max-filesize", "500m",
                url,
            ],
            cwd=d, check=True, capture_output=True, timeout=300,
        )
        if os.path.exists(outpath) and os.path.getsize(outpath) > 0:
            return outpath
        # yt-dlp might append .m4a extension
        alt = outpath + ".m4a"
        if os.path.exists(alt) and os.path.getsize(alt) > 0:
            os.rename(alt, outpath)
            return outpath
        return None
    except subprocess.CalledProcessError:
        return None
    except subprocess.TimeoutExpired:
        return None


def slice_audio(task_id: str) -> list[str]:
    """Slice audio into 5-min WAV chunks. Returns list of chunk paths."""
    d = task_dir(task_id)
    audio = os.path.join(d, "audio.m4a")
    if not os.path.exists(audio):
        return []

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", audio,
            "-ar", "16000", "-ac", "1",
            "-f", "segment", "-segment_time", "300",
            "-c:a", "pcm_s16le",
            os.path.join(d, "chunk_%03d.wav"),
        ],
        check=True, capture_output=True, timeout=120,
    )
    import glob
    return sorted(glob.glob(os.path.join(d, "chunk_*.wav")))


# ---- Transcription ----
ASR_API = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "TeleAI/TeleSpeechASR"


def get_sf_key() -> str:
    for k in ("SF_KEY", "SILICONFLOW_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v
    p = os.path.expanduser("~/.config/siliconflow/api_key")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    raise RuntimeError("SiliconFlow API key not found")


def transcribe_chunk(chunk_path: str) -> str:
    """Transcribe a single WAV chunk. Returns text or error string."""
    import urllib.request
    import json

    boundary = "----" + uuid.uuid4().hex
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    body.append(ASR_MODEL.encode() + b"\r\n")
    body.append(f"--{boundary}\r\n".encode())
    fname = os.path.basename(chunk_path)
    body.append(
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode()
    )
    body.append(b"Content-Type: audio/wav\r\n\r\n")
    with open(chunk_path, "rb") as f:
        body.append(f.read())
    body.append(b"\r\n")
    body.append(f"--{boundary}--\r\n".encode())
    data = b"".join(body)

    key = get_sf_key()
    req = urllib.request.Request(ASR_API, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("text", "")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                import time
                time.sleep(5 * (attempt + 1))
                continue
            return f"[ERROR] HTTP {e.code}: {err}"
        except Exception as e:
            if attempt < 3:
                import time
                time.sleep(5 * (attempt + 1))
                continue
            return f"[ERROR] {str(e)}"
    return "[ERROR] exhausted retries"


def transcribe_all(task_id: str, chunks: list[str]) -> str:
    """Transcribe all chunks concurrently, return full transcript."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    d = task_dir(task_id)
    results = {}

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(transcribe_chunk, c): c for c in chunks}
        for fut in as_completed(futures):
            chunk = futures[fut]
            text = fut.result()
            results[chunk] = text

    full = "\n".join(results[c] for c in chunks)
    tx_path = os.path.join(d, "full_transcript.txt")
    with open(tx_path, "w") as f:
        f.write(full)
    return full


# ---- Summarization ----

SUMMARY_PROMPT = """你是信息提炼专家。基于以下音频转录稿，写一份结构化摘要：

## 摘要格式

**标题**: [提取或概括核心主题，20字以内]
**主讲人**: [如有，提取主讲人信息]
**核心主题**: [1-2句话概括]

### 核心论点
- 论点1及推理过程
- 论点2及推理过程
...

### 金句
- "引用原文金句"（如有）

### 案例/故事
- 具体案例简述（如有）

### 总结
1. [核心观点，不超过5条]

要求：简洁、准确、不编造。转录稿可能有ASR错误，请根据上下文推断修正。"""


async def summarize_transcript(full_text: str) -> str:
    """Send full transcript to DeepSeek for structured summary."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not found")

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": f"以下是音频转录稿，请按格式生成摘要：\n\n{full_text}"},
                ],
                "max_tokens": 2000,
                "temperature": 0.3,
            },
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def update_task_status(task_id: str, status: str, error: str = ""):
    conn = get_db()
    if status == "completed":
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, now_iso(), task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, error_message = ? WHERE id = ?",
            (status, error, task_id),
        )
    conn.commit()
    conn.close()


CARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "cards")
os.makedirs(CARD_DIR, exist_ok=True)


def generate_card(task_id: str, title: str, source_name: str, summary_md: str) -> str:
    """Render HTML card → screenshot with Playwright. Returns card image path."""
    # Extract bullet points from summary
    points = _extract_points(summary_md)

    # Load and fill template
    template_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "card_template.html"
    )
    with open(template_path) as f:
        html = f.read()

    html = html.replace("{{source_name}}", source_name)
    html = html.replace("{{title}}", title[:50])

    # Build points HTML
    points_html = ""
    for p in points[:5]:
        points_html += f'<div class="point"><div class="bullet"></div><span>{p[:80]}</span></div>\n'
    html = html.replace(
        '{{#points}}\n    <div class="point">\n      <div class="bullet"></div>\n      <span>{{.}}</span>\n    </div>\n    {{/points}}',
        points_html,
    )

    # Write temp HTML
    html_path = os.path.join(CARD_DIR, f"{task_id}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # Render with Playwright
    from playwright.sync_api import sync_playwright
    png_path = os.path.join(CARD_DIR, f"{task_id}.png")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 750, "height": 1334})
        page.goto(f"file://{html_path}")
        page.screenshot(path=png_path, full_page=False)
        browser.close()

    # Cleanup HTML
    os.remove(html_path)
    return png_path


def _extract_points(md: str) -> list[str]:
    """Extract key points from summary markdown text."""
    points = []
    # Grab bullet lines, numbered list lines, and bolded lines
    in_summary = False
    for line in md.split("\n"):
        line = line.strip()
        if "### 总结" in line or "## 总结" in line:
            in_summary = True
            continue
        if line.startswith("#") and "总结" not in line:
            in_summary = False
        if in_summary and (line.startswith("- ") or line.startswith("* ") or re.match(r"^\d+\.", line)):
            point = re.sub(r"^[-\*\d]+\.?\s*", "", line)
            points.append(point.strip())
        if len(points) >= 5:
            break

    # Fallback: grab any bullet/numbered line from the whole doc
    if not points:
        for line in md.split("\n"):
            line = line.strip()
            if (line.startswith("- ") or line.startswith("* ")) and len(line) > 10:
                point = re.sub(r"^[-\*]\s*", "", line)
                points.append(point.strip())
            if len(points) >= 5:
                break

    # Last fallback: first few non-empty lines
    if not points:
        points = [l.strip() for l in md.split("\n") if l.strip() and not l.startswith("#")][:3]

    return points
