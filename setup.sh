#!/usr/bin/env bash
# StockPulse 一键安装：创建虚拟环境、安装依赖、交互式写入 .env、可选安装为 systemd 服务。
# 用法：bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ask() { # ask VAR "提示" "默认值"
  local __var=$1 __prompt=$2 __default=${3:-} __reply
  if [ -n "$__default" ]; then read -r -p "$__prompt [$__default]: " __reply; else read -r -p "$__prompt: " __reply; fi
  printf -v "$__var" '%s' "${__reply:-$__default}"
}
set_env() { # set_env KEY VALUE  (更新或追加 .env 中的一行)
  local key=$1 val=$2
  if grep -q "^${key}=" .env; then
    python3 - "$key" "$val" <<'PY'
import sys, re
k, v = sys.argv[1], sys.argv[2]
s = open(".env", encoding="utf-8").read()
s = re.sub(rf"^{re.escape(k)}=.*$", lambda m: f"{k}={v}", s, flags=re.M)
open(".env", "w", encoding="utf-8").write(s)
PY
  else
    echo "${key}=${val}" >> .env
  fi
}

say "检查 Python"
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10+"' \
  || { echo "需要 Python 3.10 或更高版本"; exit 1; }

say "创建虚拟环境并安装依赖"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "配置 .env"
[ -f .env ] || cp .env.example .env
current() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

echo "1) Telegram：在 Telegram 搜索 @BotFather，发送 /newbot 创建机器人，复制它给你的 token。"
ask TG "TELEGRAM_BOT_TOKEN（留空跳过）" "$(current TELEGRAM_BOT_TOKEN)"
echo "2) Claude：在 https://console.anthropic.com 创建 API Key。只在生成每日报告时调用，每个报告时间点调用一次。"
ask AK "ANTHROPIC_API_KEY（留空则用规则引擎）" "$(current ANTHROPIC_API_KEY)"
ask TZ_ "时区" "$(current TIMEZONE)"
ask TIMES "每日 AI 报告时间（逗号分隔）" "$(current DAILY_REPORT_TIMES)"
ask WL "关注列表（逗号分隔的美股代码）" "$(current WATCHLIST)"
ask PORT "Dashboard 端口" "8000"
TOKEN_DEFAULT="$(current API_TOKEN)"
[ -n "$TOKEN_DEFAULT" ] || TOKEN_DEFAULT="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(16))')"
echo "3) 行情：可选填 Massive（原 Polygon.io）API Key，实时性更好；留空使用 Yahoo Finance。"
ask MK "MASSIVE_API_KEY（留空跳过）" "$(current MASSIVE_API_KEY)"
ask RT "API_TOKEN（保护生成报告、修改策略等写操作）" "$TOKEN_DEFAULT"

set_env TELEGRAM_BOT_TOKEN "$TG"
set_env ANTHROPIC_API_KEY "$AK"
set_env TIMEZONE "$TZ_"
set_env DAILY_REPORT_TIMES "$TIMES"
set_env WATCHLIST "$WL"
set_env API_TOKEN "$RT"
set_env MASSIVE_API_KEY "$MK"
set_env DEMO_MODE "false"
chmod 600 .env

say "自检（离线样例数据，不联网、不调用 Claude）"
.venv/bin/python -m pytest -q

if [ -n "$TG" ]; then
  say "验证 Telegram token"
  if curl -fsS "https://api.telegram.org/bot${TG}/getMe" >/dev/null; then
    echo "Telegram token 有效。服务启动后，在 Telegram 里给你的机器人发送 /start 即可订阅。"
  else
    echo "⚠️  Telegram token 验证失败，请检查 .env 中的 TELEGRAM_BOT_TOKEN。"
  fi
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  ask SVC "安装为开机自启的 systemd 服务？(y/n)" "y"
  if [ "$SVC" = "y" ]; then
    SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    sed -e "s#__DIR__#${DIR}#g" -e "s#__USER__#$(id -un)#g" -e "s#__PORT__#${PORT}#g" \
      deploy/stockpulse.service | $SUDO tee /etc/systemd/system/stockpulse.service >/dev/null
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now stockpulse
    say "已启动：systemctl status stockpulse  ·  日志：journalctl -u stockpulse -f"
    echo "Dashboard: http://<服务器IP>:${PORT}"
    exit 0
  fi
fi

say "安装完成，手动启动："
echo "  .venv/bin/python -m stockpulse serve --port ${PORT}"
echo "立即生成一份 AI 报告并推送："
echo "  .venv/bin/python -m stockpulse report"
