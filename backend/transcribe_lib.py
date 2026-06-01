"""核心转录逻辑 — 适配 web 服务的纯函数版本（不 sys.exit，错误抛异常）"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

MAX_FILE_SIZE = 24 * 1024 * 1024  # Groq 25MB 上限留 1MB 余量
DEFAULT_SEGMENT_SECONDS = 120
DEFAULT_GROQ_TIMEOUT_SECONDS = 120
SUPPORTED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".webm"}
SUPPORTED_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|BV[a-zA-Z0-9]+)")


class TranscribeError(Exception):
    pass


def env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, value))


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://")) or bool(BILIBILI_PATTERN.search(s))


def extract_url(text: str) -> str:
    """从手机 App 分享文本里抽出真正的视频链接。

    例：'【标题】 https://b23.tv/2zvrRm7 复制此链接，打开手机Bilibili查看'
        -> 'https://b23.tv/2zvrRm7'
    抽不到链接就原样返回（可能是本地文件路径）。
    """
    s = text.strip()
    # 1) 第一个 http(s) 链接：遇空白 / 中文 / 常见全角标点即停
    m = re.search(r"https?://[^\s一-鿿，。、！？；：【】（）“”‘’]+", s)
    if m:
        return m.group(0).rstrip("/.,;)]>，。、")
    # 2) 裸 BV 号（B站 App 偶尔只给号）
    m = re.search(r"BV[a-zA-Z0-9]{8,}", s)
    if m:
        return f"https://www.bilibili.com/video/{m.group(0)}"
    return s


def find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise TranscribeError("ffmpeg not found in PATH")
    return p


def find_yt_dlp() -> list[str]:
    """优先用 venv 的 python -m yt_dlp（确保用 venv 里 pip 装的 yt-dlp），fallback 到 PATH 上的 yt-dlp"""
    # 1. 当前 Python 解释器的 yt_dlp 模块（venv 里的）
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, check=True)
        return [sys.executable, "-m", "yt_dlp"]
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass
    # 2. PATH 上的 yt-dlp 二进制
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    raise TranscribeError("yt-dlp not installed (pip install yt-dlp)")


def download(url: str, tmp_dir: str, log: Callable[[str], None]) -> tuple[str, str]:
    """下载视频，返回 (file_path, title)。带浏览器 UA + Referer + 可选 cookies"""
    yt = find_yt_dlp()
    out_tpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    is_bilibili = "bilibili" in url or "b23.tv" in url
    referer = "https://www.bilibili.com" if is_bilibili else url

    # 按平台挑 cookies 文件：优先 COOKIES_DIR/<platform>.txt（UI 上传），再 fallback 到环境变量
    cookies_file = None
    cookies_dir = os.environ.get("COOKIES_DIR")
    def _pick(platform: str, env_name: str):
        if cookies_dir:
            f = os.path.join(cookies_dir, f"{platform}.txt")
            if os.path.isfile(f):
                return f
        v = os.environ.get(env_name)
        if v and os.path.isfile(v):
            return v
        return None

    if is_bilibili:
        cookies_file = _pick("bilibili", "BILIBILI_COOKIES")
    if cookies_file:
        log(f"using cookies: {os.path.basename(cookies_file)}")

    base_args = [
        "--no-playlist", "-o", out_tpl,
        "--no-warnings", "--retries", "3",
        "--fragment-retries", "3", "--socket-timeout", "30",
        "--user-agent", ua,
        "--referer", referer,
        "--add-header", "Accept-Language:zh-CN,zh;q=0.9,en;q=0.8",
    ]
    if cookies_file:
        base_args += ["--cookies", cookies_file]

    last_err = ""
    for fmt in ("ba/b", "b"):
        log(f"yt-dlp -f {fmt} ...")
        cmd = list(yt) + ["-f", fmt] + base_args + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            break
        last_err = r.stderr
    else:
        # 给 B 站 412 加专门提示
        err_tail = last_err[-500:]
        if is_bilibili and "412" in err_tail:
            err_tail += "\n\n[提示] B站 412 = 你 VPS 的 IP 被风控。解决：导出浏览器 B 站 cookies 上传 VPS，在 .env 设 BILIBILI_COOKIES=/path/to/cookies.txt"
        raise TranscribeError(f"yt-dlp 全部尝试失败:\n{err_tail}")

    files = list(Path(tmp_dir).glob("*"))
    if not files:
        raise TranscribeError("yt-dlp 完成但没生成文件")
    f = max(files, key=lambda p: p.stat().st_mtime)
    title = re.sub(r"[_\-]+", " ", f.stem).strip()[:80] or "untitled"
    log(f"下载 OK: {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
    return str(f), title


def extract_audio(video_path: str, tmp_dir: str, log: Callable[[str], None]) -> str:
    audio = os.path.join(tmp_dir, "audio.wav")
    cmd = [
        find_ffmpeg(), "-i", video_path,
        "-vn", "-ar", "16000", "-ac", "1",
        "-acodec", "pcm_s16le", "-y", audio,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise TranscribeError(f"ffmpeg 提取音频失败:\n{r.stderr[-500:]}")
    log(f"音频 {os.path.getsize(audio) / 1024 / 1024:.1f} MB")
    return audio


def split_audio(audio: str, tmp_dir: str, log: Callable[[str], None]) -> list[str]:
    if os.path.getsize(audio) <= MAX_FILE_SIZE:
        return [audio]
    segment_seconds = env_int("GROQ_SEGMENT_SECONDS", DEFAULT_SEGMENT_SECONDS, 60, 600)
    log(f"超过 24MB，按 {segment_seconds}s 分段")
    pattern = os.path.join(tmp_dir, "chunk_%03d.wav")
    cmd = [find_ffmpeg(), "-i", audio, "-f", "segment",
           "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
           "-vn", "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
           "-y", pattern]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise TranscribeError(f"分段失败:\n{r.stderr[-500:]}")
    chunks = sorted(Path(tmp_dir).glob("chunk_*.wav"))
    log(f"分 {len(chunks)} 段")
    return [str(c) for c in chunks]


def _groq_one_call(audio_path: str, api_key: str, lang: str, model: str) -> tuple[int, dict | str]:
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = env_int("GROQ_TIMEOUT_SECONDS", DEFAULT_GROQ_TIMEOUT_SECONDS, 30, 300)
    with open(audio_path, "rb") as f:
        files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
        data = {"model": model, "response_format": "json",
                "language": lang, "temperature": "0"}
        with httpx.Client(timeout=float(timeout)) as c:
            r = c.post(url, headers=headers, files=files, data=data)
    if r.status_code == 200:
        return 200, r.json()
    return r.status_code, r.text


def transcribe_chunk(audio_path: str, api_keys: list[str], start_idx: int,
                     lang: str, model: str, log: Callable[[str], None],
                     max_server_retries: int = 5) -> tuple[str, int]:
    idx = start_idx
    keys_rejected = 0          # 429/401/403 或网络异常：换 key 计数
    server_retries = 0         # 5xx 服务端错误：退避重试计数
    while keys_rejected < len(api_keys):
        key = api_keys[idx]
        try:
            status, resp = _groq_one_call(audio_path, key, lang, model)
        except Exception as e:
            log(f"Key{idx + 1} 网络异常: {e}")
            idx = (idx + 1) % len(api_keys)
            keys_rejected += 1
            continue
        if status == 200:
            return resp.get("text", ""), idx  # type: ignore
        if status in (429, 401, 403):
            log(f"Key{idx + 1} {status} 切换")
            idx = (idx + 1) % len(api_keys)
            keys_rejected += 1
            continue
        if 500 <= status < 600:
            if server_retries >= max_server_retries:
                raise TranscribeError(
                    f"Groq 服务端 {status} 连续重试 {server_retries} 次仍失败: {str(resp)[:200]}")
            wait = min(3 * (2 ** server_retries), 60)   # 3,6,12,24,48,60...
            server_retries += 1
            next_idx = (idx + 1) % len(api_keys)
            log(
                f"Key{idx + 1} 服务端 {status} 重试 {server_retries}/{max_server_retries}，"
                f"{wait}s 后切 Key{next_idx + 1}: {str(resp)[:200]}"
            )
            idx = next_idx
            time.sleep(wait)
            continue
        raise TranscribeError(f"Groq {status}: {str(resp)[:300]}")
    raise TranscribeError("所有 Groq Key 都失败")


def transcribe_all(chunks: list[str], api_keys: list[str], lang: str,
                   model: str, log: Callable[[str], None]) -> str:
    parts = []
    cur = 0
    for i, ch in enumerate(chunks):
        log(f"转录 {i + 1}/{len(chunks)}")
        text, cur = transcribe_chunk(ch, api_keys, cur, lang, model, log)
        parts.append(text)
    return "\n\n".join(parts)


POLISH_PROMPT = """你是一位资深技术内容编辑，精通中英文混合的科技/编程领域语音转录校对。

