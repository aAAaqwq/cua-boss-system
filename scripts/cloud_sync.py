#!/usr/bin/env python3
"""cloud_sync.py — 候选人数据上云 CLI（Supabase）。

绑定账号（推荐·多用户，登录后才能连接/上传）：
  python scripts/cloud_sync.py login --email you@x.com --password ****  # 登录绑定本机 agent
  python scripts/cloud_sync.py logout                                   # 解绑

上云：
  python scripts/cloud_sync.py --dry-run            # 预览将上传什么(不连云)
  python scripts/cloud_sync.py --push               # 全量补推(需先 login)
  python scripts/cloud_sync.py --push --limit 50    # 只推最近 50 条
  python scripts/cloud_sync.py --flush              # 补推积压队列
  python scripts/cloud_sync.py --status             # 配置/登录/队列状态

配置见 .env（SUPABASE_URL / SUPABASE_ANON_KEY / CLOUD_SYNC ...）。账号由服务端创建/注册下发。
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import cloud_sync as cs


def _mask(s: str) -> str:
    if not s:
        return "(未设置)"
    return f"{s[:8]}…{s[-4:]}" if len(s) > 14 else "******"


def cmd_login(email, password) -> None:
    cfg = cs.config()
    if not email:
        email = input("邮箱: ").strip()
    if not password:
        password = getpass.getpass("密码: ")
    ok, msg = cs.login(email, password, cfg)
    if ok:
        print(f"✅ 登录绑定成功：{msg}（本机 agent 上云将以此账号身份，RLS 只能写自己的数据）")
    else:
        print(f"❌ {msg}")
        sys.exit(1)


def cmd_logout() -> None:
    cs.clear_auth()
    print("✅ 已解绑（删除本机登录态）")


def cmd_status() -> None:
    cfg = cs.config()
    ctx = cs.auth_context(cfg)
    print("=" * 52)
    print("云同步状态 (cloud_sync)")
    print("=" * 52)
    print(f"  CLOUD_SYNC        : {'on ✅' if cfg['enabled'] else 'off ⬜'}")
    print(f"  SUPABASE_URL      : {cfg['url'] or '(未设置)'}")
    print(f"  SUPABASE_ANON_KEY : {_mask(cfg['anon'])}")
    print(f"  service_role(可选) : {_mask(cfg['service_key'])}")
    if ctx is None:
        print(f"  鉴权              : ❌ 未鉴权 —— 请先 `cloud_sync.py login`")
    elif ctx["source"] == "login":
        print(f"  鉴权              : ✅ 已登录 {ctx['email']}（tenant={ctx['tenant']}）")
    else:
        print(f"  鉴权              : ⚠ service_role（拥有者模式，绕过RLS，tenant={ctx['tenant']}）")
    print(f"  待补推队列        : {cs.queue_size()} 条")


def cmd_dry_run(limit) -> None:
    cfg = cs.config()
    ctx = cs.auth_context(cfg)
    tenant = (ctx and ctx["tenant"]) or "<登录后自动填>"
    rows = cs._fetch_rows(limit=limit)
    print(f"=== DRY-RUN：将上传 {len(rows)} 条到表 `{cfg['table']}`（不连云）===")
    print(f"租户 tenant_id = {tenant}（来源：{ctx['source'] if ctx else '未鉴权'}）\n")
    for r in rows:
        p = cs.to_cloud(r, tenant)
        prev = {k: (v[:40] + f"…(+{len(v)-40})" if isinstance(v, str) and len(v) > 40 else v) for k, v in p.items()}
        print(json.dumps(prev, ensure_ascii=False))
    if ctx is None:
        print("\n⚠ 未鉴权 —— 这是预览。先 `cloud_sync.py login` 再 `--push`。")


def cmd_push(limit) -> None:
    cfg = cs.config()
    if cs.auth_context(cfg) is None:
        print("❌ 未鉴权。请先 `python scripts/cloud_sync.py login`（或拥有者配 service_role）。")
        sys.exit(1)
    cs.flush_queue(cfg)
    rows = cs._fetch_rows(limit=limit)
    print(f"推送 {len(rows)} 条 …")
    result = cs.push(rows, cfg)
    print(f"结果: {result}")
    sys.exit(0 if result.get("ok") else 1)


def cmd_flush() -> None:
    if cs.auth_context() is None:
        print("❌ 未鉴权，无法补推。")
        sys.exit(1)
    print(f"补推队列（{cs.queue_size()} 条）…")
    print(f"结果: {cs.flush_queue()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="候选人数据上云 (Supabase)")
    sub = ap.add_subparsers(dest="cmd")
    p_login = sub.add_parser("login", help="登录绑定本机 agent 到账号")
    p_login.add_argument("--email"); p_login.add_argument("--password")
    sub.add_parser("logout", help="解绑(删除本机登录态)")
    # 兼容无子命令的 flag 形式
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--flush", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.cmd == "login":
        cmd_login(args.email, args.password)
    elif args.cmd == "logout":
        cmd_logout()
    elif args.push:
        cmd_push(args.limit)
    elif args.dry_run:
        cmd_dry_run(args.limit)
    elif args.flush:
        cmd_flush()
    else:
        cmd_status()


if __name__ == "__main__":
    main()
