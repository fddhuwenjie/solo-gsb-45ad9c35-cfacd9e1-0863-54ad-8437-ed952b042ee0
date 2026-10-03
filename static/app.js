"use strict";

const state = { devices: [], pairs: [], routes: [], zones: [] };
// 串音隔离规划：按路由序号存组名（"" 表示不分组）；禁配组对 [a,b] 列表
const isolationState = { groups: [], forbidden: [] };
let lastIsolationData = null;
let activeIsolationConflict = null;
// 信号命名审计名签（按目标身份 key 存）：{ports:{ "DEV.port": {name,alias}}, pairs:{id:...}, cables:{id:...}, routes:{idx:...}}
const auditLabels = { ports: {}, pairs: {}, cables: {}, routes: {} };
let lastPlanData = null;
let lastAuditData = null;
let activeIssueId = null;
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------- 配置编辑 ---------------- */

function renderDevices() {
  $("#device-rows").innerHTML = state.devices.map((d, i) => `
    <tr>
      <td><input value="${esc(d.name)}" placeholder="如 PRE"
           oninput="updateDevice(${i}, 'name', this.value)"></td>
      <td><input value="${esc(d.inputs.join(', '))}" placeholder="如 in1, in2"
           oninput="updateDevice(${i}, 'inputs', this.value)"></td>
      <td><input value="${esc(d.outputs.join(', '))}" placeholder="如 out1, out2"
           oninput="updateDevice(${i}, 'outputs', this.value)"></td>
      <td><button class="danger" onclick="removeDevice(${i})">删除</button></td>
    </tr>`).join("");
}

function updateDevice(i, field, value) {
  if (field === "name") state.devices[i].name = value.trim();
  else state.devices[i][field] = value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  renderPairs();
  renderRoutes();
}

function addDevice() {
  state.devices.push({ name: "", inputs: [], outputs: [] });
  renderDevices(); renderPairs(); renderRoutes();
}

function removeDevice(i) {
  state.devices.splice(i, 1);
  renderDevices(); renderPairs(); renderRoutes();
}

function portOptions(dir, selected) {
  let html = `<option value="">（空）</option>`;
  for (const d of state.devices) {
    if (!d.name) continue;
    for (const p of (dir === "out" ? d.outputs : d.inputs)) {
      const v = `${d.name}.${p}`;
      html += `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(v)}</option>`;
    }
  }
  return html;
}

function renderPairs() {
  $("#pair-rows").innerHTML = state.pairs.map((p, i) => `
    <tr>
      <td><input type="number" style="width:64px" value="${esc(p.id)}"
           oninput="state.pairs[${i}].id = this.value"></td>
      <td><select onchange="state.pairs[${i}].top = this.value">${portOptions("out", p.top)}</select></td>
      <td><select onchange="state.pairs[${i}].bottom = this.value">${portOptions("in", p.bottom)}</select></td>
      <td><select onchange="state.pairs[${i}].mode = this.value">
        <option value="full" ${p.mode === "full" ? "selected" : ""}>全常态</option>
        <option value="half" ${p.mode === "half" ? "selected" : ""}>半常态</option>
        <option value="through" ${p.mode === "through" ? "selected" : ""}>直通</option>
      </select></td>
      <td><button class="danger" onclick="state.pairs.splice(${i},1);renderPairs()">删除</button></td>
    </tr>`).join("");
}

function addPair() {
  const maxId = state.pairs.reduce((m, p) => Math.max(m, Number(p.id) || 0), 0);
  state.pairs.push({ id: maxId + 1, top: "", bottom: "", mode: "half" });
  renderPairs();
}

function renderRoutes() {
  $("#route-rows").innerHTML = state.routes.map((r, i) => `
    <tr>
      <td>${i + 1}</td>
      <td><select onchange="state.routes[${i}].source = this.value">${portOptions("out", r.source)}</select></td>
      <td><select onchange="state.routes[${i}].target = this.value">${portOptions("in", r.target)}</select></td>
      <td><button class="danger" onclick="state.routes.splice(${i},1);renderRoutes()">删除</button></td>
    </tr>`).join("");
}

function addRoute() {
  state.routes.push({ source: "", target: "" });
  renderRoutes();
}

function renderAll() { renderDevices(); renderPairs(); renderRoutes(); }

/* ---------------- 示例 ---------------- */

async function loadExample(name) {
  const res = await fetch("/api/examples");
  const all = await res.json();
  const cfg = all[name];
  if (!cfg) return;
  state.devices = cfg.devices.map((d) => ({ ...d }));
  state.pairs = cfg.pairs.map((p) => ({ ...p }));
  state.routes = cfg.routes.map((r) => ({ ...r }));
  // 示例可自带走线排版分区配置；否则清空，等规划完成后预填默认单分区
  state.zones = (cfg.layout && Array.isArray(cfg.layout.zones))
    ? cfg.layout.zones.map((z) => ({
        name: z.name || "",
        columns: (z.columns || []).join(", "),
        layers: (z.layers || []).map((l) =>
          (typeof l === "object" && l && l.capacity && l.capacity !== 8)
            ? `${l.id}:${l.capacity}` : `${typeof l === "object" ? l.id : l}`).join(", "),
      }))
    : [];
  // 示例可自带信号名签（审计模块使用）
  loadAuditLabelsFromExample(cfg.labels);
  // 示例可自带串音隔离声明（隔离模块使用）
  loadIsolationFromExample(cfg.isolation, cfg.routes.length);
  renderAll();
  renderZones();
  $("#results").innerHTML = "";
  $("#drill-card").hidden = true;
  $("#drill-results").innerHTML = "";
  $("#layout-card").hidden = true;
  $("#layout-results").innerHTML = "";
  $("#audit-card").hidden = true;
  $("#audit-results").innerHTML = "";
  $("#rewire-card").hidden = true;
  $("#rewire-results").innerHTML = "";
  $("#isolation-card").hidden = true;
  $("#isolation-results").innerHTML = "";
  rewireState.targetRoutes = [];
  rewireState.protected = new Set();
  lastRewireData = null;
  lastIsolationData = null;
  lastPlanData = null;
  lastAuditData = null;
}

/* ---------------- 计算与结果展示 ---------------- */

async function compute() {
  const body = {
    devices: state.devices,
    pairs: state.pairs.map((p) => ({
      id: Number(p.id), top: p.top || null, bottom: p.bottom || null, mode: p.mode,
    })),
    routes: state.routes,
  };
  lastConfig = body;
  let data, ok;
  try {
    const res = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
    ok = res.ok;
  } catch (e) {
    data = { error: "无法连接后端：" + e.message };
    ok = false;
  }
  renderResult(data, ok);
  renderDrillPanel(data, ok);
  renderLayoutPanel(data, ok);
  renderAuditPanel(data, ok);
  renderRewirePanel(data, ok);
  renderIsolationPanel(data, ok);
  $("#results").scrollIntoView({ behavior: "smooth" });
}

/* ---------------- 单点故障评估（断路演练） ---------------- */

let lastConfig = null;
let lastFailurePoints = [];

function errorMessage(data) {
  if (!data) return "未知错误";
  if (typeof data.error === "string") return data.error;
  return data.error?.message || "未知错误";
}

function renderDrillPanel(data, httpOk) {
  const card = $("#drill-card");
  if (!httpOk || !data || data.error) {
    card.hidden = true;
    lastFailurePoints = [];
    return;
  }
  lastFailurePoints = data.failurePoints || [];
  const sel = $("#fault-select");
  if (!lastFailurePoints.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  sel.innerHTML = `<option value="">选择要切断的跳线 / 常态连接…</option>` +
    lastFailurePoints.map((fp, i) => {
      const n = fp.affectedCount;
      const tag = n === 0 ? "（无路由使用）" :
        `（影响 ${n} 条路由：${fp.affectedRoutes.map((r) => r + 1).join("、")}）`;
      const kind = fp.type === "cable" ? "跳线" : "常态";
      return `<option value="${i}">[${kind}] ${esc(fp.label)} ${tag}</option>`;
    }).join("");
  $("#drill-hint").textContent = data.ok
    ? `共 ${lastFailurePoints.length} 个可演练断点，已按受影响路由数降序排列。`
    : "当前规划本身存在无解路由，仍可对已安装跳线/生效常态做断路演练。";
  $("#drill-results").innerHTML = "";
}

async function runDrill() {
  const idx = Number($("#fault-select").value);
  const out = $("#drill-results");
  if (!Number.isInteger(idx) || !lastFailurePoints[idx]) {
    out.innerHTML = `<div class="banner warn">请先在下拉框中选择一个断点。</div>`;
    return;
  }
  const fp = lastFailurePoints[idx];
  out.innerHTML = `<div class="banner">⏳ 正在对「${esc(fp.label)}」做断路演练…</div>`;
  let data, status;
  try {
    const res = await fetch("/api/fault-drill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: lastConfig, target: { type: fp.type, id: fp.id } }),
    });
    data = await res.json();
    status = res.status;
  } catch (e) {
    out.innerHTML = `<div class="banner err">无法连接后端：${esc(e.message)}</div>`;
    return;
  }
  if (status === 400) {
    out.innerHTML = `<div class="banner err">非法演练配置：${esc(errorMessage(data))}</div>`;
    return;
  }
  // 200：全部恢复；409：演练完成但存在无替代路径的路由（assessment 携带完整结果）
  const a = data.assessment || data;
  renderDrillResult(a, status === 200, data.error);
}

function portsText(ports) {
  return (ports || []).map((p) => `${esc(p.jack)}（${esc(p.port || "?")}）`).join("、");
}

