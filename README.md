# Video Transcribe Web

视频/音频转录 Web 服务。**Groq Whisper-large-v3** 转录 + 任意 OpenAI 兼容 LLM 校对（默认 DeepSeek）。6 路并发。

## 特性

- 🎬 支持 30+ 平台（YouTube / B站 / TikTok 等，靠 yt-dlp）+ 本地文件
- ⚡ Groq Whisper-large-v3 转录（云端，比本地快 10×）
- 📝 任意 OpenAI 兼容 LLM 校对（DeepSeek / Gemini / OpenRouter / ...）
- 🚀 6 路并发（实测 6 个视频 ~60s）
- 🔒 简单密码鉴权（环境变量）
- 🎨 Modern Minimal 暗色 UI（Linear / Vercel 风）
- 📋 一键复制原文 / 校对后文本
- 🔧 systemd 部署，VPS 1GB RAM 也能跑

## 快速部署到 VPS

```bash
# 在 VPS（Ubuntu 22.04+）上跑
REPO_URL=https://github.com/<你的用户名>/video-transcribe-web.git \
  bash -c 'git clone $REPO_URL /opt/video-transcribe-web && cd $_ && sudo bash deploy.sh'

# 编辑 env（填 Groq Key + Web 密码）
sudo nano /opt/video-transcribe-web/.env

# 重启
sudo systemctl restart video-transcribe-web

# 访问
# http://<VPS-IP>:8088
```

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `GROQ_API_KEYS` | ✅ | 多个 Groq key 逗号分隔，自动轮询（在 [console.groq.com/keys](https://console.groq.com/keys) 免费创建） |
| `WEB_PASSWORD` | ⚠️ 强烈推荐 | 访问密码，留空 = 公开访问（公网部署千万别留空） |
| `PORT` | 否 | 默认 `8088` |

## 架构

```
Browser ──HTTP──> FastAPI (uvicorn :8088)
                    │
                    ├── ThreadPoolExecutor (max 12)
                    │     │
                    │     └── 每路任务:
                    │           yt-dlp → ffmpeg → Groq Whisper → LLM polish
                    │
                    └── In-memory job dict (前端 1.5s 轮询)
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/transcribe` | 提交 URL 列表，返回 `job_id` |
| `GET` | `/api/jobs/{id}` | 拿任务状态 + 结果 |
| `DELETE` | `/api/jobs/{id}` | 删任务 |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/` | 前端页面 |

所有 `/api/*` 调用需要 `X-Auth-Token: <WEB_PASSWORD>` header（如果 `WEB_PASSWORD` 已设）。

## 本地开发

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEYS=gsk_xxx
export WEB_PASSWORD=test
uvicorn main:app --reload --port 8088
```

打开 http://127.0.0.1:8088

## 已知坑

- **VPS 1GB RAM** 跑 6 路并发吃力，开 `MemoryMax=600M` 兜底（systemd 文件里）
- **ffmpeg** 必须装（`apt install ffmpeg`），脚本会自动装
- **yt-dlp** Bilibili 国外 IP 抓不抓得到看运气；YouTube 数据中心 IP 经常 403

## License

MIT
