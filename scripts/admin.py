#!/usr/bin/env python3
"""admin.py — 管理后台 CLI（仅拥有者用）。管理所有用户与跨租户数据。

⚠️ 需 service_role + PAT（在 .env，绝密）。这是【后台管理工具】，只在你本机运行，
   绝不部署、绝不进前端。账号只能由这里创建（公开注册已关闭）。

用法：
  python scripts/admin.py users                              # 列出所有用户 + 各自数据量
  python scripts/admin.py create --email x@y.com --password ****  # 开通账号(下发给用户)
  python scripts/admin.py disable --email x@y.com            # 停用账号(封禁→该用户 agent 立即失效)
  python scripts/admin.py enable  --email x@y.com            # 恢复账号
  python scripts/admin.py data                               # 各用户(租户)数据量总览
  python scripts/admin.py data --email x@y.com              # 某用户候选人概况
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import cloud_sync as cs


def _cfg() -> dict:
    cs._load_env()
    return {
        "url": os.environ.get("SUPABASE_URL", "").rstrip("/"),
        "service": os.environ.get("SUPABASE_KEY", ""),
        "pat": os.environ.get("SUPABASE_PAT", ""),
        "ref": os.environ.get("SUPABASE_PROJECT_REF", ""),
    }


def _need(c: dict, *keys) -> None:
    miss = [k for k in keys if not c[k]]
    if miss:
        print(f"❌ .env 缺少: {', '.join(miss)}（管理后台需 service_role/PAT）")
        sys.exit(1)


def _sql(c: dict, query: str):
    st, body, err = cs._http(
        f"https://api.supabase.com/v1/projects/{c['ref']}/database/query",
        "POST", {"Authorization": f"Bearer {c['pat']}", "Content-Type": "application/json"},
        {"query": query})
    if st not in (200, 201):
        print(f"❌ SQL 失败: {err or st}")
        sys.exit(1)
    return body or []


def _admin_api(c: dict, path: str, method: str = "GET", body=None):
    return cs._http(f"{c['url']}/auth/v1/admin/{path}", method,
                    {"apikey": c["service"], "Authorization": f"Bearer {c['service']}",
                     "Content-Type": "application/json"}, body)


def _all_users(c: dict) -> list:
    st, b, err = _admin_api(c, "users?per_page=200")
    if st != 200:
        print(f"❌ 取用户失败: {err or st}")
        sys.exit(1)
    return b.get("users", []) if isinstance(b, dict) else (b or [])


def _uid_by_email(c: dict, email: str) -> str:
    for u in _all_users(c):
        if (u.get("email") or "").lower() == email.lower():
            return u.get("id", "")
    print(f"❌ 找不到用户: {email}")
    sys.exit(1)


def cmd_users(c: dict) -> None:
    _need(c, "url", "service", "pat", "ref")
    users = _all_users(c)
    counts = {r["tenant_id"]: r["n"] for r in _sql(
        c, "select tenant_id::text, count(*) n from public.candidates group by tenant_id")}
    print(f"{'邮箱':<28}{'状态':<8}{'候选人':<7}{'创建':<12}")
    print("-" * 60)
    for u in users:
        banned = "停用" if u.get("banned_until") else "正常"
        print(f"{(u.get('email') or '')[:26]:<28}{banned:<8}"
              f"{counts.get(u.get('id'),0):<7}{(u.get('created_at') or '')[:10]:<12}")
    print(f"\n共 {len(users)} 个用户")


def cmd_create(c: dict, email: str, password: str) -> None:
    _need(c, "url", "service")
    if not email:
        email = input("新账号邮箱: ").strip()
    if not password:
        password = getpass.getpass("新账号密码: ")
    st, b, err = _admin_api(c, "users", "POST",
                            {"email": email, "password": password, "email_confirm": True})
    if st in (200, 201):
        print(f"✅ 已开通账号: {email}")
        print(f"   把【邮箱+密码】发给用户，让其在本地: python scripts/cloud_sync.py login --email {email} --password ****")
    else:
        print(f"❌ 创建失败: {err or st}")


def _set_ban(c: dict, email: str, ban: bool) -> None:
    _need(c, "url", "service")
    uid = _uid_by_email(c, email)
    dur = "876000h" if ban else "none"
    st, b, err = _admin_api(c, f"users/{uid}", "PUT", {"ban_duration": dur})
    if st == 200:
        print(f"✅ {'已停用' if ban else '已恢复'}: {email}" + ("（该用户 agent 续期时即失效）" if ban else ""))
    else:
        print(f"❌ 操作失败: {err or st}")


def cmd_data(c: dict, email: str) -> None:
    _need(c, "pat", "ref")
    if email:
        rows = _sql(c, f"""select count(*) 候选人, count(phone) filter(where phone!='') 手机,
          count(wechat) filter(where wechat!='') 微信, count(resume_content) filter(where resume_content!='') 简历
          from public.candidates c join auth.users u on u.id=c.tenant_id where u.email='{email}'""")
        print(f"{email} 的数据: {rows}")
    else:
        rows = _sql(c, """select u.email, count(c.*) 候选人,
          count(c.resume_content) filter(where c.resume_content!='') 简历
          from auth.users u left join public.candidates c on c.tenant_id=u.id
          group by u.email order by 候选人 desc""")
        print("各用户数据量总览:")
        for r in rows:
            print(f"  {(r.get('email') or '?'):<28} 候选人 {r.get('候选人',0):<5} 简历 {r.get('简历',0)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="管理后台(用户/数据)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users")
    p_c = sub.add_parser("create"); p_c.add_argument("--email"); p_c.add_argument("--password")
    p_d = sub.add_parser("disable"); p_d.add_argument("--email", required=True)
    p_e = sub.add_parser("enable"); p_e.add_argument("--email", required=True)
    p_da = sub.add_parser("data"); p_da.add_argument("--email")
    args = ap.parse_args()
    c = _cfg()
    if args.cmd == "users":
        cmd_users(c)
    elif args.cmd == "create":
        cmd_create(c, args.email, args.password)
    elif args.cmd == "disable":
        _set_ban(c, args.email, True)
    elif args.cmd == "enable":
        _set_ban(c, args.email, False)
    elif args.cmd == "data":
        cmd_data(c, args.email)


if __name__ == "__main__":
    main()
