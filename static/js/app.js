(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let daycut = null;
  let reports = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };
  let cutForm = { current_biz_date: "", policy: "拒写" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || j.detail || ("HTTP " + r.status));
      err.payload = j;
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    daycut = await api("/api/day-cut");
    if (!cutForm.current_biz_date) {
      cutForm.current_biz_date = daycut.settings.current_biz_date;
      cutForm.policy = daycut.settings.policy;
    }
    reports = (await api("/api/visibility-reports")).items || [];
  }

  function isStale(b) {
    return daycut && b.biz_date < daycut.settings.current_biz_date;
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      const r = await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      const moved = r.object && r.object.fate === "挪批";
      await refresh();
      await openBatch(selected);
      setMsg(
        moved
          ? `跨日挪批：对象已移入今日批 #${r.object.target_batch_id}，昨档对象数未增`
          : "已登记对象"
      );
      render();
    } catch (e) {
      const p = e.payload || {};
      if (p.fate === "拒写") {
        setMsg("跨日拒写：" + e.message + "（已留拒写标记）");
        await refresh();
        render();
        return;
      }
      if (p.fate === "挪批") {
        setMsg("跨日挪批失败：" + e.message);
        await refresh();
        render();
        return;
      }
      setMsg(e.message);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({
          as_of_date: (daycut && daycut.settings.current_biz_date) || "2026-11-16",
        }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doSaveCut() {
    setMsg("");
    try {
      const r = await api("/api/day-cut/settings", {
        method: "POST",
        body: JSON.stringify({
          current_biz_date: cutForm.current_biz_date,
          policy: cutForm.policy,
        }),
      });
      cutForm.current_biz_date = r.setting.current_biz_date;
      cutForm.policy = r.setting.policy;
      await refresh();
      setMsg("日切设置已保存：" + r.setting.current_biz_date + " · " + r.setting.policy);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doAdvance(target) {
    setMsg("");
    try {
      const r = await api("/api/day-cut/advance", {
        method: "POST",
        body: JSON.stringify({ biz_date: target }),
      });
      cutForm.current_biz_date = r.setting.current_biz_date;
      await refresh();
      setMsg(r.run.detail);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doCheck() {
    setMsg("");
    try {
      const r = await api("/api/day-cut/check", {
        method: "POST",
        body: JSON.stringify({
          as_of_date: (daycut && daycut.settings.current_biz_date) || "",
        }),
      });
      await refresh();
      setMsg(r.check.detail);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <p class="muted">当前业务日 ${daycut ? daycut.settings.current_biz_date : "-"} · 跨日策略 ${
      daycut ? daycut.settings.policy : "-"
    }</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}${isStale(b) ? ' <span class="pill warn">昨档</span>' : ""}</td>
            <td><span class="pill">${b.status}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function fateHint(b) {
    if (!daycut) return "";
    if (b.biz_date >= daycut.settings.current_biz_date) return "";
    if (daycut.settings.policy === "拒写") {
      return `<p class="fate warn">跨日拒写：此批业务日 ${b.biz_date} 早于当前 ${daycut.settings.current_biz_date}，登记将整笔不落库，仅留拒写标记。</p>`;
    }
    return `<p class="fate">跨日挪批：此批业务日 ${b.biz_date} 早于当前 ${daycut.settings.current_biz_date}，登记将原子挪入今日空批，昨档对象数不增。</p>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const mixed = detail.mixed_count || 0;
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="pill">${b.status}</span>
        ${detail.is_stale ? '<span class="pill warn">昨档</span>' : ""}
        ${mixed ? `<span class="pill warn">混日拦截 ${mixed}</span>` : ""}
      </p>
      ${fateHint(b)}
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${form.object_key}" /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" /></label>
        <button type="button" id="btn-register">写入登记</button>
        <p class="muted">点登记按当前命运一次处理，不会两种结果并存。</p>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>归属日</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${o.object_key}</td><td>${o.content_hash}</td><td>${o.byte_size}</td>
            <td>${o.biz_date}${o.biz_date !== b.biz_date ? ' <span class="pill warn">混日</span>' : ""}</td>
            <td>${o.status}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDayCut() {
    if (!daycut) return `<h2>日切设置</h2>`;
    const s = daycut.settings;
    const nextDay = nextBizDate(s.current_biz_date);
    return `
      <h2>日切设置</h2>
      <p>当前业务日 <span class="pill">${s.current_biz_date}</span>
         跨日策略 <span class="pill">${s.policy}</span></p>
      <div class="panel">
        <h3>调整</h3>
        <label>业务日<input id="f-cut-date" value="${cutForm.current_biz_date}" /></label>
        <label>跨日命运
          <select id="f-cut-policy">
            <option value="拒写" ${cutForm.policy === "拒写" ? "selected" : ""}>拒写（整笔不落库）</option>
            <option value="挪批" ${cutForm.policy === "挪批" ? "selected" : ""}>挪批（原子挪入今日空批）</option>
          </select>
        </label>
        <div class="row-actions">
          <button type="button" id="btn-cut-save">保存设置</button>
          <button type="button" class="secondary" id="btn-cut-advance">增量日切到 ${nextDay}</button>
          <button type="button" class="secondary" id="btn-cut-check">跑边界核对</button>
        </div>
      </div>
      <h3>跨日标记</h3>
      ${
        daycut.events.length
          ? `<table>
        <thead><tr><th>编号</th><th>业务日</th><th>命运</th><th>来源批</th><th>目标批</th><th>对象键</th><th>说明</th></tr></thead>
        <tbody>${daycut.events
          .map(
            (e) => `<tr>
          <td>${e.id}</td><td>${e.biz_date}</td>
          <td><span class="pill ${e.fate === "拒写" ? "warn" : ""}">${e.fate}</span></td>
          <td>#${e.source_batch_id}</td><td>${e.target_batch_id ? "#" + e.target_batch_id : "-"}</td>
          <td>${e.object_key}</td><td class="muted">${e.detail}</td>
        </tr>`
          )
          .join("")}</tbody>
      </table>`
          : `<p class="muted">暂无跨日标记。</p>`
      }
      <h3>日切记录</h3>
      ${
        daycut.stale_batches.length
          ? `<p class="fate warn">仍有 ${daycut.stale_batches.length} 个昨档批次：${daycut.stale_batches
              .map((b) => b.zone_code + "(" + b.biz_date + ")")
              .join("、")}</p>`
          : `<p class="muted">没有落后于当前业务日的批次。</p>`
      }
      ${
        daycut.runs.length
          ? `<table>
        <thead><tr><th>编号</th><th>业务日</th><th>策略</th><th>新开批</th><th>说明</th></tr></thead>
        <tbody>${daycut.runs
          .map(
            (r) => `<tr>
          <td>${r.id}</td><td>${r.biz_date}</td><td>${r.policy}</td>
          <td>${r.created_batch_count}</td><td class="muted">${r.detail}</td>
        </tr>`
          )
          .join("")}</tbody>
      </table>`
          : ""
      }
      <h3>边界核对</h3>
      ${
        daycut.checks.length
          ? `<table>
        <thead><tr><th>编号</th><th>截至</th><th>混日</th><th>对象</th><th>说明</th></tr></thead>
        <tbody>${daycut.checks
          .map(
            (c) => `<tr>
          <td>${c.id}</td><td>${c.as_of_date}</td>
          <td>${c.mixed_count ? `<span class="pill warn">${c.mixed_count}</span>` : "0"}</td>
          <td>${c.object_count}</td><td class="muted">${c.detail}</td>
        </tr>`
          )
          .join("")}</tbody>
      </table>`
          : `<p class="muted">尚未核对。</p>`
      }`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>混日</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td>
            <td>${r.mixed_object_count ? `<span class="pill warn">${r.mixed_object_count}</span>` : "0"}</td>
            <td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function nextBizDate(day) {
    const d = new Date(day + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "daycut") view.innerHTML = renderDayCut();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const cutDate = document.getElementById("f-cut-date");
    const cutPolicy = document.getElementById("f-cut-policy");
    if (cutDate) cutDate.oninput = () => (cutForm.current_biz_date = cutDate.value);
    if (cutPolicy) cutPolicy.onchange = () => (cutForm.policy = cutPolicy.value);
    const saveBtn = document.getElementById("btn-cut-save");
    if (saveBtn) saveBtn.onclick = doSaveCut;
    const advBtn = document.getElementById("btn-cut-advance");
    if (advBtn) {
      advBtn.onclick = () =>
        doAdvance(nextBizDate(daycut.settings.current_biz_date));
    }
    const checkBtn = document.getElementById("btn-cut-check");
    if (checkBtn) checkBtn.onclick = doCheck;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