function renderDrillResult(a, allRestored, error) {
  let html = "";
  if (error && !allRestored) {
    html += `<div class="banner err">❌ ${esc(error.message || "存在无替代路径的路由")}</div>`;
  } else {
    html += `<div class="banner ok">✅ 断路演练完成：受影响的 ${a.affectedCount} 条路由均可用替代路径恢复` +
      (a.newCableCount ? `，需新增 <b>${a.newCableCount}</b> 条跳线。` : "，无需新增跳线。") +
      `</div>`;
  }
  html += `<div class="result-card">
    <h3>断点：${esc(a.target.label)}</h3>
    <div class="drill-meta">
      <span>受影响路由：<b>${a.affectedCount}</b> 条</span>
      <span>未受影响：<b>${a.unaffectedRoutes.length}</b> 条</span>
      <span>新增跳线：<b>${a.newCableCount}</b> 条</span>
    </div>
    <div class="conflict-ports">断点端口：${portsText(a.breakPorts)}</div>`;
  if (a.steps.length) {
    html += `<h3 style="margin-top:12px">替代接线顺序</h3><ol class="steps">` +
      a.steps.map((s) => `<li>${esc(s)}</li>`).join("") + `</ol>`;
  }
  if (a.feedbackLoops.length) {
    html += `<div class="banner warn" style="margin-top:10px">⚠️ 替代方案引入 ${a.feedbackLoops.length} 个反馈环路：<ul class="loop-list">` +
      a.feedbackLoops.map((l) => `<li>${l.map(esc).join(" → ")}</li>`).join("") + `</ul></div>`;
  }
  html += `</div>`;

  html += `<div class="result-card"><h3>受影响路由（按失效优先、编号排序）</h3>`;
  for (const r of a.affectedRoutes) {
    if (r.status === "unroutable") {
      html += `<div class="route-block">
        <div class="route-title">路由 ${r.route + 1}：${esc(r.source)} → ${esc(r.target)}
          <span class="tag bad">失效 · 无替代路径</span></div>
        <ul class="conflict-reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        <div class="conflict-ports">断点端口：${portsText(r.breakPorts)}</div>
      </div>`;
    } else {
      const extra = r.extraCableCount
        ? `替代需新增 ${r.extraCableCount} 条跳线（${r.newCables.map((c) => `#${c}`).join("、")}）`
        : "0 新增跳线";
      html += `<div class="route-block">
        <div class="route-title">路由 ${r.route + 1}：${esc(r.source)} → ${esc(r.target)}
          <span class="tag ok">已恢复 · ${esc(extra)}</span></div>
        ${chainHtml(r.chain)}
      </div>`;
    }
  }
  html += `</div>`;

  html += `<div class="result-card"><h3>未受影响路由（占用状态保持不变）</h3>` +
    (a.unaffectedRoutes.length
      ? `<ul class="notes">` + a.unaffectedRoutes.map((r) =>
          `<li style="color:var(--muted)">路由 ${r.route + 1}：${esc(r.source)} → ${esc(r.target)}` +
          (r.originalCables.length ? `（沿用跳线 ${r.originalCables.map((c) => `#${c}`).join("、")}）` : "（常态/设备内部通路）"))
        .join("") + `</ul>`
      : `<p class="hint">无。</p>`) + `</div>`;

  html += `<div class="result-card"><h3>断路后前面板状态</h3>${panelSvg(a.pairs, a.cables)}</div>`;

  // 单点排序总表（受影响数量排序，供横向比较）
  html += `<div class="result-card"><h3>单点故障排序（按受影响路由数降序）</h3>
    <table class="grid"><thead><tr><th>类型</th><th>断点</th><th>受影响路由数</th><th>受影响路由</th></tr></thead><tbody>` +
    a.failurePoints.map((fp) => `<tr${fp.type === a.target.type && fp.id === a.target.id ?
      ' style="outline:1px solid var(--red)"' : ""}>
      <td>${fp.type === "cable" ? "跳线" : "常态"}</td>
      <td>${esc(fp.label)}</td>
      <td>${fp.affectedCount}</td>
      <td>${fp.affectedRoutes.map((r) => r + 1).join("、") || "—"}</td>
    </tr>`).join("") + `</tbody></table></div>`;

  $("#drill-results").innerHTML = html;
}

function chainHtml(chain) {
  return `<div class="chain">` + chain.map((item) => {
    if (item.kind === "port") {
      return `<div class="node">${esc(item.port)}` +
        (item.jack ? `<span class="jack">${esc(item.jack)}</span>` : "") + `</div>`;
    }
    return `<div class="edge ${item.type}"><span class="edge-label">${esc(item.label)}</span>→</div>`;
  }).join("") + `</div>`;
}

const CABLE_COLORS = ["#4da3ff", "#ff9f43", "#c56cf0", "#3ecf8e", "#ff6b6b", "#ffcf5c"];

function panelSvg(pairs, cables) {
  if (!pairs.length) return "";
  const colW = 96, x0 = 60, topY = 64, botY = 128, height = 300;
  const width = x0 * 2 + colW * (pairs.length - 1);
  const pos = {};
  pairs.forEach((p, i) => {
    pos[p.topJack] = { x: x0 + i * colW, y: topY };
    pos[p.bottomJack] = { x: x0 + i * colW, y: botY };
  });
  const plugged = {};
  cables.forEach((c) => { plugged[c.fromJack] = c.id; plugged[c.toJack] = c.id; });

  let s = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
  s += `<rect x="8" y="30" width="${width - 16}" height="140" rx="10" fill="#12151a" stroke="#2e3542"/>`;
  s += `<text x="20" y="22" fill="#9aa4b2" font-size="12">跳线盘前面板</text>`;

  // 常态连接指示（插孔左侧竖线）
  for (const p of pairs) {
    const t = pos[p.topJack], b = pos[p.bottomJack];
    if (p.mode !== "through" && p.topPort && p.bottomPort) {
      const color = p.normalActive ? "#3ecf8e" : "#5a6270";
      s += `<line x1="${t.x - 18}" y1="${t.y}" x2="${b.x - 18}" y2="${b.y}" stroke="${color}" stroke-width="3"/>`;
    }
  }
  // 跳线（画在面板下方，模拟垂下的线）
  cables.forEach((c, i) => {
    const a = pos[c.fromJack], b = pos[c.toJack];
    const color = CABLE_COLORS[i % CABLE_COLORS.length];
    const depth = 60 + Math.abs(a.x - b.x) * 0.28;
    const mx = (a.x + b.x) / 2, my = botY + depth;
    s += `<path d="M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}" fill="none" stroke="${color}" stroke-width="3.5"/>`;
    const ly = botY + depth * 0.5 - 8;
    s += `<text x="${mx}" y="${ly}" fill="${color}" font-size="12" text-anchor="middle" font-weight="bold">#${c.id}</text>`;
  });
  // 插孔与标签
  for (const p of pairs) {
    for (const [jack, port, dy] of [[p.topJack, p.topPort, -14], [p.bottomJack, p.bottomPort, 24]]) {
      const { x, y } = pos[jack];
      const fill = plugged[jack] ? CABLE_COLORS[(plugged[jack] - 1) % CABLE_COLORS.length] : "#1d222b";
      s += `<circle cx="${x}" cy="${y}" r="9" fill="${fill}" stroke="#9aa4b2" stroke-width="1.5"/>`;
      s += `<text x="${x}" y="${y + dy}" fill="#9aa4b2" font-size="10" text-anchor="middle">${esc(port || "—")}</text>`;
      s += `<text x="${x + 14}" y="${y + 4}" fill="#e6e9ef" font-size="10">${esc(jack)}</text>`;
    }
    s += `<text x="${pos[p.topJack].x}" y="${botY + 40}" fill="#9aa4b2" font-size="10" text-anchor="middle">第${p.id}对 · ${esc(p.modeLabel)}</text>`;
  }
  s += `<text x="20" y="${height - 12}" fill="#9aa4b2" font-size="11">绿线＝生效常态　灰线＝已断开常态　彩色圆点＝已插跳线</text>`;
  s += `</svg>`;
  return s;
}

function renderResult(data, httpOk) {
  const el = $("#results");
  if (!httpOk || data.error) {
    el.innerHTML = `<div class="banner err">配置错误：${esc(errorMessage(data))}</div>`;
    return;
  }
  let html = "";
  if (data.ok) {
    const extra = data.cableCount === 0 ? "全部走常态连接，无需跳线。" : "";
    html += `<div class="banner ok">✅ 规划成功：共需 <b>${data.cableCount}</b> 条跳线。${extra}</div>`;
  } else {
    html += `<div class="banner err">❌ 存在 ${data.conflicts.length} 条无解路由，冲突端口见下方标注。</div>`;
  }
  if (data.feedbackLoops.length) {
    html += `<div class="banner warn">⚠️ 检测到 ${data.feedbackLoops.length} 个反馈环路：<ul class="loop-list">` +
      data.feedbackLoops.map((l) => `<li>${l.map(esc).join(" → ")}</li>`).join("") +
      `</ul></div>`;
  }

  if (data.steps.length) {
    html += `<div class="result-card"><h3>接线顺序</h3><ol class="steps">` +
      data.steps.map((s) => `<li>${esc(s)}</li>`).join("") + `</ol></div>`;
  }
  if (data.notes.length) {
    html += `<div class="result-card"><h3>插头对常态连接的影响</h3><ul class="notes">` +
      data.notes.map((n) => `<li>${esc(n)}</li>`).join("") + `</ul></div>`;
  }

  html += `<div class="result-card"><h3>信号链</h3>`;
  for (const r of data.routes) {
    const tag = r.status === "ok"
      ? `<span class="tag ok">可行${r.cables.length ? ` · ${r.cables.length} 条跳线` : " · 0 跳线"}</span>`
      : `<span class="tag bad">无解</span>`;
    html += `<div class="route-block"><div class="route-title">路由 ${r.index + 1}：${esc(r.source)} → ${esc(r.target)} ${tag}</div>`;
    if (r.status === "ok") {
      html += chainHtml(r.chain);
    } else {
      const c = data.conflicts.find((x) => x.route === r.index);
      html += `<ul class="conflict-reasons">` + c.reasons.map((x) => `<li>${esc(x)}</li>`).join("") + `</ul>`;
      if (c.ports.length) {
        html += `<div class="conflict-ports">冲突端口：` +
          c.ports.map((p) => `${esc(p.jack)}（${esc(p.port || "?")}）`).join("、") + `</div>`;
      }
    }
    html += `</div>`;
  }
  html += `</div>`;

  html += `<div class="result-card"><h3>前面板跳线图</h3>${panelSvg(data.pairs, data.cables)}</div>`;
  el.innerHTML = html;
}

/* ---------------- 前面板走线排版 ---------------- */

let lastLayoutData = null;
let layoutZoom = 1;

function renderZones() {
  $("#zone-rows").innerHTML = state.zones.map((z, i) => `
    <tr>
      <td style="width:130px"><input value="${esc(z.name || "")}" placeholder="如 左区"
           oninput="state.zones[${i}].name = this.value"></td>
      <td><input value="${esc(z.columns || "")}" placeholder="如 1, 2, 3"
           oninput="state.zones[${i}].columns = this.value"></td>
      <td style="width:200px"><input value="${esc(z.layers || "")}" placeholder="如 1, 2 或 1:6, 2:4"
           oninput="state.zones[${i}].layers = this.value"></td>
      <td style="width:64px"><button class="danger" onclick="removeZone(${i})">删除</button></td>
    </tr>`).join("");
}

function addZone() {
  state.zones.push({ name: "", columns: "", layers: "1, 2" });
  renderZones();
}

function removeZone(i) {
  state.zones.splice(i, 1);
  renderZones();
}

function parseZoneSpecs() {
  return state.zones.map((z) => ({
    name: (z.name || "").trim() || "分区",
    columns: String(z.columns || "").split(/[,，\s]+/).filter(Boolean).map(Number),
    layers: String(z.layers || "").split(/[,，\s]+/).filter(Boolean).map((t) => {
      const [id, cap] = t.split(/[:：]/);
      return cap !== undefined && cap !== ""
        ? { id: Number(id), capacity: Number(cap) } : Number(id);
    }),
  }));
}

function renderLayoutPanel(data, httpOk) {
  const card = $("#layout-card");
  if (!httpOk || !data || data.error || !Array.isArray(data.pairs)) {
    card.hidden = true;
    return;
  }
  if (!state.zones.length && data.pairs.length) {
    state.zones = [{
      name: "默认区",
      columns: data.pairs.map((p) => p.id).join(", "),
      layers: "1, 2",
    }];
    renderZones();
  }
  $("#layout-hint").textContent = data.cables.length
    ? `规划共 ${data.cables.length} 根跳线：分区列位固定后，优化器将分配槽层与左右绕行方向。`
    : "当前规划未安装跳线（全部走常态），仍可查看面板排版。";
  card.hidden = false;
}

async function runLayout() {
  const out = $("#layout-results");
  const specs = parseZoneSpecs();
  if (!specs.length) {
    out.innerHTML = `<div class="banner warn">请至少定义一个面板分区。</div>`;
    return;
  }
  for (let i = 0; i < specs.length; i++) {
    if (!specs[i].columns.length || !specs[i].layers.length ||
        specs[i].columns.some((c) => !Number.isInteger(c)) ||
        specs[i].layers.some((l) => !Number.isInteger(l.id))) {
      out.innerHTML = `<div class="banner err">分区「${esc(specs[i].name)}」配置不完整：列位与槽层必须为非空整数列表。</div>`;
      return;
    }
  }
  out.innerHTML = `<div class="banner">⏳ 正在求解走线排版（交叉数 → 跨区线数 → 同层长度）…</div>`;
  let data, status;
  try {
    const res = await fetch("/api/panel-layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: lastConfig, layout: { zones: specs } }),
    });
    data = await res.json();
    status = res.status;
  } catch (e) {
    out.innerHTML = `<div class="banner err">无法连接后端：${esc(e.message)}</div>`;
    return;
  }
  if (status === 400) {
    out.innerHTML = `<div class="banner err">排版配置错误：${esc(errorMessage(data))}</div>`;
    return;
  }
  lastLayoutData = data;
  layoutZoom = 1;
  renderLayoutResult(data);
}

function metricRows(m) {
  return [
    ["交叉数（同层逆序对）", "crossings"],
    ["跨区线数", "crossZone"],
    ["同层总长度（列距）", "length"],
    ["方向对撞", "gapConflicts"],
    ["未分配跳线", "unassigned"],
  ].map(([label, key]) => {
    const b = m.baseline[key], o = m.optimized[key];
    let tag = "";
    if (key !== "crossZone" && b !== o) {
      tag = o < b
        ? ` <span class="tag ok">↓ ${Math.abs(b - o)}</span>`
        : ` <span class="tag warn">↑ ${Math.abs(b - o)}</span>`;
    }
    return `<tr><td>${label}</td><td>${b}</td><td>${o}${tag}</td></tr>`;
  }).join("");
}

function renderLayoutResult(data) {
  let html = "";
  if (!data.planOk) {
    html += `<div class="banner warn">注意：原规划本身存在无解路由；排版仅覆盖已成功安装的 ${data.cables.length} 根跳线。</div>`;
  }
  if (data.ok) {
    html += `<div class="banner ok">✅ 排版完成：无交叉、无冲突。优化顺序：${
      data.metrics.objective.map(esc).join(" → ")}</div>`;
  } else {
    const n = data.conflicts.length;
    html += `<div class="banner err">❌ 排版完成，但存在 <b>${n}</b> 个无法避免的冲突（无公共槽层 / 容量受限导致的残留交叉）。</div>`;
  }
  if (data.solverNote) {
    html += `<div class="banner warn">⚠️ ${esc(data.solverNote)}</div>`;
  }

  html += `<div class="result-card"><h3>优化前后指标</h3>
    <table class="grid metrics"><thead><tr><th>指标</th><th>优化前（贪心基线）</th><th>优化后</th></tr></thead>
    <tbody>${metricRows(data.metrics)}</tbody></table>
    <p class="hint" style="margin-top:8px">说明：列位由分区固定后“跨区线数”为定值，仅作为交叉数并列时的次序参考；
    长度＝水平跨度＋槽层深度＋反向绕行附加（0.5 列距）。</p></div>`;

  html += `<div class="result-card"><h3>每根跳线的走线方案</h3>
    <table class="grid metrics"><thead><tr>
      <th>跳线</th><th>起点（插孔 · 列 · 区）</th><th>终点（插孔 · 列 · 区）</th>
      <th>走线层</th><th>方向</th><th>长度</th><th>跨区</th><th>状态</th>
    </tr></thead><tbody>` +
    data.cables.map((c) => `<tr${c.status !== "routed" ? ' class="row-bad"' : ""}>
      <td><b>#${c.id}</b></td>
      <td>${esc(c.fromJack)}（${esc(c.fromPort)}）<br><span class="hint">列 ${c.fromCol} · ${esc(c.fromZone)}</span></td>
      <td>${esc(c.toJack)}（${esc(c.toPort)}）<br><span class="hint">列 ${c.toCol} · ${esc(c.toZone)}</span></td>
      <td>${c.layer ? `槽层 ${c.layer}` : "—"}</td>
      <td>${esc(c.directionLabel || "—")}</td>
      <td>${c.length ?? "—"}</td>
      <td>${c.crossZone ? "是" : "否"}</td>
      <td>${c.status === "routed"
        ? '<span class="tag ok">已分配</span>'
        : `<span class="tag bad">未分配</span><div class="hint">${esc(c.reason || "")}</div>`}</td>
    </tr>`).join("") + `</tbody></table></div>`;

  if (data.conflicts.length) {
    html += `<div class="result-card"><h3>交叉对与无法避免的冲突</h3><ul class="notes">` +
      data.conflicts.map((cf) => `<li>
        <span class="tag ${cf.type === "crossing" && cf.unavoidable ? "bad" : "warn"}">
          ${cf.type === "crossing" ? "交叉" : "未分配"}</span> ${esc(cf.reason)}
        ${cf.type === "crossing" ? `（跳线 ${cf.cables.map((c) => `#${c}`).join(" ↔ ")}，槽层 ${cf.layer}）` : ""}
      </li>`).join("") + `</ul></div>`;
  }

  html += `<div class="result-card"><h3>可缩放面板走线图</h3>
    <div class="zoom-controls">
      <button onclick="zoomLayout(1.25)">＋ 放大</button>
      <button onclick="zoomLayout(0.8)">－ 缩小</button>
      <button onclick="zoomLayout(0)">100% 复位</button>
      <span class="hint" id="zoom-label">缩放 100%</span>
    </div>
    <div class="svg-wrap" id="layout-svg-wrap">${layoutSvg(data)}</div>
    <p class="hint" style="margin-top:8px">
      彩色水平线＝各分区走线槽层（标注 已用/容量）；跳线从上行 T 插孔垂入槽层，
      水平走向另一端后升至下行 B 插孔；起点小横段指示左/右绕行进槽方向；
      <span style="color:var(--red)">红色 ✕</span>＝无法避免的同层交叉；
      <span style="color:var(--red)">红色虚线</span>＝未分配走线层的跳线。</p>
  </div>`;

  $("#layout-results").innerHTML = html;
}

function zoomLayout(factor) {
  const svg = $("#layout-svg");
  if (!svg) return;
  layoutZoom = factor === 0 ? 1 : Math.min(6, Math.max(0.35, layoutZoom * factor));
  svg.setAttribute("width", Math.round(svg.dataset.w * layoutZoom));
  svg.setAttribute("height", Math.round(svg.dataset.h * layoutZoom));
  $("#zoom-label").textContent = `缩放 ${Math.round(layoutZoom * 100)}%`;
}

const LAYER_COLORS = { 1: "#4da3ff", 2: "#3ecf8e", 3: "#ffcf5c", 4: "#c56cf0", 5: "#ff9f43" };
const layerColor = (id) => LAYER_COLORS[id] || "#8d96a3";

function layoutSvg(data) {
  const jacks = data.jacks;
  if (!jacks.length) return "";
  const colW = 96, x0 = 64, topY = 64, botY = 128;
  const ductY0 = 200, ductGap = 26;
  const nCol = Math.max(...jacks.map((j) => j.col));
  const width = x0 * 2 + colW * Math.max(0, nCol - 1);
  const x = (col) => x0 + (col - 1) * colW;

  // 槽层深度：按分区出现顺序的首次出现序（与后端一致）
  const depth = {};
  for (const z of data.zones)
    for (const l of z.layers) if (!(l.id in depth)) depth[l.id] = Object.keys(depth).length + 1;
  const maxDepth = Math.max(1, ...Object.values(depth));
  const height = ductY0 + (maxDepth - 1) * ductGap + 70;

  // 各跳线颜色
  const cableColor = (id) => CABLE_COLORS[(id - 1) % CABLE_COLORS.length];
  const cableById = Object.fromEntries(data.cables.map((c) => [c.id, c]));

  let s = `<svg id="layout-svg" data-w="${width}" data-h="${height}"
    viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;

  // 面板与插孔
  s += `<rect x="8" y="30" width="${width - 16}" height="140" rx="10" fill="#12151a" stroke="#2e3542"/>`;
  s += `<text x="20" y="22" fill="#9aa4b2" font-size="12">跳线盘前面板（按分区列位）</text>`;

  // 分区分隔线与名称
  for (let i = 0; i < data.zones.length; i++) {
    const z = data.zones[i];
    const cx = (x(z.range[0]) + x(z.range[1])) / 2;
    s += `<text x="${cx}" y="48" fill="${layerColor(i + 1)}" font-size="12"
         text-anchor="middle" font-weight="bold">${esc(z.name)}</text>`;
    if (i > 0) {
      const bx = (x(data.zones[i - 1].range[1]) + x(z.range[0])) / 2;
      s += `<line x1="${bx}" y1="34" x2="${bx}" y2="${height - 26}"
           stroke="#5a6270" stroke-width="1.5" stroke-dasharray="5,5"/>`;
    }
  }

  const pos = {};
  for (const j of jacks) {
    pos[j.topJack] = { x: x(j.col), y: topY, col: j.col };
    pos[j.bottomJack] = { x: x(j.col), y: botY, col: j.col };
  }

  // 常态连接指示
  for (const j of jacks) {
    if (j.modeLabel !== "直通" && j.topPort && j.bottomPort) {
      s += `<line x1="${x(j.col) - 18}" y1="${topY}" x2="${x(j.col) - 18}" y2="${botY}"
           stroke="${j.normalActive ? "#3ecf8e" : "#5a6270"}" stroke-width="3"/>`;
    }
  }

  // 走线槽层（每分区每槽一段）
  for (const z of data.zones) {
    for (const l of z.layers) {
      const y = ductY0 + (l.depth - 1) * ductGap;
      const x1 = x(z.range[0]) - 30, x2 = x(z.range[1]) + 30;
      const col = layerColor(l.id);
      s += `<line x1="${x1}" y1="${y}" x2="${x2}" y2="${y}" stroke="${col}"
           stroke-width="5" stroke-linecap="round" opacity="0.55"/>`;
      s += `<text x="${x1 + 4}" y="${y - 8}" fill="${col}" font-size="10">L${l.id} ${l.used}/${l.capacity}</text>`;
    }
  }
  s += `<text x="14" y="${ductY0 - 14}" fill="#9aa4b2" font-size="11">走线槽层（前→后）</text>`;

  // 未分配跳线：红色虚弧线直连
  for (const c of data.cables.filter((c) => c.status !== "routed")) {
    const a = pos[c.fromJack], b = pos[c.toJack];
    s += `<path d="M ${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${botY + 52 + Math.abs(a.x - b.x) * 0.25}
         ${b.x} ${b.y}" fill="none" stroke="#ff6b6b" stroke-width="2.5" stroke-dasharray="6,4"/>`;
    s += `<text x="${(a.x + b.x) / 2}" y="${botY + 30}" fill="#ff6b6b" font-size="11"
         text-anchor="middle">⚠ #${c.id} 未分配</text>`;
  }

  // 已分配跳线：垂入槽层 → 水平（含方向小横段）→ 升起
  for (const c of data.cables.filter((c) => c.status === "routed")) {
    const a = pos[c.fromJack], b = pos[c.toJack];
    const y = ductY0 + (depth[c.layer] - 1) * ductGap;
    const col = cableColor(c.id);
    const j1 = c.direction === "L" ? -11 : 11;   // T 端进槽小横段
    const j2 = c.direction === "L" ? 11 : -11;   // B 端升起小横段
    let d = `M ${a.x} ${a.y + 9} V ${y} `;
    d += `L ${a.x + j1} ${y} L ${b.x + j2} ${y} L ${b.x} ${y} V ${b.y - 9}`;
    s += `<path d="${d}" fill="none" stroke="${col}" stroke-width="3" stroke-linejoin="round"/>`;
    s += `<text x="${(a.x + b.x) / 2}" y="${y - 7}" fill="${col}" font-size="11"
         text-anchor="middle" font-weight="bold">#${c.id} · L${c.layer} · ${esc(c.directionLabel || "")}</text>`;
  }

  // 交叉标记
  for (const cp of data.crossingPairs) {
    const [i, j] = cp.cables.map((id) => cableById[id]);
    const y = ductY0 + (depth[cp.layer] - 1) * ductGap;
    const cx = (pos[i.fromJack].x + pos[i.toJack].x
                + pos[j.fromJack].x + pos[j.toJack].x) / 4;
    s += `<g><circle cx="${cx}" cy="${y}" r="9" fill="rgba(255,107,107,.25)"
         stroke="#ff6b6b" stroke-width="2"/>
         <line x1="${cx - 5}" y1="${y - 5}" x2="${cx + 5}" y2="${y + 5}" stroke="#ff6b6b" stroke-width="2"/>
         <line x1="${cx - 5}" y1="${y + 5}" x2="${cx + 5}" y2="${y - 5}" stroke="#ff6b6b" stroke-width="2"/></g>`;
  }

  // 插孔与标签
  for (const j of jacks) {
    for (const [jack, port, dy] of [
      [j.topJack, j.topPort, -13], [j.bottomJack, j.bottomPort, 24]]) {
      const { x: xx, y: yy } = pos[jack];
      const owner = data.cables.find((c) => c.fromJack === jack || c.toJack === jack);
      const fill = owner && owner.status === "routed" ? cableColor(owner.id) : "#1d222b";
      s += `<circle cx="${xx}" cy="${yy}" r="9" fill="${fill}" stroke="#9aa4b2" stroke-width="1.5"/>`;
      s += `<text x="${xx}" y="${yy + dy}" fill="#9aa4b2" font-size="10" text-anchor="middle">${esc(port || "—")}</text>`;
      s += `<text x="${xx + 14}" y="${yy + 4}" fill="#e6e9ef" font-size="10">${esc(jack)}</text>`;
    }
    s += `<text x="${x(j.col)}" y="${botY + 40}" fill="#9aa4b2" font-size="10" text-anchor="middle">第${j.pair}对</text>`;
  }
  s += `</svg>`;
  return s;
}

