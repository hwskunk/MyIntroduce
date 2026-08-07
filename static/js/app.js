/* ═══════════════════════════════════════════════════════════
   mint.lu — 终端前端逻辑
   单一持久终端窗口：入口 → 访客聊天(SSE流式) / 管理员后台，
   登录只在同一个窗口内切换内容，不再整体重绘成"新页面"。
   ═══════════════════════════════════════════════════════════ */
"use strict";

const state = {
  company: localStorage.getItem("mykb_company") || null,
  role: localStorage.getItem("mykb_role") || null,
  ownerName: null, // 本人姓名，用于 AI 身份标签
};

let contactsCache = null; // 联系方式数据缓存（历史消息重新渲染联系区块用）

// 启动时获取本人姓名（失败则回退显示 "AI"）
fetch("/api/config")
  .then((r) => r.json())
  .then((d) => { state.ownerName = d.owner_name || null; })
  .catch(() => {});

// 启动时预取联系方式（历史消息里带 contacts 标记的消息需重新渲染联系区块）
async function loadContactsCache() {
  try {
    const res = await fetch("/api/contacts");
    contactsCache = await res.json();
  } catch (_) { /* 保持 null */ }
}
loadContactsCache();

/* ── 持久化终端窗口骨架（只创建一次，后续只换内容）── */
const app = document.getElementById("app");
app.innerHTML = `
<div class="term-window">
  <div class="term-titlebar">
    <span class="tb-logo" id="win-title">mint.lu</span>
    <span class="tb-version">v2.1</span>
    <span class="tb-status"><span class="led" id="win-led-dot"></span><span id="win-led">ONLINE</span></span>
  </div>
  <div class="term-status hidden" id="win-status"></div>
  <div class="term-content" id="win-content"></div>
  <div class="term-input-row" id="win-input"></div>
  <div class="term-footer hidden" id="win-footer"></div>
</div>`;

const titleEl = document.getElementById("win-title");
const statusEl = document.getElementById("win-status");
const contentEl = document.getElementById("win-content");
const inputRowEl = document.getElementById("win-input");
const footerEl = document.getElementById("win-footer");

/* ── 工具 ── */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/* ── markdown 渲染（marked + DOMPurify，本地 vendor）── */
function renderMD(text) {
  if (window.marked) {
    const html = window.marked.parse(text || "", { breaks: true });
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
  }
  return esc(text || ""); // 库未加载时退化为纯文本
}

function renderMessage(who, text) {
  const label = `<span class="who">[${who}]</span> `;
  if (!text) return label;
  let html = renderMD(text);
  // 把身份标签注入到第一个块元素内部，使标签与首行内容同行
  // （pre 除外：代码块不能内嵌标签，标签放在代码块之前）
  const tag = html.match(/^(<([a-z][a-z0-9]*)[^>]*>)/i);
  if (tag && /^(p|h[1-6]|blockquote)$/i.test(tag[2])) {
    html = tag[1] + label + html.slice(tag[1].length);
  } else {
    html = label + html;
  }
  return html;
}

function truncateName(s, max) {
  // 超长名称截断加省略号，避免把消息框撑爆
  const t = String(s || "");
  return t.length > max ? t.slice(0, max) + "…" : t;
}

function whoLabel(role) {
  // 访客消息显示公司名（超长截断），AI 消息显示本人姓名
  if (role === "user") return truncateName(state.company, 14) || "访客";
  return state.ownerName || "AI";
}

/* 知识库外：联系方式独立区块（文字 + 二维码） */
function renderContacts(items, images) {
  const box = el(`<div class="contact-box">
    <div class="contact-title">想进一步了解？欢迎通过以下方式联系我</div>
    <div class="contact-list"></div>
  </div>`);
  const list = box.querySelector(".contact-list");
  for (const c of items) {
    list.appendChild(el(`<div class="contact-item"><span class="ci-label">${esc(c.label)}</span><span class="ci-value">${esc(c.value)}</span></div>`));
  }
  if (images && images.length) {
    const qr = el(`<div class="contact-qr"></div>`);
    for (const img of images) {
      qr.appendChild(el(`<div class="qr-item"><img src="${esc(img.url)}" alt="${esc(img.label)}" onerror="this.style.display='none'"><div class="qr-label">${esc(img.label)} 扫码添加</div></div>`));
    }
    box.appendChild(qr);
  }
  return box;
}

