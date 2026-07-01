#!/bin/bash
# 伯乐桌面端 · 双击启动
# 起本地服务(后台，nohup 完全脱离——关掉本窗口/退出终端都不影响)，
# 再以「应用窗口」模式打开(独立窗口、无地址栏，像原生 App)。
# 退出：在伯乐界面点左下「关闭程序」。
cd "$(dirname "$0")/.." || exit 1
PORT=8765
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  osascript -e 'display alert "缺少 Python" message "请先安装 Python 3.10+ 再打开伯乐。"' 2>/dev/null
  echo "❌ 未找到 python3"; read -r -p "按回车关闭…" _; exit 1
fi

# 服务未在跑才起；nohup + 全量重定向 → 与本终端彻底解耦
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
  nohup "$PY" desktop/server.py --port "$PORT" --no-open >/tmp/bole_desktop.log 2>&1 &
  for _ in $(seq 1 25); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null && break
    sleep 0.3
  done
fi

# 优先 Chrome 应用窗口模式；没有则退回默认浏览器
CHROME="/Applications/Google Chrome.app"
if [ -d "$CHROME" ]; then
  open -na "$CHROME" --args --app="http://127.0.0.1:$PORT/" \
    --user-data-dir="$HOME/.bole-app-profile" --no-first-run >/dev/null 2>&1
else
  open "http://127.0.0.1:$PORT/"
fi
echo "✅ 伯乐已启动 → http://127.0.0.1:$PORT/  （可关闭本终端窗口）"