/* ---------------- 信号命名一致性审计 ---------------- */

function loadAuditLabelsFromExample(labels) {
  auditLabels.ports = {}; auditLabels.pairs = {};
  auditLabels.cables = {}; auditLabels.routes = {};
  if (!labels || typeof labels !== "object") return;
  for (const row of labels.ports || []) {
    if (row && row.port) {
      auditLabels.ports[row.port] = { name: row.name || "", alias: row.alias || "" };
    }
  }
  for (const row of labels.pairs || []) {
    if (row && row.id != null) {
      auditLabels.pairs[row.id] = { name: row.name || "", alias: row.alias || "" };
    }
  }
  for (const row of labels.cables || []) {
    if (row && row.id != null) {
      auditLabels.cables[row.id] = { name: row.name || "", alias: row.alias || "" };
    }
  }
  for (const row of labels.routes || []) {
    if (row && row.index != null) {
      auditLabels.routes[row.index] = { name: row.name || "", alias: row.alias || "" };
    }
  }
}

function switchAuditTab(tab) {
  for (const btn of document.querySelectorAll(".audit-tab")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  for (const pane of document.querySelectorAll(".audit-tabpane")) {
    pane.hidden = pane.dataset.pane !== tab;
  }
}

function labelInput(kind, key, field, value) {
  const map = auditLabels[kind];
  const cur = map[key] || { name: "", alias: "" };
  cur[field] = value;
  if (cur.name || cur.alias) map[key] = cur;
  else delete map[key];
}

function rowHtml(kind, key, label, wide) {
  const v = auditLabels[kind][key] || { name: "", alias: "" };
  return `<tr>
    <td class="hint"${wide ? ` style="width:${wide}"` : ""}>${esc(label)}</td>
    <td><input class="route-name-input" placeholder="规范信号名，如 VOX"
         value="${esc(v.name)}" oninput="labelInput('${kind}', ${JSON.stringify(key)}, 'name', this.value)"></td>
    <td><input placeholder="别名，如 人声" value="${esc(v.alias || "")}"
         oninput="labelInput('${kind}', ${JSON.stringify(key)}, 'alias', this.value)"></td>
  </tr>`;
}

function renderAuditPanel(data, httpOk) {
  const card = $("#audit-card");
  if (!httpOk || !data || data.error || !Array.isArray(data.pairs)) {
    card.hidden = true;
    lastPlanData = null;
    return;
  }
  lastPlanData = data;

  // 端口名签表（全部已定义端口）
  const portRows = [];
  for (const d of state.devices) {
    if (!d.name) continue;
    for (const p of d.inputs || []) portRows.push([`${d.name}.${p}`, `输入 · ${d.name}.${p}`]);
    for (const p of d.outputs || []) portRows.push([`${d.name}.${p}`, `输出 · ${d.name}.${p}`]);
  }
  $("#audit-port-rows").innerHTML = portRows.length
    ? portRows.map(([k, label]) => rowHtml("ports", k, label, "200px")).join("")
    : `<tr><td colspan="3" class="hint">暂无端口。</td></tr>`;

  // 插孔对名签表
  $("#audit-pair-rows").innerHTML = data.pairs.map((p) => {
    const label = `第 ${p.id} 对（${p.modeLabel}）：${esc(p.topPort || "—")} ↔ ${esc(p.bottomPort || "—")}`
      + (p.normalActive ? " · 常态生效" : "");
    return rowHtml("pairs", p.id, label, "320px");
  }).join("");

  // 已安装跳线名签表
  $("#audit-cable-rows").innerHTML = data.cables.length
    ? data.cables.map((c) => rowHtml("cables", c.id,
        `跳线 #${c.id}：${c.fromJack}（${c.fromPort}） ↔ ${c.toJack}（${c.toPort}）`,
        "340px")).join("")
    : `<tr><td colspan="3" class="hint">当前规划未安装跳线（全部走常态/设备内部）。</td></tr>`;

  // 路由名签表（/api/plan 的 routes 字段：index/source/target/status）
  $("#audit-route-rows").innerHTML = data.routes.map((r) => {
    const key = r.index;
    const v = auditLabels.routes[key] || { name: "", alias: "" };
    const bad = r.status === "conflict";
    return `<tr${bad ? ' class="row-bad"' : ""}>
      <td>${key + 1}${bad ? ' <span class="tag bad">无解</span>' : ""}</td>
      <td class="hint">${esc(r.source)} → ${esc(r.target)}</td>
      <td><input class="route-name-input" placeholder="该路由携带的信号名"
           value="${esc(v.name)}" oninput="labelInput('routes', ${key}, 'name', this.value)"></td>
      <td><input placeholder="别名（选填）" value="${esc(v.alias || "")}"
           oninput="labelInput('routes', ${key}, 'alias', this.value)"></td>
    </tr>`;
  }).join("");

  $("#audit-hint").textContent = data.cables.length
    ? `规划共 ${data.cables.length} 根跳线、${data.pairs.length} 对插孔：填写名签后运行审计。`
    : "当前规划全部走常态：仍可在端口 / 插孔对 / 路由上命名并审计。";
  card.hidden = false;
  lastAuditData = null;
  $("#audit-results").innerHTML = "";
}

function collectAuditLabels() {
  const pick = (map) => Object.entries(map)
    .filter(([, v]) => (v.name || "").trim() || (v.alias || "").trim())
    .map(([k, v]) => [k, { name: (v.name || "").trim(), alias: (v.alias || "").trim() }]);
  return {
    ports: pick(auditLabels.ports).map(([port, v]) => ({ port, ...v })),
    pairs: pick(auditLabels.pairs).map(([id, v]) => ({ id: Number(id), ...v })),
    cables: pick(auditLabels.cables).map(([id, v]) => ({ id: Number(id), ...v })),
    routes: pick(auditLabels.routes).map(([index, v]) => ({ index: Number(index), ...v })),
  };
}

async function runAudit() {
  const out = $("#audit-results");
  if (!lastConfig) {
    out.innerHTML = `<div class="banner warn">请先计算一次路由方案。</div>`;
    return;
  }
  out.innerHTML = `<div class="banner">⏳ 正在沿设备内部 / 生效常态 / 跳线传播名签并归并端点…</div>`;
  let data, status;
  try {
    const res = await fetch("/api/signal-audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: lastConfig, labels: collectAuditLabels() }),
    });
    data = await res.json();
    status = res.status;
  } catch (e) {
    out.innerHTML = `<div class="banner err">无法连接后端：${esc(e.message)}</div>`;
    return;
  }
  if (status !== 200) {
    out.innerHTML = `<div class="banner err">审计配置错误：${esc(errorMessage(data))}</div>`;
    return;
  }
  lastAuditData = data;
  activeIssueId = null;
  renderAuditResult(data);
}

