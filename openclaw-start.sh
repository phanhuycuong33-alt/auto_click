#!/usr/bin/env bash
# ============================================================
#  openclaw-start.sh — Khởi động toàn bộ hệ OpenClaw sau khi bật máy
#
#  Cách dùng (trên HOST, sau khi git pull):
#    chmod +x openclaw-start.sh     # lần đầu
#    ./openclaw-start.sh            # mỗi lần bật máy
#
#  Script làm đủ 4 việc:
#    1. Start Docker container (objective_galileo) nếu chưa chạy
#    2. Bật ai_provide.py trên HOST (Web Chat free backend)
#    3. Trong container: bật gateway + router agent
#    4. Mở TUI cho bạn chat
# ============================================================
set -u

CONTAINER="objective_galileo"
AUTO_DIR="$HOME/automation/auto_click"
PYTHON=".venv/bin/python3"
IN_CONTAINER_DIR="/root/.openclaw/workspace"

green() { echo "✅ $1"; }
yellow() { echo "⚠️  $1"; }
red() { echo "❌ $1"; }

echo "=========================================="
echo " OpenClaw — khởi động hệ thống"
echo "=========================================="

# ---------- 1. Container ----------
if docker ps --format '{{.Names}}' | grep -qw "$CONTAINER"; then
  green "Container $CONTAINER đang chạy"
else
  if docker ps -a --format '{{.Names}}' | grep -qw "$CONTAINER"; then
    echo "▶ Start container $CONTAINER ..."
    docker start "$CONTAINER" >/dev/null && green "Container $CONTAINER đã start"
  else
    red "Không tìm thấy container $CONTAINER"; exit 1
  fi
fi

# ---------- 2. ai_provide.py trên HOST ----------
if curl -s -m 3 http://127.0.0.1:8765/v1/models 2>/dev/null | grep -q 'web-chat'; then
  green "ai_provide đã chạy (cổng 8765)"
else
  if [ -x "$AUTO_DIR/.venv/bin/python3" ]; then
    echo "▶ Bật ai_provide.py trên host ..."
    (cd "$AUTO_DIR" && nohup $PYTHON ai_provide.py --provider deepseek \
      > /tmp/ai_provide.log 2>&1 &)
    # đợi lên tối đa 15s
    for i in $(seq 1 15); do
      sleep 1
      if curl -s -m 2 http://127.0.0.1:8765/v1/models 2>/dev/null | grep -q 'web-chat'; then break; fi
    done
    if curl -s -m 3 http://127.0.0.1:8765/v1/models 2>/dev/null | grep -q 'web-chat'; then
      green "ai_provide đã chạy (log: /tmp/ai_provide.log)"
    else
      yellow "ai_provide chưa lên — xem /tmp/ai_provide.log (không chặn các bước sau)"
    fi
  else
    yellow "Không thấy $AUTO_DIR/.venv — bỏ qua Web Chat free (model khác vẫn chạy)"
  fi
fi

# ---------- helper: chạy lệnh trong container ----------
dc() { docker exec -i "$CONTAINER" bash -lc "$@"; }

# ---------- 3a. Gateway trong container ----------
if dc 'curl -s -m 3 http://127.0.0.1:18789/ >/dev/null 2>&1 || openclaw gateway status 2>/dev/null | grep -qi "probe.*ok"'; then
  green "OpenClaw Gateway đang chạy"
else
  echo "▶ Bật OpenClaw Gateway trong container ..."
  dc "nohup openclaw gateway > /tmp/openclaw-gw.out 2>&1 &"
  for i in $(seq 1 20); do
    sleep 1
    if dc 'openclaw gateway status 2>/dev/null | grep -qi "probe.*ok"'; then break; fi
  done
  if dc 'openclaw gateway status 2>/dev/null | grep -qi "probe.*ok"'; then
    green "OpenClaw Gateway đã chạy"
  else
    red "Gateway không lên — vào container xem /tmp/openclaw-gw.out"; exit 1
  fi
fi

# ---------- 3b. Router agent trong container ----------
if dc 'curl -s -m 3 http://127.0.0.1:8766/v1/models 2>/dev/null' | grep -q '"auto"'; then
  green "Router agent đang chạy"
else
  echo "▶ Bật Router agent trong container ..."
  dc "cd $IN_CONTAINER_DIR/supervisor && nohup python3 router.py > /tmp/router.out 2>&1 &"
  for i in $(seq 1 10); do
    sleep 1
    if dc 'curl -s -m 2 http://127.0.0.1:8766/v1/models 2>/dev/null' | grep -q '"auto"'; then break; fi
  done
  if dc 'curl -s -m 3 http://127.0.0.1:8766/v1/models 2>/dev/null' | grep -q '"auto"'; then
    green "Router agent đã chạy"
  else
    yellow "Router không lên — xem /tmp/router.out trong container (fallback model vẫn hoạt động)"
  fi
fi

echo ""
echo "=========================================="
green "Hệ thống sẵn sàng! Giờ chạy TUI:"
echo ""
echo "    docker exec -it $CONTAINER openclaw tui"
echo ""
echo "(hoặc: docker exec -it $CONTAINER bash  →  gõ: openclaw tui)"
echo "Model chính: Router auto — tự phân phối free models."
echo "=========================================="

# Hỏi có muốn mở TUI ngay không
read -r -p "Mở TUI luôn? [y/N] " ans
if [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; then
  docker exec -it "$CONTAINER" openclaw tui
fi
