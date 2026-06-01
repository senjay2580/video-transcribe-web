"""FastAPI 主入口：URL 提交 → 后台并发转录 → 前端轮询 + 历史持久化"""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from transcribe_lib import TranscribeError, transcribe_one

# ─── Env 配置 ─────────────────────────────────────────────────────────
GROQ_API_KEYS = [k.strip() for k in os.environ.get("GROQ_API_KEYS", "").split(",") if k.strip()]
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").strip()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat").strip()
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "data/jobs")).resolve()
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", "data/cookies")).resolve()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["COOKIES_DIR"] = str(COOKIES_DIR)  # 给 transcribe_lib 用

if not GROQ_API_KEYS or all(k.startswith("PLACEHOLDER") for k in GROQ_API_KEYS):
    print("[WARN] GROQ_API_KEYS 未设置或是占位符，转录会失败")

app = FastAPI(title="Video Transcribe Web")

# in-memory 任务表（启动时从 JOBS_DIR 加载）
JOBS: dict[str, dict] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=12)


def check_auth(token: Optional[str]):
    if WEB_PASSWORD and token != WEB_PASSWORD:
        raise HTTPException(status_code=401, detail="auth required")


# ─── 持久化 ──────────────────────────────────────────────────────────

def _save_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        (JOBS_DIR / f"{job_id}.json").write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[WARN] save job {job_id}: {e}")


def _load_jobs():
    for f in JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # 启动时如果有未完成的任务，标记为 failed（进程崩溃过）
            if data.get("status") == "running":
                data["status"] = "done"
                for it in data.get("items", []):
                    if it.get("status") in ("running", "pending"):
                        it["status"] = "failed"
                        it["error"] = "服务重启中断"
            JOBS[data["id"]] = data
        except Exception as e:
            print(f"[WARN] load {f}: {e}")
    print(f"[INFO] loaded {len(JOBS)} jobs from {JOBS_DIR}")


_load_jobs()


# ─── Models ──────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    urls: list[str]
    lang: str = "zh"
    do_polish: bool = True


# ─── 后台任务 ──────────────────────────────────────────────────────────

def _worker(job_id: str, item_idx: int, url: str, req: TranscribeRequest):
    job = JOBS.get(job_id)
    if not job:
        return
    item = job["items"][item_idx]
    item["status"] = "running"
    item["started_at"] = time.time()
    _save_job(job_id)

    def log(msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        item["logs"].append(line)
        if len(item["logs"]) > 200:
            item["logs"] = item["logs"][-200:]

    try:
        result = transcribe_one(
            url, GROQ_API_KEYS,
            lang=req.lang,
            llm_base_url=LLM_BASE_URL,
            llm_key=LLM_API_KEY,
            llm_model=LLM_MODEL,
            do_polish=req.do_polish,
            log=log,
        )
        item.update({
            "status": "done",
            "title": result["title"],
            "raw": result["raw"],
            "polished": result["polished"],
            "elapsed": time.time() - item["started_at"],
        })
    except TranscribeError as e:
        item.update({"status": "failed", "error": str(e),
                     "elapsed": time.time() - item["started_at"]})
    except Exception as e:
        item.update({"status": "failed", "error": f"内部异常: {type(e).__name__}: {e}",
                     "elapsed": time.time() - item["started_at"]})

    # 全部跑完更新总状态
    if all(it["status"] in ("done", "failed") for it in job["items"]):
        job["status"] = "done"
        job["finished_at"] = time.time()
    _save_job(job_id)


def _job_summary(j: dict) -> dict:
    """侧边栏用的轻量摘要 — 不含全文/日志"""
    return {
        "id": j["id"],
        "status": j["status"],
        "created_at": j["created_at"],
        "finished_at": j.get("finished_at"),
        "items": [
            {"idx": i["idx"], "url": i["url"], "status": i["status"],
             "title": i.get("title"), "elapsed": i.get("elapsed")}
            for i in j["items"]
        ],
    }


# ─── API ─────────────────────────────────────────────────────────────

async def _parse_transcribe_body(request: Request) -> TranscribeRequest:
    """容错解析提交参数。

    手机端某些浏览器（云加速 / 省流量代理）或明文 HTTP 链路会吞掉 POST body 或改写
    Content-Type，FastAPI 收到空 body 就报 422 (loc=["body"], input=null)。这里：
      ① 直接读原始 body 按 JSON 解析（不依赖 Content-Type）→ 抗 Content-Type 被改写；
      ② body 为空时退回 query 参数（前端会把同一份数据同时塞进 URL）→ 抗 body 被吞。
    """
    try:
        raw = await request.body()
        if raw:
            return TranscribeRequest.model_validate_json(raw)
    except Exception:
        pass
    qp = request.query_params
    return TranscribeRequest(
        urls=qp.getlist("u"),
        lang=qp.get("lang", "zh"),
        do_polish=str(qp.get("do_polish", "true")).lower() not in ("false", "0", ""),
    )


@app.post("/api/transcribe")
async def start_transcribe(request: Request, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    req = await _parse_transcribe_body(request)
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "no urls")
    if not GROQ_API_KEYS or all(k.startswith("PLACEHOLDER") for k in GROQ_API_KEYS):
        raise HTTPException(500, "服务端 GROQ_API_KEYS 未配置或是占位符，去 SSH 改 /opt/video-transcribe-web/.env")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "created_at": time.time(),
        "items": [
            {"idx": i, "url": u, "status": "pending", "logs": [],
             "title": None, "raw": None, "polished": None,
             "error": None, "elapsed": None, "started_at": None}
            for i, u in enumerate(urls)
        ],
    }
    _save_job(job_id)
    for i, u in enumerate(urls):
        EXECUTOR.submit(_worker, job_id, i, u, req)
    return {"job_id": job_id, "count": len(urls)}


