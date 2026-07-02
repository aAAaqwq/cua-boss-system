"use strict";
/* 伯乐 AI 招聘助手 · © 2026 Daniel Li (Open CAIO) · 版权所有 All rights reserved. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => (await fetch(path, opts)).json();
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

/* ── 登录门禁 ── */
async function boot() {
  const a = await api("/api/auth");
  if (a.logged_in) enterApp(a.email);
  else showLogin();
}
function showLogin() { $("#loginScreen").hidden = false; $("#app").hidden = true; }
function enterApp(email) {
  $("#loginScreen").hidden = true;
  $("#app").hidden = false;
  $("#userEmail").textContent = email || "已登录";
  loadDoctor(); loadBoard();
  if (!localStorage.getItem("bole_guide_seen")) openGuide();   // 首次登录自动弹引导
}

/* ── 使用引导 ── */
function openGuide() { $("#guideOverlay").hidden = false; }
function closeGuide() { $("#guideOverlay").hidden = true; localStorage.setItem("bole_guide_seen", "1"); }
$("#closeGuide").addEventListener("click", closeGuide);
$("#guideDone").addEventListener("click", closeGuide);
$("#guideTabs").addEventListener("click", (e) => {
  const t = e.target.closest(".gtab");
  if (!t) return;
  $$(".gtab").forEach((x) => x.classList.toggle("is-active", x === t));
  $$(".gpane").forEach((p) => p.classList.toggle("is-active", p.dataset.gpane === t.dataset.gtab));
});
$$(".goto").forEach((b) => b.addEventListener("click", () => {
  closeGuide();
  const nav = document.querySelector(`.nav-item[data-view="${b.dataset.goto}"]`);
  if (nav) nav.click();
}));
// 登录前必须勾选同意隐私条款
$("#agreeTerms").addEventListener("change", (e) => { $("#loginBtn").disabled = !e.target.checked; });
$("#showTerms").addEventListener("click", () => { $("#termsOverlay").hidden = false; });
$("#closeTerms").addEventListener("click", () => {
  $("#termsOverlay").hidden = true;
  $("#agreeTerms").checked = true; $("#loginBtn").disabled = false;  // 读完即视为同意
});

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#loginBtn"), err = $("#loginErr");
  if (!$("#agreeTerms").checked) { err.textContent = "请先阅读并同意隐私与数据安全条款"; return; }
  btn.disabled = true; btn.textContent = "登录中…"; err.textContent = "";
  const res = await api("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: $("#loginEmail").value, password: $("#loginPassword").value }),
  });
  btn.disabled = false; btn.textContent = "登录";
  if (res.ok) enterApp(res.email);
  else err.textContent = res.error || "登录失败";
});
$("#logoutBtn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  showLogin();
});
$("#quitBtn").addEventListener("click", async () => {
  if (!confirm("关闭伯乐？正在跑的任务会一并停止。")) return;
  try {
    await api("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (_) { /* 服务已停，忽略 */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;font:16px -apple-system;color:#555">' +
    '伯乐已关闭，可以关掉此窗口了。</div>';
  setTimeout(() => window.close(), 400);
});

/* ── 视图切换 ── */
$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-item");
  if (!btn) return;
  if (btn.id === "guideBtn") { openGuide(); return; }   // 引导是弹层，不切视图
  const view = btn.dataset.view;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b === btn));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));
  if (view === "board") loadBoard();
  if (view === "settings") { loadConfig(); loadDoctor(); }
});

