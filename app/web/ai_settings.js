"use strict";
// 小光单机版：AI 接口设置（服务商 / Key / 地址 / 模型 / 测试 / 保存）。
// 只调用本机 control_api 的 /xiaoguang/ai*，不把 Key 写进 console、URL 或页面文本。
(() => {
  const BASE = "";
  const $ = id => document.getElementById(id);
  const A = { providers: [], settings: null, busy: false };

  async function api(path, { method = "GET", data } = {}) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") headers["Content-Type"] = "application/json";
    const r = await fetch(BASE + path, {
      method, headers, credentials: "same-origin", cache: "no-store",
      body: method === "GET" ? undefined : JSON.stringify(data || {}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(body.message || body.error || "请求没有完成");
      e.status = r.status; e.body = body; throw e;
    }
    return body;
  }

  function setMsg(el, text, bad) {
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("bad", !!bad);
  }

  function preset() {
    const id = $("ai-provider").value;
    return A.providers.find(p => p.id === id) || null;
  }

  function fillProviders(list) {
    A.providers = list || [];
    const sel = $("ai-provider");
    const old = sel.value;
    sel.replaceChildren(...A.providers.map(p => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name;
      return o;
    }));
    if (old && A.providers.some(p => p.id === old)) sel.value = old;
    else if (A.settings && A.settings.provider) sel.value = A.settings.provider;
    syncPreset(false);
  }

  function syncPreset(overwriteBase) {
    const p = preset();
    if (!p) return;
    $("ai-provider-note").textContent = p.note || "";
    const keyLink = $("ai-key-link"), docsLink = $("ai-docs-link");
    keyLink.hidden = !p.key_url;
    docsLink.hidden = !p.docs;
    if (p.key_url) keyLink.href = p.key_url; else keyLink.removeAttribute("href");
    if (p.docs) docsLink.href = p.docs; else docsLink.removeAttribute("href");
    if (overwriteBase) $("ai-base").value = p.base_url || "";
    $("ai-key").placeholder = p.needs_key
      ? "粘贴 Key；已设置过就留空不改"
      : "这个服务一般不用 Key，可留空";
  }

  function applySettings(s) {
    A.settings = s;
    if (!s || !s.ok) {
      setMsg($("ai-key-hint"), (s && s.error) || "读取 AI 设置失败", true);
      return;
    }
    if (s.provider) $("ai-provider").value = s.provider;
    if (s.base_url) $("ai-base").value = s.base_url;
    if (s.model) $("ai-model").value = s.model;
    $("ai-key").value = "";
    setMsg($("ai-key-hint"), s.key_error || (s.has_key
      ? `已保存：${s.key_hint}，留空表示不改。`
      : (s.needs_key ? "还没有保存 Key。" : "这个服务不用 Key。")), !!s.key_error);
    syncPreset(false);
    if (s.base_url) $("ai-base").value = s.base_url;   // syncPreset 不覆盖已保存地址
  }

  async function loadAll() {
    try {
      const [ps, st] = await Promise.all([api("/xiaoguang/ai/providers"), api("/xiaoguang/ai")]);
      fillProviders(ps.providers || []);
      applySettings(st);
    } catch (e) {
      setMsg($("ai-models-msg"), e.message, true);
    }
  }

  function formData(includeKey) {
    const data = {
      provider: $("ai-provider").value,
      base_url: $("ai-base").value.trim(),
      model: $("ai-model").value.trim(),
    };
    const key = $("ai-key").value.trim();
    if (includeKey && key) data.key = key;
    return data;
  }

  async function fetchModels() {
    if (A.busy) return;
    A.busy = true; $("ai-models-btn").disabled = true;
    setMsg($("ai-models-msg"), "正在拉模型列表…");
    try {
      const r = await api("/xiaoguang/ai/models", { method: "POST", data: formData(true) });
      if (!r.ok) { setMsg($("ai-models-msg"), r.error || "拉不到模型列表", true); return; }
      const list = $("ai-model-list");
      list.replaceChildren(...(r.models || []).map(m => {
        const o = document.createElement("option"); o.value = m; return o;
      }));
      const menu = $("ai-model-menu");
      if (menu) {
        menu.replaceChildren(...(r.models || []).map(m => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "wz-menu-item"; b.textContent = m; b.dataset.v = m;
          return b;
        }));
        menu.hidden = !(r.models || []).length;
      }
      if ((r.models || []).length) {
        if (!$("ai-model").value.trim()) $("ai-model").value = r.models[0];
        setMsg($("ai-models-msg"), `拿到 ${r.models.length} 个模型；下拉选一个，或直接改上面的输入框。`);
      } else {
        setMsg($("ai-models-msg"), "接口没有返回模型，手动填模型名也可以。");
      }
    } catch (e) {
      setMsg($("ai-models-msg"), e.message, true);
    } finally {
      A.busy = false; $("ai-models-btn").disabled = false;
    }
  }

  function showResult(box, cls, lines) {
    box.className = "xg-ai-result" + (cls ? " " + cls : "");
    box.replaceChildren(...lines.filter(Boolean).map(t => {
      const s = document.createElement("span"); s.className = "line"; s.textContent = t; return s;
    }));
    box.hidden = false;
  }

  async function test() {
    if (A.busy) return;
    A.busy = true; $("ai-test-btn").disabled = true;
    setMsg($("ai-save-msg"), "正在测试…");
    try {
      const r = await api("/xiaoguang/ai/test", { method: "POST", data: formData(true) });
      if (!r.ok) {
        showResult($("ai-test-result"), "bad", ["没有连通：" + (r.error || "未知原因")]);
        setMsg($("ai-save-msg"), "");
        return;
      }
      const lines = [`连通 ✓　模型：${r.model || "—"}　耗时 ${r.elapsed_ms} ms`];
      if (r.reply) lines.push("模型回复：" + r.reply);
      if (r.tool_support) {
        lines.push("支持工具调用 ✓（换装、带路、查礼包这些功能可以用）");
        showResult($("ai-test-result"), "good", lines);
      } else {
        lines.push("不支持工具调用：小光的换装、带路等功能会用不了，建议换模型。");
        showResult($("ai-test-result"), "warn", lines);
      }
      setMsg($("ai-save-msg"), "");
    } catch (e) {
      showResult($("ai-test-result"), "bad", ["没有连通：" + e.message]);
    } finally {
      A.busy = false; $("ai-test-btn").disabled = false;
    }
  }

  async function save() {
    if (A.busy) return;
    A.busy = true; $("ai-save-btn").disabled = true;
    setMsg($("ai-save-msg"), "正在保存…");
    $("ai-restart-btn").hidden = true;
    try {
      const r = await api("/xiaoguang/ai", { method: "POST", data: formData(true) });
      $("ai-key").value = "";
      setMsg($("ai-key-hint"), r.key_error || (r.has_key
        ? `已保存：${r.key_hint}，留空表示不改。`
        : "还没有保存 Key。"), !!r.key_error);
      showResult($("ai-save-result"), r.configured ? "good" : "warn",
        [r.message || "已保存", r.configured ? "配置已齐全。" : "配置还不完整，补齐后聊天才能用。"]);
      $("ai-restart-btn").hidden = !r.restart_required;
      $("ai-save-msg").textContent = "";
      refreshBanner();
    } catch (e) {
      showResult($("ai-save-result"), "bad", ["保存失败：" + e.message]);
    } finally {
      A.busy = false; $("ai-save-btn").disabled = false;
    }
  }

  async function restartChat() {
    $("ai-restart-btn").disabled = true;
    try {
      const r = await api("/xiaoguang/chat/restart", { method: "POST", data: {} });
      showResult($("ai-save-result"), "good", [r.message || "聊天已重启"]);
      $("ai-restart-btn").hidden = true;
    } catch (e) {
      showResult($("ai-save-result"), "bad", ["重启失败：" + e.message]);
    } finally {
      $("ai-restart-btn").disabled = false;
    }
  }

  async function refreshBanner() {
    try {
      const s = await api("/xiaoguang/ai");
      $("ai-banner").hidden = !!s.configured;
    } catch (_) {
      // 没登录/接口没起时不打扰：xiaoguang.js 会显示它自己的错误
    }
  }

  function openSettings() {
    const box = $("settings");
    box.hidden = false;
    $("b-settings").setAttribute("aria-expanded", "true");
    const first = $("ai-provider");
    if (first) first.focus();
  }
  function closeSettings() {
    $("settings").hidden = true;
    $("b-settings").setAttribute("aria-expanded", "false");
  }

  function boot() {
    loadAll();
    refreshBanner();
    $("b-settings").addEventListener("click", openSettings);
    $("b-ai-banner").addEventListener("click", openSettings);
    $("b-settings-close").addEventListener("click", closeSettings);
    $("settings-backdrop").addEventListener("click", closeSettings);
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("settings").hidden) closeSettings(); });
    $("ai-provider").addEventListener("change", () => syncPreset(true));
    $("ai-models-btn").addEventListener("click", fetchModels);
  if ($("ai-model")) {
    $("ai-model").addEventListener("focus", () => {
      const m = $("ai-model-menu");
      if (m && m.children.length) m.hidden = false;
    });
  }
  if ($("ai-model-menu")) {
    $("ai-model-menu").addEventListener("click", (e) => {
      const b = e.target.closest(".wz-menu-item");
      if (!b) return;
      $("ai-model").value = b.dataset.v || "";
      $("ai-model-menu").hidden = true;
    });
  }
  document.addEventListener("click", (e) => {
    const m = $("ai-model-menu");
    if (!m) return;
    if (!e.target.closest("#ai-model-menu") && e.target.id !== "ai-model") m.hidden = true;
  });
    $("ai-test-btn").addEventListener("click", test);
    $("ai-save-btn").addEventListener("click", save);
    $("ai-restart-btn").addEventListener("click", restartChat);
    setInterval(() => { if (!document.hidden) refreshBanner(); }, 30000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
