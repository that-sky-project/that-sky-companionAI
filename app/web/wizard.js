/* 小光首次安装向导（W4b）：9 步，状态存 config.json，页面只做交互与轮询。 */
(function () {
  "use strict";

  const STEP_IDS = ["risk", "mumu", "apk", "data", "capture", "login", "ai", "owner", "finish"];
  const state = { summary: null, step: "risk", taskTimer: null, capTimer: null,
    taskSeen: false, aiFilled: false, pending: "" };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Native title-bar controls only; this bridge cannot read files or credentials.
  function setupWindowControls() {
    const controls = document.getElementById("window-controls") || document.querySelector(".window-controls");
    if (!controls) return;
    const defs = [["win-min", "minimize"], ["win-max", "toggle_maximize"], ["win-close", "close"]];
    const api = window.pywebview && window.pywebview.api;
    let available = 0;
    defs.forEach(([id, method]) => {
      const button = document.getElementById(id);
      if (!button) return;
      const enabled = !!(api && typeof api[method] === "function");
      button.disabled = !enabled;
      if (enabled) available += 1;
      if (button.dataset.nativeWired === "true") return;
      button.dataset.nativeWired = "true";
      button.addEventListener("click", () => {
        const current = window.pywebview && window.pywebview.api;
        if (!current || typeof current[method] !== "function") return;
        try {
          const result = current[method]();
          if (result && typeof result.catch === "function") result.catch(() => {});
        } catch (_) {}
      });
    });
    controls.hidden = available === 0;
  }

  function setupMotionPreference() {
    try {
      const value = window.localStorage.getItem("xg.desktop.motion");
      if (["auto", "soft", "off"].includes(value)) document.documentElement.setAttribute("data-motion", value);
    } catch (_) {}
  }

  async function api(path, opts) {
    const init = Object.assign({ credentials: "same-origin", cache: "no-store" }, opts || {});
    if (init.body && typeof init.body !== "string") {
      init.headers = Object.assign({ "Content-Type": "application/json" }, init.headers || {});
      init.body = JSON.stringify(init.body);
    }
    const r = await fetch(path, init);
    let body = {};
    try { body = await r.json(); } catch (e) { body = { ok: false, message: "接口返回的不是 JSON" }; }
    body.__http = r.status;
    return body;
  }

  function showResult(el, text, ok) {
    if (!el) return;
    el.hidden = false;
    el.className = "wz-result " + (ok ? "ok" : "bad");
    el.textContent = text || "";
  }

  function stepIndex(id) { return STEP_IDS.indexOf(id); }

  // 错误“详情”：错误码 + 异常类型 + 原始文本（后端已把路径里的用户名换成 <user>）。
  function detailLines(code, detail, extra) {
    const d = detail || {};
    const out = [];
    const c = code || d.code;
    if (c) out.push("错误码：" + c);
    if (d.type) out.push("类型：" + d.type);
    if (d.text) out.push(d.text);
    if (extra) out.push(extra);
    if (d.at) out.push("时间：" + d.at);
    return out.join("\n");
  }

  function go(id, push) {
    if (!STEP_IDS.includes(id)) id = "risk";
    state.step = id;
    document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== id; });
    document.querySelectorAll("#wz-steps li").forEach((li) => {
      const active = li.dataset.step === id;
      li.classList.toggle("active", active);
      if (active) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
    if (push !== false) history.replaceState(null, "", "#" + id);
    $("wz-prev").disabled = stepIndex(id) <= 0;
    $("wz-next").disabled = stepIndex(id) >= STEP_IDS.length - 1;
    $("wz-footer-note").textContent = "";
    if (id === "ai" && !state.aiFilled) loadAI();
    if (id === "login") ensureDevice();
    if (id === "capture") pollCapture();
    if (id === "data" && state.summary && state.summary.task) pollTask();
    if (id === "finish") renderSummary();
  }

  function render(summary) {
    if (!summary || summary.ok === false) return;
    state.summary = summary;
    const byId = {};
    (summary.steps || []).forEach((s) => { byId[s.id] = s; });
    document.querySelectorAll("#wz-steps li").forEach((li) => {
      const s = byId[li.dataset.step] || {};
      li.classList.toggle("done", !!s.done);
      li.title = s.done ? ("已完成" + (s.at ? "：" + s.at : "")) : (s.hint || "");
    });
    const doneCount = (summary.steps || []).filter((s) => s.done).length;
    $("wz-badge").textContent = summary.completed ? "已完成" : ("已完成 " + doneCount + " / 9");
    $("wz-risk-state").textContent = byId.risk && byId.risk.done
      ? "已同意（" + (byId.risk.at || "已记录") + "）。"
      : "尚未确认，请阅读说明后从告知页继续。";
    if (summary.apk && !$("wz-apk-path").value) $("wz-apk-path").value = summary.apk;
    if (summary.capture) renderCapture(summary.capture);
    if (summary.task) renderTask(summary.task);
    if (summary.device) renderDevice(summary.device);
    renderSummary();
  }

  function renderSummary() {
    const s = state.summary;
    if (!s) return;
    $("wz-summary").innerHTML = (s.steps || []).map((x) =>
      "<li>" + (x.done ? "✓ " : "· ") + esc(x.title) + (x.done ? "" : "（未完成）") + "</li>").join("");
  }

  async function refresh() {
    const s = await api("/api/wizard");
    if (s.ok === false && s.__http === 403) {
      $("wz-badge").textContent = "请从小光启动器重新打开";
      return s;
    }
    render(s);
    return s;
  }

  // ---------------------------------------------------------------- 长任务
  function renderTask(t) {
    if (!t) return;
    const p = t.total ? Math.round(100 * (t.done || 0) / t.total) : (t.running ? 0 : 100);
    $("wz-data-progress").value = Math.min(100, Math.max(0, p));
    $("wz-data-pct").textContent = t.total ? (t.done || 0) + " / " + t.total : (t.running ? "…" : "—");
    if (t.running) {
      $("wz-data-start").disabled = true;
      $("wz-task-stop").hidden = false;
      $("wz-data-msg").textContent = (t.msg || t.stage || "正在跑…") +
        (t.eta_s != null ? "（预计还要 " + Math.max(0, Math.round(t.eta_s / 60)) + " 分钟）" : "");
      $("wz-data-log").textContent = (t.lines || []).map((x) =>
        [x.stage, x.msg, x.done != null ? x.done + "/" + (x.total == null ? "?" : x.total) : ""]
          .filter(Boolean).join("  ")).join("\n");
    } else {
      $("wz-data-start").disabled = false;
      $("wz-task-stop").hidden = true;
      if (t.error) $("wz-data-msg").textContent = "失败：" + t.error + "（可以点“开始生成”重试，勾上续跑）";
      else if (t.ok) $("wz-data-msg").textContent = "完成了。";
    }
    const det = t.error_detail || {};
    if (!t.running && (t.error || det.text)) {
      $("wz-task-details").hidden = false;
      $("wz-task-detail").textContent = detailLines(t.error_code, det,
        t.log ? ("日志：" + t.log) : "");
    } else {
      $("wz-task-details").hidden = true;
    }
  }

  function stopTaskPoll() {
    if (state.taskTimer) { clearInterval(state.taskTimer); state.taskTimer = null; }
  }

  function pollTask(onDone) {
    stopTaskPoll();
    let quiet = 0;
    state.taskTimer = setInterval(async () => {
      const t = await api("/api/wizard/task");
      const task = t.task || null;
      if (task) {
        state.taskSeen = true;
        renderTask(task);
        if (!task.running) {
          stopTaskPoll();
          await refresh();
          if (onDone) onDone(task);
        }
      } else if (++quiet > 8) {
        stopTaskPoll();
      }
    }, 1500);
  }

  // ---------------------------------------------------------------- 第 2 步
  async function checkMumu() {
    const b = $("wz-mumu-check");
    b.disabled = true;
    showResult($("wz-mumu-result"), "正在检查 MuMu、adb、Root…", true);
    const r = await api("/api/wizard/mumu", { method: "POST", body: { adb: $("wz-adb").value.trim() } });
    b.disabled = false;
    const lines = [
      r.message || "",
      r.adb ? "adb：" + r.adb : "",
      r.serial ? "MuMu 实例：" + r.serial + "（游戏进程 " + r.pid + "）" : "",
      r.root ? "Root：已拿到" : "",
      r.version_code ? "游戏版本号：" + r.version_code : "",
      r.scene ? "当前图：" + r.scene : "",
    ].filter(Boolean);
    showResult($("wz-mumu-result"), lines.join("\n"), !!r.ok);
    if (r.ok) await refresh();
  }

  // ---------------------------------------------------------------- 第 3 步
  async function pullApk() {
    const b = $("wz-apk-pull");
    b.disabled = true;
    showResult($("wz-apk-result"), "已开始从 MuMu 拉安装包，进度在下面第 4 步的日志里；也可以先做别的。", true);
    const r = await api("/api/wizard/apk/pull", { method: "POST", body: {} });
    b.disabled = false;
    if (!r.ok) { showResult($("wz-apk-result"), r.message || "拉取失败", false); return; }
    pollTask(async (task) => {
      if (task && task.ok) {
        await refresh();
        const apk = (state.summary && state.summary.apk) || "";
        if (apk) {
          $("wz-apk-path").value = apk;
          await checkApk(true);
        }
      } else if (task && !task.ok) {
        showResult($("wz-apk-result"), "拉取失败：" + (task.error || "未知错误"), false);
      }
    });
  }

  async function checkApk(auto) {
    const path = $("wz-apk-path").value.trim();
    if (!path) { showResult($("wz-apk-result"), "先填 APK 路径，或点“从 MuMu 拉安装包”。", false); return; }
    const b = $("wz-apk-check");
    b.disabled = true;
    showResult($("wz-apk-result"), auto ? "拉取完成，正在校验安装包…" : "正在校验安装包…", true);
    const r = await api("/api/wizard/apk/check", { method: "POST", body: { path: path } });
    b.disabled = false;
    showResult($("wz-apk-result"), (r.ok ? "✓ " : "✗ ") + (r.message || ""), !!r.ok);
    if (r.ok) await refresh();
  }

  // ---------------------------------------------------------------- 第 4 步
  async function startData() {
    const b = $("wz-data-start");
    b.disabled = true;
    $("wz-data-msg").textContent = "正在启动…";
    const r = await api("/api/wizard/data/start", { method: "POST",
      body: { resume: $("wz-data-resume").checked } });
    b.disabled = false;
    if (!r.ok) { $("wz-data-msg").textContent = r.message || "启动失败"; return; }
    pollTask();
  }

  // ---------------------------------------------------------------- 第 5 步
  function platform() {
    const el = document.querySelector("input[name=wz-platform]:checked");
    return el ? el.value : "android";
  }

  // 私有登录服务负责设备登记；这里仅展示连接状态。
  function deviceLine(d) {
    if (!d) return "正在检查…";
    if (d.managed) return d.message || "设备由私有登录服务管理";
    if (d.registered) {
      return "已注册（" + (d.device_name || "WindowsPC") + "，id 末 4 位 " +
        (d.device_id_tail || "----") + "）" + (d.platform === "ios" ? " · iOS 档" : "");
    }
    return d.message || "未注册（首次登录会自动注册）";
  }

  function renderDevice(r) {
    const d = (r && (r.current || r.device)) || {};
    const el = $("wz-device-status");
    if (!el) return;
    el.textContent = deviceLine(d);
    if (r && r.pool) el.title = "设备池：" + r.pool.total + " 台（安卓 " +
      r.pool.android + " / iOS " + r.pool.ios + "）";
  }

  async function ensureDevice() {
    try {
      const r = await api("/api/device/ensure", { method: "POST", body: { platform: platform() } });
      renderDevice(r);
      return r;
    } catch (e) {
      $("wz-device-msg").textContent = e.message || "登录服务连接失败，请重试";
      return null;
    }
  }

  async function reRegisterDevice() {
    const b = $("wz-device-register");
    b.disabled = true;
    $("wz-device-msg").textContent = "正在检查私有登录服务连接…";
    try {
      const r = await api("/api/device/register", { method: "POST",
        body: { platform: platform(), confirm: true } });
      renderDevice(r);
      $("wz-device-msg").textContent = r.message || "服务连接正常";
    } catch (e) { $("wz-device-msg").textContent = e.message || "检查失败，请稍后重试"; }
    finally { b.disabled = false; }
  }

  function showRisk(info, retry) {
    if (!info) return;
    state.retry = retry || null;
    // 后台把短信号码/内容放在 verify（上行短信验证）或 device（设备风控）里，这里摊平
    const nested = info.verify || info.device || {};
    info = Object.assign({}, info, nested, {
      tips: nested.tips || info.tips || info.message,
      alt_number: nested.alt || info.alt_number || "",
    });
    const card = $("wz-risk-card");
    card.hidden = false;
    $("wz-risk-tips").textContent = info.tips || "按提示完成验证后回来重试。";
    const extra = $("wz-risk-extra");
    extra.innerHTML = "";
    const row = (label, value) => {
      const p = document.createElement("p");
      const b = document.createElement("strong");
      b.textContent = label + "：";
      const code = document.createElement("code");
      code.textContent = value;
      code.style.cssText = "font-size:1.15em;user-select:all;padding:2px 6px;border-radius:6px;background:rgba(0,0,0,.06)";
      const copy = document.createElement("button");
      copy.type = "button"; copy.className = "wz-btn"; copy.textContent = "复制";
      copy.style.marginLeft = "8px";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(value); copy.textContent = "已复制"; }
        catch (e) { copy.textContent = "请手动选中复制"; }
      });
      p.append(b, code, copy);
      extra.appendChild(p);
    };
    const isSms = (info.risk === "sms_verify") || (!info.risk && info.content);
    if (isSms && (info.content || info.number)) {
      const how = document.createElement("p");
      how.textContent = "请用这个游戏号绑定的手机，编辑一条短信：内容填下面的“短信内容”，发到下面的号码。发完回来点“我已发送，继续验证”。";
      extra.appendChild(how);
      if (info.content) row("短信内容", info.content);
      if (info.number) row("发送到", info.number);
      if (info.alt_number && info.alt_number !== info.number) row("发不出去可试", info.alt_number);
      if (info.number) {
        const a = document.createElement("a");
        a.href = "sms:" + info.number + "?body=" + encodeURIComponent(info.content || "");
        a.textContent = "（在手机上打开本页时，可直接点这里打开发短信）";
        a.className = "wz-note";
        extra.appendChild(a);
      }
    }
    if (info.url) {
      const p = document.createElement("p");
      if (!isSms) {
        const how = document.createElement("p");
        how.textContent = "网易没有给可发送的验证码，而是要求账号本人在网易的页面上处理（例如换设备、或在网易大神 App 里做账号修复）。请点下面的链接看网易具体要求，处理完再点“处理完了，重新登录”。";
        extra.appendChild(how);
      }
      const a = document.createElement("a");
      a.href = info.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.textContent = "打开网易的验证页面";
      p.appendChild(a);
      extra.appendChild(p);
    }
    if (!isSms) {
      const b = document.createElement("button");
      b.className = "wz-btn"; b.type = "button"; b.textContent = "处理完了，重新登录";
      b.addEventListener("click", async () => {
        $("wz-risk-card").hidden = true;
        if (state.retry) await state.retry();
      });
      extra.appendChild(b);
      return;
    }
    if (info.ticket) {
      const b = document.createElement("button");
      b.className = "wz-btn";
      b.type = "button";
      b.textContent = "我已发送，继续验证";
      b.addEventListener("click", async () => {
        b.disabled = true;
        $("wz-login-msg").textContent = "正在向网易确认…";
        const r = await api("/login/confirm-sms", { method: "POST", body: { ticket: info.ticket } });
        b.disabled = false;
        if (!r.ok) { $("wz-login-msg").textContent = r.message || "网易还没确认到短信，等一会儿再点一次"; return; }
        $("wz-risk-card").hidden = true;
        $("wz-login-msg").textContent = "验证通过，正在用刚才填的信息重新登录…";
        // 网易流程：上行短信验证通过后要再登录一次才拿到登录态（与服务器后台 riskSent 一致）
        if (r.account) { state.retry = null; await loginDone(); }
        else if (state.retry) await state.retry(); else await loginDone();
      });
      extra.appendChild(b);
    }
  }

  async function sendSms() {
    const mobile = $("wz-mobile").value.trim();
    if (!/^\d{11}$/.test(mobile)) { $("wz-login-msg").textContent = "手机号要 11 位数字"; return; }
    $("wz-login-msg").textContent = "正在连接私有登录服务…";
    if (!await ensureDevice()) { $("wz-login-msg").textContent = $("wz-device-msg").textContent || "登录服务暂不可用"; return; }
    const r = await api("/login/send-sms", { method: "POST", body: { mobile: mobile, platform: platform() } });
    $("wz-login-msg").textContent = r.message || (r.ok ? "验证码已发出" : "发送失败");
    if (r.ok) { $("wz-step2").hidden = false; state.pending = r.pending || r.ticket || ""; }
    if (r.manual) showRisk({ tips: r.message, content: r.manual.content, number: r.manual.number });
    if (r.need_risk || r.risk) showRisk(r);
  }

  async function verifySms() {
    const r = await api("/login/verify", { method: "POST",
      body: { pending: state.pending || "", code: $("wz-code").value.trim() } });
    $("wz-login-msg").textContent = r.message || (r.ok ? "登录成功" : "登录失败");
    if (r.need_risk || r.risk) showRisk(r, verifySms);
    if (r.ok) await loginDone();
  }

  async function emailLogin() {
    if (state.loginBusy) return;
    state.loginBusy = true;
    try { await emailLoginInner(); } finally { state.loginBusy = false; }
  }

  async function emailLoginInner() {
    $("wz-login-msg").textContent = "正在连接私有登录服务…";
    if (!await ensureDevice()) { $("wz-login-msg").textContent = $("wz-device-msg").textContent || "登录服务暂不可用"; return; }
    const r = await api("/login/email", { method: "POST",
      body: { email: $("wz-email").value.trim(), password: $("wz-password").value, platform: platform(),
        remember: !!($("wz-remember") && $("wz-remember").checked) } });
    $("wz-login-msg").textContent = r.message || (r.ok ? "登录成功" : "登录失败");
    if (r.need_risk || r.risk) showRisk(r, emailLogin);
    if (r.ok) await loginDone();
  }

  async function loginDone() {
    const r = await api("/api/wizard/login/done", { method: "POST", body: {} });
    $("wz-login-msg").textContent = r.message || (r.ok ? "这一步完成了" : "还没登录成功");
    if (r.ok) {
      $("wz-password").value = "";
      $("wz-code").value = "";
      state.retry = null;
      await refresh(); await ensureDevice();
    }
  }

  // ---------------------------------------------------------------- 第 5 步（录制遇境）
  function renderCapture(c) {
    if (!c) return;
    const label = { idle: "还没开始", starting: "正在启动", searching: "正在找 MuMu",
      ready: "就绪", waiting: "等进图", recording: "正在录", registering: "正在登记",
      error: "出错", stopped: "已停止" }[c.state] || c.state || "—";
    $("wz-cap-state").textContent = label;
    const m = c.mumu || {};
    $("wz-cap-mumu").textContent = m.serial ? (m.serial + "（进程 " + m.pid + "，Root " + (m.root ? "有" : "没有") + "）") : "—";
    $("wz-cap-scene").textContent = c.scene ? ((c.scene_name || c.scene) + "（" + c.scene + "）") : "—";
    $("wz-cap-recorded").textContent = (c.recorded && c.recorded.length) ? c.recorded.join("、") : "—";
    $("wz-cap-msg").textContent = c.last_error ? (c.last_error + "；" + (c.message || "")) : (c.message || "—");
    const running = !!(c.state && !["idle", "stopped"].includes(c.state));
    $("wz-cap-start").hidden = running;
    $("wz-cap-stop").hidden = !running;
    const capDet = c.last_error_detail || {};
    if (c.last_error || capDet.text) {
      $("wz-cap-details").hidden = false;
      $("wz-cap-detail").textContent = detailLines(c.last_error_code, capDet,
        c.last_record_dir ? ("录制目录：" + c.last_record_dir) : "");
    } else {
      $("wz-cap-details").hidden = true;
    }
    $("wz-cap-register").hidden = !(c.last_record_dir && !running &&
      (c.state === "error" || !!c.last_error));
    const cat = c.catalog || {};
    $("wz-catalog-start").hidden = !(cat.needed || (state.summary && state.summary.account));
  }

  async function pollCapture() {
    if (state.capTimer) clearInterval(state.capTimer);
    state.capTimer = setInterval(async () => {
      if (state.step !== "capture") { clearInterval(state.capTimer); state.capTimer = null; return; }
      const c = await api("/api/capture/status");
      if (c && !c.__http) renderCapture(c);
      const t = await api("/api/wizard/task");
      if (t && t.task) {
        renderTask(t.task);
        if (t.task.kind === "catalog" && t.task.running) {
          const p = t.task.total ? Math.round(100 * (t.task.done || 0) / t.task.total) : 0;
          $("wz-cat-progress").value = p;
          $("wz-cat-pct").textContent = t.task.total ? (t.task.done || 0) + " / " + t.task.total : "…";
          $("wz-cat-msg").textContent = t.task.msg || "正在生成地图目录…";
        } else if (t.task.kind === "catalog" && !t.task.running) {
          $("wz-catalog-start").disabled = false;
          $("wz-cat-msg").textContent = t.task.ok ? "地图目录生成完成。" :
            ("生成失败：" + (t.task.error || "未知错误") + "，可以重试。");
          if (!t.task.ok) {
            $("wz-cat-details").hidden = false;
            $("wz-cat-detail").textContent = detailLines(t.task.error_code,
              t.task.error_detail, t.task.log ? ("日志：" + t.task.log) : "");
          } else {
            $("wz-cat-details").hidden = true;
          }
          if (t.task.ok) refresh();
        }
      }
    }, 2000);
  }

  async function capStart() {
    const r = await api("/api/wizard/capture/start", { method: "POST", body: {} });
    $("wz-cap-msg").textContent = r.message || (r.ok ? "已开始" : "启动失败");
    if (!r.ok) $("wz-cap-msg").textContent += (r.detail ? ("：" + r.detail) : "");
    await refresh();
  }

  async function capStop() {
    const r = await api("/api/wizard/capture/stop", { method: "POST", body: {} });
    $("wz-cap-msg").textContent = r.message || "已停止";
    await refresh();
  }

  async function capRegister() {
    const b = $("wz-cap-register");
    b.disabled = true;
    $("wz-cap-msg").textContent = "正在重新登记已有录制…";
    const r = await api("/api/wizard/capture/register", { method: "POST", body: {} });
    $("wz-cap-msg").textContent = r.message || (r.ok ? "已重新登记" : "重新登记失败");
    if (!r.ok && r.detail) {
      $("wz-cap-details").hidden = false;
      $("wz-cap-detail").textContent = detailLines(r.detail.code, r.detail);
    }
    b.disabled = false;
    await refresh();
    if (r.ok) pollCapture();
  }

  async function catalogStart() {
    $("wz-catalog-start").disabled = true;
    const r = await api("/api/wizard/catalog/start", { method: "POST", body: {} });
    $("wz-cat-msg").textContent = r.message || (r.ok ? "已开始生成地图目录" : "启动失败");
    if (r.ok) pollTask();
    setTimeout(() => { $("wz-catalog-start").disabled = false; }, 3000);
  }

  // ---------------------------------------------------------------- 第 7 步
  async function loadAI() {
    state.aiFilled = true;
    const p = await api("/xiaoguang/ai/providers");
    const cur = await api("/xiaoguang/ai");
    if (!p.providers) return;
    $("wz-provider").innerHTML = p.providers.map((x) =>
      '<option value="' + esc(x.id) + '">' + esc(x.name) + "</option>").join("");
    const pick = (cur && cur.provider) || "deepseek";
    $("wz-provider").value = pick;
    $("wz-key-hint").textContent = (cur && cur.key_hint) || "未设置";
    $("wz-model").value = (cur && cur.model) || "";
    await fillProvider(cur && cur.base_url);
  }

  async function fillProvider(base) {
    const p = await api("/xiaoguang/ai/providers");
    const id = $("wz-provider").value;
    const item = (p.providers || []).find((x) => x.id === id) || {};
    $("wz-provider-note").textContent = item.note || "";
    $("wz-base").value = base || item.base_url || "";
    if (item.needs_key === false) $("wz-key-hint").textContent = "这个服务商不需要 Key";
  }

  async function aiModels() {
    $("wz-models-msg").textContent = "正在拉…";
    const r = await api("/xiaoguang/ai/models", { method: "POST", body: aiPayload() });
    if (!r.ok) { $("wz-models-msg").textContent = r.error || r.message || "拉取失败，可手填模型"; return; }
    const list = r.models || [];
    $("wz-model-list").innerHTML = list.map((m) => '<option value="' + esc(m) + '"></option>').join("");
    // 原生 datalist 在部分环境里展不开，这里同时渲染一个真正可点的列表。
    const menu = $("wz-model-menu");
    menu.innerHTML = list.map((m) => '<button type="button" class="wz-menu-item" data-v="' + esc(m) + '">' + esc(m) + "</button>").join("");
    menu.hidden = list.length === 0;
    $("wz-models-msg").textContent = "拉到 " + list.length + " 个，点下面的列表选一个";
    if (!$("wz-model").value && list.length) $("wz-model").value = list[0];
  }

  function aiPayload() {
    return { provider: $("wz-provider").value, base_url: $("wz-base").value.trim(),
      model: $("wz-model").value.trim(), key: $("wz-key").value };
  }

  async function aiTest() {
    $("wz-ai-msg").textContent = "正在测试…";
    const r = await api("/xiaoguang/ai/test", { method: "POST", body: aiPayload() });
    $("wz-ai-msg").textContent = r.ok ? (r.tool_support ? "支持工具调用" : "不支持工具调用，建议换模型") :
      (r.message || r.error || "测试失败");
    showResult($("wz-ai-result"), r.ok ?
      ("回复：" + (r.reply || "") + "\n耗时：" + (r.elapsed_s || "?") + " 秒；工具调用：" + (r.tool_support ? "支持" : "不支持")) :
      (r.message || r.error || "测试失败"), !!r.ok);
  }

  async function aiSave() {
    $("wz-ai-msg").textContent = "正在保存…";
    const r = await api("/xiaoguang/ai", { method: "POST", body: aiPayload() });
    $("wz-ai-msg").textContent = r.message || (r.ok ? "已保存" : "保存失败");
    if (r.ok) { $("wz-key").value = ""; await refresh(); }
  }

  async function aiDone() {
    const r = await api("/api/wizard/ai/done", { method: "POST", body: {} });
    $("wz-ai-msg").textContent = r.message || (r.ok ? "这一步完成了" : "还没配齐");
    if (r.ok) await refresh();
  }

  // ---------------------------------------------------------------- 第 8 步
  let friendsRecovering = false;
  async function recoverFriends() {
    if (friendsRecovering) return;
    if (!window.confirm("恢复会话会续期当前小光账号，可能使游戏里的旧登录退出。请确认已退出 MuMu 中的小光账号，再继续。")) return;
    friendsRecovering = true;
    const button = $("wz-friends-recover");
    const refreshButton = $("wz-friends-refresh");
    if (button) button.disabled = true;
    if (refreshButton) refreshButton.disabled = true;
    $("wz-friends-msg").textContent = "正在恢复会话，请等待；此操作只提交一次，最多约 6 分钟。";
    try {
      const r = await api("/renew", { method: "POST", body: { confirm: true, source: "friends" } });
      if (!r.ok) {
        $("wz-friends-msg").textContent = r.message || r.detail || "恢复未完成，请查看登录验证提示。";
        return;
      }
      await refreshFriends();
    } catch (e) {
      $("wz-friends-msg").textContent = "恢复结果尚未确认：" + (e.message || "网络连接中断") + "；不会自动重复提交。";
    } finally {
      friendsRecovering = false;
      if (button) button.disabled = false;
      if (refreshButton) refreshButton.disabled = false;
    }
  }

  async function refreshFriends() {
    $("wz-friends-msg").textContent = "正在读好友…";
    const r = await api("/friends");
    const recoveryButton = $("wz-friends-recover");
    if (recoveryButton) recoveryButton.hidden = r.recoverable !== true;
    const list = $("wz-friends");
    list.innerHTML = "";
    const rows = r.friends || r.rows || r.list || (Array.isArray(r) ? r : []);
    if (!r.ok) { $("wz-friends-msg").textContent = r.message || "读不到好友（先在第 6 步登录）"; return; }
    if (!rows.length) { $("wz-friends-msg").textContent = "好友列表是空的：先在游戏里互加好友。"; return; }
    $("wz-friends-msg").textContent = "点一个好友选为“哥哥”。";
    rows.forEach((f) => {
      const li = document.createElement("li");
      const name = f.name || f.nickname || f.alias || (f.uuid || "").slice(0, 8);
      li.innerHTML = "<span>" + esc(name) + "</span><small>" + esc((f.uuid || "").slice(0, 8)) + "</small>";
      const b = document.createElement("button");
      b.className = "wz-btn quiet";
      b.type = "button";
      b.textContent = "选他";
      b.addEventListener("click", () => {
        $("wz-owner-uuid").value = f.uuid || "";
        list.querySelectorAll("li").forEach((x) => x.classList.remove("sel"));
        li.classList.add("sel");
      });
      li.appendChild(b);
      list.appendChild(li);
    });
  }

  async function loadPersonas() {
    const r = await api("/personas");
    const sel = $("wz-persona");
    const rows = r.personas || r.rows || [];
    if (Array.isArray(rows)) {
      rows.forEach((p) => {
        const o = document.createElement("option");
        o.value = p.id || p.name || "";
        o.textContent = p.name || p.id || "人设";
        sel.appendChild(o);
      });
    }
  }

  async function ownerSave() {
    const r = await api("/api/wizard/owner", { method: "POST", body: {
      uuid: $("wz-owner-uuid").value.trim(), title: $("wz-title").value.trim(),
      persona: $("wz-persona").value } });
    $("wz-owner-msg").textContent = r.message || (r.ok ? "已保存" : "保存失败");
    if (r.ok) await refresh();
  }

  // ---------------------------------------------------------------- 第 9 步
  async function finishWizard() {
    const b = $("wz-finish");
    b.disabled = true;
    const r = await api("/api/wizard/finish", { method: "POST", body: {} });
    if (!r.ok) { $("wz-finish-msg").textContent = r.message || "还没完成"; b.disabled = false; await refresh(); return; }
    $("wz-finish-msg").textContent = "完成，正在让小光上线…";
    const go = await api("/xiaoguang/start", { method: "POST", body: {} });
    $("wz-finish-msg").textContent = go.ok ? "已提交上线，正在回遇境（约半分钟）。" :
      (go.message || "上线没成功，到面板里点“上线”再试。");
    setTimeout(() => { location.href = "/desktop"; }, 1200);
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    document.querySelectorAll("#wz-steps li").forEach((li) => {
      li.addEventListener("click", () => go(li.dataset.step));
      if (!li.querySelector("button,a")) {
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            go(li.dataset.step);
          }
        });
      }
    });
    $("wz-prev").addEventListener("click", () => go(STEP_IDS[Math.max(0, stepIndex(state.step) - 1)]));
    $("wz-next").addEventListener("click", () => go(STEP_IDS[Math.min(STEP_IDS.length - 1, stepIndex(state.step) + 1)]));

    $("wz-mumu-check").addEventListener("click", checkMumu);
    $("wz-apk-pull").addEventListener("click", pullApk);
    $("wz-apk-check").addEventListener("click", () => checkApk(false));
    $("wz-data-start").addEventListener("click", startData);
    $("wz-task-stop").addEventListener("click", async () => {
      await api("/api/wizard/task/stop", { method: "POST", body: {} });
      stopTaskPoll(); await refresh();
    });
    $("wz-tab-sms").addEventListener("click", () => {
      $("wz-pane-sms").hidden = false; $("wz-pane-email").hidden = true;
      $("wz-tab-sms").classList.add("active"); $("wz-tab-email").classList.remove("active");
    });
    $("wz-tab-email").addEventListener("click", () => {
      $("wz-pane-sms").hidden = true; $("wz-pane-email").hidden = false;
      $("wz-tab-email").classList.add("active"); $("wz-tab-sms").classList.remove("active");
    });
    $("wz-send").addEventListener("click", sendSms);
    $("wz-verify").addEventListener("click", verifySms);
    $("wz-email-btn").addEventListener("click", emailLogin);
    $("wz-login-done").addEventListener("click", loginDone);
    $("wz-device-register").addEventListener("click", reRegisterDevice);

    $("wz-cap-start").addEventListener("click", capStart);
    $("wz-cap-stop").addEventListener("click", capStop);
    $("wz-cap-register").addEventListener("click", capRegister);
    $("wz-catalog-start").addEventListener("click", catalogStart);

    $("wz-provider").addEventListener("change", () => fillProvider(""));
    $("wz-models").addEventListener("click", aiModels);
  $("wz-model").addEventListener("focus", () => {
    const m = $("wz-model-menu");
    if (m && m.innerHTML.trim()) m.hidden = false;
  });
  $("wz-model-menu").addEventListener("click", (e) => {
    const b = e.target.closest(".wz-menu-item");
    if (!b) return;
    $("wz-model").value = b.dataset.v || "";
    $("wz-model-menu").hidden = true;
  });
  document.addEventListener("click", (e) => {
    const menu = $("wz-model-menu");
    if (!menu) return;
    if (!e.target.closest("#wz-model-menu") && e.target.id !== "wz-model") menu.hidden = true;
  });
    $("wz-ai-test").addEventListener("click", aiTest);
    $("wz-ai-save").addEventListener("click", aiSave);
    $("wz-ai-done").addEventListener("click", aiDone);

    $("wz-friends-refresh").addEventListener("click", refreshFriends);
    const recoveryButton = document.createElement("button");
    recoveryButton.id = "wz-friends-recover";
    recoveryButton.type = "button";
    recoveryButton.className = "wz-btn quiet";
    recoveryButton.textContent = "恢复会话并重试";
    recoveryButton.hidden = true;
    recoveryButton.addEventListener("click", recoverFriends);
    $("wz-friends-refresh").insertAdjacentElement("afterend", recoveryButton);

    $("wz-owner-save").addEventListener("click", ownerSave);
    $("wz-finish").addEventListener("click", finishWizard);
  }

  async function boot() {
    setupMotionPreference();
    setupWindowControls();
    window.addEventListener("pywebviewready", setupWindowControls);
    bind();
    const s = await refresh();
    if (!s || s.ok === false) return;
    loadPersonas();
    const wanted = (location.hash || "").replace("#", "");
    go(STEP_IDS.includes(wanted) ? wanted : (s.current || (s.completed ? "finish" : "risk")), false);
    setInterval(refresh, 5000);
  }

  window.addEventListener("xg-service-account-changed", () => { refresh(); });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
