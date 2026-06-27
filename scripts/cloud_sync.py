#!/usr/bin/env python3
"""cloud_sync.py — 候选人数据上云 CLI（Supabase）。

用法：
  python scripts/cloud_sync.py --dry-run            # 预览将上传什么(不连云，没凭证也能看)
  python scripts/cloud_sync.py --dry-run --limit 3  # 只看前 3 条
  python scripts/cloud_sync.py --push               # 全量补推到云端(需配置好 .env)
  python scripts/cloud_sync.py --push --limit 50    # 只推最近 50 条
  python scripts/cloud_sync.py --flush              # 补推本地积压队列
  python scripts/cloud_sync.py --status             # 看配置/队列状态(密钥脱敏)

配置见 .env（SUPABASE_URL / SUPABASE_KEY / TENANT_ID / CLOUD_SYNC / CLOUD_TABLE）。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import cloud_sync as cs


def _mask(secret: str) -> str:
    if not secret:
        return "(未设置)"
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else "******"


def cmd_status() -> None:
    cfg = cs.config()
    print("=" * 50)
    print("云同步配置 (cloud_sync)")
    print("=" * 50)
    print(f"  CLOUD_SYNC   : {'on ✅' if cfg['enabled'] else 'off ⬜'}")
    print(f"  SUPABASE_URL : {cfg['url'] or '(未设置)'}")
    print(f"  SUPABASE_KEY : {_mask(cfg['key'])}")
    print(f"  TENANT_ID    : {cfg['tenant'] or '(未设置)'}")
    print(f"  CLOUD_TABLE  : {cfg['table']}")
    print(f"  配置完整     : {'是 ✅' if cs.is_configured(cfg) else '否 —— 缺 URL/KEY/TENANT'}")
    print(f"  待补推队列   : {cs.queue_size()} 条")


def cmd_dry_run(limit: int) -> None:
    cfg = cs.config()
    tenant = cfg["tenant"] or "<TENANT_ID-待填>"
    rows = cs._fetch_rows(limit=limit)
    print(f"=== DRY-RUN：将上传 {len(rows)} 条到表 `{cfg['table']}`（不连云）===")
    print(f"租户 tenant_id = {tenant}")
    print(f"上云字段({len(cs._CLOUD_COLUMNS)}个) + tenant_id：{', '.join(cs._CLOUD_COLUMNS)}\n")
    for r in rows[:limit] if limit else rows:
        p = cs.to_cloud(r, tenant)
        # 摘要展示（长字段截断，避免刷屏）
        preview = {}
        for k, v in p.items():
            if isinstance(v, str) and len(v) > 40:
                preview[k] = v[:40] + f"…(+{len(v) - 40})"
            else:
                preview[k] = v
        print(json.dumps(preview, ensure_ascii=False))
    if not cs.is_configured(cfg):
        print("\n⚠ 当前未配置 SUPABASE_URL/KEY/TENANT_ID —— 这是预览。填好 .env 后用 --push 真上传。")


def cmd_push(limit: int) -> None:
    cfg = cs.config()
    if not cs.is_configured(cfg):
        print("❌ 未配置完整（缺 SUPABASE_URL/KEY/TENANT_ID）。先填 .env，或用 --dry-run 预览。")
        sys.exit(1)
    cs.flush_queue(cfg)  # 先补积压
    rows = cs._fetch_rows(limit=limit)
    print(f"推送 {len(rows)} 条到 {cfg['url']}/rest/v1/{cfg['table']} …")
    result = cs.push(rows, cfg)
    print(f"结果: {result}")
    sys.exit(0 if result.get("ok") else 1)


def cmd_flush() -> None:
    cfg = cs.config()
    if not cs.is_configured(cfg):
        print("❌ 未配置完整，无法补推。")
        sys.exit(1)
    print(f"补推队列（{cs.queue_size()} 条）…")
    result = cs.flush_queue(cfg)
    print(f"结果: {result}")


def main() -> None:
    ap = argparse.ArgumentParser(description="候选人数据上云 (Supabase)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="预览将上传什么(不连云)")
    g.add_argument("--push", action="store_true", help="全量/近 N 条上云")
    g.add_argument("--flush", action="store_true", help="补推本地积压队列")
    g.add_argument("--status", action="store_true", help="查看配置与队列状态")
    ap.add_argument("--limit", type=int, default=None, help="只处理最近 N 条(默认全部)")
    args = ap.parse_args()

    if args.status:
        cmd_status()
    elif args.dry_run:
        cmd_dry_run(args.limit)
    elif args.push:
        cmd_push(args.limit)
    elif args.flush:
        cmd_flush()


if __name__ == "__main__":
    main()