function clearAuditLabels() {
  auditLabels.ports = {}; auditLabels.pairs = {};
  auditLabels.cables = {}; auditLabels.routes = {};
  if (lastPlanData) renderAuditPanel(lastPlanData, true);
  $("#audit-results").innerHTML = "";
}

const ISSUE_TYPE_LABEL = {
  mutex: "互斥名称",
  "alias-ring": "别名环",
  unlabeled: "无标签断点",
  crosstalk: "跨路由串线",
};

function renderAuditResult(data) {
  const c = data.summary.issueCounts;
  let html = "";
  if (!data.issueTotal) {
    html += `<div class="banner ok">✅ 命名一致性审计通过：${data.summary.activeComponents} 个承载路由的连通分量上，
      所有规范名互不冲突、别名无环、信号断点均有名签、未发现跨路由串线。审计未改动任何路由。</div>`;
  } else {
    const errN = c.mutex + c["alias-ring"] + c.crosstalk;
    html += `<div class="banner ${errN ? "err" : "warn"}">${errN
      ? `❌ 发现 <b>${errN}</b> 个命名一致性错误`
      : "⚠️ 未发现硬冲突"}，另有 <b>${c.unlabeled}</b> 个无标签断点提醒。
      点击任一问题可在下方拓扑图上高亮其最短证据路径。审计不修改路由。</div>`;
    if (c.crosstalk) {
      html += `<div class="banner warn">⚠️ <b>${c.crosstalk}</b> 处跨路由串线只由路由规划与物理连通决定，
        与规范名/别名无关：改名不能消警，需调整路由或断开独立源之间的连通后重新规划；复审时同一物理证据路径仍会高亮。</div>`;
    }
  }

  html += `<div class="audit-stat-row">
    <span class="audit-stat"><b>${data.summary.components}</b>连通分量</span>
    <span class="audit-stat ${c.mutex ? "err" : "ok"}"><b>${c.mutex}</b>互斥名称</span>
    <span class="audit-stat ${c["alias-ring"] ? "err" : "ok"}"><b>${c["alias-ring"]}</b>别名环</span>
    <span class="audit-stat ${c.crosstalk ? "err" : "ok"}"><b>${c.crosstalk}</b>跨路由串线</span>
    <span class="audit-stat ${c.unlabeled ? "warn" : "ok"}"><b>${c.unlabeled}</b>无标签断点</span>
    <span class="audit-stat"><b>${data.summary.labeledPorts}</b>已命名端口</span>
  </div>`;

  if (data.issues.length) {
    html += `<div class="result-card"><h3>问题清单（含最短证据路径与修正预览）</h3>`;
    for (const iss of data.issues) {
      html += issueCardHtml(iss);
    }
    if (data.summary.truncated) {
      html += `<div class="banner warn" style="margin-top:10px">问题总数 ${data.summary.issueTotal} 条，
        仅展示前 ${data.summary.issueShown} 条；请先采纳部分修正以消歧后再审计。</div>`;
    }
    html += `</div>`;
  }

  html += `<div class="result-card"><h3>审计拓扑（按连通分量着色，点击问题高亮证据路径）</h3>
    <div class="audit-svg-wrap" id="audit-svg-wrap">${auditTopologySvg(data)}</div>
    <div class="legend">
      不同底色＝不同连通分量（只有承载路由的分量才参与串线判定）；
      <span class="swatch" style="background:rgba(255,107,107,.35);border-color:#ff6b6b"></span>问题端点
      <span class="swatch" style="background:rgba(255,207,92,.35);border-color:#ffcf5c"></span>证据路径（粗亮线）
      <span class="swatch" style="background:#ffcf5c;border-color:#5a4a10"></span>上插孔“并”＝半常态插线并送分流点；
      面板上方<b style="color:#ffcf5c">黄色虚线弧</b>＝证据经过的设备内部通路
    </div></div>`;

  $("#audit-results").innerHTML = html;
}

function issueCardHtml(iss) {
  const evPorts = iss.evidence.filter((x) => x.kind === "port").map((x) => x.port);
  const fixes = (iss.fixes || []).map((fx, i) => {
    let change = "";
    if (fx.action === "rename") {
      change = `<span class="fix-from">${esc(fx.fromName)}</span> → 规范名 <span class="fix-to">${esc(fx.toName)}</span>，原名保留为别名`;
    } else if (fx.action === "add-label") {
      change = `补填规范名 <span class="fix-to">${esc(fx.name || "（需人工指定）")}</span>`;
    } else if (fx.action === "break-alias") {
      change = `删除别名 <span class="fix-from">${esc(fx.removedAlias)}</span> 以剪断环边，规范名统一为 <span class="fix-to">${esc(fx.name)}</span>`;
    }
    return `<li>${esc(fx.targetLabel)}：${change}
      <button onclick="applyAuditFix(${iss.id}, ${i}); event.stopPropagation()">采纳</button></li>`;
  }).join("");
  // 跨路由串线不提供可消警的改名操作：只能调整路由规划或物理连通
  const fixBlock = fixes
    ? `<ul class="fix-list">${fixes}</ul>`
    : (iss.type === "crosstalk"
      ? `<div class="issue-sub">⚠ ${esc(iss.resolution ||
          "跨路由串线不提供可消警的改名操作：只能调整路由规划或断开独立源之间的物理连通后重新规划。")}</div>`
      : "");
  const tagLabel = iss.subtype === "cycle" ? "别名环"
    : iss.subtype === "diverging" ? "别名分叉" : ISSUE_TYPE_LABEL[iss.type];
  return `<div class="issue-card severity-${iss.severity}" id="issue-card-${iss.id}"
       onclick="highlightIssue(${iss.id})">
    <div class="issue-head">
      <span class="issue-type ${iss.type}">${esc(tagLabel)}</span>
      <b>问题 #${iss.id + 1}</b>
      ${iss.suggestedName ? `<span class="hint">建议规范名：<b style="color:var(--green)">${esc(iss.suggestedName)}</b></span>` : ""}
      ${iss.component != null ? `<span class="hint">连通分量 #${iss.component + 1}</span>` : ""}
    </div>
    <div class="issue-msg">${esc(iss.message)}</div>
    ${fixBlock}
    <span class="issue-evidence-toggle">▸ 最短证据路径（${evPorts.length} 个端点）</span>
    <div class="evidence-box" hidden>${chainHtml(iss.evidence)}</div>
  </div>`;
}

function highlightIssue(id) {
  if (!lastAuditData) return;
  const iss = lastAuditData.issues.find((x) => x.id === id);
  if (!iss) return;
  activeIssueId = activeIssueId === id ? null : id;
  for (const card of document.querySelectorAll(".issue-card")) {
    card.classList.toggle("active", Number(card.id.split("-")[2]) === activeIssueId);
  }
  for (const card of document.querySelectorAll(".issue-card")) {
    const cid = Number(card.id.split("-")[2]);
    const box = card.querySelector(".evidence-box");
    const toggle = card.querySelector(".issue-evidence-toggle");
    if (box) {
      box.hidden = cid !== activeIssueId;
      if (toggle) toggle.textContent = (cid === activeIssueId ? "▾ " : "▸ ") +
        `最短证据路径（${lastAuditData.issues[cid].evidence.filter((x) => x.kind === "port").length} 个端点）`;
    }
  }
  const wrap = $("#audit-svg-wrap");
  if (wrap) wrap.innerHTML = auditTopologySvg(lastAuditData);
  if (activeIssueId != null) {
    const el = document.querySelector("#audit-svg .evidence-node");
    if (el && wrap) wrap.scrollLeft = Math.max(0, Number(el.getAttribute("data-cx") || 0) - wrap.clientWidth / 2);
  }
}

function applyAuditFix(issueId, fixIdx) {
  if (!lastAuditData) return;
  const iss = lastAuditData.issues.find((x) => x.id === issueId);
  const fx = iss && iss.fixes[fixIdx];
  if (!fx) return;
  const t = fx.target;
  const kindMap = { port: "ports", pair: "pairs", cable: "cables", route: "routes" };
  const kind = kindMap[t.kind];
  const key = t.kind === "port" ? t.port
    : t.kind === "route" ? t.index : t.id;
  const prev = auditLabels[kind][key] || { name: "", alias: "" };
  if (fx.action === "rename") {
    auditLabels[kind][key] = {
      name: fx.toName,
      alias: fx.alias || prev.alias || fx.fromName || "",
    };
  } else if (fx.action === "add-label") {
    auditLabels[kind][key] = { name: fx.name || "", alias: prev.alias || "" };
    if (!fx.name) {
      // 无唯一建议名：定位到输入框让用户手填
      renderAuditPanel(lastPlanData, true);
      switchAuditTab(kind);
      const pane = document.querySelector(`.audit-tabpane[data-pane="${kind}"]`);
      if (pane) pane.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
  } else if (fx.action === "break-alias") {
    auditLabels[kind][key] = {
      name: fx.name || prev.name || "",
      alias: fx.alias || "",           // null = 删除别名
    };
  }
  renderAuditPanel(lastPlanData, true);
  runAudit();
}

/* ---- 审计拓扑 SVG ---- */

const COMP_COLORS = ["#1c3a5e", "#3a2a55", "#1f4a3c", "#5a3a1e",
  "#4a2440", "#1e4450", "#4a3a24", "#2c3e50"];

function auditTopologySvg(data) {
  const pairs = data.pairs;
  if (!pairs.length) return "";
  const cables = data.cables;
  const colW = 104, x0 = 64, topY = 70, botY = 140, height = 330;
  const width = x0 * 2 + colW * (pairs.length - 1);
  const pos = {};
  pairs.forEach((p, i) => {
    pos[p.topJack] = { x: x0 + i * colW, y: topY, pair: p };
    pos[p.bottomJack] = { x: x0 + i * colW, y: botY, pair: p };
  });

  // 端口 → 连通分量
  const compOfPort = {};
  data.components.forEach((comp) => {
    for (const pp of comp.ports) compOfPort[pp.port] = comp;
  });
  const colorOfPort = (port) => {
    const comp = compOfPort[port];
    if (!comp) return "#12151a";
    return COMP_COLORS[comp.id % COMP_COLORS.length];
  };

  const iss = activeIssueId != null ? data.issues[activeIssueId] : null;
  const evidencePorts = new Set();
  const evidenceEdges = new Set();
  if (iss) {
    for (let i = 0; i < iss.evidence.length; i++) {
      const it = iss.evidence[i];
      if (it.kind === "port") evidencePorts.add(it.port);
      if (it.kind === "edge") {
        const a = iss.evidence[i - 1], b = iss.evidence[i + 1];
        if (a && b && a.kind === "port" && b.kind === "port") {
          evidenceEdges.add(`${it.type}:${it.cableId || it.pairId || it.device || ""}:${a.port}|${b.port}`);
        }
      }
    }
  }
  const hitPorts = new Set();
  if (iss) {
    // 问题的直接端点：证据首尾 + 涉及名签所在端口
    const portsInEv = iss.evidence.filter((x) => x.kind === "port");
    if (portsInEv.length) {
      hitPorts.add(portsInEv[0].port);
      hitPorts.add(portsInEv[portsInEv.length - 1].port);
    }
    if (iss.fixes) {
      for (const fx of iss.fixes) {
        if (fx.target.kind === "port") hitPorts.add(fx.target.port);
      }
    }
  }

  let s = `<svg id="audit-svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
  s += `<rect x="8" y="32" width="${width - 16}" height="150" rx="10" fill="#12151a" stroke="#2e3542"/>`;
  s += `<text x="20" y="24" fill="#9aa4b2" font-size="12">信号命名审计拓扑 · ${data.components.length} 个连通分量（底色区分）</text>`;

  // 常态连接
  for (const p of pairs) {
    if (p.mode === "through" || !p.topPort || !p.bottomPort) continue;
    const t = pos[p.topJack], b = pos[p.bottomJack];
    const active = p.normalActive;
    const edgeKey = `normal:${p.id}:${p.topPort}|${p.bottomPort}`;
    const edgeKeyR = `normal:${p.id}:${p.bottomPort}|${p.topPort}`;
    const onEv = iss && (evidenceEdges.has(edgeKey) || evidenceEdges.has(edgeKeyR));
    let col = active ? "#3ecf8e" : "#5a6270";
    let w = 3, dash = "";
    if (onEv) { col = "#ffcf5c"; w = 6; }
    if (p.modeLabel === "半常态") dash = ' stroke-dasharray="2,0"';
    s += `<line x1="${t.x - 18}" y1="${t.y}" x2="${b.x - 18}" y2="${b.y}"
         stroke="${col}" stroke-width="${w}"${dash} opacity="${active ? 0.9 : 0.5}"/>`;
  }

  // 跳线（弧线下垂）
  const pluggedCable = {};
  cables.forEach((c, i) => {
    pluggedCable[c.fromJack] = c; pluggedCable[c.toJack] = c;
    const a = pos[c.fromJack], b = pos[c.toJack];
    const edgeKeyA = `cable:${c.id}:${c.fromPort}|${c.toPort}`;
    const edgeKeyB = `cable:${c.id}:${c.toPort}|${c.fromPort}`;
    const onEv = iss && (evidenceEdges.has(edgeKeyA) || evidenceEdges.has(edgeKeyB));
    const color = onEv ? "#ffcf5c" : CABLE_COLORS[c.id % CABLE_COLORS.length];
    const depth = 64 + Math.abs(a.x - b.x) * 0.3;
    const mx = (a.x + b.x) / 2, my = botY + depth;
    s += `<path d="M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}" fill="none"
         stroke="${color}" stroke-width="${onEv ? 5.5 : 3}"/>`;
    s += `<text x="${mx}" y="${my * 0.62}" fill="${color}" font-size="12"
         text-anchor="middle" font-weight="bold">#${c.id}</text>`;
  });

  // 设备内部证据边：输入插孔与同设备输出插孔（可能跨列），用黄色虚线在面板上方连接
  if (iss) {
    const jackPosOfPort = {};
    for (const p of pairs) {
      if (p.topPort) jackPosOfPort[p.topPort] = pos[p.topJack];
      if (p.bottomPort) jackPosOfPort[p.bottomPort] = pos[p.bottomJack];
    }
    const drawnDev = new Set();
    for (let i = 0; i < iss.evidence.length; i++) {
      const it = iss.evidence[i];
      if (it.kind !== "edge" || it.type !== "device") continue;
      const a = iss.evidence[i - 1], b = iss.evidence[i + 1];
      if (!a || !b || a.kind !== "port" || b.kind !== "port") continue;
      const pa = jackPosOfPort[a.port], pb = jackPosOfPort[b.port];
      const key = `${it.device}:${[a.port, b.port].sort().join("|")}`;
      if (!pa || !pb || drawnDev.has(key)) continue;
      drawnDev.add(key);
      s += `<path d="M ${pa.x} ${pa.y - 12} Q ${(pa.x + pb.x) / 2} 36 ${pb.x} ${pb.y - 12}"
           fill="none" stroke="#ffcf5c" stroke-width="2.5" stroke-dasharray="6,4" opacity="0.95"/>`;
      s += `<text x="${(pa.x + pb.x) / 2}" y="44" fill="#ffcf5c" font-size="10"
           text-anchor="middle">${esc(it.device)} 内部</text>`;
    }
  }

  // 半常态并送点：上插孔插线且常态保持 → 在上插孔打 “并” 小标
  for (const p of pairs) {
    if (p.modeLabel !== "半常态" || !p.normalActive || !pluggedCable[p.topJack]) continue;
    const t = pos[p.topJack];
    s += `<g>
      <circle cx="${t.x + 13}" cy="${t.y - 12}" r="7" fill="#ffcf5c" stroke="#5a4a10" stroke-width="1"/>
      <text x="${t.x + 13}" y="${t.y - 8}" fill="#2a2100" font-size="9"
        text-anchor="middle" font-weight="bold">并</text>
    </g>`;
  }
  // 插孔
  for (const p of pairs) {
    for (const [jack, port, dy, isTop] of [
      [p.topJack, p.topPort, -15, true], [p.bottomJack, p.bottomPort, 26, false]]) {
      const { x, y } = pos[jack];
      const fill = port ? colorOfPort(port) : "#1d222b";
      const isHit = iss && port && hitPorts.has(port);
      const isEv = iss && port && evidencePorts.has(port);
      s += `<circle class="${isEv ? "evidence-node" : ""}" data-cx="${x}"
           cx="${x}" cy="${y}" r="${isHit ? 12 : 9}"
           fill="${fill}" stroke="${isHit ? "#ff6b6b" : isEv ? "#ffcf5c" : "#9aa4b2"}"
           stroke-width="${isHit ? 3 : 1.5}"/>`;
      s += `<text x="${x}" y="${y + dy}" fill="${isHit ? "#ff9f9f" : "#9aa4b2"}" font-size="10"
           text-anchor="middle">${esc(port || "—")}</text>`;
      s += `<text x="${x + 14}" y="${y + 4}" fill="#e6e9ef" font-size="10">${esc(jack)}</text>`;
    }
    s += `<text x="${pos[p.topJack].x}" y="${botY + 42}" fill="#9aa4b2" font-size="10"
         text-anchor="middle">第${p.id}对 · ${esc(p.modeLabel)}</text>`;
  }

  // 问题信息条
  if (iss) {
    s += `<g transform="translate(16,${height - 40})">
      <rect x="0" y="0" width="${width - 48}" height="28" rx="6" fill="rgba(255,107,107,.12)" stroke="#ff6b6b"/>
      <text x="10" y="19" fill="#ff9f9f" font-size="12">🔎 问题 #${iss.id + 1}（${esc(ISSUE_TYPE_LABEL[iss.type] || iss.type)}）：
        黄色路径为最短证据（${iss.evidence.filter((x) => x.kind === "edge").length} 跳），红色描边端点为冲突/断点</text>
    </g>`;
  }
  s += `</svg>`;
  return s;
}

