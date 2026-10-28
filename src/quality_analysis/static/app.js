(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  // Never put credentials in storage, URLs, window properties, logs or generated markup.
  let token = "";
  let identity = null;
  let exampleScope = null;
  let session = null;
  let generation = 0;
  let selection = 0;
  let busy = false;
  const pending = new Set();
  const statusNames = {
    awaiting_confirmation: "等待核心计划确认", completed: "本轮已完成", limited: "本轮证据有限",
    needs_clarification: "需要澄清，请明确范围后提交新请求", blocked: "执行已阻断",
    reconciliation_required: "推理结果未知，需显式对账，不自动重试", running: "执行中",
    preparing: "计划准备中", out_of_scope: "问题不在质量分析范围内"
  };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const pretty = (value) => JSON.stringify(value, null, 2);
  const providerLabel = (provider) => `${provider.label} (${provider.fingerprint})${provider.endpoints ? " · " + Object.entries(provider.endpoints).map(([key, value]) => `${key}=${value}`).join("；") : ""}`;
  const listValue = (id) => $(id).value.split(/[,，]/).map((v) => v.trim()).filter(Boolean);
  const id = () => crypto.randomUUID();

  function clearIdentity() {
    generation += 1;
    selection += 1;
    for (const controller of pending) controller.abort();
    pending.clear();
    token = "";
    identity = null;
    exampleScope = null;
    session = null;
    busy = false;
    $("token").value = "";
    $("workbench").hidden = true;
    $("identity").textContent = "尚未登录；角色与工厂授权以服务器为准。";
    for (const key of ["sessions", "report", "versions", "core-plan", "extended-plan", "followup-plan", "trace", "search-results", "audit", "calibration"]) $(key).replaceChildren();
    for (const key of ["session-scope", "session-error", "error", "notice"]) { $(key).textContent = ""; $(key).hidden = true; }
    for (const form of ["analyze-form", "followup-form", "document-form", "search-form", "acl-form", "calibration-form"]) $(form).reset();
    for (const key of ["confirm-form", "followup-form", "retry-button", "versions-panel", "audit-button", "audit", "document-editor", "document-admin", "calibration-panel"]) $(key).hidden = true;
    $("calls").replaceChildren();
    $("reconcile-form").reset();
    $("reconciliation-panel").hidden = true;
    $("reconcile-form").hidden = true;
    $("session-status").textContent = "请选择会话或生成新计划。";
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }

  function invalidateDocumentViews() {
    session = null;
    selection += 1;
    for (const key of ["search-results", "report", "versions", "core-plan", "extended-plan", "followup-plan", "trace", "audit", "sessions", "calibration"]) $(key).replaceChildren();
    for (const key of ["confirm-form", "followup-form", "retry-button", "versions-panel", "audit-button", "audit", "session-scope", "session-error", "calibration"]) $(key).hidden = true;
    $("calls").replaceChildren();
    $("reconcile-form").reset();
    $("reconciliation-panel").hidden = true;
    $("session-status").textContent = "文档或索引已变化；旧报告/计划已清除，请刷新会话验证快照或创建新分析。";
  }

  async function api(path, body, method = body === undefined ? "GET" : "POST") {
    const epoch = generation;
    const controller = new AbortController();
    pending.add(controller);
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal, credentials: "omit", cache: "no-store"});
      const payload = await response.json();
      if (epoch !== generation) throw new DOMException("身份已变化", "AbortError");
      if (!response.ok) {
        if (response.status === 401) {
          clearIdentity();
          $("error").textContent = "身份令牌无效或已撤销；已清除当前身份与页面数据。";
          $("error").hidden = false;
          throw new DOMException("认证已失效", "AbortError");
        }
        throw new Error(payload.error?.message || "请求失败，未获得可用结果");
      }
      return payload;
    } finally { pending.delete(controller); }
  }

  async function act(work) {
    if (busy) return;
    const epoch = generation;
    busy = true;
    $("error").hidden = true;
    $("notice").hidden = true;
    document.querySelectorAll("#workbench button").forEach((button) => { button.disabled = true; });
    try { await work(); }
    catch (error) {
      if (error.name !== "AbortError" && epoch === generation) {
        $("error").textContent = error.message || "操作失败";
        $("error").hidden = false;
      }
    } finally {
      if (epoch === generation) {
        busy = false;
        document.querySelectorAll("#workbench button").forEach((button) => { button.disabled = false; });
      }
    }
  }

  function notice(message) { $("notice").textContent = message; $("notice").hidden = false; }
  function scope() {
    const result = {plant_id: $("plant").value.trim(), product_id: $("product").value.trim(), start_date: $("start").value, end_date: $("end").value, metric_id: $("metric").value, line_ids: listValue("lines"), min_group_n: Number($("min-n").value)};
    if (!result.plant_id || !result.product_id || !result.start_date || !result.end_date || !result.metric_id) throw new Error("必须明确填写工厂、产品、开始日期、结束日期与指标。");
    if (result.end_date <= result.start_date) throw new Error("结束日期必须晚于开始日期（结束日期不包含）。");
    return result;
  }

  function planCard(sq, selectable) {
    const card = element("label", undefined, "sq-card");
    if (selectable) {
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.value = sq.sq_id;
      // Even core execution needs a deliberate checkbox selection and confirmation.
      card.append(checkbox);
    }
    card.append(element("strong", `${sq.sq_id} · ${sq.title}`));
    card.append(element("span", `${sq.tier === "core" ? "核心" : "扩展（不自动执行）"} · 来源：${sq.sources.join("、")} · SQL分组授权：${sq.group_by || "总体（不分组）"} · 依赖：${sq.depends_on.join("、") || "无"}`));
    return card;
  }

  function renderReport(report, target, heading) {
    target.append(element("h3", heading));
    if (report.stale) target.append(element("p", report.reason || "此报告快照已过期；不得当作当前有效证据，请新建分析。", "error"));
    if (report.round_id) target.append(element("p", `轮次 ${report.round_id} · provider ${report.provider}`));
    for (const section of report.sections || []) {
      const block = element("article", undefined, "report-section");
      block.append(element("h4", `${section.sq_id} · ${section.outcome}`));
      block.append(element("p", `当前修订：${section.revision_id}`));
      if (!(section.evidence || []).length) block.append(element("p", "没有可支持本节的有效证据。", "notice"));
      for (const evidence of section.evidence || []) {
        const detail = element("details");
        detail.append(element("summary", `${evidence.evidence_id} · ${evidence.summary}`));
        detail.append(element("p", `来源：${evidence.source}`));
        detail.append(element("pre", pretty(evidence.payload)));
        if (evidence.citations?.length) detail.append(element("pre", pretty(evidence.citations)));
        block.append(detail);
      }
      target.append(block);
    }
    target.append(element("h4", "未执行项（不是分析结论）"));
    const remaining = element("ul");
    for (const sq of report.unexecuted || []) remaining.append(element("li", `${sq.sq_id} · ${sq.title}`));
    if (!remaining.childElementCount) remaining.append(element("li", "无；以本轮明确授权范围为准。"));
    target.append(remaining);
    for (const limitation of report.limitations || []) target.append(element("p", limitation, "muted"));
  }

  function renderSession(value) {
    session = value;
    selection += 1;
    $("session-status").textContent = `${value.session_id} · ${statusNames[value.status] || value.status} · 模型调用 ${value.calls ?? 0} / 工具 ${value.tools ?? 0} / token ${value.tokens ?? 0}`;
    $("session-scope").textContent = pretty(value.request);
    $("session-scope").hidden = false;
    $("session-error").textContent = value.last_error ? `${value.last_error.code}：${value.last_error.message}` : "";
    $("session-error").hidden = !value.last_error;
    $("retry-button").hidden = !["running", "preparing", "reconciliation_required"].includes(value.status) && !(value.status === "blocked" && ["provider_unavailable", "provider_http"].includes(value.last_error?.code));
    $("trace").replaceChildren(...(value.trace || []).map((name) => element("span", name)));
    for (const key of ["core-plan", "extended-plan", "followup-plan", "report", "versions", "audit"]) $(key).replaceChildren();
    $("audit").hidden = true;
    $("calls").replaceChildren();
    $("calls").hidden = true;
    $("reconcile-form").reset();
    $("reconciliation-panel").hidden = false;
    $("reconcile-form").hidden = identity?.role !== "admin";
    const subquestions = value.plan?.subquestions || [];
    for (const sq of subquestions) {
      $(sq.tier === "core" ? "core-plan" : "extended-plan").append(planCard(sq, sq.tier === "core"));
      $("followup-plan").append(planCard(sq, true));
    }
    $("confirm-form").hidden = value.status !== "awaiting_confirmation" || !value.plan_fingerprint;
    if (value.status === "awaiting_confirmation" && !value.plan_fingerprint) {
      $("session-error").textContent = "计划快照已失效，内容不再展示；请使用当前范围创建新分析。";
      $("session-error").hidden = false;
    }
    $("followup-form").hidden = !["completed", "limited"].includes(value.status) || Boolean(value.report?.stale);
    $("followup-question").value = "";
    $("audit-button").hidden = false;
    if (value.report) renderReport(value.report, $("report"), "本轮最新报告");
    const versions = value.versions || [];
    $("versions-panel").hidden = !versions.length;
    for (const version of versions) {
      const detail = element("details");
      detail.append(element("summary", `轮次 ${version.ordinal ?? ""} · ${version.round_id} · ${version.status}`));
      if (version.report) renderReport(version.report, detail, "历史报告（仅历史参考）");
      else detail.append(element("p", "本轮没有生成报告。"));
      $("versions").append(detail);
    }
  }

  async function sessions() {
    const payload = await api("/api/sessions");
    $("sessions").replaceChildren();
    for (const item of payload.sessions) {
      const row = element("li");
      const button = element("button", `${item.request?.question || item.session_id} · ${statusNames[item.status] || item.status}`);
      button.type = "button";
      button.addEventListener("click", () => act(async () => { renderSession(await api(`/api/sessions/${encodeURIComponent(item.session_id)}`)); }));
      row.append(button);
      $("sessions").append(row);
    }
    if (!payload.sessions.length) $("sessions").append(element("li", "当前身份暂无分析会话。"));
  }
  const chosen = (container) => [...$(container).querySelectorAll("input:checked")].map((input) => input.value);

  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const candidate = $("token").value;
    clearIdentity();
    token = candidate;
    act(async () => {
      const payload = await api("/api/me");
      identity = payload.actor;
      exampleScope = payload.example_scope;
      $("identity").textContent = `${identity.actor_id} · ${identity.role} · 授权工厂：${identity.plants.join("、")}`;
      $("provider-label").textContent = providerLabel(payload.provider);
      $("workbench").hidden = false;
      $("document-editor").hidden = !["analyst", "admin"].includes(identity.role);
      $("document-admin").hidden = identity.role !== "admin";
      $("calibration-panel").hidden = identity.role !== "admin";
      await sessions();
    });
  });
  // Starting to replace an identity immediately clears all old identity data.
  $("token").addEventListener("input", () => {
    if (token || identity) { const candidate = $("token").value; clearIdentity(); $("token").value = candidate; }
  });
  $("logout").addEventListener("click", clearIdentity);
  window.addEventListener("pagehide", clearIdentity);
  $("example").addEventListener("click", () => {
    if (!exampleScope) return notice("此身份没有可用的合成示例范围，请手动明确填写。");
    for (const [key, source] of [["plant", "plant_id"], ["product", "product_id"], ["start", "start_date"], ["end", "end_date"], ["metric", "metric_id"], ["min-n", "min_group_n"]]) $(key).value = exampleScope[source] ?? (key === "min-n" ? 20 : "");
    $("lines").value = (exampleScope.line_ids || []).join(",");
    notice("已按你的显式操作填入合成示例范围，请检查后提交。此操作没有生成计划或执行分析。");
  });
  $("refresh-sessions").addEventListener("click", () => act(async () => {
    const current = await api("/api/me");
    if (pretty(current.actor) !== pretty(identity)) {
      clearIdentity();
      notice("服务器身份或授权已变化；旧令牌与页面数据已清除，请重新验证身份。");
      return;
    }
    await sessions();
    if (session) renderSession(await api(`/api/sessions/${encodeURIComponent(session.session_id)}`));
  }));
  $("analyze-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => { renderSession(await api("/api/sessions", {request_id: id(), question: $("question").value, scope: scope()})); await sessions(); });
  });
  $("confirm-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const selected = chosen("core-plan");
      if (!selected.length) throw new Error("请明确勾选至少一个核心 SQ。");
      renderSession(await api(`/api/sessions/${encodeURIComponent(session.session_id)}/confirm`, {action_id: id(), selected_sq_ids: selected, plan_fingerprint: session.plan_fingerprint}));
      await sessions();
    });
  });
  $("followup-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const selected = chosen("followup-plan");
      if (!selected.length) throw new Error("请明确勾选本次追问授权的 SQ。");
      renderSession(await api(`/api/sessions/${encodeURIComponent(session.session_id)}/followup`, {request_id: id(), question: $("followup-question").value, selected_sq_ids: selected}));
      await sessions();
    });
  });
  $("retry-button").addEventListener("click", () => {
    if (!session || !window.confirm("显式请求恢复当前会话？将沿用原授权范围与预算。未知推理结果会阻止重试，必须先由管理员对账。")) return;
    act(async () => {
      renderSession(await api(`/api/sessions/${encodeURIComponent(session.session_id)}/retry`, {action_id: id()}));
      await sessions();
    });
  });
  $("calls-button").addEventListener("click", () => act(async () => {
    const current = selection;
    const result = await api(`/api/sessions/${encodeURIComponent(session.session_id)}/calls`);
    if (current !== selection) return;
    $("calls").textContent = pretty(result.calls);
    $("calls").hidden = false;
  }));
  $("reconcile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const withoutResult = $("reconcile-no-result").checked;
      const raw = $("reconcile-result").value.trim();
      if (withoutResult === Boolean(raw)) throw new Error("请选择提交真实回执或明确授权无结果重试，不能同时选择。");
      let result = null;
      if (raw) {
        try { result = JSON.parse(raw); }
        catch { throw new Error("回执必须是有效JSON对象；不得猜测内容或用量。"); }
      }
      const resolved = await api(`/api/sessions/${encodeURIComponent(session.session_id)}/calls/${encodeURIComponent($("reconcile-call").value)}/resolve`, {note: $("reconcile-note").value, result, authorize_retry_without_result: withoutResult});
      $("reconcile-form").reset();
      $("calls").textContent = pretty(resolved);
      $("calls").hidden = false;
      notice("对账结果已记录。未执行任何重试；需要时请另行显式请求恢复。");
    });
  });
  $("audit-button").addEventListener("click", () => act(async () => {
    const current = selection;
    const payload = await api(`/api/sessions/${encodeURIComponent(session.session_id)}/audit`);
    if (current !== selection) return;
    $("audit").textContent = pretty(payload.audit);
    $("audit").hidden = false;
  }));
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const result = await api("/api/documents/search", {query: $("search-query").value, scope: scope()});
      $("search-results").replaceChildren(element("p", `校准版本：${result.calibration_version} · 索引修订：${result.index_revision} · ${result.low_prior ? "低先验，不能当作充分证据" : "检索候选，仍需取证验证"}`));
      for (const hit of result.hits || []) {
        const detail = element("details");
        detail.append(element("summary", `${hit.document_id} v${hit.version} · ${hit.chunk_id} · 得分 ${hit.score}`));
        detail.append(element("pre", hit.text));
        detail.append(element("p", `哈希 ${hit.content_hash} · 原文偏移 ${hit.quote_start}–${hit.quote_end} · 渠道 ${hit.channels.join("、")}`));
        const drill = element("button", "校验当前原文引用");
        drill.type = "button";
        drill.addEventListener("click", () => act(async () => {
          const checked = await api(`/api/documents/${encodeURIComponent(hit.document_id)}/chunks/${encodeURIComponent(hit.chunk_id)}`);
          detail.append(element("pre", pretty(checked)));
        }));
        detail.append(drill);
        $("search-results").append(detail);
      }
      if (!result.hits?.length) $("search-results").append(element("p", "没有可见命中。"));
    });
  });
  $("document-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const epoch = generation;
      const summaries = {};
      for (const key of ["phenomenon", "context", "hypothesis", "evidence", "action"]) summaries[key] = $(`summary-${key}`).value;
      const payload = {document_id: $("document-id").value, title: $("document-title").value, plant_id: $("document-plant").value, shared_with: listValue("document-shared"), summaries};
      const file = $("document-file").files[0];
      let endpoint = "ingest";
      if (file) {
        if (file.size > 750000) throw new Error("文件过大；编码后整个 JSON 请求体必须不超过1 MiB。");
        const bytes = new Uint8Array(await file.arrayBuffer());
        if (epoch !== generation) return;
        let binary = "";
        for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
        payload.filename = file.name;
        payload.content_base64 = btoa(binary);
        endpoint = "upload";
      } else payload.text = $("document-text").value;
      if (new TextEncoder().encode(JSON.stringify(payload)).length > 1048576) throw new Error("整个请求体超过1 MiB，请减少文件或正文大小。");
      const result = await api(`/api/documents/${endpoint}`, payload);
      notice(`入库完成：${pretty(result)}。索引变化可能使已有计划或报告快照失效。`);
      invalidateDocumentViews();
    });
  });
  $("acl-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      await api(`/api/documents/${encodeURIComponent($("acl-id").value)}/acl`, {shared_with: listValue("acl-shared")}, "PATCH");
      invalidateDocumentViews();
      notice("ACL 已更新；旧检索结果已清除，请重新检索。既有会话快照可能已失效。");
    });
  });
  $("delete-document").addEventListener("click", () => {
    const documentId = $("acl-id").value;
    if (!documentId || !window.confirm(`确定删除报告 ${documentId}？旧引用会失效。`)) return;
    act(async () => {
      await api(`/api/documents/${encodeURIComponent(documentId)}`, undefined, "DELETE");
      invalidateDocumentViews();
      notice("报告已删除；旧结果已清除，请重新分析以获得当前证据。");
    });
  });
  $("calibration-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(async () => {
      const original = $("calibration-original").checked;
      if (original && $("calibration-cases").value.trim()) throw new Error("原创标签与自定义标签只能选择其一。");
      let cases = null;
      if (!original) {
        try { cases = JSON.parse($("calibration-cases").value); }
        catch { throw new Error("请提供有效的CalibrationCase数组JSON，或明确勾选原创合成标签。"); }
        if (!Array.isArray(cases)) throw new Error("校准标签必须是JSON数组。");
      }
      const result = await api("/api/documents/calibrate", {plant_id: $("calibration-plant").value, cases, use_original_cases: original, install: $("calibration-install").checked});
      $("calibration").textContent = pretty(result);
      $("calibration").hidden = false;
      $("search-results").replaceChildren();
      notice(result.installed ? "实测校准已明确安装；请重新检索。结果不代表生产验收。" : "实测完成但未安装；检索阈值未改变。");
    });
  });
  const healthEpoch = generation;
  api("/api/health").then((health) => {
    if (healthEpoch === generation && !identity) $("provider-label").textContent = `${providerLabel(health.provider)} · 仅进程健康，未运行推理验收`;
  }).catch((error) => {
    if (error.name !== "AbortError" && healthEpoch === generation) $("provider-label").textContent = "无法读取服务配置；未确认模型状态。";
  });
})();
