#!/usr/bin/env python3
"""scripts/bole.py — 和「伯乐」对话（DeepSeek 驱动）

产品化用：桌面 App「问伯乐」对话框的 CLI 内核；也可终端直接聊。
人设/能力见 app/bole_agent.py，用 .env 的 DEEPSEEK_API_KEY。

用法:
  python scripts/bole.py                       # 交互对话(REPL)
  python scripts/bole.py --ask "帮我跑一遍招聘流程"   # 单轮
  python scripts/bole.py --ask "谁最合适" --json     # JSON 输出(给 App/前端调用)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.bole_agent import ask, chat  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="和「伯乐」对话（DeepSeek 驱动）")
    p.add_argument("--ask", help="单轮提问")
    p.add_argument("--json", action="store_true", help="JSON 输出（供 App/前端调用）")
    args = p.parse_args()

    if args.ask:
        reply, err = ask(args.ask)
        if args.json:
            print(json.dumps({"ok": bool(reply), "reply": reply or "", "error": err},
                             ensure_ascii=False))
        else:
            print(reply or f"⚠ {err}")
        sys.exit(0 if reply else 1)

    # 交互 REPL
    print("🎯 伯乐（DeepSeek 驱动）— 直接说话，Ctrl-C / Ctrl-D 退出\n")
    history: list[dict] = []
    while True:
        try:
            q = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        reply, err = chat(history + [{"role": "user", "content": q}])
        if reply:
            print(f"伯乐 > {reply}\n")
            history += [{"role": "user", "content": q},
                        {"role": "assistant", "content": reply}]
            history = history[-20:]   # 控上下文长度
        else:
            print(f"⚠ {err}\n")


if __name__ == "__main__":
    main()