/* ---------------- 无中断改线演练 ---------------- */

const rewireState = { targetRoutes: [], protected: new Set() };
let lastRewireData = null;

function renderRewirePanel(data, httpOk) {
  const card = $("#rewire-card");
  if (!httpOk || !data || data.error || !Array.isArray(data.pairs)) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  if (!rewireState.targetRoutes.length) {
    rewireState.targetRoutes = state.routes.map((r) => ({ ...r }));
  }
  // 当前路由表变化后，丢弃越界的保护勾选
  for (const i of [...rewireState.protected]) {
    if (i >= state.routes.length) rewireState.protected.delete(i);
  }
  renderRewireTargetRows();
  renderRewireProtectList(data);
  $("#rewire-hint").textContent =
    `当前规划安装 ${data.cables.length} 根跳线：编辑目标路由并勾选保护路由后生成改线方案。`;
}

function renderRewireTargetRows() {
  $("#rewire-target-rows").innerHTML = rewireState.targetRoutes.map((r, i) => `
    <tr>
      <td>${i + 1}</td>
      <td><select onchange="rewireState.targetRoutes[${i}].source = this.value">${portOptions("out", r.source)}</select></td>
      <td><select onchange="rewireState.targetRoutes[${i}].target = this.value">${portOptions("in", r.target)}</select></td>
      <td><button class="danger" onclick="rewireState.targetRoutes.splice(${i},1);renderRewireTargetRows()">删除</button></td>
    </tr>`).join("");
}

function addRewireRoute() {
  rewireState.targetRoutes.push({ source: "", target: "" });
  renderRewireTargetRows();
}

function syncRewireTarget() {
  rewireState.targetRoutes = state.routes.map((r) => ({ ...r }));
  renderRewireTargetRows();
}

function renderRewireProtectList(data) {
  const el = $("#rewire-protect-list");
  if (!state.routes.length) {
    el.innerHTML = `<p class="hint">当前没有路由。</p>`;
    return;
  }
  el.innerHTML = state.routes.map((r, i) => {
    const planned = data.routes[i];
    const bad = !planned || planned.status !== "ok";
    return `<label class="protect-row${bad ? " bad" : ""}">
      <input type="checkbox" ${rewireState.protected.has(i) ? "checked" : ""}
        onchange="toggleRewireProtect(${i}, this.checked)" ${bad ? "disabled" : ""}>
      <span class="protect-no">路由 ${i + 1}</span>
      <span class="hint">${esc(r.source)} → ${esc(r.target)}</span>
      ${bad ? '<span class="tag bad">现状无解，不可保护</span>' : ""}
    </label>`;
  }).join("");
}

function toggleRewireProtect(i, on) {
  if (on) rewireState.protected.add(i);
  else rewireState.protected.delete(i);
}

async function runRewire() {
  const out = $("#rewire-results");
  if (!lastConfig) {
    out.innerHTML = `<div class="banner warn">请先计算一次路由方案。</div>`;
    return;
  }
  const target = rewireState.targetRoutes.map((r) => ({
    source: r.source || null, target: r.target || null,
  }));
  if (!target.length) {
    out.innerHTML = `<div class="banner warn">请至少填写一条目标路由。</div>`;
    return;
  }
  const incomplete = target.findIndex((r) => !r.source || !r.target);
  if (incomplete >= 0) {
    out.innerHTML = `<div class="banner warn">目标路由第 ${incomplete + 1} 条的信号源 / 目标未选择完整。</div>`;
    return;
  }
  out.innerHTML = `<div class="banner">⏳ 正在按现状重建跳线并搜索无中断改线顺序…</div>`;
  let data, status;
  try {
    const res = await fetch("/api/seamless-rewire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: lastConfig,
        targetRoutes: target,
        protectedRoutes: [...rewireState.protected].sort((a, b) => a - b),
      }),
    });
    data = await res.json();
    status = res.status;
  } catch (e) {
    out.innerHTML = `<div class="banner err">无法连接后端：${esc(e.message)}</div>`;
    return;
  }
  lastRewireData = data;
  if (status === 400) {
    renderRewireInvalid(data);
    return;
  }
  if (status === 409) {
    renderRewireConflict(data);
    return;
  }
  renderRewireResult(data);
}

function renderRewireInvalid(data) {
  const err = data.error || {};
  let extra = "";
  const conf = err.details && err.details.conflicts;
  if (Array.isArray(conf) && conf.length) {
    extra = `<div class="result-card"><h3>目标配置中的无解路由（不产生半成品方案）</h3>` +
      conf.map((c) => `<div class="route-block">
        <div class="route-title">目标路由 ${c.route + 1}：${esc(c.source)} → ${esc(c.target)}
          <span class="tag bad">无解</span></div>
        <ul class="conflict-reasons">${(c.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${c.ports && c.ports.length ? `<div class="conflict-ports">冲突端口：` +
          c.ports.map((p) => `${esc(p.jack)}（${esc(p.port || "?")}）`).join("、") + `</div>` : ""}
      </div>`).join("") + `</div>`;
  }
  const silent = err.details && err.details.silentRoutes;
  if (Array.isArray(silent) && silent.length) {
    extra += `<div class="result-card"><h3>目标跳线就位后仍不通声的路由</h3><ul class="notes">` +
      silent.map((r) => `<li>${esc(r.source)} → ${esc(r.target)}</li>`).join("") + `</ul></div>`;
  }
  const d = err.details;
  if (d && (d.index != null || d.source)) {
    extra += `<div class="conflict-ports">${esc(d.source || "")} → ${esc(d.target || "")}</div>`;
  }
  $("#rewire-results").innerHTML =
    `<div class="banner err">❌ ${esc(err.message || "改线请求非法")}</div>` + extra;
}

function renderRewireConflict(data) {
  const err = data.error || {};
  const p = data.assessment || data;
  let html = `<div class="banner err">❌ ${esc(err.message || "不存在能保持保护路由连续通声的改线顺序")}</div>`;
  if (p.solverNote) html += `<div class="banner warn">⚠️ ${esc(p.solverNote)}</div>`;
  const pr = (p.protectedRoutes || []).map((x) =>
    `路由 ${x.index + 1}（${x.source} → ${x.target}）`).join("、") || "（未指定）";
  html += `<div class="result-card"><h3>保护路由</h3><p class="hint">${esc(pr)}</p></div>`;
  const blockers = p.blockers || [];
  if (blockers.length) {
    const title = p.inconclusive ? "搜索中发现的阻塞点" : "结构化阻塞原因";
    html += `<div class="result-card"><h3>${title}</h3><ul class="notes">` +
      blockers.map((b) => `<li><span class="tag ${blockerTagClass(b.type)}">${esc(blockerTypeLabel(b.type))}</span>
        ${esc(b.message)}` + blockerDetail(b) + `</li>`).join("") + `</ul></div>`;
  }
  html += `<div class="result-card"><h3>现状面板（起点）</h3>${panelSvg(p.baseline.pairs, p.baseline.cables)}</div>`;
  if (p.target) {
    html += `<div class="result-card"><h3>目标面板（期望终态）</h3>${panelSvg(p.target.pairs, p.target.cables)}</div>`;
  }
  $("#rewire-results").innerHTML = html;
}