/* 索要简历：简历文件区块（查看 / 下载） */
function renderResume(data) {
  const v = (data && data.view_url) || "/api/resume";
  const d = (data && data.download_url) || "/api/resume?download=1";
  return el(`<div class="resume-box">
    <div class="resume-title">RESUME :: 简历文件</div>
    <div class="resume-actions">
      <a class="resume-btn" href="${esc(v)}" target="_blank" rel="noopener">查看简历</a>
      <a class="resume-btn" href="${esc(d)}" download>下载简历</a>
    </div>
  </div>`);
}
function setTitle(t) { titleEl.textContent = t; }
function setStatus(html) {
  statusEl.innerHTML = html || "";
  statusEl.classList.toggle("hidden", !html);
}
function setContent(html) {
  contentEl.innerHTML = html;
  contentEl.scrollTop = 0;
}
function setInput(html) {
  inputRowEl.innerHTML = html || "";
  inputRowEl.classList.toggle("hidden", !html);
}
function setFooter(html, show) {
  footerEl.innerHTML = html || "";
  footerEl.classList.toggle("hidden", !show);
}
/* 顶栏状态灯：LED 呼吸绿点 + 文字（BOOT / ONLINE） */
function setLed(txt, mode) {
  const dot = document.getElementById("win-led-dot");
  const label = document.getElementById("win-led");
  if (dot) dot.className = "led" + (mode === "warn" ? " led-warn" : mode === "off" ? " led-off" : "");
  if (label) label.textContent = txt || "ONLINE";
}
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

/* boot 开机自检行 HTML（签名元素，真实模块与统计） */
function bootLineHtml(l) {
  if (l.type === "title") return `<div class="boot-title">${esc(l.text)}</div>`;
  if (l.type === "sep") return `<div class="boot-sep">${esc(l.text || "─".repeat(26))}</div>`;
  if (l.type === "ok") {
    const suffix = l.val ? `<span class="val">  ${esc(l.val)}</span>` : "";
    return `<div class="boot-line"><span class="pfx-ok">[ OK ]</span> <span class="label">${esc(l.label)}</span>${suffix}</div>`;
  }
  if (l.type === "dim") return `<div class="boot-line dim">${esc(l.text || "")}</div>`;
  return `<div class="boot-line">${esc(l.text || "")}</div>`;
}