@app.get("/api/jobs")
def list_jobs(x_auth_token: Optional[str] = Header(None)):
    """列出所有任务（摘要，无全文）"""
    check_auth(x_auth_token)
    out = [_job_summary(j) for j in JOBS.values()]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, x_auth_token: Optional[str] = Header(None)):
    """获取单个任务完整数据（含全文 + 日志）"""
    check_auth(x_auth_token)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    JOBS.pop(job_id, None)
    (JOBS_DIR / f"{job_id}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.delete("/api/jobs")
def delete_all_jobs(x_auth_token: Optional[str] = Header(None)):
    """清空所有历史"""
    check_auth(x_auth_token)
    n = len(JOBS)
    JOBS.clear()
    for f in JOBS_DIR.glob("*.json"):
        f.unlink(missing_ok=True)
    return {"ok": True, "deleted": n}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "groq_keys": len(GROQ_API_KEYS),
        "auth_required": bool(WEB_PASSWORD),
        "llm_configured": bool(LLM_API_KEY),
        "llm_model": LLM_MODEL,
        "jobs_in_memory": len(JOBS),
    }


# ─── Cookies 上传 / 管理 ──────────────────────────────────────────────

ALLOWED_COOKIE_PLATFORMS = {"bilibili"}


@app.get("/api/cookies")
def cookies_status(x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    out = {}
    for p in ALLOWED_COOKIE_PLATFORMS:
        f = COOKIES_DIR / f"{p}.txt"
        if f.exists():
            s = f.stat()
            out[p] = {"exists": True, "size": s.st_size, "mtime": s.st_mtime}
        else:
            out[p] = {"exists": False}
    return out


class CookiesUpload(BaseModel):
    content: str


PLATFORM_DOMAIN = {
    "bilibili": "bilibili.com",
}


def _header_to_netscape(header: str, domain: str) -> str:
    """把 'k1=v1; k2=v2;' 这种浏览器复制出来的字符串转成 Netscape cookies.txt 格式"""
    lines = ["# Netscape HTTP Cookie File",
             "# Converted from raw cookie header"]
    # 未来 10 年的时间戳作为 expires
    far_future = 2_000_000_000
    for part in header.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        # 字段: domain  include_subdomains  path  secure  expires  name  value
        lines.append(f".{domain}\tTRUE\t/\tFALSE\t{far_future}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


@app.post("/api/cookies/{platform}")
def upload_cookies(platform: str, body: CookiesUpload,
                   x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    if platform not in ALLOWED_COOKIE_PLATFORMS:
        raise HTTPException(400, f"platform must be one of {sorted(ALLOWED_COOKIE_PLATFORMS)}")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "empty content")
    if len(content) > 1024 * 1024:
        raise HTTPException(413, "cookies > 1MB, too large")
    head = content[:2048]

    if "\t" in head or "# Netscape" in head:
        # 已经是 Netscape 格式
        normalized = content
        fmt = "netscape"
    elif ";" in head and "=" in head:
        # 浏览器复制的 raw cookie header
        normalized = _header_to_netscape(content, PLATFORM_DOMAIN[platform])
        if normalized.count("\n") < 3:  # 至少有一条 cookie
            raise HTTPException(400, "解析失败：没识别出任何 cookie 键值对")
        fmt = "converted-from-header"
    else:
        raise HTTPException(400, "无法识别 cookies 格式。支持：① Netscape cookies.txt 格式 ② 浏览器 DevTools 复制的 'k1=v1; k2=v2;' 形式")

    path = COOKIES_DIR / f"{platform}.txt"
    path.write_text(normalized, encoding="utf-8")
    path.chmod(0o600)
    return {"ok": True, "platform": platform, "size": len(normalized), "format": fmt}


@app.delete("/api/cookies/{platform}")
def delete_cookies(platform: str, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    if platform not in ALLOWED_COOKIE_PLATFORMS:
        raise HTTPException(400, "invalid platform")
    (COOKIES_DIR / f"{platform}.txt").unlink(missing_ok=True)
    return {"ok": True}


# ─── 前端静态文件 ─────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