function blockerTypeLabel(t) {
  return ({
    "jack-occupied": "插孔占用",
    "must-remove-cable": "必拆保护跳线",
    "must-break-normal": "必断保护常态",
    "no-bypass-jack": "无旁路插孔",
    "protected-silent-baseline": "保护路由现状失声",
    "goal-not-audible": "目标态不通声",
  })[t] || t;
}
function blockerTagClass(t) {
  return t === "jack-occupied" ? "warn" : "bad";
}
function blockerDetail(b) {
  let parts = [];
  if (b.route != null) parts.push(`路由 ${b.route + 1}`);
  if (b.pair != null) parts.push(`第 ${b.pair} 对`);
  if (b.plugJack) parts.push(`插孔 ${b.plugJack}`);
  if (b.cable) parts.push(`跳线 ${b.cable[0]}→${b.cable[1]}`);
  return parts.length ? `<div class="conflict-ports">${esc(parts.join(" · "))}</div>` : "";
}

const REWIRE_ACTION_META = {
  remove: { cls: "bad", icon: "🔌" },
  complete: { cls: "ok", icon: "🔀" },
  park: { cls: "warn", icon: "⏏️" },
  add: { cls: "ok", icon: "➕" },
  "temp-add": { cls: "warn", icon: "🟡" },
  "temp-remove": { cls: "warn", icon: "🟡" },
};

function renderRewireResult(data) {
  let html = "";
  html += `<div class="banner ok">✅ 无中断改线方案已生成：共 <b>${data.stepCount}</b> 步
    （${data.metrics.plugOps} 个插/拔动作），每个插/拔动作后的中间快照上保护路由均通声。</div>`;
  if (data.solverNote) html += `<div class="banner warn">⚠️ ${esc(data.solverNote)}</div>`;
  if (data.feedbackLoops && data.feedbackLoops.length) {
    html += `<div class="banner warn">⚠️ 目标配置存在 ${data.feedbackLoops.length} 个反馈环路：<ul class="loop-list">` +
      data.feedbackLoops.map((l) => `<li>${l.map(esc).join(" → ")}</li>`).join("") + `</ul></div>`;
  }

  const m = data.metrics;
  html += `<div class="audit-stat-row">
    <span class="audit-stat"><b>${m.plugOps}</b>插/拔动作</span>
    <span class="audit-stat ok"><b>${m.removeCount}</b>拆线</span>
    <span class="audit-stat ok"><b>${m.moveCount}</b>换接${m.parkCount ? `（含 ${m.parkCount} 次让位）` : ""}</span>
    <span class="audit-stat ok"><b>${m.addCount}</b>补线</span>
    <span class="audit-stat ${m.tempCableCount ? "warn" : "ok"}"><b>${m.tempCableCount}</b>临时跳线</span>
    <span class="audit-stat ${m.newCableCount ? "warn" : "ok"}"><b>${m.newCableCount}</b>动用新跳线</span>
    <span class="audit-stat ok"><b>${m.reusedCableCount}</b>复用兼容跳线</span>
  </div>`;

  const pr = data.protectedRoutes.map((x) =>
    `路由 ${x.index + 1}（${x.source} → ${x.target}）`).join("、");
  html += `<div class="result-card"><h3>保护路由（每一个插/拔动作后的中间快照均校验通声）</h3>
    <p class="hint">${esc(pr) || "（未指定保护路由）"}</p></div>`;

  // 复用归类
  const ru = data.reuse;
  const reuseItems = [];
  const pushGroup = (title, arr, tagCls) => arr.forEach((e) => reuseItems.push(
    `<li><span class="tag ${tagCls}">${title}</span> ${esc(e.label)}：${esc(e.fromJack)}（${esc(e.fromPort || "—")}）
     → ${esc(e.toJack)}（${esc(e.toPort || "—")}）` +
    (e.originalFromJack ? ` <span class="hint">（原 ${esc(e.originalFromJack)}→${esc(e.originalToJack)}）</span>` : "") +
    `</li>`));
  pushGroup("保持不动", ru.unchanged, "ok");
  pushGroup("一端换接", ru.moved, "warn");
  pushGroup("拆下复用", ru.recycled, "warn");
  pushGroup("动用新跳线", ru.new, "bad");
  ru.removedOriginal.forEach((e) => reuseItems.push(
    `<li><span class="tag bad">完全拆除</span> ${esc(e.label)}：${esc(e.fromJack)}→${esc(e.toJack)}</li>`));
  html += `<div class="result-card"><h3>跳线复用方案（优先复用兼容跳线）</h3>
    <ul class="notes">${reuseItems.join("") || "<li>无跳线变更。</li>"}</ul></div>`;

  if (data.temporaryCables.length) {
    html += `<div class="result-card"><h3>临时跳线（仅过渡使用，单独列出）</h3><ul class="notes">` +
      data.temporaryCables.map((t) => `<li><span class="tag warn">${esc(t.label)}</span>
        ${esc(t.fromJack)}（${esc(t.fromPort || "—")}） → ${esc(t.toJack)}（${esc(t.toPort || "—")}）
        <span class="hint">第 ${t.addStep} 步接入 · 第 ${t.removeStep ?? "—"} 步拆除</span>` +
        (t.protects.length ? ` <span class="hint">保通：${t.protects.map((r) => `路由 ${r + 1}`).join("、")}</span>` : "") +
        `</li>`).join("") + `</ul></div>`;
  }

  // 逐步操作预览
  html += `<div class="result-card"><h3>逐步操作预览（点击任一步查看该步完成后的面板状态）</h3>
    <div class="rewire-steps" id="rewire-step-list">` +
    data.steps.map((s, i) => rewireStepHtml(s, i, data.steps.length)).join("") + `</div>
    <div id="rewire-step-detail"></div></div>`;

  html += `<div class="result-card"><h3>现状面板（起点）</h3>${panelSvg(data.baseline.pairs, data.baseline.cables)}</div>`;
  html += `<div class="result-card"><h3>目标面板（终点）</h3>${panelSvg(data.target.pairs, data.target.cables)}</div>`;

  $("#rewire-results").innerHTML = html;
  if (data.steps.length) selectRewireStep(0);
}

function rewireStepHtml(s, i, total) {
  const meta = REWIRE_ACTION_META[s.action] || { cls: "ok", icon: "•" };
  const prot = s.protected.length
    ? s.protected.map((p) => `<span class="prot-chip ${p.audible ? "on" : "off"}"
         title="${esc(p.source)} → ${esc(p.target)}${p.viaTemporary.length ? `（经临时跳线 ${p.viaTemporary.map((t) => "T" + t).join("、")}）` : ""}">
         路由${p.routeNo} ${p.audible ? "✓通声" : "✗失声"}</span>`).join("")
    : '<span class="hint">无保护路由</span>';
  return `<button class="rewire-step ${meta.cls}" id="rewire-step-btn-${i}" onclick="selectRewireStep(${i})">
    <span class="step-no">第 ${s.seq}/${total} 步</span>
    <span class="step-icon">${meta.icon}</span>
    <span class="step-title">${esc(s.actionLabel)}</span>
    <span class="step-prot">${prot}</span>
  </button>`;
}

function selectRewireStep(i) {
  const data = lastRewireData;
  if (!data || !data.steps[i]) return;
  document.querySelectorAll(".rewire-step").forEach((b, k) =>
    b.classList.toggle("active", k === i));
  const s = data.steps[i];
  const changes = s.jackChanges.map((c) => `<li>
    <span class="tag ${c.change === "plug" ? "ok" : "bad"}">${c.change === "plug" ? "插入" : "拔下"}</span>
    插孔 <b>${esc(c.jack)}</b>（${esc(c.port || "—")}）</li>`).join("");
  const aff = s.affectedRoutes.length
    ? s.affectedRoutes.map((r) => {
        const badge = r.audibleBefore === r.audibleAfter
          ? (r.audibleAfter ? '<span class="tag ok">保持通声</span>' : '<span class="tag bad">不通声</span>')
          : (r.audibleAfter ? '<span class="tag ok">恢复通声 ↑</span>' : '<span class="tag bad">失声 ↓</span>');
        const which = r.currentIndex != null ? `当前#${r.currentIndex + 1}` : "";
        const which2 = r.targetIndex != null ? `目标#${r.targetIndex + 1}` : "";
        return `<li>${badge} ${esc(r.source)} → ${esc(r.target)}
          <span class="hint">${esc([which, which2].filter(Boolean).join(" / "))}</span>
          ${r.protected ? '<span class="tag warn">保护</span>' : ""}</li>`;
      }).join("")
    : '<li class="hint">无路由受本步影响。</li>';
  const protDetail = s.protected.length
    ? s.protected.map((p) => `<div class="route-block">
        <div class="route-title">路由 ${p.routeNo}：${esc(p.source)} → ${esc(p.target)}
          <span class="tag ${p.audible ? "ok" : "bad"}">${p.audible ? "✓ 仍可用" : "✗ 失声"}</span>
          ${p.viaTemporary.length ? `<span class="tag warn">经临时跳线 ${p.viaTemporary.map((t) => "T" + t).join("、")}</span>` : ""}
        </div>
        ${p.path ? `<div class="hint">通路：${p.path.map((x) => esc(x.port)).join(" → ")}</div>` : ""}
      </div>`).join("")
    : '<p class="hint">未指定保护路由。</p>';

  // 每个插/拔动作完成后的中间快照与保护通声校验（不再只看整步完成后）
  const micro = (s.microOps && s.microOps.length === 2) ? s.microOps
    : s.jackChanges.map((c) => ({
        op: c.order || 1, jack: c.jack, port: c.port, change: c.change,
        protected: s.protected, protectedAllAudible: s.protectedAllAudible,
        snapshot: s.snapshot,
      }));
  const microHtml = micro.map((o, k) => {
    const chips = o.protected.length
      ? o.protected.map((p) => `<span class="prot-chip ${p.audible ? "on" : "off"}">
           路由${p.routeNo} ${p.audible ? "✓通声" : "✗失声"}</span>`).join("")
      : '<span class="hint">无保护路由</span>';
    return `<div class="micro-op">
      <div class="micro-op-head">
        <span class="tag ${o.change === "plug" ? "ok" : "bad"}">动作 ${o.op} · ${o.change === "plug" ? "插入" : "拔下"}</span>
        <b>${esc(o.jack)}</b><span class="hint">（${esc(o.port || "—")}）</span>
        <span class="micro-chip">${chips}</span>
      </div>
      <h5>该动作完成后的前面板（保护路由${o.protectedAllAudible ? "全程通声" : "失声"}）</h5>
      ${rewireSnapshotSvg(o.snapshot)}
    </div>`;
  }).join("");

  $("#rewire-step-detail").innerHTML = `
    <div class="step-detail-head"><b>${esc(s.summary)}</b></div>
    <div class="rewire-detail-cols">
      <div><h4>插孔变更（按实际操作次序）</h4><ul class="notes">${changes}</ul></div>
      <div><h4>受影响路由</h4><ul class="notes">${aff}</ul></div>
    </div>
    <h4>每个插/拔动作后的中间快照与保护通声校验</h4>${microHtml}
    <h4>本步完成后的保护路由状态</h4>${protDetail}`;
  const det = $("#rewire-step-detail");
  det.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function rewireSnapshotSvg(snap) {
  // 复用面板图，但临时跳线用特殊颜色/虚线
  const pairs = snap.pairs, cables = snap.cables;
  if (!pairs.length) return "";
  const colW = 96, x0 = 60, topY = 64, botY = 128, height = 300;
  const width = x0 * 2 + colW * (pairs.length - 1);
  const pos = {};
  pairs.forEach((p, i) => {
    pos[p.topJack] = { x: x0 + i * colW, y: topY };
    pos[p.bottomJack] = { x: x0 + i * colW, y: botY };
  });
  const plugged = {};
  const pending = {};
  cables.forEach((c) => {
    if (c.fromJack) plugged[c.fromJack] = c;
    if (c.toJack) plugged[c.toJack] = c;
  });
  (snap.pendingPlugs || []).forEach((pp) => { pending[pp.jack] = pp; });
  let s = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
  s += `<rect x="8" y="30" width="${width - 16}" height="140" rx="10" fill="#12151a" stroke="#2e3542"/>`;
  for (const p of pairs) {
    const t = pos[p.topJack], b = pos[p.bottomJack];
    if (p.mode !== "through" && p.topPort && p.bottomPort) {
      const color = p.normalActive ? "#3ecf8e" : "#5a6270";
      s += `<line x1="${t.x - 18}" y1="${t.y}" x2="${b.x - 18}" y2="${b.y}" stroke="${color}" stroke-width="3"/>`;
    }
  }
  cables.forEach((c) => {
    const color = c.temporary ? "#ffcf5c" : CABLE_COLORS[c.id % CABLE_COLORS.length];
    const dash = c.temporary ? ' stroke-dasharray="7,5"' : "";
    if (c.dangling || !c.fromJack || !c.toJack) {
      // 一端悬空：另一只手还没插/已拔下，画一段落到走线槽的悬空线头
      const jk = c.fromJack || c.toJack;
      const a = pos[jk];
      const my = botY + 60;
      s += `<path d="M ${a.x} ${a.y} Q ${a.x} ${my + 10} ${a.x + 26} ${my + 18}" `
        + `fill="none" stroke="${color}" stroke-width="3.5" stroke-linecap="round"${dash}/>`;
      s += `<circle cx="${a.x + 26}" cy="${my + 18}" r="5" fill="none" stroke="${color}" stroke-width="2.5"/>`;
      s += `<text x="${a.x + 40}" y="${my + 22}" fill="${color}" font-size="11" font-weight="bold">悬空</text>`;
      return;
    }
    const a = pos[c.fromJack], b = pos[c.toJack];
    const depth = 60 + Math.abs(a.x - b.x) * 0.28;
    const mx = (a.x + b.x) / 2, my = botY + depth;
    s += `<path d="M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}" fill="none" stroke="${color}" stroke-width="3.5"${dash}/>`;
    s += `<text x="${mx}" y="${my * 0.62}" fill="${color}" font-size="12" text-anchor="middle" font-weight="bold">${c.temporary ? "T" : "#"}${c.id}</text>`;
  });
  for (const p of pairs) {
    for (const [jack, port, dy] of [[p.topJack, p.topPort, -14], [p.bottomJack, p.bottomPort, 24]]) {
      const { x, y } = pos[jack];
      const owner = plugged[jack];
      const pp = pending[jack];
      const fill = owner
        ? (owner.temporary ? "#ffcf5c" : CABLE_COLORS[owner.id % CABLE_COLORS.length])
        : (pp ? (pp.temporary ? "#ffcf5c" : CABLE_COLORS[pp.id % CABLE_COLORS.length]) : "#1d222b");
      s += `<circle cx="${x}" cy="${y}" r="9" fill="${fill}" stroke="#9aa4b2" stroke-width="1.5"/>`;
      s += `<text x="${x}" y="${y + dy}" fill="#9aa4b2" font-size="10" text-anchor="middle">${esc(port || "—")}</text>`;
      s += `<text x="${x + 14}" y="${y + 4}" fill="#e6e9ef" font-size="10">${esc(jack)}</text>`;
    }
    s += `<text x="${pos[p.topJack].x}" y="${botY + 40}" fill="#9aa4b2" font-size="10" text-anchor="middle">第${p.id}对</text>`;
  }
  s += `<text x="20" y="${height - 12}" fill="#ffcf5c" font-size="11">黄色虚线＝临时跳线 · 空心线头＝悬空插头（插/拔中间瞬间）</text>`;
  s += `</svg>`;
  return s;
}

/* ---------------- 串音隔离规划 ---------------- */

const GROUP_COLORS = ["#4da3ff", "#3ecf8e", "#ffcf5c", "#c56cf0",
  "#ff9f43", "#ff6b6b", "#5ad1e0", "#a1e86b", "#f78fb3", "#9aa4b2"];

function groupColor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return GROUP_COLORS[h % GROUP_COLORS.length];
}