async function api(url, opts = {}) {
  // 公司名通过查询参数传递（HTTP 头不能携带中文）
  const sep = url.includes("?") ? "&" : "?";
  const u = state.company ? `${url}${sep}company=${encodeURIComponent(state.company)}` : url;
  // 8s 超时：网络异常时不至于永远"连接中"
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const resp = await fetch(u, { ...opts, signal: ctrl.signal, headers: opts.headers || {} });
    if (!resp.ok) {
      let msg = "HTTP " + resp.status;
      try { msg = (await resp.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return resp.json();
  } catch (e) {
    if (e && e.name === "AbortError") throw new Error("请求超时：服务无响应，请确认服务在运行");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/* ═══════════════════════════════════════════════════════════
   入口（同一窗口内的"连接"阶段）
   ═══════════════════════════════════════════════════════════ */
async function showLanding() {
  setTitle("mint.lu");
  setStatus("");
  setContent("");
  setInput("");
  setFooter("", false);
  setLed("BOOT", "warn");

  // boot 开机自检：简洁的就绪仪式（面向访客，不列内部技术细节）
  const lines = [
    { type: "title", text: "mint.lu :: INTRO" },
    { type: "sep" },
    { type: "ok", label: "知识库已就绪" },
    { type: "ok", label: "服务运行正常" },
    { type: "dim", text: "欢迎使用，填写公司名称即可开始对话。" },
    { type: "sep" },
  ];
  for (const l of lines) {
    contentEl.appendChild(el(bootLineHtml(l)));
    contentEl.scrollTop = contentEl.scrollHeight;
    await delay(70);
  }
  setLed("ONLINE");

  // 说明面板 + 操作提示（一次出现）
  contentEl.appendChild(el(`
    <div class="landing-note">
      <div class="ln-title">关于「公司名称」— 无需顾虑</div>
      <div class="t-line dim">· 仅用于留档区分：这条会话单独存档，方便你之后回来查看</div>
      <div class="t-line dim">· 无需全称或实名：随手写个代号即可</div>
      <div class="t-line dim">· 仅存于本机数据库：不会公开，也不会提交给任何第三方</div>
      <div class="t-line dim">· 无需任何敏感信息：手机号、邮箱、证件等统统不用填</div>
    </div>`));
  // 注意：el() 只返回模板的 firstElementChild，多个并列元素必须包一层容器
  contentEl.appendChild(el(`
    <div>
      <div class="t-line dim center">管理员请直接输入访问密钥进入后台。</div>
      <div class="t-line dim">&nbsp;</div>
      <div class="t-line dim center" id="landing-msg">填写公司名称后按回车，或点击「建立连接」开始对话</div>
    </div>
  `));
  contentEl.scrollTop = contentEl.scrollHeight;

  setInput(`
    <span class="prompt">访客公司名称&gt;</span>
    <input id="company-input" autofocus autocomplete="off" spellcheck="false" placeholder="例：某某科技有限公司">
    <button class="btn" id="connect-btn">建立连接</button>
  `);

  const input = document.getElementById("company-input");
  const btn = document.getElementById("connect-btn");
  const msg = document.getElementById("landing-msg");

  async function doConnect() {
    const c = input.value.trim();
    if (!c) {
      if (msg) { msg.className = "t-line center err"; msg.textContent = "[ERR] 公司名称不能为空"; }
      return;
    }
    try {
      btn.disabled = true;
      btn.textContent = "连接中...";
      if (msg) { msg.className = "t-line center"; msg.textContent = "正在建立连接..."; }
      const res = await api("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: c }),
      });
      state.company = res.company;
      state.role = res.role;
      localStorage.setItem("mykb_company", state.company);
      localStorage.setItem("mykb_role", state.role);
      // 同一窗口内提示连接成功，再进入对应视图
      if (msg) { msg.className = "t-line ok"; msg.textContent = "连接成功。进入会话窗口..."; }
      setTimeout(() => (state.role === "admin" ? showAdmin() : showChat()), 300);
    } catch (e) {
      console.error("[doConnect]", e);
      if (msg) { msg.className = "t-line center err"; msg.textContent = "[ERR] " + (e && e.message ? e.message : e); }
      btn.disabled = false;
      btn.textContent = "建立连接";
    }
  }
  btn.onclick = doConnect;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") doConnect(); });
  input.focus();
}

/* ═══════════════════════════════════════════════════════════
   访客聊天（同一窗口）
   ═══════════════════════════════════════════════════════════ */
function showChat() {
  setTitle("mint.lu :: SESSION");
  setStatus(`<span class="conn">[CONN]</span> 已连接 | 访客: ${esc(state.company)}`);
  setContent(`<div class="t-line dim center">加载历史会话...</div>`);
  setInput(`
    <span class="prompt">&gt;</span>
    <input id="chat-input" autofocus autocomplete="off" spellcheck="false" placeholder="输入你的问题...">
    <button class="btn" id="send-btn">发送</button>
  `);
  setFooter(`
    <span class="hint">提示：像和真人聊天一样提问，我会基于知识库中的真实信息回答。</span>
    <button class="btn" id="logout-btn">返回入口 / 切换身份</button>
  `, true);

  const msgBox = contentEl;
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");

  function appendMsg(role, text, sources, meta) {
    const who = whoLabel(role);
    const m = el(`<div class="msg ${role}"><div class="body"></div></div>`);
    m.querySelector(".body").innerHTML = renderMessage(who, text);
    if (sources && sources.length) {
      const s = el(`<div class="sources">参考来源：${sources.map(esc).join("，")}</div>`);
      m.appendChild(s);
    }
    // 历史消息：原消息附带联系方式/简历区块时重新渲染
    if (meta && meta.contacts && contactsCache) {
      m.appendChild(renderContacts(contactsCache.items || [], contactsCache.images || []));
    }
    if (meta && meta.resume) {
      m.appendChild(renderResume(meta.resume === true ? null : meta.resume));
    }
    msgBox.appendChild(m);
    msgBox.scrollTop = msgBox.scrollHeight;
    return m;
  }

  // 新会话欢迎语：前端打字机效果逐字流出（不写入会话历史，刷新后重新出现）
  async function typeWelcome() {
    const m = el(`<div class="msg ai"><div class="body"></div></div>`);
    msgBox.appendChild(m);
    const bodyEl = m.querySelector(".body");
    let name = "本人";
    try {
      const cfg = await api("/api/config");
      name = cfg.owner_name || name;
    } catch (e) { /* 拿不到名字就用默认称呼 */ }
    const text = `你好！我是${name}，欢迎${state.company}的访客。你可以问我关于我的教育背景、技能、项目经验、获奖经历等问题，也可以直接向我要我的简历。有什么想了解的，尽管问吧！`;
    let i = 0;
    await new Promise(resolve => {
      const timer = setInterval(() => {
        i += 2;
        bodyEl.innerHTML = renderMessage(whoLabel("ai"), text.slice(0, i));
        msgBox.scrollTop = msgBox.scrollHeight;
        if (i >= text.length) { clearInterval(timer); resolve(); }
      }, 24);
    });
  }

  async function loadHistory() {
    try {
      const { messages } = await api("/api/thread/history");
      msgBox.innerHTML = "";
      if (!messages.length) {
        msgBox.appendChild(el(`<div class="t-line dim center">[INFO] 新会话，发送第一条消息开始吧。</div>`));
        await typeWelcome();
      } else {
        if (!contactsCache) await loadContactsCache();
        for (const m of messages) appendMsg(m.role, m.content, null, m.meta);
      }
    } catch (e) {
      msgBox.innerHTML = `<div class="t-line err center">[ERR] 加载历史失败: ${esc(e.message)}</div>`;
    }
  }

  async function send() {
    const msg = input.value.trim();
    if (!msg || sendBtn.disabled) return;
    input.value = "";
    sendBtn.disabled = true;

    appendMsg("user", msg);
    const aiEl = appendMsg("ai", "");
    const bodyEl = aiEl.querySelector(".body");
    bodyEl.classList.add("typing");

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: state.company, message: msg }),
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let full = "";
      let sources = [];
      let contactsData = null;
      let resumeData = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // 归一化 \r\n → \n，兼容不同 SSE 分隔符实现
        buf = buf.replace(/\r\n/g, "\n");
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const evt = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const rawLine of evt.split("\n")) {
            const line = rawLine.replace(/\r$/, "");
            if (!line.startsWith("data: ")) continue;
            const data = JSON.parse(line.slice(6));
            if (data.delta) {
              full += data.delta;
              bodyEl.innerHTML = renderMessage(whoLabel("ai"), full);
              msgBox.scrollTop = msgBox.scrollHeight;
            } else if (data.done) {
              sources = data.sources || [];
            } else if (data.contacts) {
              contactsData = data.contacts;
            } else if (data.resume) {
              resumeData = data.resume;
            } else if (data.error) {
              throw new Error(data.error);
            }
          }
        }
      }
      bodyEl.classList.remove("typing");
      if (sources.length) {
        const s = el(`<div class="sources">参考来源：${sources.map(esc).join("，")}</div>`);
        aiEl.appendChild(s);
      }
      if (contactsData && (contactsData.items || contactsData.images)) {
        aiEl.appendChild(renderContacts(contactsData.items || [], contactsData.images || []));
      }
      if (resumeData) {
        aiEl.appendChild(renderResume(resumeData));
      }
      if (!full) bodyEl.innerHTML = renderMessage(whoLabel("ai"), "抱歉，知识库中暂时没有找到相关信息。");
    } catch (e) {
      bodyEl.classList.remove("typing");
      bodyEl.innerHTML = renderMessage(whoLabel("ai"), "抱歉，系统遇到了一个错误：" + e.message);
    }
    sendBtn.disabled = false;
    input.focus();
  }

  sendBtn.onclick = send;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  document.getElementById("logout-btn").onclick = logout;
  loadHistory();
  input.focus();
}