你收到的文本来自 Whisper 语音识别引擎的原始输出。Whisper 在处理中文技术内容时有以下系统性缺陷，你需要凭借自身知识储备来修复：

1. **谐音乱码**：Whisper 经常把英文技术术语听成无意义的中文谐音字组合。根据上下文还原为正确的英文/中文写法。
2. **大小写与拼写**：所有技术产品名、框架名、语言名、工具名，必须使用其官方正确大小写（JavaScript / GitHub / Claude）。
3. **断词粘连**：长段无标点文本需要断句、加标点、分段。
4. **口语冗余**：过度重复的语气词适当精简，保留说话者自然风格。
5. **内容忠实**：绝不添加原文没说的内容，不改变原意。

通读全文后输出校对后的完整正文。按话题自然分段，段间空一行。不要加任何前言/标题/说明/总结，直接输出正文。

## 原文
{text}"""


def polish_text(raw: str, llm_base_url: str, llm_key: str, llm_model: str,
                log: Callable[[str], None]) -> str:
    """走任意 OpenAI 兼容 endpoint 做校对。失败返回原文。"""
    if not llm_base_url or not llm_key:
        log("跳过校对（未配置 LLM）")
        return raw
    url = llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": llm_model,
        "messages": [{"role": "user", "content": POLISH_PROMPT.format(text=raw)}],
        "temperature": 0.3,
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=180.0) as c:
            r = c.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            log("校对完成")
            return r.json()["choices"][0]["message"]["content"]
        log(f"校对 LLM {r.status_code}: {r.text[:200]}，回原文")
    except Exception as e:
        log(f"校对异常: {e}，回原文")
    return raw


def transcribe_one(
    input_str: str,
    api_keys: list[str],
    *,
    lang: str = "zh",
    model: str = "whisper-large-v3",
    llm_base_url: str = "",
    llm_key: str = "",
    llm_model: str = "deepseek-chat",
    do_polish: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    """处理一个 URL/文件，返回 {title, raw, polished}"""
    with tempfile.TemporaryDirectory(prefix="vt_") as td:
        if os.path.isfile(input_str):
            media, title = input_str, Path(input_str).stem[:80]
        else:
            url = extract_url(input_str)
            if not (url.startswith(("http://", "https://")) or BILIBILI_PATTERN.search(url)):
                raise TranscribeError(f"既不是文件也不是可识别的链接: {input_str[:120]}")
            if url != input_str.strip():
                log(f"从分享文本提取链接: {url}")
            media, title = download(url, td, log)
        ext = Path(media).suffix.lower()
        audio = media if ext in SUPPORTED_AUDIO_EXT else extract_audio(media, td, log)
        chunks = split_audio(audio, td, log)
        raw = transcribe_all(chunks, api_keys, lang, model, log)
        polished = polish_text(raw, llm_base_url, llm_key, llm_model, log) if do_polish else raw
    return {"title": title, "raw": raw, "polished": polished}