function loadIsolationFromExample(iso, nRoutes) {
  isolationState.groups = new Array(nRoutes).fill("");
  isolationState.forbidden = [];
  if (!iso || typeof iso !== "object") return;
  if (Array.isArray(iso.groups)) {
    iso.groups.forEach((g, i) => { isolationState.groups[i] = (g || ""); });
  }
  if (Array.isArray(iso.forbiddenPairs)) {
    isolationState.forbidden = iso.forbiddenPairs
      .filter((p) => Array.isArray(p) && p.length === 2)
      .map((p) => [String(p[0]), String(p[1])].sort());
  }
}

function renderIsolationPanel(data, httpOk) {
  const card = $("#isolation-card");
  if (!httpOk || !data || data.error || !Array.isArray(data.pairs)) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  // 路由数量变化时补齐 / 截断组名数组
  if (isolationState.groups.length !== state.routes.length) {
    isolationState.groups = state.routes.map((_, i) => isolationState.groups[i] || "");
  }
  renderIsolationGroupRows();
  renderForbiddenControls();
  renderIsolationOverview();
  $("#isolation-hint").textContent =
    `填写隔离组与禁配组对后计算：系统会在全常态/半常态/直通语义下回溯重排全部路由。`;
  lastIsolationData = null;
  $("#isolation-results").innerHTML = "";
}

function renderIsolationGroupRows() {
  $("#isolation-group-rows").innerHTML = state.routes.map((r, i) => {
    const g = isolationState.groups[i] || "";
    const color = g ? groupColor(g) : "transparent";
    return `<tr>
      <td>${i + 1}</td>
      <td class="hint">${esc(r.source || "—")} → ${esc(r.target || "—")}</td>
      <td style="width:260px"><div class="group-input-wrap">
        <span class="group-dot" style="background:${color}"></span>
        <input value="${esc(g)}" placeholder="如 主唱 / 对讲（留空不分组）"
          oninput="setIsolationGroup(${i}, this.value)"></div></td>
    </tr>`;
  }).join("");
}

function setIsolationGroup(i, value) {
  isolationState.groups[i] = value.trim();
  renderIsolationGroupRows();
  renderForbiddenControls();
  renderIsolationOverview();
}

function groupNames() {
  return [...new Set(isolationState.groups.filter(Boolean))].sort();
}

function renderForbiddenControls() {
  const names = groupNames();
  const opts = (sel) => names.map((n) =>
    `<option value="${esc(n)}" ${n === sel ? "selected" : ""}>${esc(n)}</option>`).join("");
  const a = $("#isolation-forbid-a");
  const b = $("#isolation-forbid-b");
  const av = a.value, bv = b.value;
  a.innerHTML = opts(av && names.includes(av) ? av : names[0]);
  b.innerHTML = opts(bv && names.includes(bv) && bv !== a.value
    ? bv : names.filter((n) => n !== a.value)[0]);
  // 选项不足时禁用添加按钮的语义由 addForbiddenPair 校验
  $("#isolation-forbid-list").innerHTML = isolationState.forbidden.length
    ? isolationState.forbidden.map((p, i) => `
      <span class="forbid-chip">
        <span class="group-tag" style="--gc:${groupColor(p[0])}">${esc(p[0])}</span>
        <span class="hint">不得连通</span>
        <span class="group-tag" style="--gc:${groupColor(p[1])}">${esc(p[1])}</span>
        <button class="danger" onclick="removeForbiddenPair(${i})">删除</button>
      </span>`).join("")
    : `<p class="hint">尚未声明禁配组对。</p>`;
}

function addForbiddenPair() {
  const a = $("#isolation-forbid-a").value;
  const b = $("#isolation-forbid-b").value;
  if (!a || !b) {
    $("#isolation-results").innerHTML =
      `<div class="banner warn">请先为至少两条路由填写不同的隔离组名。</div>`;
    return;
  }
  if (a === b) {
    $("#isolation-results").innerHTML =
      `<div class="banner warn">同一隔离组不能与自身声明禁配。</div>`;
    return;
  }
  const key = [a, b].sort();
  if (isolationState.forbidden.some((p) => p[0] === key[0] && p[1] === key[1])) {
    $("#isolation-results").innerHTML =
      `<div class="banner warn">组对 “${esc(a)} ✕ ${esc(b)}” 已在禁配列表中。</div>`;
    return;
  }
  isolationState.forbidden.push(key);
  renderForbiddenControls();
  $("#isolation-results").innerHTML = "";
}

function removeForbiddenPair(i) {
  isolationState.forbidden.splice(i, 1);
  renderForbiddenControls();
}

function renderIsolationOverview() {
  const names = groupNames();
  $("#isolation-group-overview").innerHTML = names.length
    ? names.map((n) => {
        const idxs = isolationState.groups
          .map((g, i) => (g === n ? i : -1)).filter((i) => i >= 0);
        const others = new Set();
        isolationState.forbidden.forEach(([a, b]) => {
          if (a === n) others.add(b);
          if (b === n) others.add(a);
        });
        return `<div class="group-overview-row">
          <span class="group-tag" style="--gc:${groupColor(n)}">${esc(n)}</span>
          <span class="hint">路由 ${idxs.map((i) => i + 1).join("、") || "—"}</span>
          <span class="hint">禁配：${[...others].map((x) => esc(x)).join("、") || "无"}</span>
        </div>`;
      }).join("")
    : `<p class="hint">还没有隔离组：在上方表格为路由填写组名。</p>`;
}

async function runIsolation() {
  const out = $("#isolation-results");
  if (!lastConfig) {
    out.innerHTML = `<div class="banner warn">请先计算一次路由方案。</div>`;
    return;
  }
  // 组名长度与路由对齐
  while (isolationState.groups.length < state.routes.length) isolationState.groups.push("");
  const groups = isolationState.groups.slice(0, state.routes.length).map((g) => g || null);
  const used = new Set(groups.filter(Boolean));
  const badFp = isolationState.forbidden.find((p) => !used.has(p[0]) || !used.has(p[1]));
  if (badFp) {
    out.innerHTML = `<div class="banner err">禁配组 “${esc(badFp[0])} / ${esc(badFp[1])}”
      已不被任何路由使用，请删除后重试。</div>`;
    return;
  }
  out.innerHTML = `<div class="banner">⏳ 正在按隔离约束回溯重排全部路由（尽量少用跳线）…</div>`;
  let data, status;
  try {
    const res = await fetch("/api/crosstalk-isolation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: lastConfig,
        isolation: {
          groups,
          forbiddenPairs: isolationState.forbidden.map((p) => [p[0], p[1]]),
        },
      }),
    });
    data = await res.json();
    status = res.status;
  } catch (e) {
    out.innerHTML = `<div class="banner err">无法连接后端：${esc(e.message)}</div>`;
    return;
  }
  if (status === 400) {
    out.innerHTML = `<div class="banner err">隔离配置错误：${esc(errorMessage(data))}</div>`;
    return;
  }
  lastIsolationData = data;
  activeIsolationConflict = null;
  if (status === 409) renderIsolationConflict(data);
  else renderIsolationSuccess(data);
}

function renderIsolationSuccess(data) {
  let html = `<div class="banner ok">✅ 串音隔离规划完成：全部禁配组对的物理连通分量已分离，
    共需 <b>${data.cableCount}</b> 条跳线。</div>`;
  const iso = data.isolation;
  if (iso.solverNote) html += `<div class="banner warn">⚠️ ${esc(iso.solverNote)}</div>`;
  if (!iso.greedyOk) {
    html += `<div class="banner ok">🔀 输入顺序贪心方案<b>不满足隔离</b>
      （贪心用 ${iso.greedyCableCount} 条跳线即出现共分量），
      已通过<b>回溯重排 / 替代路径</b>得到本方案。</div>`;
  } else {
    html += `<div class="banner" style="background:rgba(77,163,255,.1);border:1px solid var(--accent)">
      输入顺序贪心方案本身已满足隔离；规划顺序 ${iso.routeOrder.map((i) => i + 1).join(" → ")}，
      回溯搜索访问 ${iso.searchNodes} 个节点。</div>`;
  }
  if (data.feedbackLoops.length) {
    html += `<div class="banner warn">⚠️ 检测到 ${data.feedbackLoops.length} 个反馈环路（保留检测、仅警告）：<ul class="loop-list">` +
      data.feedbackLoops.map((l) => `<li>${l.map(esc).join(" → ")}</li>`).join("") + `</ul></div>`;
  }

  // 统计条
  html += `<div class="audit-stat-row">
    <span class="audit-stat"><b>${data.groups.length}</b>隔离组</span>
    <span class="audit-stat ok"><b>${data.components.length}</b>物理分量</span>
    <span class="audit-stat ok"><b>${data.forbiddenPairs.length}</b>禁配组对（全部满足）</span>
    <span class="audit-stat"><b>${data.cableCount}</b>跳线</span>
    <span class="audit-stat"><b>${iso.searchNodes}</b>搜索节点</span>
  </div>`;

  // 隔离组 → 分量
  html += `<div class="result-card"><h3>隔离组拓扑（组 → 物理连通分量）</h3><ul class="notes">` +
    data.groups.map((g) => `<li><span class="group-tag" style="--gc:${groupColor(g.name)}">${esc(g.name)}</span>
      路由 ${g.routeIndexes.map((i) => i + 1).join("、")}
      <span class="hint">→ 分量 ${g.componentIds.map((c) => c + 1).join("、")}</span></li>`).join("")
    + `</ul><p class="hint" style="margin-top:6px">同色节点为同一隔离组；禁配组落在不同底色分量。</p></div>`;

  if (data.steps.length) {
    html += `<div class="result-card"><h3>接线顺序</h3><ol class="steps">` +
      data.steps.map((s) => `<li>${esc(s)}</li>`).join("") + `</ol></div>`;
  }
  if (data.notes.length) {
    html += `<div class="result-card"><h3>插头对常态连接的影响</h3><ul class="notes">` +
      data.notes.map((n) => `<li>${esc(n)}</li>`).join("") + `</ul></div>`;
  }

  html += `<div class="result-card"><h3>每条路由的链路</h3>` + data.routes.map((r) => {
    const gc = r.group ? groupColor(r.group) : "#5a6270";
    const tag = `<span class="group-tag" style="--gc:${gc}">${esc(r.group || "未分组")}</span>`;
    return `<div class="route-block"><div class="route-title">路由 ${r.index + 1}
      （规划第 ${r.planningStep} 步，分量 ${r.component + 1}）：
      ${esc(r.source)} → ${esc(r.target)} ${tag}
      <span class="tag ok">${r.cables.length} 条跳线</span></div>
      ${chainHtml(r.chain)}</div>`;
  }).join("") + `</div>`;

  html += `<div class="result-card"><h3>隔离拓扑图（按分量着色 · 按隔离组描边）</h3>
    <div class="audit-svg-wrap">${isolationTopologySvg(data, null)}</div>
    <div class="legend">底色＝物理连通分量；插孔描边颜色＝该路由的隔离组；
      <span class="swatch" style="background:#3ecf8e;border-color:#3ecf8e"></span>生效常态
      <span class="swatch" style="background:#4da3ff;border-color:#4da3ff"></span>跳线</div>
  </div>`;

  $("#isolation-results").innerHTML = html;
}

