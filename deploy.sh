#!/bin/bash
# 一键部署脚本（在 VPS 上跑）
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/<USER>/video-transcribe-web/main/deploy.sh | bash
# 或本地：
#   git clone <repo> /opt/video-transcribe-web && cd $_ && sudo bash deploy.sh
set -e

APP_DIR=/opt/video-transcribe-web
SERVICE=video-transcribe-web
PORT=8088

echo "==> 检查系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv ffmpeg curl git

echo "==> Clone / 更新代码"
if [ ! -d "$APP_DIR/.git" ]; then
  if [ -z "$REPO_URL" ]; then
    echo "ERROR: 第一次部署请传 REPO_URL=https://github.com/<user>/video-transcribe-web.git"
    exit 1
  fi
  git clone "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR" && git pull --ff-only
fi

cd "$APP_DIR"

echo "==> 装 Python 依赖"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r backend/requirements.txt -q

echo "==> 检查 .env"
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "⚠️  已创建 .env（从 .env.example）"
    echo "⚠️  必须编辑 /opt/video-transcribe-web/.env 填入 GROQ_API_KEYS 和 WEB_PASSWORD"
    echo "⚠️  填完后重跑: systemctl restart $SERVICE"
  fi
fi

echo "==> 安装 systemd 服务"
cp systemd/$SERVICE.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE

echo "==> 防火墙开放端口 $PORT"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow $PORT/tcp || true
fi

sleep 2
echo ""
echo "==> 状态"
systemctl status $SERVICE --no-pager -n 5 || true
echo ""
echo "==> 健康检查"
curl -sS http://127.0.0.1:$PORT/api/health || echo "(还在启动)"
echo ""
echo ""
echo "✅ 完成。访问: http://$(curl -s ifconfig.me):$PORT"
echo "日志: journalctl -u $SERVICE -f"
echo "重启: systemctl restart $SERVICE"
