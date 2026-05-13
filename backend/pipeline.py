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