function renderIsolationConflict(data) {
  const err = data.error || {};
  let html = "";
  if (data.inconclusive) {
    html += `<div class="banner warn">⚠️ 搜索达到节点上限，结论为 <b>inconclusive</b>：
      尚未找到满足全部隔离约束的规划，已列出搜索中发现的冲突，但未证明绝对无解。
      <b>未提交任何部分方案。</b></div>`;
  } else {
    html += `<div class="banner err">❌ 不存在满足全部串音隔离约束的完整规划：
      穷尽回溯重排与替代路径后仍有禁配组无法分离。<b>不返回接线步骤，不提交部分方案。</b></div>`;
  }
  html += `<div class="audit-stat-row">
    <span class="audit-stat err"><b>${data.conflicts.length}</b>冲突组对</span>
    <span class="audit-stat warn"><b>${data.unsatisfiedRoutes.length}</b>未满足路由</span>
    <span class="audit-stat"><b>${data.searchNodes}</b>搜索节点</span>
    <span class="audit-stat"><b>${data.terminal.cableCount}</b>终止状态跳线（仅分析）</span>
  </div>`;

  // 冲突卡片（点击高亮最短共享证据）
  if (data.conflicts.length) {
    html += `<div class="result-card"><h3>冲突组对与最短共享证据（点击在拓扑图高亮）</h3>` +
      data.conflicts.map((c, i) => {
        const evPorts = (c.evidence || []).filter((x) => x.kind === "port");
        return `<div class="issue-card severity-error" id="iso-conflict-${i}"
          onclick="highlightIsolationConflict(${i})">
          <div class="issue-head">
            <span class="group-tag" style="--gc:${groupColor(c.groups[0])}">${esc(c.groups[0])}</span>
            <span class="hint">不得连通</span>
            <span class="group-tag" style="--gc:${groupColor(c.groups[1])}">${esc(c.groups[1])}</span>
            ${c.structural ? '<span class="tag bad">结构性·无法消除</span>'
              : '<span class="tag bad">共分量</span>'}
          </div>
          <div class="issue-msg">${esc(c.message)}</div>
          <div class="hint" style="margin-top:4px">涉及路由：
            组 “${esc(c.groups[0])}” 路由 ${(c.routesByGroup[c.groups[0]] || []).map((x) => x + 1).join("、")}；
            组 “${esc(c.groups[1])}” 路由 ${(c.routesByGroup[c.groups[1]] || []).map((x) => x + 1).join("、")}
          </div>
          <span class="issue-evidence-toggle">▸ 最短共享证据（${evPorts.length} 个端点）</span>
          <div class="evidence-box" hidden>${c.evidence ? chainHtml(c.evidence) : "（无）"}</div>
        </div>`;
      }).join("") + `</div>`;
  }

  // 未满足路由
  html += `<div class="result-card"><h3>未满足路由（不提交部分方案）</h3>` +
    data.unsatisfiedRoutes.map((u) => `<div class="route-block">
      <div class="route-title">路由 ${u.index + 1}：${esc(u.source)} → ${esc(u.target)}
        <span class="group-tag" style="--gc:${u.group ? groupColor(u.group) : "#5a6270"}">${esc(u.group || "未分组")}</span>
        <span class="tag bad">${u.status === "isolation-conflict" ? "隔离冲突" : "不可路由"}</span></div>
      <ul class="conflict-reasons">${u.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    </div>`).join("") + `</div>`;

  // 终止状态拓扑（高亮证据）
  html += `<div class="result-card"><h3>终止状态隔离拓扑（冲突高亮；仅供分析，非可施工方案）</h3>
    <div class="audit-svg-wrap" id="iso-svg-wrap">${isolationTopologySvg(data, null)}</div>
    <div class="legend">红色描边＝卷入冲突的端点；黄色粗线＝最短共享证据。</div></div>`;

  // 贪心对照
  const bl = data.baseline || {};
  html += `<div class="result-card"><h3>输入顺序贪心对照（失败原因）</h3>
    <p class="hint">贪心安装 ${esc(bl.greedyCableCount)} 条跳线、
    规划路由 ${(bl.unroutable || []).length ? `，其中第 ${bl.unroutable.map((u) => u.index + 1).join("、")} 条无法规划` : "全部规划"}；
    违反禁配组对：${(bl.violatedPairs || []).map((p) => `“${esc(p[0])} ✕ ${esc(p[1])}”`).join("、") || "无"}。
    下方为贪心的物理拓扑——它不是可施工方案，仅说明贪心为何失败。</p>
    <div class="audit-svg-wrap">${isolationTopologySvg(data, "baseline")}</div>
  </div>`;

  $("#isolation-results").innerHTML = html;
}

function highlightIsolationConflict(i) {
  const data = lastIsolationData;
  if (!data) return;
  const c = data.conflicts[i];
  if (!c) return;
  activeIsolationConflict = activeIsolationConflict === i ? null : i;
  document.querySelectorAll("#isolation-results .issue-card").forEach((card, k) =>
    card.classList.toggle("active", k === activeIsolationConflict));
  document.querySelectorAll("#isolation-results .evidence-box").forEach((box, k) => {
    box.hidden = k !== activeIsolationConflict;
  });
  const wrap = $("#iso-svg-wrap");
  if (wrap) wrap.innerHTML = isolationTopologySvg(data, null);
}

/* ---- 隔离拓扑 SVG ---- */

function isolationTopologySvg(data, variant) {
  // variant: null = 最终/终止状态（含冲突高亮）；"baseline" = 输入顺序贪心对照
  const pairs = data.pairs;
  if (!pairs.length) return "";
  const cables = variant === "baseline" ? (data.baseline.cables || []) : data.cables;
  const components = variant === "baseline"
    ? (data.baseline.components || []) : data.components;
  const pairSnap = variant === "baseline" ? (data.baseline.pairs || pairs) : pairs;

  const colW = 104, x0 = 64, topY = 70, botY = 140, height = 330;
  const width = x0 * 2 + colW * (pairs.length - 1);
  const pos = {};
  pairs.forEach((p, i) => {
    pos[p.topJack] = { x: x0 + i * colW, y: topY };
    pos[p.bottomJack] = { x: x0 + i * colW, y: botY };
  });
  const snapById = Object.fromEntries(pairSnap.map((p) => [p.id, p]));
  const compOfPort = {};
  components.forEach((c) => { for (const pp of c.ports) compOfPort[pp.port] = c; });
  const compColor = (c) => c ? COMP_COLORS[c.id % COMP_COLORS.length] : "#12151a";

  const iss = (variant === null && activeIsolationConflict != null && data.conflicts)
    ? data.conflicts[activeIsolationConflict] : null;
  const evidencePorts = new Set();
  const evidenceEdges = new Set();
  if (iss && iss.evidence) {
    for (let i = 0; i < iss.evidence.length; i++) {
      const it = iss.evidence[i];
      if (it.kind === "port") evidencePorts.add(it.port);
      if (it.kind === "edge") {
        const a = iss.evidence[i - 1], b = iss.evidence[i + 1];
        if (a && b && a.kind === "port" && b.kind === "port") {
          evidenceEdges.add(`${it.type}:${it.cableId || it.pairId || it.device || ""}:${a.port}|${b.port}`);
        }
      }
    }
  }
  const hitPorts = new Set();
  if (iss) (iss.evidence || []).forEach((x) => { if (x.kind === "port") hitPorts.add(x.port); });
  // 冲突组路由的全部源/目标端口也描边
  if (iss) {
    for (const r of (data.unsatisfiedRoutes || [])) hitPorts.add(r.source);
  }

  // 端口 → 隔离组（从路由表取，成功用 routes，失败用 unsatisfied/groups）
  const groupOfPort = {};
  const routeList = data.routes || data.unsatisfiedRoutes || [];
  routeList.forEach((r) => {
    if (r.group) { groupOfPort[r.source] = r.group; groupOfPort[r.target] = r.group; }
  });

  let s = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
  s += `<rect x="8" y="32" width="${width - 16}" height="150" rx="10" fill="#12151a" stroke="#2e3542"/>`;
  s += `<text x="20" y="24" fill="#9aa4b2" font-size="12">${variant === "baseline"
    ? "输入顺序贪心对照拓扑（失败展示，非方案）"
    : "串音隔离拓扑 · 底色为物理连通分量"} · ${components.length} 个分量</text>`;

  for (const p of pairs) {
    const sp = snapById[p.id] || p;
    if (sp.mode === "through" || !sp.topPort || !sp.bottomPort) continue;
    const t = pos[p.topJack], b = pos[p.bottomJack];
    const ekA = `normal:${p.id}:${sp.topPort}|${sp.bottomPort}`;
    const ekB = `normal:${p.id}:${sp.bottomPort}|${sp.topPort}`;
    const onEv = iss && (evidenceEdges.has(ekA) || evidenceEdges.has(ekB));
    let col = sp.normalActive ? "#3ecf8e" : "#5a6270";
    if (onEv) col = "#ffcf5c";
    s += `<line x1="${t.x - 18}" y1="${t.y}" x2="${b.x - 18}" y2="${b.y}"
      stroke="${col}" stroke-width="${onEv ? 6 : 3}" opacity="${sp.normalActive ? .9 : .5}"/>`;
  }

  cables.forEach((c) => {
    const a = pos[c.fromJack], b = pos[c.toJack];
    if (!a || !b) return;
    const ekA = `cable:${c.id}:${c.fromPort}|${c.toPort}`;
    const ekB = `cable:${c.id}:${c.toPort}|${c.fromPort}`;
    const onEv = iss && (evidenceEdges.has(ekA) || evidenceEdges.has(ekB));
    const color = onEv ? "#ffcf5c" : CABLE_COLORS[c.id % CABLE_COLORS.length];
    const depth = 64 + Math.abs(a.x - b.x) * 0.3;
    const mx = (a.x + b.x) / 2, my = botY + depth;
    s += `<path d="M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}" fill="none"
      stroke="${color}" stroke-width="${onEv ? 5.5 : 3}"/>`;
    s += `<text x="${mx}" y="${my * 0.62}" fill="${color}" font-size="12"
      text-anchor="middle" font-weight="bold">#${c.id}</text>`;
  });

  // 设备内部证据边：黄色虚线弧（跨列）
  if (iss && iss.evidence) {
    const jackPosOfPort = {};
    for (const p of pairs) {
      if (p.topPort) jackPosOfPort[p.topPort] = pos[p.topJack];
      if (p.bottomPort) jackPosOfPort[p.bottomPort] = pos[p.bottomJack];
    }
    const drawn = new Set();
    for (let i = 0; i < iss.evidence.length; i++) {
      const it = iss.evidence[i];
      if (it.kind !== "edge" || it.type !== "device") continue;
      const a = iss.evidence[i - 1], b = iss.evidence[i + 1];
      if (!a || !b || a.kind !== "port" || b.kind !== "port") continue;
      const pa = jackPosOfPort[a.port], pb = jackPosOfPort[b.port];
      const key = `${it.device}:${[a.port, b.port].sort().join("|")}`;
      if (!pa || !pb || drawn.has(key)) continue;
      drawn.add(key);
      s += `<path d="M ${pa.x} ${pa.y - 12} Q ${(pa.x + pb.x) / 2} 36 ${pb.x} ${pb.y - 12}"
        fill="none" stroke="#ffcf5c" stroke-width="2.5" stroke-dasharray="6,4"/>`;
      s += `<text x="${(pa.x + pb.x) / 2}" y="44" fill="#ffcf5c" font-size="10"
        text-anchor="middle">${esc(it.device)} 内部</text>`;
    }
  }

  for (const p of pairs) {
    for (const [jack, port, dy] of [[p.topJack, p.topPort, -15], [p.bottomJack, p.bottomPort, 26]]) {
      if (!pos[jack]) continue;
      const { x, y } = pos[jack];
      const comp = port ? compOfPort[port] : null;
      const fill = compColor(comp);
      const g = port ? groupOfPort[port] : null;
      const stroke = g ? groupColor(g) : "#9aa4b2";
      const isHit = iss && port && hitPorts.has(port);
      const isEv = iss && port && evidencePorts.has(port);
      s += `<circle cx="${x}" cy="${y}" r="${isHit ? 12 : 9}" fill="${fill}"
        stroke="${isHit ? "#ff6b6b" : isEv ? "#ffcf5c" : stroke}"
        stroke-width="${isHit || g ? 3 : 1.5}"/>`;
      s += `<text x="${x}" y="${y + dy}" fill="${isHit ? "#ff9f9f" : "#9aa4b2"}" font-size="10"
        text-anchor="middle">${esc(port || "—")}</text>`;
      s += `<text x="${x + 14}" y="${y + 4}" fill="#e6e9ef" font-size="10">${esc(jack)}</text>`;
    }
    s += `<text x="${pos[p.topJack].x}" y="${botY + 42}" fill="#9aa4b2" font-size="10"
      text-anchor="middle">第${p.id}对 · ${esc(p.modeLabel)}</text>`;
  }

  // 分量图例（右下角列出各组 → 分量）
  s += `<text x="20" y="${height - 12}" fill="#9aa4b2" font-size="11">插孔描边色＝隔离组；底色＝物理分量。` +
    (iss ? " 黄色粗线＝最短共享证据，红色描边＝冲突端点。" : "") + `</text>`;
  s += `</svg>`;
  return s;
}

/* ---------------- 初始化 ---------------- */

$("#example-select").addEventListener("change", (e) => {
  if (e.target.value) loadExample(e.target.value);
});

loadExample("standard").catch(() => {
  $("#results").innerHTML = `<div class="banner err">无法从后端载入示例，请确认服务已启动。</div>`;
});
