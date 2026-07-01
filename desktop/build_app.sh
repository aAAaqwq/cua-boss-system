#!/bin/bash
# 生成原生 macOS App「伯乐.app」（用系统自带 osacompile，无需任何第三方依赖）。
# 产物是真正的 Mach-O 应用（不是脚本包），Finder 双击即用、有独立图标、无终端窗口。
# 为本机构建：把仓库绝对路径与 python 解释器烧进去，最稳。移机请重跑本脚本。
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
	-- 已在跑就直接开浏览器，不重复起
	try
		do shell script "curl -s -o /dev/null http://127.0.0.1:8765/"
		do shell script "open http://127.0.0.1:8765/"
		return
	end try
	do shell script "cd $ROOT && $PY desktop/server.py > /tmp/bole_desktop.log 2>&1 &"
	delay 1.5
	do shell script "open http://127.0.0.1:8765/"
end run
OSA

rm -rf "$OUT"
osacompile -o "$OUT" "$TMP"
rm -f "$TMP"
echo "✅ 已生成: $OUT"
echo "   双击即用（首次打开若被 Gatekeeper 拦，右键→打开一次即可）。"
