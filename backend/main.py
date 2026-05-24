"""FastAPI 主入口：URL 提交 → 后台 6 路并发转录 → 前端轮询拿结果"""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from transcribe_lib import TranscribeError, transcribe_one

GROQ_API_KEYS = [k.strip() for k in os.environ.get("GROQ_API_KEYS", "").split(",") if k.strip()]
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

if not GROQ_API_KEYS:
    print("[WARN] GROQ_API_KEYS 环境变量未设置，转录会失败")

app = FastAPI(title="Video Transcribe Web")

# in-memory 任务表
JOBS: dict[str, dict] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=12)  # 池子开大点，前端限并发


def check_auth(token: Optional[str]):
    if WEB_PASSWORD and token != WEB_PASSWORD:
        raise HTTPException(status_code=401, detail="auth required")


# ─── Models ──────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    urls: list[str]
    lang: str = "zh"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    do_polish: bool = True


# ─── 后台任务 ──────────────────────────────────────────────────────────

def _worker(job_id: str, item_idx: int, url: str, req: TranscribeRequest):
    job = JOBS[job_id]
    item = job["items"][item_idx]
    item["status"] = "running"
    item["started_at"] = time.time()

    def log(msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        item["logs"].append(line)
        if len(item["logs"]) > 200:
            item["logs"] = item["logs"][-200:]

    try:
        result = transcribe_one(
            url, GROQ_API_KEYS,
            lang=req.lang,
            llm_base_url=req.llm_base_url,
            llm_key=req.llm_api_key,
            llm_model=req.llm_model,
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


# ─── API ─────────────────────────────────────────────────────────────

@app.post("/api/transcribe")
def start_transcribe(req: TranscribeRequest, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "no urls")
    if not GROQ_API_KEYS:
        raise HTTPException(500, "服务端未配置 GROQ_API_KEYS")

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
    for i, u in enumerate(urls):
        EXECUTOR.submit(_worker, job_id, i, u, req)
    return {"job_id": job_id, "count": len(urls)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, x_auth_token: Optional[str] = Header(None)):
    check_auth(x_auth_token)
    JOBS.pop(job_id, None)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "groq_keys": len(GROQ_API_KEYS),
        "auth_required": bool(WEB_PASSWORD),
        "jobs_in_memory": len(JOBS),
    }


# ─── 前端静态文件 ─────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