/* ═══════════════════════════════════════════════════════════
   管理员后台（同一窗口）
   ═══════════════════════════════════════════════════════════ */
function showAdmin() {
  setTitle("mint.lu :: ADMIN");
  setStatus(`<span class="conn">[ROOT]</span> 管理员后台 | 全部会话 / 知识库管理 / 文档上传`);
  setContent(`
    <div class="tabs">
      <button class="tab active" data-tab="threads">对话窗口</button>
      <button class="tab" data-tab="upload">上传文件</button>
      <button class="tab" data-tab="docs">知识库文档</button>
    </div>
    <div class="panel" id="admin-panel"></div>
  `);
  setInput("");
  setFooter(`
    <span class="hint">ADMIN MODE :: ROOT</span>
    <button class="btn" id="logout-btn">退出管理员模式</button>
  `, true);

  const panel = document.getElementById("admin-panel");
  document.getElementById("logout-btn").onclick = logout;

  document.querySelectorAll(".tab").forEach((tb) => {
    tb.onclick = () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tb.classList.add("active");
      switchTab(tb.dataset.tab);
    };
  });

  const tabs = { threads: renderThreads, upload: renderUpload, docs: renderDocs };
  function switchTab(name) { tabs[name](panel); }
  switchTab("threads");
}

