/* W4b：主页面上的“退出小光”、开机自启动、未完成向导时转去向导。 */
(function () {
  "use strict";

  async function j(path, opts) {
    const r = await fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store" }, opts || {}));
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.message || body.error || ("HTTP " + r.status));
    return body;
  }

  // 首次安装还没走完向导：直接转到向导页（不打断 W1/W2 的页面内容测试）。
  async function maybeWizard() {
    if (location.pathname !== "/" && location.pathname !== "/index.html") return;
    try {
      const w = await j("/api/wizard");
      if (w && w.completed === false) location.replace("/wizard");
    } catch (e) { /* 403/没登录就留在面板 */ }
  }

  function bindQuit() {
    const b = document.getElementById("b-quit");
    if (!b) return;
    b.addEventListener("click", async () => {
      if (!confirm("退出小光？会先停小光（长连接/聊天）和后台，再关掉后台页面。")) return;
      b.disabled = true;
      b.textContent = "正在退出…";
      try {
        await j("/api/quit", { method: "POST", headers: { "Content-Type": "application/json" },
          body: "{}", keepalive: true });
        document.body.innerHTML = '<main style="max-width:640px;margin:80px auto;font:16px/1.8 sans-serif;padding:0 20px">' +
          '<h1>小光已退出</h1><p>长连接、聊天、后台都已经停了，可以关掉这个页面。</p>' +
          '<p>下次双击桌面上的“小光”就能再打开。</p></main>';
      } catch (e) {
        b.disabled = false;
        b.textContent = "退出小光";
        alert("退出失败：" + e.message);
      }
    });
  }

  function bindAutostart() {
    const box = document.getElementById("autostart");
    if (!box) return;
    const note = document.getElementById("autostart-note");
    j("/api/autostart").then((s) => {
      box.checked = !!s.enabled;
      if (s.supported === false) {
        box.disabled = true;
        if (note) note.textContent = "只有 Windows 支持开机自启动。";
      }
    }).catch(() => {});
    box.addEventListener("change", async () => {
      box.disabled = true;
      try {
        const r = await j("/api/autostart", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: box.checked }) });
        box.checked = !!r.enabled;
        if (note) note.textContent = r.enabled ? "已设置：下次登录 Windows 会自动打开小光。"
          : "已关闭开机自启动。";
      } catch (e) {
        box.checked = !box.checked;
        if (note) note.textContent = "设置失败：" + e.message;
      } finally {
        box.disabled = false;
      }
    });
  }

  function bindCapture() {
    const start = document.getElementById("b-cap-start");
    const stop = document.getElementById("b-cap-stop");
    if (!start || !stop) return;
    const stateEl = document.getElementById("cap-state");
    const mumuEl = document.getElementById("cap-mumu");
    const sceneEl = document.getElementById("cap-scene");
    const recordedEl = document.getElementById("cap-recorded");
    const msgEl = document.getElementById("cap-msg");

    async function poll() {
      try {
        const c = await j("/api/capture/status");
        const labels = { idle: "还没开始", starting: "正在启动", searching: "正在找 MuMu",
          ready: "就绪", waiting: "等进图", recording: "正在录", registering: "正在登记",
          error: "出错", stopped: "已停止" };
        stateEl.textContent = labels[c.state] || c.state || "—";
        const m = c.mumu || {};
        mumuEl.textContent = m.serial ? (m.serial + "（进程 " + m.pid + "，Root " + (m.root ? "有" : "没有") + "）") : "—";
        sceneEl.textContent = c.scene ? ((c.scene_name || c.scene) + "（" + c.scene + "）") : "—";
        recordedEl.textContent = (c.recorded && c.recorded.length) ? c.recorded.join("、") : "—";
        msgEl.textContent = c.last_error ? (c.last_error + "；" + (c.message || "")) : (c.message || "");
        const running = !!(c.state && !["idle", "stopped"].includes(c.state));
        start.hidden = running;
        stop.hidden = !running;
      } catch (e) { /* 录图进程没起也不影响面板 */ }
    }

    async function act(which) {
      const btn = which === "start" ? start : stop;
      btn.disabled = true;
      try {
        const r = await j("/api/wizard/capture/" + which, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}" });
        msgEl.textContent = r.message || (r.ok ? "已提交" : "操作失败");
        if (!r.ok && r.detail) msgEl.textContent += "：" + r.detail;
      } catch (e) {
        msgEl.textContent = "操作失败：" + e.message;
      } finally {
        btn.disabled = false;
        poll();
      }
    }

    start.addEventListener("click", () => act("start"));
    stop.addEventListener("click", () => act("stop"));
    poll();
    setInterval(poll, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { maybeWizard(); bindQuit(); bindAutostart(); bindCapture(); });
  } else {
    maybeWizard(); bindQuit(); bindAutostart(); bindCapture();
  }
})();
