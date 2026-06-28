// bole-admin-api — 管理后台 API（Cloudflare Worker）
// service_role 只作为 Worker secret 存在服务端，绝不下发浏览器。
// 鉴权：管理员用 Supabase 账号登录拿 token → Worker 校验 token 有效 + uid 在 ADMIN_UIDS 白名单 → 才用 service_role 执行管理操作。
//
// 环境变量(secret/vars)：SUPABASE_URL, ANON_KEY, SERVICE_KEY, ADMIN_UIDS(逗号分隔)

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization,content-type",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
};
const J = (o, s = 200) =>
  new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", ...CORS } });

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    // ── 1) 校验调用者是管理员 ──
    const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!token) return J({ error: "未登录" }, 401);
    const ures = await fetch(`${env.SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: env.ANON_KEY, Authorization: `Bearer ${token}` },
    });
    if (!ures.ok) return J({ error: "登录态无效" }, 401);
    const user = await ures.json();
    const admins = (env.ADMIN_UIDS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!admins.includes(user.id)) return J({ error: "无管理员权限" }, 403);

    // ── 2) 管理操作（用 service_role，绕过 RLS）──
    const svc = { apikey: env.SERVICE_KEY, Authorization: `Bearer ${env.SERVICE_KEY}` };
    const url = new URL(req.url);
    const path = url.pathname;

    try {
      if (path === "/api/users") {
        const r = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users?per_page=200`, { headers: svc });
        const d = await r.json();
        const users = d.users || d || [];
        const cr = await fetch(`${env.SUPABASE_URL}/rest/v1/candidates?select=tenant_id`, { headers: svc });
        const rows = (await cr.json()) || [];
        const cnt = {};
        rows.forEach((x) => (cnt[x.tenant_id] = (cnt[x.tenant_id] || 0) + 1));
        return J({
          users: users.map((u) => ({
            id: u.id, email: u.email, banned: !!u.banned_until,
            created_at: u.created_at, count: cnt[u.id] || 0,
          })),
        });
      }

      if (path === "/api/data") {
        const tenant = url.searchParams.get("tenant"); // 可选：只看某用户
        let q = `${env.SUPABASE_URL}/rest/v1/candidates?select=*&order=score.desc.nullslast&limit=1000`;
        if (tenant) q += `&tenant_id=eq.${tenant}`;
        const r = await fetch(q, { headers: svc });
        return J({ rows: (await r.json()) || [] });
      }

      // 统一取 Supabase 返回的真实错误文案（透传，便于定位）
      const errMsg = (d) => (d && (d.msg || d.error_description || d.error_code || d.error || d.message)) || JSON.stringify(d);

      if (path === "/api/create" && req.method === "POST") {
        const b = await req.json();
        if (!b.email || !b.password) return J({ error: "缺邮箱/密码" }, 400);
        const r = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users`, {
          method: "POST", headers: { ...svc, "content-type": "application/json" },
          body: JSON.stringify({ email: b.email, password: b.password, email_confirm: true }),
        });
        const d = await r.json();
        return J(r.ok ? { ok: true, id: d.id, email: d.email } : { error: errMsg(d) }, r.ok ? 200 : 400);
      }

      if (path === "/api/update_email" && req.method === "POST") {
        const b = await req.json();
        if (!b.id || !b.email) return J({ error: "缺 id/email" }, 400);
        const r = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users/${b.id}`, {
          method: "PUT", headers: { ...svc, "content-type": "application/json" },
          body: JSON.stringify({ email: b.email, email_confirm: true }),
        });
        const d = await r.json();
        return J(r.ok ? { ok: true, email: d.email } : { error: errMsg(d) }, r.ok ? 200 : 400);
      }

      if (path === "/api/reset_password" && req.method === "POST") {
        const b = await req.json();
        if (!b.id || !b.password) return J({ error: "缺 id/password" }, 400);
        const r = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users/${b.id}`, {
          method: "PUT", headers: { ...svc, "content-type": "application/json" },
          body: JSON.stringify({ password: b.password }),
        });
        const d = await r.json();
        return J(r.ok ? { ok: true } : { error: errMsg(d) }, r.ok ? 200 : 400);
      }

      if (path === "/api/ban" && req.method === "POST") {
        const b = await req.json(); // {id, ban:boolean}
        const r = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users/${b.id}`, {
          method: "PUT", headers: { ...svc, "content-type": "application/json" },
          body: JSON.stringify({ ban_duration: b.ban ? "876000h" : "none" }),
        });
        const d = await r.json();
        return J(r.ok ? { ok: true } : { error: errMsg(d) }, r.ok ? 200 : 400);
      }

      return J({ error: "not found" }, 404);
    } catch (e) {
      return J({ error: String(e) }, 500);
    }
  },
};