/* ── 装机自检 ── */
async function loadDoctor() {
  const list = $("#doctorList");
  if (list) list.innerHTML = '<div class="empty">自检中…</div>';
  const d = await api("/api/doctor");
  const pill = $("#healthPill"), txt = $("#healthText");
  if (!d || !d.checks) {
    pill.className = "health-pill bad"; txt.textContent = "自检失败";
    if (list) list.innerHTML = `<div class="empty">自检未返回：${esc(d && d.error)}</div>`;
    return;
  }
  const critFail = d.checks.filter((c) => c.critical && !c.ok).length;
  pill.className = "health-pill " + (critFail ? "bad" : "ok");
  txt.textContent = critFail ? `${critFail} 项待处理` : "前置就绪";
  if (!list) return;
  const mark = (c) => (c.ok ? "✅" : c.critical ? "❌" : "⚠️");
  let html = d.checks.map((c) =>
    `<div class="dr-row"><span class="mk">${mark(c)}</span>
      <span class="nm">${esc(c.name)}</span><span class="dt">${esc(c.detail)}</span></div>`).join("");
  html += '<div class="dr-sep">以下需人工确认</div>';
  html += (d.manual || []).map((m) =>
    `<div class="dr-row manual"><span class="mk">◻︎</span><span class="nm">${esc(m)}</span></div>`).join("");
  list.innerHTML = html;
}
$("#recheck").addEventListener("click", loadDoctor);
$("#fixBtn").addEventListener("click", async () => {
  const box = $("#fixResult"), btn = $("#fixBtn");
  btn.disabled = true; btn.textContent = "修复中…";
  const r = await api("/api/doctor/fix", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  btn.disabled = false; btn.textContent = "一键修复";
  box.hidden = false;
  const fixed = (r.fixed || []).map((f) => `<div class="fx ok">✅ 已自动生成 ${esc(f)}</div>`).join("");
  const inst = (r.manual_install || []).map((m) => `<div class="fx warn">⚠️ 需手动装 <b>${esc(m.name)}</b>：<code>${esc(m.how)}</code></div>`).join("");
  const perms = (r.perm_links || []).map((p) =>
    `<div class="fx"><a href="${esc(p.url)}">🔗 打开「${esc(p.name)}」设置 →</a></div>`).join("");
  box.innerHTML =
    (fixed || '<div class="fx">没有可自动生成的缺失配置。</div>') +
    (inst ? `<div class="fx-h">需你手动安装：</div>${inst}` : "") +
    `<div class="fx-h">需你手动授权（macOS 不允许程序自行授予，点开设置里勾选）：</div>${perms}` +
    `<div class="fx">ℹ️ DeepSeek Key 在上方「DeepSeek API」里填。</div>`;
  loadDoctor();
});

/* ── 看板 ── */
async function loadBoard() {
  const d = await api("/api/dashboard");
  const s = d.stats || {};
  const cards = [
    ["a", s.total, "候选人总数"], ["b", s.has_resume, "已收简历"], ["c", s.scored, "已评分"],
    ["a", s.has_wechat, "已加微信"], ["d", s.interviewed, "已约面试"], ["c", s.today, "今日更新"],
  ];
  $("#statGrid").innerHTML = cards.map(([cls, n, lbl]) =>
    `<div class="stat ${cls}"><div class="num">${n ?? 0}</div><div class="lbl">${lbl}</div></div>`).join("");
  const list = $("#topList");
  if (!d.ok) { list.innerHTML = `<div class="empty">${esc(d.reason || "暂无数据")}</div>`; return; }
  if (!d.top || !d.top.length) { list.innerHTML = '<div class="empty">还没有评分数据，先收集并评分。</div>'; return; }
  list.innerHTML = d.top.map((c, i) =>
    `<button class="top-row" data-uid="${esc(c.uid)}"><span class="rk">${i + 1}</span>
      <span class="who"><strong>${esc(c.name)}</strong>
        <div class="meta">${esc(c.school || "—")} · ${esc(c.degree || "—")} · ${esc(c.job_position || "—")}</div></span>
      <span class="sc">${Number(c.score || 0).toFixed(1)}</span></button>`).join("");
  $$("#topList .top-row").forEach((row) =>
    row.addEventListener("click", () => openCandidate(row.dataset.uid)));
}
$("#refreshBoard").addEventListener("click", loadBoard);

/* ── 候选人详情弹层 ── */
let modalData = null;
async function openCandidate(uid) {
  if (!uid) return;
  const modal = $("#candidateModal");
  modal.hidden = false;
  $("#modalBody").innerHTML = '<div class="empty">加载中…</div>';
  const d = await api("/api/candidate?uid=" + encodeURIComponent(uid));
  if (!d.ok) { $("#modalBody").innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
  modalData = d;
  const c = d.candidate;
  $("#modalHead").innerHTML =
    `<div class="mh-name">${esc(c.name)}<span class="mh-score">${Number(c.score || 0).toFixed(1)}</span></div>
     <div class="mh-meta">${esc(c.school || "—")} · ${esc(c.degree || "—")} · 沟通岗位「${esc(c.job_position || "—")}」 · ${esc(c.status || "")}</div>`;
  $$(".mtab").forEach((t, i) => t.classList.toggle("is-active", i === 0));
  renderTab("info");
}
$("#modalTabs").addEventListener("click", (e) => {
  const t = e.target.closest(".mtab");
  if (!t) return;
  $$(".mtab").forEach((x) => x.classList.toggle("is-active", x === t));
  renderTab(t.dataset.tab);
});
function closeModal() { $("#candidateModal").hidden = true; modalData = null; }
$("#closeModal").addEventListener("click", closeModal);
$("#modalBackdrop").addEventListener("click", closeModal);

function renderTab(tab) {
  const d = modalData; if (!d) return;
  const c = d.candidate, body = $("#modalBody");
  if (tab === "info") {
    const rows = [
      ["姓名", c.name], ["学校", c.school], ["学历", c.degree], ["沟通岗位", c.job_position],
      ["状态", c.status], ["微信", c.wechat], ["手机", c.phone], ["邮箱", c.email],
      ["有简历", c.has_resume ? "是" : "否"], ["面试", [c.interview_type, c.interview_date, c.interview_time].filter(Boolean).join(" ")],
      ["备注", c.notes], ["更新时间", c.updated_at],
    ];
    body.innerHTML = `<dl class="info-grid">${rows.map(([k, v]) =>
      `<div><dt>${k}</dt><dd>${esc(v || "—")}</dd></div>`).join("")}</dl>`;
  } else if (tab === "resume") {
    if (d.has_pdf) {
      body.innerHTML = `<iframe class="resume-frame" src="/api/resume?uid=${encodeURIComponent(c.uid)}"></iframe>
        <p class="hint mt">解析文本 ${d.resume_text_len} 字（供 AI 评分）。</p>`;
    } else if (d.resume_text_len) {
      body.innerHTML = `<div class="resume-text">${esc(d.resume_text)}</div>
        <p class="hint mt">无 PDF 附件，以下为解析/在线简历文本（${d.resume_text_len} 字）。</p>`;
    } else {
      body.innerHTML = '<div class="empty">这位候选人还没有简历附件或正文。</div>';
    }
  } else if (tab === "score") {
    renderScore(body, d);
  } else if (tab === "chat") {
    if (!d.chat.length) { body.innerHTML = '<div class="empty">还没有聊天记录。</div>'; return; }
    body.innerHTML = `<div class="chat-log inline">${d.chat.map((m) => {
      const who = m.role === "boss" || m.role === "assistant" ? "user" : "bot";
      return `<div class="msg ${who}"><div class="bubble">${esc(m.content || m.text || "")}</div></div>`;
    }).join("")}</div>`;
  }
}

function renderScore(body, d) {
  const c = d.candidate, sl = d.scoring_logic || { dimensions: [] };
  const logic = sl.dimensions.map((x) =>
    `<div class="dim-row"><span class="dim-name">${esc(x.name)}</span>
      <span class="dim-w">权重 ${x.weight}</span>
      <span class="dim-desc">${esc(x.description || "")}</span></div>`).join("");
  body.innerHTML = `
    <div class="score-head">
      <div class="score-big">${Number(c.score || 0).toFixed(1)}<small>/100</small></div>
      <div class="score-sum">${esc(c.score_summary || "（暂无综合评价，可点『重新评分』现算）")}
        <div class="hint mt">评分于 ${esc(c.scored_at || "未评分")}</div></div>
    </div>
    <div class="dim-block"><div class="dim-title">评分维度与权重 · 岗位「${esc(sl.job_id || "—")}」</div>${logic || '<div class="empty">无维度配置</div>'}</div>
    <div class="run-actions"><span class="save-msg" id="rescoreMsg"></span>
      <button class="primary" id="rescoreBtn">重新评分（现算每维度得分+依据）</button></div>
    <div id="rescoreResult"></div>`;
  $("#rescoreBtn").addEventListener("click", () => doRescore(c.uid));
}

async function doRescore(uid) {
  const btn = $("#rescoreBtn"), msg = $("#rescoreMsg"), out = $("#rescoreResult");
  btn.disabled = true; btn.textContent = "评分中（调 DeepSeek）…"; msg.textContent = "";
  const r = await api("/api/candidate/rescore", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ uid }),
  });
  btn.disabled = false; btn.textContent = "重新评分（现算每维度得分+依据）";
  if (!r.ok) { msg.className = "save-msg err"; msg.textContent = r.error || "评分失败"; return; }
  out.innerHTML = `<div class="dim-title mt">本次评分结果 · 总分 ${r.total}（岗位「${esc(r.job_id || "—")}」）</div>
    ${r.dimensions.map((x) =>
      `<div class="dim-score"><div class="dim-score-head"><b>${esc(x.name)}</b>
        <span>${x.raw}/10 × 权重${x.weight} = <b>${x.weighted}</b></span></div>
        <div class="dim-evidence">${esc(x.evidence || "")}</div></div>`).join("")}
    <div class="score-sum mt">${esc(r.summary || "")}</div>`;
  loadBoard();
}

