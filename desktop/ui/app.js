"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

/* ── 视图切换 ── */
$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-item");
  if (!btn) return;
  const view = btn.dataset.view;
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b === btn));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));
  if (view === "board") loadBoard();
  if (view === "settings") { loadConfig(); loadDoctor(); }
});

/* ── 装机自检（侧栏健康灯 + 设置页列表）── */
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
      <span class="nm">${esc(c.name)}</span>
      <span class="dt">${esc(c.detail)}</span></div>`).join("");
  html += '<div class="dr-sep">以下需人工确认</div>';
  html += (d.manual || []).map((m) =>
    `<div class="dr-row manual"><span class="mk">◻︎</span><span class="nm">${esc(m)}</span></div>`).join("");
  list.innerHTML = html;
}
$("#recheck").addEventListener("click", loadDoctor);

/* ── 看板 ── */
async function loadBoard() {
  const d = await api("/api/dashboard");
  const s = d.stats || {};
  const cards = [
    ["a", s.total, "候选人总数"], ["b", s.has_resume, "已收简历"],
    ["c", s.scored, "已评分"], ["a", s.has_wechat, "已加微信"],
    ["d", s.interviewed, "已约面试"], ["c", s.today, "今日更新"],
  ];
  $("#statGrid").innerHTML = cards.map(([cls, n, lbl]) =>
    `<div class="stat ${cls}"><div class="num">${n ?? 0}</div><div class="lbl">${lbl}</div></div>`).join("");
  const list = $("#topList");
  if (!d.ok) { list.innerHTML = `<div class="empty">${esc(d.reason || "暂无数据")}</div>`; return; }
  if (!d.top || !d.top.length) { list.innerHTML = '<div class="empty">还没有评分数据，先收集并评分。</div>'; return; }
  list.innerHTML = d.top.map((c, i) =>
    `<div class="top-row"><span class="rk">${i + 1}</span>
      <span class="who"><strong>${esc(c.name)}</strong>
        <div class="meta">${esc(c.school || "—")} · ${esc(c.degree || "—")} · ${esc(c.job_position || "—")}</div></span>
      <span class="sc">${Number(c.score || 0).toFixed(1)}</span></div>`).join("");
}
$("#refreshBoard").addEventListener("click", loadBoard);

/* ── 操作台 ── */
const TASK_META = {
  greet: { title: "打招呼", limit: 20, degree: true },
  collect: { title: "收简历", limit: 20, degree: true },
  chat: { title: "智能沟通", limit: 20, degree: true },
  pipeline: { title: "全流程", pipeline: true },
};
let currentTask = null, pollTimer = null, logCursor = 0, currentJob = null;

$$(".act-card").forEach((card) =>
  card.addEventListener("click", () => openRunConfig(card.dataset.task)));

function openRunConfig(task) {
  currentTask = task;
  const m = TASK_META[task];
  $$(".act-card").forEach((c) => c.classList.toggle("is-selected", c.dataset.task === task));
  $("#runTitle").textContent = m.title + " · 配置";
  let f = "";
  if (m.pipeline) {
    f = ["greet", "collect", "chat"].map((k) =>
      `<label class="field"><span>${TASK_META[k].title}人数</span>
        <input type="number" min="0" id="p_${k}" value="20" /></label>`).join("");
  } else {
    f = `<label class="field"><span>人数（limit）</span>
      <input type="number" min="0" id="p_limit" value="${m.limit}" /></label>`;
  }
  if (m.degree || m.pipeline) {
    f += `<label class="field"><span>最低学历</span>
      <select id="p_degree"><option value="">不限</option>
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
  if (m.pipeline) {
    ["greet", "collect", "chat"].forEach((k) => (params[k] = +$(`#p_${k}`).value || 0));
  } else {
    params.limit = +$("#p_limit").value || 0;
  }
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
      c.textContent += st.log.join("\n") + "\n";
      c.scrollTop = c.scrollHeight;
      logCursor = st.next;
    }
    const badge = $("#runStatus");
    badge.textContent = { running: "运行中…", done: "✅ 完成", failed: "❌ 失败", stopped: "⏹ 已停止" }[st.status] || st.status;
    badge.className = "run-status " + st.status;
    if (st.status === "running") { pollTimer = setTimeout(pollJob, 1200); return; }
  }
  currentJob = null;
}
$("#stopRun").addEventListener("click", async () => {
  if (currentJob) await api(`/api/job/${currentJob}/stop`, { method: "POST" });
});

/* ── 问伯乐 ── */
const history = [];
$("#chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chatText"), text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg("user", text);
  const think = addMsg("bot thinking", "伯乐思考中…");
  const res = await api("/api/bole", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, history }),
  });
  think.remove();
  if (res.ok) {
    addMsg("bot", res.reply);
    history.push({ role: "user", content: text }, { role: "assistant", content: res.reply });
    if (history.length > 40) history.splice(0, history.length - 40);
  } else {
    addMsg("bot", "⚠ " + (res.error || "调用失败") + "（去『设置』确认 DeepSeek Key）");
  }
});
function addMsg(cls, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  const log = $("#chatLog");
  log.appendChild(el); log.scrollTop = log.scrollHeight;
  return el;
}

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
  const body = {
    deepseek_api_key: $("#dsKey").value.trim(),
    deepseek_model: $("#dsModel").value.trim(),
    deepseek_base_url: $("#dsBase").value.trim(),
    cloud_sync: $("#cloudSync").value,
  };
  const msg = $("#saveMsg");
  const res = await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (res.ok) {
    msg.className = "save-msg"; msg.textContent = "已保存 ✓";
    $("#dsKey").value = "";
    loadConfig(); loadDoctor();
  } else {
    msg.className = "save-msg err"; msg.textContent = res.error || "保存失败";
  }
  setTimeout(() => (msg.textContent = ""), 3000);
});

/* 启动 */
loadDoctor();
loadBoard();
