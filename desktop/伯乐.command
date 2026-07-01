#!/bin/bash
# 伯乐桌面端 · 双击启动
# 会起一个本地服务并自动打开浏览器。关闭：在此窗口按 Ctrl-C，或直接关窗口。
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.10+"
  read -r -p "按回车关闭…" _
  exit 1
fi
exec "$PY" desktop/server.py