/* ── 操作台 ── */
const TASK_META = {
  greet: { title: "打招呼", limit: 20, degree: true }, collect: { title: "收简历", limit: 20, degree: true },
  chat: { title: "智能沟通", limit: 20, degree: true }, pipeline: { title: "全流程", pipeline: true },
};
let currentTask = null, logCursor = 0, currentJob = null;
$$(".act-card").forEach((card) => card.addEventListener("click", () => openRunConfig(card.dataset.task)));
function openRunConfig(task) {
  currentTask = task;
  const m = TASK_META[task];
  $$(".act-card").forEach((c) => c.classList.toggle("is-selected", c.dataset.task === task));
  $("#runTitle").textContent = m.title + " · 配置";
  // 打招呼人数：0 = 打到 BOSS 每日上限（不是「跳过」）。给 HR 用统一 min=1 防误触发大规模操作。
  const numField = (k, label, isGreet) =>
    `<label class="field"><span>${label}人数</span>
      <input type="number" min="${isGreet ? 1 : 0}" id="${k}" value="20" />
      ${isGreet ? '<em class="fhint">打招呼按成功人数计；想打到每日上限请填一个很大的数</em>' : ""}</label>`;
  let f = m.pipeline
    ? ["greet", "collect", "chat"].map((k) => numField(`p_${k}`, TASK_META[k].title, k === "greet")).join("")
    : `<label class="field"><span>人数（limit）</span>
        <input type="number" min="${currentTask === "greet" ? 1 : 0}" id="p_limit" value="${m.limit}" />
        ${currentTask === "greet" ? '<em class="fhint">打招呼按成功人数计；想打到每日上限请填一个很大的数</em>' : ""}</label>`;
  if (m.degree || m.pipeline) {
    f += `<label class="field"><span>最低学历</span><select id="p_degree"><option value="">不限</option>
      <option>大专</option><option>本科</option><option>硕士</option><option>博士</option></select></label>`;
  }
  $("#runFields").innerHTML = f;
  $("#runConfig").hidden = false;
  $("#runConfig").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("#closeRun").addEventListener("click", () => {
  $("#runConfig").hidden = true;
  $$(".act-card").forEach((c) => c.classList.remove("is-selected"));
});
$("#startRun").addEventListener("click", async () => {
  const m = TASK_META[currentTask];
  const params = { dry_run: $("#dryRun").checked };
  if (m.pipeline) ["greet", "collect", "chat"].forEach((k) => (params[k] = +$(`#p_${k}`).value || 0));
  else params.limit = +$("#p_limit").value || 0;
  const deg = $("#p_degree");
  if (deg && deg.value) params.min_degree = deg.value;
  const res = await api("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: currentTask, params }),
  });
  if (!res.ok) { alert("启动失败：" + (res.error || "")); return; }
  currentJob = res.job_id; logCursor = 0;
  $("#consolePanel").hidden = false;
  $("#console").textContent = `$ ${res.cmd}\n`;
  $("#consolePanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  pollJob();
});
async function pollJob() {
  if (!currentJob) return;
  const st = await api(`/api/job/${currentJob}?since=${logCursor}`);
  if (st.ok) {
    if (st.log && st.log.length) {
      const c = $("#console");
      c.textContent += st.log.join("\n") + "\n"; c.scrollTop = c.scrollHeight; logCursor = st.next;
    }
    const badge = $("#runStatus");
    badge.textContent = { running: "运行中…", done: "✅ 完成", failed: "❌ 失败", stopped: "⏹ 已停止" }[st.status] || st.status;
    badge.className = "run-status " + st.status;
    if (st.status === "running") { setTimeout(pollJob, 1200); return; }
  }
  currentJob = null;
}
$("#stopRun").addEventListener("click", async () => { if (currentJob) await api(`/api/job/${currentJob}/stop`, { method: "POST" }); });

/* ── 问伯乐 ── */
const history = [];
const STARTER_CHIPS = ["帮我跑一遍今天的招聘流程", "打招呼 20 人", "看看谁最合适", "收 30 份简历", "同步一下岗位"];
const FOLLOWUP_CHIPS = ["那就开始吧", "换个方向", "谁最合适？", "帮我约面试"];

function renderChips(items) {
  const box = $("#chatChips");
  box.innerHTML = items.map((t) => `<button class="chip">${esc(t)}</button>`).join("");
  $$(".chip", box).forEach((c) => c.addEventListener("click", () => sendChat(c.textContent)));
}
$("#chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#chatText").value.trim();
  if (text) sendChat(text);
});
async function sendChat(text) {
  $("#chatText").value = "";
  $("#chatChips").innerHTML = "";
  addMsg("user", text);
  $("#botStatus").textContent = "正在输入…";
  const think = addMsg("bot", "", { typing: true });
  const res = await api("/api/bole", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, history }),
  });
  think.remove();
  $("#botStatus").textContent = "在线 · AI 招聘助手";
  if (res.ok) {
    if (res.actions && res.actions.length) addActionLine(res.actions);
    addMsg("bot", res.reply);
    history.push({ role: "user", content: text }, { role: "assistant", content: res.reply });
    if (history.length > 40) history.splice(0, history.length - 40);
    loadSuggestions(res.reply);   // 动态：让伯乐按这次回复生成快捷选项
  } else {
    addMsg("bot", "⚠ " + (res.error || "调用失败") + "（去『设置』确认 DeepSeek Key）");
    renderChips(STARTER_CHIPS);
  }
}

