#!/bin/bash
# © 2026 Daniel Li (Open CAIO). 伯乐 AI 招聘助手 · 版权所有 All rights reserved.
# 生成原生 macOS App「伯乐.app」（用系统自带 osacompile，无需任何第三方依赖）。
# 产物是真正的 Mach-O 应用（不是脚本包），Finder 双击即用、有独立图标、无终端窗口。
# 为本机构建：把仓库绝对路径与 python 解释器烧进去。移机请重跑本脚本。
#
# 用法:  bash desktop/build_app.sh
# 产物:  desktop/伯乐.app（已 gitignore，本地构建物，不入库）
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/desktop/伯乐.app"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "❌ 未找到 python3，请先装 Python 3.10+"; exit 1; fi

TMP="$(mktemp -t bole).applescript"
cat > "$TMP" <<OSA
on run
	set port to "8765"
	set root to "$ROOT"
	set py to "$PY"
	-- 服务未在跑才起：nohup + 全量重定向，与 osascript 彻底解耦（不会被回收）
	try
		do shell script "curl -s -o /dev/null http://127.0.0.1:" & port & "/"
	on error
		do shell script "cd " & quoted form of root & " && nohup " & py & " desktop/server.py --port " & port & " --no-open > /tmp/bole_desktop.log 2>&1 &"
		delay 1.8
	end try
	-- 优先 Chrome 应用窗口模式（独立窗口、无地址栏，像原生 App）
	set chrome to "/Applications/Google Chrome.app"
	if (do shell script "[ -d " & quoted form of chrome & " ] && echo yes || echo no") is "yes" then
		do shell script "open -na " & quoted form of chrome & " --args --app=http://127.0.0.1:" & port & "/ --user-data-dir=$HOME/.bole-app-profile --no-first-run"
	else
		do shell script "open http://127.0.0.1:" & port & "/"
	end if
end run
OSA

rm -rf "$OUT"
osacompile -o "$OUT" "$TMP"
rm -f "$TMP"
echo "✅ 已生成: $OUT"
echo "   双击即用（首次若被 Gatekeeper 拦：右键→打开一次）。退出：界面左下「关闭程序」。"