/* 对话窗口 */
async function renderThreads(panel) {
  panel.innerHTML = `<div class="center"><span class="spin"></span> 加载会话...</div>`;
  let threads = [];
  try { threads = await api("/api/admin/threads"); }
  catch (e) { panel.innerHTML = `<div class="t-line err">[ERR] ${esc(e.message)}</div>`; return; }

  if (!threads.length) {
    panel.innerHTML = `<div class="t-line dim">[INFO] 暂无会话记录。</div>`;
    return;
  }
  const total = threads.reduce((s, t) => s + t.total_messages, 0);
  let html = `<div class="t-line">访客公司数：<span class="num">${threads.length}</span> | 消息总数：<span class="num">${total}</span></div>
  <table class="term-table mt">
    <tr><th>公司 / 会话</th><th>消息</th><th>轮次</th><th>最后活跃</th><th>总结</th></tr>`;
  for (const t of threads) {
    html += `<tr data-id="${esc(t.thread_id)}">
      <td>${esc(t.thread_id)}</td><td class="num">${t.total_messages}</td>
      <td class="num">${t.total_rounds}</td><td class="num">${esc(t.last_time || "")}</td>
      <td>${t.has_summary ? "是" : "—"}</td></tr>`;
  }
  html += `</table><div class="t-line dim mt">点击一行查看该会话全部内容。</div>`;
  panel.innerHTML = html;

  panel.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.onclick = () => viewThread(panel, tr.dataset.id);
  });
}

async function viewThread(panel, id) {
  let msgs = [];
  try { msgs = await api(`/api/admin/thread/${encodeURIComponent(id)}`); }
  catch (e) { panel.innerHTML = `<div class="t-line err">[ERR] ${esc(e.message)}</div>`; return; }
  let html = `<div class="t-line"><span class="pfx">[VIEW]</span> 会话 ${esc(id)} · 共 ${msgs.length} 条消息</div>`;
  for (const m of msgs) {
    // 管理员看会话：访客身份用会话所属公司名（id，超长截断），AI 用本人姓名
    const who = m.role === "user" ? truncateName(id, 14) : (state.ownerName || "AI");
    const cls = m.role === "user" ? "user" : "ai";
    html += `<div class="msg ${cls}"><div class="body">${renderMessage(who, m.content)}</div></div>`;
  }
  html += `<button class="btn danger mt" id="del-thread">清空此会话</button>
           <button class="btn mt" id="back-threads" style="margin-left:8px">返回列表</button>`;
  panel.innerHTML = html;
  document.getElementById("back-threads").onclick = () => renderThreads(panel);
  document.getElementById("del-thread").onclick = async () => {
    if (!confirm(`确认清空会话「${id}」？不可恢复。`)) return;
    await api(`/api/admin/thread/${encodeURIComponent(id)}`, { method: "DELETE" });
    renderThreads(panel);
  };
}