// 回复已显示后，异步取「结合本次回复」的动态快捷回复（不拖慢正文）
async function loadSuggestions(reply) {
  $("#chatChips").innerHTML = '<span class="chips-loading">生成建议…</span>';
  let suggestions = [];
  try {
    const s = await api("/api/bole/suggest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history, reply }),
    });
    suggestions = (s && s.suggestions) || [];
  } catch (_) { /* 忽略，走兜底 */ }
  renderChips(suggestions.length ? suggestions : FOLLOWUP_CHIPS);
}
function nowHM() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
// 先 esc 再把 **粗体** 转 <strong>（esc 已中和 HTML，安全）
function fmt(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}
// 透明化：伯乐真的调了哪些工具（读了真实数据/跑了真实脚本）
function addActionLine(actions) {
  const el = document.createElement("div");
  el.className = "action-line";
  el.innerHTML = "🔧 " + actions.map((a) => esc(a)).join(" · ");
  const log = $("#chatLog");
  log.appendChild(el); log.scrollTop = log.scrollHeight;
  lastSender = null; // 打断分组，下一条气泡带尾巴
}
let lastSender = null;
function addMsg(cls, text, opts = {}) {
  const sender = cls.split(" ")[0]; // "bot" | "user"
  const log = $("#chatLog");
  const grouped = !opts.typing && sender === lastSender;
  if (grouped && log.lastElementChild) log.lastElementChild.classList.add("no-tail");
  const el = document.createElement("div");
  el.className = "msg " + cls + (grouped ? " grouped" : "");
  if (opts.typing) {
    el.innerHTML = '<div class="bubble typing"><span></span><span></span><span></span></div>';
  } else {
    const ticks = sender === "user" ? '<span class="ticks">✓✓</span>' : "";
    el.innerHTML = `<div class="bubble">${fmt(text)}` +
      `<span class="meta"><span class="time">${nowHM()}</span>${ticks}</span></div>`;
    lastSender = sender;
  }
  log.appendChild(el); log.scrollTop = log.scrollHeight;
  return el;
}
// 初始问候 + 起始建议（放在 addMsg / lastSender 定义之后，避免 TDZ）
addMsg("bot", "你好，我是伯乐。想让我帮你打招呼、收简历，还是看看谁最合适？直接说，或点下面的按钮。");
renderChips(STARTER_CHIPS);

