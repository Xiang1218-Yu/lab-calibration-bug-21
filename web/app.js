/* CalTrack SPA — vanilla JS, hash router, no build step. */
(() => {
  "use strict";

  // ---------- API ----------
  const api = async (path, opts = {}) => {
    const res = await fetch("/api" + path, {
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      ...opts,
    });
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* non-json */ }
    if (!res.ok) {
      const msg = (payload && payload.error) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return payload ? payload.data : null;
  };

  // ---------- helpers ----------
  const $app = document.getElementById("app");
  const esc = (s) => String(s == null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmt = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  const STATUS_ICON = {
    pass: "✓", fail: "✕", conditional: "◐",
    info: "ℹ", warning: "⚠", critical: "⛔",
    open: "●", monitoring: "◔", resolved: "✓",
    success: "✓", failed: "✕", retrying: "↻", timeout: "⏱",
    pending: "…", running: "▶", processing: "▶", done: "✓",
    duplicate: "⧉", error: "!",
  };
  const STATUS_LABEL = {
    pass: "通过", fail: "不合格", conditional: "条件通过",
    info: "信息", warning: "警告", critical: "严重",
    open: "待处理", monitoring: "观察中", resolved: "已处理",
    success: "成功", failed: "失败", retrying: "重试中", timeout: "超时",
    pending: "等待", running: "运行中", processing: "处理中", done: "完成",
    duplicate: "判重", error: "错误",
  };
  const badge = (kind) => {
    const k = kind || "neutral";
    return `<span class="badge ${esc(k)}"><span class="dot">${STATUS_ICON[k] || "•"}</span>${esc(STATUS_LABEL[k] || k)}</span>`;
  };

  let toastTimer = null;
  const toast = (msg, isErr) => {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.style.background = isErr ? "var(--critical)" : "var(--ink)";
    t.style.color = isErr ? "#fff" : "var(--page)";
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
  };

  const qs = (obj) => {
    const p = new URLSearchParams();
    Object.entries(obj).forEach(([k, v]) => { if (v !== "" && v != null) p.set(k, v); });
    const s = p.toString();
    return s ? "?" + s : "";
  };

  // device cache for selects
  let deviceCache = null;
  const loadDevices = async (force) => {
    if (!deviceCache || force) deviceCache = await api("/devices");
    return deviceCache;
  };
  const deviceOptions = async (selected) => {
    const devs = await loadDevices();
    return ['<option value="">全部设备</option>']
      .concat(devs.map((d) => `<option value="${d.id}" ${String(d.id) === String(selected) ? "selected" : ""}>${esc(d.code)} · ${esc(d.name)}</option>`))
      .join("");
  };

  // ---------- theme ----------
  const applyTheme = (mode) => {
    if (mode) document.documentElement.setAttribute("data-theme", mode);
    else document.documentElement.removeAttribute("data-theme");
    document.getElementById("themeBtn").textContent =
      document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
  };
  document.getElementById("themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = cur ? (cur === "dark" ? "light" : "dark") : (dark ? "light" : "dark");
    localStorage.setItem("caltrack-theme", next);
    applyTheme(next);
  });
  applyTheme(localStorage.getItem("caltrack-theme"));

  // ---------- router ----------
  const routes = {};
  let cleanupFns = [];
  const onCleanup = (fn) => cleanupFns.push(fn);
  const go = (hash) => { location.hash = hash; };

  const router = async () => {
    cleanupFns.forEach((fn) => { try { fn(); } catch (_) {} });
    cleanupFns = [];
    const hash = location.hash || "#/dashboard";
    const parts = hash.replace(/^#\//, "").split("/");
    const name = parts[0] || "dashboard";
    const arg = parts[1];
    document.querySelectorAll("#nav a").forEach((a) => {
      a.classList.toggle("active", a.dataset.route === name ||
        (name === "device" && a.dataset.route === "devices") ||
        (name === "issue" && a.dataset.route === "issues"));
    });
    const fn = routes[name] || routes.notfound;
    $app.innerHTML = "";
    try {
      await fn(arg);
    } catch (e) {
      $app.innerHTML = `<div class="card empty">加载失败：${esc(e.message)}</div>`;
    }
  };
  window.addEventListener("hashchange", router);

  // ================= VIEWS =================

  routes.notfound = async () => {
    $app.innerHTML = `<div class="card empty">页面不存在 · <a href="#/dashboard">返回总览</a></div>`;
  };

  // ---- Dashboard ----
  routes.dashboard = async () => {
    const d = await api("/dashboard");
    const tile = (label, value, cls, icon) =>
      `<div class="tile ${cls || ""}"><div class="label">${icon || ""} ${label}</div><div class="value">${value}</div></div>`;
    $app.innerHTML = `
      <div class="page-head"><h2>总览</h2><span class="sub">设备校准与异常集中视图</span></div>
      <div class="tiles">
        ${tile("设备总数", d.devices, "", "🧰")}
        ${tile("校准记录", d.calibrations, "", "📐")}
        ${tile("运行事件", d.events, "", "📡")}
        ${tile("待处理问题", d.open_issues, d.open_issues ? "alert" : "good", "🧯")}
        ${tile("校准不合格", d.failed_calibrations, d.failed_calibrations ? "warn" : "good", "✕")}
        ${tile("严重事件", d.critical_events, d.critical_events ? "alert" : "", "⛔")}
      </div>
      <div class="card">
        <h3>最近问题 <span class="count">自动规则生成 / 校准驱动状态</span></h3>
        ${d.recent_issues.length ? issueTable(d.recent_issues) : '<div class="empty">暂无问题 🎉</div>'}
      </div>`;
    bindIssueRowClicks();
  };

  function issueTable(issues) {
    return `<div class="table-wrap"><table>
      <thead><tr><th>状态</th><th>等级</th><th>问题</th><th class="num">事件数</th><th>更新时间</th></tr></thead>
      <tbody>${issues.map((i) => `
        <tr class="clickable" data-issue="${i.id}">
          <td>${badge(i.status)}</td>
          <td>${badge(i.severity)}</td>
          <td><strong>${esc(i.title)}</strong>${i.device_code ? ` <span class="muted mono">${esc(i.device_code)}</span>` : ""}<div class="muted" style="font-size:12px">${esc(i.description || "")}</div></td>
          <td class="num">${i.event_count}</td>
          <td class="muted">${fmt(i.updated_at)}</td>
        </tr>`).join("")}</tbody></table></div>`;
  }
  function bindIssueRowClicks() {
    $app.querySelectorAll("tr[data-issue]").forEach((tr) =>
      tr.addEventListener("click", () => go("#/issue/" + tr.dataset.issue)));
  }

  // ---- Devices ----
  routes.devices = async () => {
    const devs = await api("/devices");
    $app.innerHTML = `
      <div class="page-head"><h2>设备档案</h2>
        <button class="btn primary" id="newDev">＋ 新增设备</button>
      </div>
      <div class="card">
        <div class="toolbar"><input type="text" id="devSearch" placeholder="搜索编号 / 名称 / 位置…"></div>
        <div class="table-wrap"><table>
          <thead><tr><th>编号</th><th>名称</th><th>类型</th><th>位置</th><th>状态</th><th class="num">校准</th><th class="num">事件</th><th class="num">待处理</th><th>最近校准</th></tr></thead>
          <tbody id="devBody">${devRows(devs)}</tbody>
        </table></div>
      </div>
      <div class="card" id="devFormCard" style="display:none">${deviceFormHtml()}</div>`;

    const render = (q) => {
      const filtered = q ? devs.filter((d) =>
        [d.code, d.name, d.location].join(" ").toLowerCase().includes(q.toLowerCase())) : devs;
      document.getElementById("devBody").innerHTML = devRows(filtered);
      bindDevClicks();
    };
    const bindDevClicks = () => $app.querySelectorAll("tr[data-dev]").forEach((tr) =>
      tr.addEventListener("click", () => go("#/device/" + tr.dataset.dev)));
    bindDevClicks();
    document.getElementById("devSearch").addEventListener("input", (e) => render(e.target.value));
    document.getElementById("newDev").addEventListener("click", () => {
      document.getElementById("devFormCard").style.display = "block";
      document.getElementById("devFormCard").scrollIntoView({ behavior: "smooth" });
    });
    document.getElementById("devForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/devices", { method: "POST", body: JSON.stringify({
          code: f.code.value, name: f.name.value, device_type: f.device_type.value,
          manufacturer: f.manufacturer.value, model: f.model.value,
          serial_number: f.serial_number.value, location: f.location.value,
        }) });
        deviceCache = null;
        toast("设备已创建");
        router();
      } catch (err) { toast(err.message, true); }
    });
  };

  function devRows(devs) {
    if (!devs.length) return '<tr><td colspan="9" class="empty">无设备</td></tr>';
    return devs.map((d) => {
      const s = d.summary || {};
      return `<tr class="clickable" data-dev="${d.id}">
        <td class="mono">${esc(d.code)}</td>
        <td>${esc(d.name)} ${d.is_active ? "" : '<span class="badge neutral">停用</span>'}</td>
        <td>${esc(d.device_type)}</td>
        <td>${esc(d.location || "—")}</td>
        <td>${s.last_calibration_result ? badge(s.last_calibration_result) : '<span class="muted">—</span>'}</td>
        <td class="num">${s.calibration_count ?? 0}</td>
        <td class="num">${s.event_count ?? 0}</td>
        <td class="num">${s.open_issue_count ? `<span style="color:var(--critical);font-weight:700">${s.open_issue_count}</span>` : 0}</td>
        <td class="muted">${fmt(s.last_calibration_at)}</td>
      </tr>`;
    }).join("");
  }
  function deviceFormHtml() {
    return `<h3>新增设备</h3><form id="devForm" class="grid-form">
      <div class="field"><label>编号 *</label><input name="code" required placeholder="如 BAL-001"></div>
      <div class="field"><label>名称 *</label><input name="name" required placeholder="如 分析天平 #1"></div>
      <div class="field"><label>类型</label><input name="device_type" placeholder="balance / ph_meter…"></div>
      <div class="field"><label>厂商</label><input name="manufacturer"></div>
      <div class="field"><label>型号</label><input name="model"></div>
      <div class="field"><label>序列号</label><input name="serial_number"></div>
      <div class="field"><label>位置</label><input name="location"></div>
      <div class="field" style="justify-content:end"><button class="btn primary" type="submit">保存</button></div>
    </form>`;
  }

  // ---- Device detail ----
  routes.device = async (id) => {
    const [d, cals, events, issues] = await Promise.all([
      api(`/devices/${id}`), api(`/calibrations?device_id=${id}&limit=500`),
      api(`/events?device_id=${id}&limit=500`), api(`/issues?device_id=${id}`),
    ]).catch((e) => { throw e; });
    if (!d) { $app.innerHTML = '<div class="card empty">设备不存在</div>'; return; }
    const activeIssues = issues.filter((i) => i.status !== "resolved");
    $app.innerHTML = `
      <span class="back-link" id="back">← 返回设备列表</span>
      <div class="page-head"><h2>${esc(d.code)} · ${esc(d.name)}</h2>
        ${badge(d.is_active ? "info" : "neutral")}
        ${activeIssues.length ? badge("open") : badge("resolved")}
      </div>
      <div class="card">
        <h3>设备档案</h3>
        <div class="dev-meta">
          <div><div class="k">类型</div>${esc(d.device_type)}</div>
          <div><div class="k">厂商 / 型号</div>${esc(d.manufacturer || "—")} ${esc(d.model || "")}</div>
          <div><div class="k">序列号</div>${esc(d.serial_number || "—")}</div>
          <div><div class="k">位置</div>${esc(d.location || "—")}</div>
          <div><div class="k">校准次数</div>${d.summary.calibration_count}（最近 ${fmt(d.summary.last_calibration_at)}）</div>
          <div><div class="k">事件次数</div>${d.summary.event_count}（最近 ${fmt(d.summary.last_event_at)}）</div>
        </div>
      </div>

      <div class="section-row">
        <div class="card">
          <h3>登记校准结果</h3>
          <form id="calForm" class="grid-form">
            <div class="field"><label>校准时间 *</label><input name="calibrated_at" type="datetime-local" required></div>
            <div class="field"><label>结果 *</label><select name="result">
              <option value="pass">通过 pass</option><option value="conditional">条件通过 conditional</option><option value="fail">不合格 fail</option></select></div>
            <div class="field"><label>校准员</label><input name="technician"></div>
            <div class="field"><label>实测值</label><input name="measured_value" type="number" step="any"></div>
            <div class="field"><label>标称值</label><input name="nominal_value" type="number" step="any"></div>
            <div class="field"><label>允差</label><input name="tolerance" type="number" step="any"></div>
            <div class="field"><label>单位</label><input name="unit" placeholder="g / pH / °C"></div>
            <div class="field" style="grid-column:1/-1"><label>备注</label><input name="notes"></div>
            <div class="field"><button class="btn primary" type="submit">保存校准</button></div>
          </form>
        </div>
        <div class="card">
          <h3>登记运行事件</h3>
          <form id="evtForm" class="grid-form">
            <div class="field"><label>发生时间 *</label><input name="occurred_at" type="datetime-local" required></div>
            <div class="field"><label>等级 *</label><select name="severity">
              <option value="info">信息 info</option><option value="warning">警告 warning</option><option value="critical">严重 critical</option></select></div>
            <div class="field"><label>事件代码</label><input name="code" placeholder="E_DRIFT"></div>
            <div class="field" style="grid-column:1/-1"><label>描述 *</label><input name="message" required placeholder="如 电极漂移超阈值"></div>
            <div class="field"><button class="btn primary" type="submit">保存事件</button></div>
          </form>
        </div>
      </div>

      ${activeIssues.length ? `<div class="card">
        <h3>待处理问题 <span class="count">${activeIssues.length}</span></h3>
        ${activeIssues.map(issueCard).join("")}</div>` : ""}

      <div class="card">
        <h3>异常时间线 <span class="count">事件 + 校准合并，按时间倒序</span></h3>
        ${timelineHtml([
          ...events.map((e) => ({ kind: "event", at: e.occurred_at, ...e })),
          ...cals.map((c) => ({ kind: "cal", at: c.calibrated_at, ...c })),
        ])}
      </div>

      <div class="card">
        <h3>校准历史 <span class="count">${cals.length} 条</span></h3>
        ${calTable(cals)}
      </div>`;

    document.getElementById("back").addEventListener("click", () => go("#/devices"));
    // default form times to now
    const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    ["calForm", "evtForm"].forEach((fid) => { const f = document.getElementById(fid); if (f) f.querySelector("[type=datetime-local]").value = nowLocal; });

    document.getElementById("calForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const num = (v) => (v === "" ? null : parseFloat(v));
      try {
        await api("/calibrations", { method: "POST", body: JSON.stringify({
          device_id: Number(id), calibrated_at: f.calibrated_at.value,
          result: f.result.value, technician: f.technician.value,
          measured_value: num(f.measured_value.value), nominal_value: num(f.nominal_value.value),
          tolerance: num(f.tolerance.value), unit: f.unit.value, notes: f.notes.value,
        }) });
        toast("校准已保存（问题状态已按结果联动）");
        router();
      } catch (err) { toast(err.message, true); }
    });
    document.getElementById("evtForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/events", { method: "POST", body: JSON.stringify({
          device_id: Number(id), occurred_at: f.occurred_at.value,
          severity: f.severity.value, code: f.code.value, message: f.message.value,
        }) });
        toast("事件已登记（异常规则已重新评估）");
        router();
      } catch (err) { toast(err.message, true); }
    });
    bindIssueActions();
  };

  function issueCard(i) {
    return `<div class="issue-card ${esc(i.status)}" style="margin-bottom:12px">
      <div class="t-head">${badge(i.status)} ${badge(i.severity)} <strong>${esc(i.title)}</strong>
        <span class="muted mono">${esc(i.rule)}</span></div>
      <div class="issue-desc">${esc(i.description || "")}</div>
      <div class="issue-meta">关联事件 ${i.event_count} · 首次 ${fmt(i.first_event_at)} · 最近 ${fmt(i.last_event_at)}
        ${i.resolved_at ? `· 处理于 ${fmt(i.resolved_at)}` : ""}</div>
      <div class="toolbar" style="margin:6px 0 0">
        <a class="btn small" href="#/issue/${i.id}">查看详情</a>
        ${i.status !== "resolved"
          ? `<button class="btn small primary" data-resolve="${i.id}">标记已处理</button>`
          : `<button class="btn small" data-reopen="${i.id}">重新打开</button>`}
      </div>
      ${i.resolution ? `<div class="muted" style="font-size:12px;margin-top:4px">处理说明：${esc(i.resolution)}</div>` : ""}
    </div>`;
  }
  function bindIssueActions() {
    $app.querySelectorAll("[data-resolve]").forEach((b) => b.addEventListener("click", async () => {
      const resolution = prompt("处理说明（可留空）：", "已现场处理并复测");
      if (resolution === null) return;
      try { await api(`/issues/${b.dataset.resolve}/resolve`, { method: "POST", body: JSON.stringify({ resolution }) });
        toast("问题已标记为已处理"); router();
      } catch (e) { toast(e.message, true); }
    }));
    $app.querySelectorAll("[data-reopen]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/issues/${b.dataset.reopen}/reopen`, { method: "POST", body: JSON.stringify({}) });
        toast("问题已重新打开"); router();
      } catch (e) { toast(e.message, true); }
    }));
  }

  function timelineHtml(items) {
    if (!items.length) return '<div class="empty">暂无事件 / 校准</div>';
    items.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
    return `<ul class="timeline">${items.slice(0, 200).map((x) => {
      if (x.kind === "event") {
        return `<li class="sev-${esc(x.severity)}">
          <div class="t-head"><span class="t-time">${fmt(x.occurred_at)}</span>${badge(x.severity)}
            ${x.code ? `<span class="mono muted">${esc(x.code)}</span>` : ""}</div>
          <div class="t-body">${esc(x.message)}</div></li>`;
      }
      return `<li class="cal-${esc(x.result)}">
        <div class="t-head"><span class="t-time">${fmt(x.calibrated_at)}</span>${badge(x.result)}
          <span class="muted">校准 · ${esc(x.technician || "—")}</span></div>
        <div class="t-body">${x.measured_value != null
          ? `实测 <strong>${x.measured_value}</strong>${x.unit ? " " + esc(x.unit) : ""} / 标称 ${x.nominal_value ?? "—"}（允差 ±${x.tolerance ?? "—"}）`
          : "校准记录"}${x.notes ? " · " + esc(x.notes) : ""} <span class="muted mono">[${esc(x.source)}]</span></div></li>`;
    }).join("")}</ul>`;
  }

  function calTable(cals) {
    if (!cals.length) return '<div class="empty">暂无校准记录</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>时间</th><th>设备</th><th>结果</th><th>校准员</th><th class="num">实测</th><th class="num">标称</th><th class="num">允差</th><th>备注</th></tr></thead>
      <tbody>${cals.map((c) => `<tr>
        <td class="muted">${fmt(c.calibrated_at)}</td>
        <td>${c.device_code ? `<a href="#/device/${c.device_id}">${esc(c.device_code)}</a>` : ""}</td>
        <td>${badge(c.result)}</td>
        <td>${esc(c.technician || "—")}</td>
        <td class="num">${c.measured_value ?? "—"}</td><td class="num">${c.nominal_value ?? "—"}</td>
        <td class="num">${c.tolerance ?? "—"}</td>
        <td class="muted">${esc(c.notes || "")}</td></tr>`).join("")}</tbody></table></div>`;
  }

  // ---- Calibrations (global) ----
  routes.calibrations = async () => {
    $app.innerHTML = `<div class="page-head"><h2>校准记录</h2><span class="sub">按设备 / 结果 / 时间范围查询</span></div>
      <div class="card"><div class="toolbar">
        <select id="fDev">${await deviceOptions()}</select>
        <select id="fResult"><option value="">全部结果</option><option value="pass">通过</option><option value="conditional">条件通过</option><option value="fail">不合格</option></select>
        <input type="date" id="fStart"><input type="date" id="fEnd">
        <button class="btn primary" id="fGo">查询</button>
      </div><div id="calList"><div class="empty">设置条件后点击“查询”</div></div></div>`;
    const run = async () => {
      const params = qs({ device_id: fDev.value, result: fResult.value,
        start: fStart.value ? fStart.value + "T00:00:00" : "",
        end: fEnd.value ? fEnd.value + "T23:59:59" : "" });
      document.getElementById("calList").innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
      const rows = await api("/calibrations" + params);
      document.getElementById("calList").innerHTML = calTable(rows) +
        `<div class="muted poll-note" style="margin-top:8px">共 ${rows.length} 条</div>`;
    };
    document.getElementById("fGo").addEventListener("click", run);
    document.getElementById("fDev").addEventListener("change", run);
    document.getElementById("fResult").addEventListener("change", run);
  };

  // ---- Events (global) ----
  routes.events = async () => {
    $app.innerHTML = `<div class="page-head"><h2>运行事件</h2>
        <button class="btn primary" id="newEvt">＋ 登记事件</button>
      </div>
      <div class="card" id="evtFormCard" style="display:none"><h3>登记事件</h3>
        <form id="evtFormG" class="grid-form">
          <div class="field"><label>设备 *</label><select name="device_id" required>${(await loadDevices()).map((d) => `<option value="${d.id}">${esc(d.code)} · ${esc(d.name)}</option>`).join("")}</select></div>
          <div class="field"><label>发生时间 *</label><input name="occurred_at" type="datetime-local" required></div>
          <div class="field"><label>等级 *</label><select name="severity"><option value="info">信息</option><option value="warning">警告</option><option value="critical">严重</option></select></div>
          <div class="field"><label>事件代码</label><input name="code" placeholder="E_OVERHEAT"></div>
          <div class="field" style="grid-column:1/-1"><label>描述 *</label><input name="message" required></div>
          <div class="field"><button class="btn primary" type="submit">保存</button></div>
        </form></div>
      <div class="card"><div class="toolbar">
        <select id="fDev">${await deviceOptions()}</select>
        <select id="fSev"><option value="">全部等级</option><option value="info">信息</option><option value="warning">警告</option><option value="critical">严重</option></select>
        <input type="date" id="fStart"><input type="date" id="fEnd">
        <button class="btn primary" id="fGo">查询</button>
      </div><div id="evtList"><div class="empty">设置条件后点击“查询”</div></div></div>`;

    const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    document.querySelector("#evtFormG [type=datetime-local]").value = nowLocal;
    document.getElementById("newEvt").addEventListener("click", () => {
      const c = document.getElementById("evtFormCard"); c.style.display = c.style.display === "none" ? "block" : "none";
    });
    document.getElementById("evtFormG").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/events", { method: "POST", body: JSON.stringify({
          device_id: Number(f.device_id.value), occurred_at: f.occurred_at.value,
          severity: f.severity.value, code: f.code.value, message: f.message.value }) });
        toast("事件已保存"); router();
      } catch (err) { toast(err.message, true); }
    });
    const run = async () => {
      const params = qs({ device_id: fDev.value, severity: fSev.value,
        start: fStart.value ? fStart.value + "T00:00:00" : "",
        end: fEnd.value ? fEnd.value + "T23:59:59" : "" });
      document.getElementById("evtList").innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
      const rows = await api("/events" + params);
      document.getElementById("evtList").innerHTML = eventTable(rows) +
        `<div class="muted poll-note" style="margin-top:8px">共 ${rows.length} 条</div>`;
    };
    document.getElementById("fGo").addEventListener("click", run);
    ["fDev", "fSev"].forEach((id) => document.getElementById(id).addEventListener("change", run));
  };

  function eventTable(rows) {
    if (!rows.length) return '<div class="empty">无事件</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>时间</th><th>设备</th><th>等级</th><th>代码</th><th>描述</th></tr></thead>
      <tbody>${rows.map((e) => `<tr>
        <td class="muted">${fmt(e.occurred_at)}</td>
        <td><a href="#/device/${e.device_id}">${esc(e.device_code || e.device_id)}</a></td>
        <td>${badge(e.severity)}</td>
        <td class="mono">${esc(e.code || "—")}</td>
        <td>${esc(e.message)}</td></tr>`).join("")}</tbody></table></div>`;
  }

  // ---- Issues ----
  routes.issues = async () => {
    $app.innerHTML = `<div class="page-head"><h2>待处理问题</h2><span class="sub">连续异常自动生成，校准结果驱动状态</span></div>
      <div class="card"><div class="toolbar">
        <select id="fStatus"><option value="">全部状态</option><option value="open">待处理</option><option value="monitoring">观察中</option><option value="resolved">已处理</option></select>
        <select id="fSev"><option value="">全部等级</option><option value="warning">警告</option><option value="critical">严重</option></select>
        <button class="btn primary" id="fGo">查询</button>
      </div><div id="issueList"><div class="empty"><span class="spinner"></span> 加载中…</div></div></div>`;
    const run = async () => {
      const rows = await api("/issues" + qs({ status: fStatus.value, severity: fSev.value }));
      document.getElementById("issueList").innerHTML = rows.length
        ? issueTable(rows) : '<div class="empty">无问题 🎉</div>';
      bindIssueRowClicks();
    };
    document.getElementById("fGo").addEventListener("click", run);
    document.getElementById("fStatus").addEventListener("change", run);
    document.getElementById("fSev").addEventListener("change", run);
    run();
  };

  routes.issue = async (id) => {
    const i = await api(`/issues/${id}`);
    if (!i) { $app.innerHTML = '<div class="card empty">问题不存在</div>'; return; }
    $app.innerHTML = `<span class="back-link" id="back">← 返回问题列表</span>
      <div class="page-head"><h2>${esc(i.title)}</h2> ${badge(i.status)} ${badge(i.severity)}</div>
      <div class="card issue-card ${esc(i.status)}">
        <div class="issue-meta">设备 <a href="#/device/${i.device_id}">${esc(i.device_code || i.device_id)}</a>
          · 规则 <span class="mono">${esc(i.rule)}</span> · 关联事件 ${i.event_count}</div>
        <div class="issue-desc">${esc(i.description || "")}</div>
        <div class="issue-meta">首次 ${fmt(i.first_event_at)} · 最近 ${fmt(i.last_event_at)}
          ${i.resolved_at ? `· 处理于 ${fmt(i.resolved_at)}` : ""}</div>
        ${i.resolution ? `<div class="issue-desc">处理说明：${esc(i.resolution)}</div>` : ""}
        <div class="toolbar">
          ${i.status !== "resolved"
            ? `<button class="btn primary" id="resolveBtn">标记已处理</button>`
            : `<button class="btn" id="reopenBtn">重新打开</button>`}
        </div>
      </div>
      <div class="card"><h3>关联事件 <span class="count">${i.events.length}</span></h3>
        ${timelineHtml(i.events.map((e) => ({ kind: "event", at: e.occurred_at, ...e })))}</div>`;
    document.getElementById("back").addEventListener("click", () => go("#/issues"));
    const rb = document.getElementById("resolveBtn");
    if (rb) rb.addEventListener("click", async () => {
      const resolution = prompt("处理说明：", "已现场处理并复测通过");
      if (resolution === null) return;
      try { await api(`/issues/${id}/resolve`, { method: "POST", body: JSON.stringify({ resolution }) });
        toast("已处理"); router(); } catch (e) { toast(e.message, true); }
    });
    const ob = document.getElementById("reopenBtn");
    if (ob) ob.addEventListener("click", async () => {
      try { await api(`/issues/${id}/reopen`, { method: "POST", body: JSON.stringify({}) });
        toast("已重新打开"); router(); } catch (e) { toast(e.message, true); }
    });
  };

  // ---- Imports ----
  routes.imports = async () => {
    const SAMPLE = `device_code,calibrated_at,result,technician,measured_value,nominal_value,tolerance,unit,notes
BAL-001,2026-09-08 09:00,pass,张工,200.000,200.0,0.005,g,常规校准
PH-014,2026-09-08 10:00,pass,王工,7.01,7.00,0.05,pH,复测通过
NEW-100,2026-09-08 11:00,pass,李工,10.0,10.0,0.1,mL,新设备(自动建档)
BAL-001,2026-09-08 09:00,pass,张工,200.000,200.0,0.005,g,重复行
PH-014,2026-09-08 12:00,maybe,王工,7.0,7.0,0.05,pH,非法结果
OVN-007,not-a-date,pass,赵工,,,,,错误时间`;
    $app.innerHTML = `<div class="page-head"><h2>导入校准记录</h2><span class="sub">CSV / TSV / JSON，字段校验 · 判重 · 错误明细</span></div>
      <div class="card">
        <div class="toolbar">
          <select id="fmt"><option value="csv">CSV</option><option value="tsv">TSV</option><option value="json">JSON</option></select>
          <label class="field check"><input type="checkbox" id="autoCreate" checked> 未知设备自动建档</label>
          <label class="field check"><input type="checkbox" id="sync"> 同步处理(调试)</label>
          <input type="file" id="file" accept=".csv,.tsv,.json,.txt">
          <button class="btn" id="sample">填入示例</button>
          <button class="btn primary" id="submit">开始导入</button>
        </div>
        <div class="field"><label>粘贴内容（或选择文件）</label>
          <textarea id="content" rows="8" style="width:100%;font-family:ui-monospace,Menlo,monospace;font-size:12px" placeholder="device_code,calibrated_at,result,technician,..."></textarea></div>
        <div id="importResult"></div>
      </div>
      <div class="card"><h3>最近导入</h3><div id="importHistory"><div class="empty"><span class="spinner"></span>…</div></div></div>`;

    document.getElementById("sample").addEventListener("click", () => {
      document.getElementById("content").value = SAMPLE;
      document.getElementById("fmt").value = "csv";
    });
    document.getElementById("file").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const text = await f.text();
      document.getElementById("content").value = text;
      if (f.name.endsWith(".json")) document.getElementById("fmt").value = "json";
      else if (f.name.endsWith(".tsv")) document.getElementById("fmt").value = "tsv";
      else document.getElementById("fmt").value = "csv";
    });
    document.getElementById("submit").addEventListener("click", async () => {
      const content = document.getElementById("content").value;
      if (!content.trim()) { toast("请粘贴或选择文件", true); return; }
      const fmt = document.getElementById("fmt").value;
      const auto = document.getElementById("autoCreate").checked;
      const sync = document.getElementById("sync").checked;
      const resBox = document.getElementById("importResult");
      resBox.innerHTML = '<div class="result-banner ok"><span class="spinner"></span> 已提交，处理中…</div>';
      try {
        const job = await api(`/imports?fmt=${fmt}&auto_create=${auto}&sync=${sync}&filename=upload.${fmt}`,
          { method: "POST", body: content });
        if (sync) { renderImport(job); loadHistory(); }
        else pollImport(job.id, resBox);
      } catch (e) {
        resBox.innerHTML = `<div class="result-banner err">导入失败：${esc(e.message)}</div>`;
      }
    });
    const loadHistory = async () => {
      const jobs = await api("/imports?limit=20");
      const el = document.getElementById("importHistory");
      el.innerHTML = jobs.length ? importTable(jobs) : '<div class="empty">暂无导入</div>';
      el.querySelectorAll("tr[data-import]").forEach((tr) =>
        tr.addEventListener("click", async () => {
          const job = await api(`/imports/${tr.dataset.import}`);
          renderImport(job);
          document.getElementById("importResult").scrollIntoView({ behavior: "smooth", block: "nearest" });
        }));
    };
    loadHistory();
    const timer = setInterval(loadHistory, 4000);
    onCleanup(() => clearInterval(timer));
  };

  const pollImport = (id, box) => {
    const tick = async () => {
      const job = await api(`/imports/${id}`);
      if (job.status === "done" || job.status === "failed") {
        clearInterval(t); renderImport(job);
        document.querySelector("#importHistory") && (api("/imports?limit=20").then((j) => {
          const el = document.getElementById("importHistory"); if (el) el.innerHTML = importTable(j);
        }));
        return;
      }
      box.innerHTML = `<div class="result-banner ok"><span class="spinner"></span> 后台处理中（${esc(job.status)}）…</div>`;
    };
    const t = setInterval(tick, 900);
    onCleanup(() => clearInterval(t));
    tick();
  };

  function renderImport(job) {
    const s = job.summary || {};
    const errs = job.errors || [];
    const box = document.getElementById("importResult");
    box.innerHTML = `
      <div class="result-banner ${s.errors || s.duplicate ? "err" : "ok"}">
        导入 #${job.id} 完成：共 <strong>${s.total ?? 0}</strong> 行 ·
        接受 <strong>${s.accepted ?? 0}</strong> ·
        判重 <strong>${s.duplicate ?? 0}</strong> ·
        错误 <strong>${s.errors ?? 0}</strong>
        ${(s.transitions || []).length ? `<br>状态联动：${s.transitions.map((t) => `问题#${t.issue_id} ${esc(STATUS_LABEL[t.from]||t.from)}→${esc(STATUS_LABEL[t.to]||t.to)}`).join("；")}` : ""}
      </div>
      ${errs.length ? `<div class="table-wrap"><table class="error-table">
        <thead><tr><th class="num">行号</th><th>类型</th><th>明细</th></tr></thead>
        <tbody>${errs.map((e) => `<tr>
          <td class="num">${e.row}</td>
          <td>${e.duplicate ? badge("duplicate") : badge("error")}</td>
          <td>${(e.errors || []).map(esc).join("<br>")}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
  }
  function importTable(jobs) {
    return `<div class="table-wrap"><table>
      <thead><tr><th class="num">#</th><th>文件</th><th>状态</th><th class="num">总计</th><th class="num">接受</th><th class="num">判重</th><th class="num">错误</th><th>完成时间</th></tr></thead>
      <tbody>${jobs.map((j) => `<tr class="clickable" data-import="${j.id}">
        <td class="num">${j.id}</td><td class="mono">${esc(j.filename || "—")}</td>
        <td>${badge(j.status === "done" ? "done" : j.status)}</td>
        <td class="num">${j.total_rows}</td><td class="num">${j.accepted_rows}</td>
        <td class="num">${j.duplicate_rows}</td>
        <td class="num">${j.error_rows ? `<span style="color:var(--critical)">${j.error_rows}</span>` : 0}</td>
        <td class="muted">${fmt(j.finished_at)}</td></tr>`).join("")}</tbody></table></div>`;
  }

  // ---- Background jobs ----
  routes.jobs = async () => {
    $app.innerHTML = `<div class="page-head"><h2>后台任务</h2>
      <span class="sub poll-note">失败重试 · 超时恢复 · 重复执行保护（自动刷新）</span></div>
      <div class="card"><div class="table-wrap"><table>
        <thead><tr><th class="num">#</th><th>任务</th><th>状态</th><th class="num">尝试</th><th>下次运行</th><th>心跳</th><th>最近错误 / 结果</th></tr></thead>
        <tbody id="jobBody"><tr><td colspan="7" class="empty"><span class="spinner"></span>…</td></tr></tbody>
      </table></div></div>`;
    const load = async () => {
      const jobs = await api("/jobs?limit=50");
      const body = document.getElementById("jobBody");
      if (!body) return;
      body.innerHTML = jobs.length ? jobs.map((j) => `<tr>
        <td class="num">${j.id}</td>
        <td class="mono">${esc(j.task_name)}${j.idempotency_key ? `<div class="muted" style="font-size:11px">${esc(j.idempotency_key)}</div>` : ""}</td>
        <td>${badge(j.status)}</td>
        <td class="num">${j.attempts}/${j.max_attempts}</td>
        <td class="muted">${j.status === "retrying" ? fmt(j.next_run_at) : "—"}</td>
        <td class="muted">${fmt(j.heartbeat_at)}</td>
        <td class="muted" style="max-width:340px">${esc(j.last_error || (j.result_json ? "✓ 完成" : "—"))}</td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty">暂无任务。去「导入」页发起一个。</td></tr>';
    };
    load();
    const t = setInterval(load, 3000);
    onCleanup(() => clearInterval(t));
  };

  // boot
  router();
})();