/* 上传文件 */
function renderUpload(panel) {
  panel.innerHTML = `
    <div class="t-line dim">支持格式：.md .txt .pdf .docx .xlsx .pptx .html .png .jpg .jpeg</div>
    <div class="file-zone mt">点击选择文件（可多选），导入后自动入库并同步知识库。</div>
    <input type="file" id="up-input" multiple hidden>
    <button class="btn" id="up-pick">选择文件</button>
    <button class="btn" id="up-upload" disabled>导入到知识库</button>
    <div class="mt" id="up-result"></div>`;

  const input = document.getElementById("up-input");
  const pick = document.getElementById("up-pick");
  const upBtn = document.getElementById("up-upload");
  const result = document.getElementById("up-result");
  const zone = panel.querySelector(".file-zone");

  pick.onclick = () => input.click();
  zone.onclick = () => input.click();
  input.onchange = () => {
    if (input.files.length) {
      zone.textContent = `已选择 ${input.files.length} 个文件`;
      upBtn.disabled = false;
    }
  };
  upBtn.onclick = async () => {
    const fd = new FormData();
    for (const f of input.files) fd.append("files", f);
    upBtn.disabled = true;
    upBtn.textContent = "导入中...";
    try {
      const { results } = await api("/api/admin/upload", { method: "POST", body: fd });
      let html = "";
      results.forEach((r, i) => {
        const added = (r.added || []).length, changed = (r.changed || []).length;
        html += `<div class="t-line">${esc(input.files[i].name)} → 新增${added} 变化${changed}</div>`;
      });
      result.innerHTML = html || `<div class="t-line ok">[ OK ] 导入完成</div>`;
    } catch (e) {
      result.innerHTML = `<div class="t-line err">[ERR] ${esc(e.message)}</div>`;
    }
    upBtn.disabled = false;
    upBtn.textContent = "导入到知识库";
    input.value = "";
  };
}

/* 知识库文档 */
async function renderDocs(panel) {
  panel.innerHTML = `<div class="center"><span class="spin"></span> 加载文档...</div>`;
  let docs = [];
  try { docs = await api("/api/admin/docs"); }
  catch (e) { panel.innerHTML = `<div class="t-line err">[ERR] ${esc(e.message)}</div>`; return; }

  if (!docs.length) {
    panel.innerHTML = `<div class="t-line dim">[INFO] 知识库为空，请在上传面板添加文档。</div>`;
    return;
  }
  let html = `<div class="t-line">文档数：<span class="num">${docs.length}</span></div>
  <table class="term-table mt">
    <tr><th>文档</th><th>来源</th><th>片段</th><th>字符</th><th>入库时间</th></tr>`;
  for (const d of docs) {
    html += `<tr data-id="${esc(d.id)}">
      <td>${esc(d.title)}</td><td class="num">${esc(d.source)}</td>
      <td class="num">${d.chunk_count}</td><td class="num">${d.char_count}</td>
      <td class="num">${esc(d.created_at)}</td></tr>`;
  }
  html += `</table><div class="t-line dim mt">点击一行查看全文。</div>
  <button class="btn mt" id="rescan-btn">重新扫描 data/</button>`;
  panel.innerHTML = html;

  document.getElementById("rescan-btn").onclick = async () => {
    const s = await api("/api/admin/rescan", { method: "POST" });
    const sum = (a) => a.length;
    alert(`同步完成：新增${sum(s.added)} 变化${sum(s.changed)} 删除${sum(s.removed)}`);
    renderDocs(panel);
  };
  panel.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.onclick = () => viewDoc(panel, tr.dataset.id);
  });
}

async function viewDoc(panel, id) {
  let doc = null;
  try { doc = await api(`/api/admin/docs/${encodeURIComponent(id)}`); }
  catch (e) { panel.innerHTML = `<div class="t-line err">[ERR] ${esc(e.message)}</div>`; return; }
  panel.innerHTML = `
    <div class="t-line"><span class="pfx">[DOC]</span> ${esc(doc.title)} · ${doc.char_count} 字符 · ${doc.chunk_count} 片段</div>
    <div class="msg ai mt"><div class="body">${renderMessage("CONTENT", doc.content)}</div></div>
    <button class="btn" id="back-docs">返回列表</button>`;
  document.getElementById("back-docs").onclick = () => renderDocs(panel);
}

/* ═══════════════════════════════════════════════════════════
   路由
   ═══════════════════════════════════════════════════════════ */
function logout() {
  state.company = null;
  state.role = null;
  localStorage.removeItem("mykb_company");
  localStorage.removeItem("mykb_role");
  showLanding();
}

if (!state.company) showLanding();
else if (state.role === "admin") showAdmin();
else showChat();