/* ── 设置 ── */
async function loadConfig() {
  const c = await api("/api/config");
  if (!c.ok) return;
  $("#dsKey").placeholder = c.deepseek_key_set ? `${c.deepseek_key_masked}（留空表示不改动）` : "sk-…（尚未配置）";
  $("#dsModel").value = c.deepseek_model || "";
  $("#dsBase").value = c.deepseek_base_url || "";
  $("#cloudSync").value = c.cloud_sync || "on";
  $("#keyState").textContent = c.deepseek_key_set ? "已配置" : "未配置 · 智能回复将降级";
}
$("#saveConfig").addEventListener("click", async () => {
  const msg = $("#saveMsg");
  const res = await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      deepseek_api_key: $("#dsKey").value.trim(), deepseek_model: $("#dsModel").value.trim(),
      deepseek_base_url: $("#dsBase").value.trim(), cloud_sync: $("#cloudSync").value,
    }),
  });
  if (res.ok) { msg.className = "save-msg"; msg.textContent = "已保存 ✓"; $("#dsKey").value = ""; loadConfig(); loadDoctor(); }
  else { msg.className = "save-msg err"; msg.textContent = res.error || "保存失败"; }
  setTimeout(() => (msg.textContent = ""), 3000);
});
$("#testConfig").addEventListener("click", async () => {
  const msg = $("#saveMsg"), btn = $("#testConfig");
  btn.disabled = true; btn.textContent = "测试中…"; msg.className = "save-msg"; msg.textContent = "";
  const r = await api("/api/config/test", { method: "POST" });
  btn.disabled = false; btn.textContent = "测试连接";
  if (r.ok) { msg.className = "save-msg"; msg.textContent = `✓ 连接正常，DeepSeek 回复「${r.sample}」`; }
  else { msg.className = "save-msg err"; msg.textContent = "✗ " + (r.error || "连接失败"); }
});

/* 启动 */
boot();
