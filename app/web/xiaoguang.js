"use strict";
// 小光 AI 面板（本机版）：接口 /xiaoguang/*，由 control_api 本机提供，cookie 鉴权。
(() => {
  const BASE = "";
  const $ = id => document.getElementById(id);
  const MAP_SUB = { 3526133726: "家园", 1638008359: "霞谷", 2477345666: "蝴蝶平原", 817373972: "墓土入口" };
  const S = { csrf: "", status: null, map: null, busy: false, seen: new Set(), title: "哥哥" };
  const T = () => S.title || "哥哥";            // 对哥哥的称呼（后台可改，2026-11-06 r25）

  async function api(path, { method = "GET", data } = {}) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = S.csrf; }
    const r = await fetch(BASE + path, { method, headers, credentials: "same-origin", cache: "no-store",
      body: method === "GET" ? undefined : JSON.stringify(data || {}) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(body.message || "请求没有完成"); e.status = r.status; e.code = body.error; e.body = body; throw e; }
    return body;
  }

  function notice(html, sub) {
    const n = $("notice"); n.innerHTML = html; n.hidden = false; $("panel").hidden = true;
    $("hero-sub").textContent = sub || "暂时看不到小光的状态";
  }
  const text = (id, v) => { $(id).textContent = v; };
  const yes = v => (v ? "是" : "否");

  function render(st) {
    S.status = st;
    const on = st.online, starting = st.runner && !st.online;
    $("orb").className = "hero-orb" + (on ? " on" : starting ? " wait" : "");
    const badge = $("badge");
    badge.className = "badge " + (on ? "on" : starting ? "wait" : "off");
    badge.textContent = on ? "在线" : starting ? "连接中" : "离线";
    if (st.ownerTitle && st.ownerTitle !== S.title) applyTitle(st.ownerTitle);
    text("hero-sub", on ? `在 ${st.mapName} 陪大家聊天` + (st.room.ownerHere ? `，${T()}就在身边 ✨` : "")
      : starting ? "正在进入房间…" : "现在没有上线");
    text("f-map", st.runner ? st.mapName : "—");
    text("f-players", st.room && st.room.players != null && st.runner ? `${st.room.players} 人` : "—");
    text("f-owner", st.runner ? yes(st.room && st.room.ownerHere) : "—");
    text("f-follow", st.runner ? (st.followOwner ? "开" : "关") : "—");
    text("f-since", st.since ? st.since.slice(5, 16) : "—");
    const sess = st.session || {};
    text("f-session", sess.state === "ok" ? "正常" : sess.state === "warn" ? "需留意" : sess.state === "error" ? "已失效" : "未知");
    const stopped = $("f-stopped");
    stopped.hidden = !st.stopped; stopped.textContent = st.stopped ? "上次停下的原因：" + st.stopped : "";
    if (st.v === 2) return renderV2(st);
    if (S.map === null) { S.map = st.map || 817373972; $("join").checked = st.runner ? !!st.followOwner : true; }
    renderMaps(st.maps || []);
    $("b-start").disabled = S.busy || st.runner;
    $("b-stop").disabled = S.busy || !st.runner;
    $("b-restart").disabled = S.busy;
    $("b-renew").disabled = S.busy;
    if (S.accts) renderAccounts(S.accts);              // 上线/下线后“切换”按钮跟着变
  }


  // ---------- 2026-11-06 r23：新版小光（地图目录）——状态跟游戏同步，点地图直接去 ----------
  function renderV2(st) {
    const go = st.going, on = st.online;
    if (go) {
      $("orb").className = "hero-orb wait"; $("badge").className = "badge wait"; $("badge").textContent = "出发中";
      text("hero-sub", `正在前往「${go.name}」…（大约半分钟）`);
    } else if (st.changing && st.runner) {
      text("hero-sub", `正在进入「${st.pendingName}」…`);
    }
    text("f-map", go ? `→ ${go.name}` : st.runner ? st.mapName : "—");
    text("f-map-sub", go ? "正在过去" : on ? "和游戏里同步" : "点下方地图就能让他过去");
    text("f-follow-label", "本次窗口到");
    text("f-follow", st.windowEnd || "—");
    const sv = st.stable || {};
    text("f-version", !sv.label ? "—" : sv.matches ? `稳定版 ${sv.label.replace("stable-", "")} ✓`
      : `稳定版 ${sv.label.replace("stable-", "")}（已改动 ${sv.changedCount} 个文件）`);
    $("join-wrap").hidden = true;
    $("b-start").textContent = "回遇境"; $("b-restart").textContent = "原地重连";
    $("b-start").disabled = S.busy || !!go;
    $("b-restart").disabled = S.busy || !!go || !st.runner;
    $("b-stop").disabled = S.busy || !!go || !st.runner;
    $("b-renew").disabled = S.busy;
    renderMapsV2(st);
    if (S.accts) renderAccounts(S.accts);
    clearTimeout(S.fast);
    if (go || (st.runner && !on) || st.changing) S.fast = setTimeout(refresh, 2000);   // 过图时 2 秒刷一次
  }

  function renderMapsV2(st) {
    const all = st.maps || [], q = ($("map-q").value || "").trim().toLowerCase();
    const box = $("mapgroups"), here = st.runner ? st.map : null, goingTo = st.going ? st.going.map : null;
    const shown = q ? all.filter(m => (m.name + " " + m.scene + " " + m.region).toLowerCase().includes(q)) : all;
    const key = q + "|" + shown.map(m => m.id).join(",");
    if (box.dataset.key !== key) {
      box.dataset.key = key;
      const groups = new Map();
      for (const m of shown) { if (!groups.has(m.region)) groups.set(m.region, []); groups.get(m.region).push(m); }
      box.replaceChildren(...[...groups].map(([region, maps]) => {
        const g = document.createElement("div"); g.className = "xg-mapgroup";
        const h = document.createElement("h3"); h.textContent = `${region} · ${maps.length}`;
        const grid = document.createElement("div"); grid.className = "maps";
        grid.replaceChildren(...maps.map(m => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "map"; b.dataset.id = String(m.id); b.title = m.scene;
          b.append(m.name);
          const sub = document.createElement("span"); sub.className = "sub"; b.append(sub);
          b.addEventListener("click", () => goTo(m));
          return b;
        }));
        g.append(h, grid); return g;
      }));
      if (!shown.length) { const p = document.createElement("p"); p.className = "input-note"; p.textContent = "没有找到这张图"; box.replaceChildren(p); }
    }
    for (const b of box.querySelectorAll(".map")) {
      const id = Number(b.dataset.id), isHere = id === here, isGo = id === goingTo;
      b.classList.toggle("here", isHere && !goingTo); b.classList.toggle("going", isGo);
      b.disabled = S.busy || !!goingTo;
      b.querySelector(".sub").textContent = isGo ? "正在前往…" : isHere && !goingTo ? (st.online ? "小光在这 ✨" : "正在连接") : "";
    }
    text("map-count", `共 ${all.length} 张能去`);
  }

  function goTo(m) {
    const st = S.status || {};
    if (st.going) return;
    if (st.runner && st.map === m.id && st.online) { text("ctl-msg", `小光已经在「${m.name}」了`); return; }
    if (!confirm(`让小光去「${m.name}」？\n他会离开现在的房间，在那张图重新进入，大约半分钟。`)) return;
    act("/xiaoguang/start", { map: m.id }, `正在前往「${m.name}」…`);
  }

  function applyTitle(t) {
    S.title = t;
    document.querySelectorAll(".t-title").forEach(el => { el.textContent = t; });
    const inp = $("title-input");
    if (inp && document.activeElement !== inp && !inp.value) inp.placeholder = `现在是“${t}”，例如 哥哥、姐姐、宝宝`;
    if (S.friends) renderFriends(S.friends);
  }

  let titleTimer = null;
  function titleMsg(msg, bad) { const m = $("title-msg"); m.textContent = msg; m.classList.toggle("bad", !!bad); }
  function checkTitleSoon() {
    clearTimeout(titleTimer);
    const t = $("title-input").value.trim();
    if (!t) { titleMsg("只能 1～6 个汉字、字母或数字；会按游戏聊天审核规则检查。"); return; }
    titleTimer = setTimeout(async () => {
      try { const r = await api("/xiaoguang/owner/title", { method: "POST", data: { title: t, check: true } });
        if ($("title-input").value.trim() === t) titleMsg(r.ok ? `“${t}”可以用 ✓` : r.message, !r.ok); }
      catch (e) { titleMsg(e.message, true); }
    }, 450);
  }
  async function saveTitle() {
    const t = $("title-input").value.trim();
    if (!t) { titleMsg("先填一个称呼", true); return; }
    if (!confirm(`让小光以后叫他“${t}”？小光会原地重新上线，大约半分钟。`)) return;
    titleMsg("正在保存…");
    try { const r = await api("/xiaoguang/owner/title", { method: "POST", data: { title: t } });
      titleMsg(r.message || "已保存"); if (r.title) applyTitle(r.title); $("title-input").value = ""; await refresh(); }
    catch (e) { titleMsg(e.message, true); }
  }

  function renderMaps(all) {
    // 地图录到哪张就有哪张（可能一百多张）：只列当前这个号录过的图，没录的只报个数
    const maps = all.filter(m => m.ready !== false || m.id === S.map);
    const box = $("maps");
    const key = maps.map(m => m.id).join(",");
    if (box.dataset.key !== key) {
      box.dataset.key = key;
      box.replaceChildren(...maps.map(m => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "map"; b.setAttribute("role", "radio"); b.dataset.id = String(m.id);
        b.append(m.name);
        const sub = document.createElement("span"); sub.className = "sub"; sub.textContent = MAP_SUB[m.id] || "";
        b.append(sub);
        b.addEventListener("click", () => { S.map = m.id; renderMaps(S.allMaps || all); });
        return b;
      }));
    }
    S.allMaps = all;
    const ready = Object.fromEntries(all.map(m => [m.id, m.ready !== false]));
    for (const b of box.children) {
      const id = Number(b.dataset.id);
      b.setAttribute("aria-checked", String(id === S.map));
      b.disabled = !ready[id];                       // 地图录制绑定账号：当前号没录过的图进不去
      b.title = ready[id] ? "" : "当前这个号还没录过这张图";
      b.querySelector(".sub").textContent = ready[id] ? (MAP_SUB[id] || "") : "这个号还没录";
    }
    const left = all.length - all.filter(m => m.ready !== false).length;
    const note = $("maps-note");
    if (note) note.textContent = left ? `另外还有 ${left} 张图这个号没录过；想去哪张，用自动录图工具在 MuMu 里走进去录一次就会出现在这里。`
      : "用自动录图工具在 MuMu 里走进新的图，录好后会自动出现在这里。";
  }

  function renderLogs(rows) {
    const box = $("chat");
    if (!rows.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = "还没有聊天记录"; box.replaceChildren(li); return; }
    const quiet = new Set();
    rows.forEach((r, i) => { if (r.event === "not_replied" && i > 0 && rows[i - 1].event === "heard") quiet.add(i - 1); });
    const items = [];
    rows.forEach((r, i) => {
      const time = (r.ts || "").slice(11, 16);
      // 非好友在本地频道说的话只显示成省略号，连续几条合成一行
      if (r.event === "heard" && quiet.has(i) && /^[.。…·\s]*$/.test(r.text || "")) {
        const last = items[items.length - 1];
        if (last && last.dataset.hidden) {
          const n = Number(last.dataset.hidden) + 1;
          last.dataset.hidden = String(n); last.dataset.key = r.ts + r.event;
          last.textContent = `${last.dataset.from}–${time}  ${n} 条看不到内容的消息（非好友）`;
          return;
        }
        const li = document.createElement("li"); li.className = "tag faint";
        li.dataset.hidden = "1"; li.dataset.from = time; li.dataset.key = r.ts + r.event;
        li.textContent = `${time}  ${r.who} 说了看不到内容的话（非好友）`;
        items.push(li);
        return;
      }
      if (r.event === "heard" || r.event === "said") {
        const li = document.createElement("li");
        li.className = "msg " + (r.event === "said" ? "me" : "them") + (quiet.has(i) ? " quiet" : "");
        const meta = document.createElement("span"); meta.className = "meta";
        meta.textContent = (r.event === "said" ? "小光" : r.who) + " · " + time + (quiet.has(i) ? " · 没叫小光 / 看不到" : "");
        const t = document.createElement("span"); t.className = "text"; t.textContent = r.text;
        li.append(meta, t); li.dataset.key = r.ts + r.event + r.text; items.push(li);
        return;
      }
      const label = {
        emote: `小光做了「${r.text}」`, control: `小光：${({ approach: "走过去", follow: "跟着走", stop: "停下", light: "点火", idle: "放下蜡烛",
          home: "回去", sit: "坐下", stand: "站起来", accept_offer: "接受互动" })[r.text] || r.text}`,
        offer_to_me: `${r.who} 邀请小光${r.text}`, offer_accept_requested: `小光接受了${r.text}`, offer_denied: `${T()}拒绝了${r.text}`,
        offer_expired: "邀请超时未批准", friend_add: "收到好友码，加了好友", friend_code_shared: `把好友码发给了 ${r.who}`,
        outfit_read: `看了看 ${r.who} 的装扮`,
      }[r.event];
      if (!label) return;
      const li = document.createElement("li"); li.className = "tag"; li.textContent = `${time}  ${label}`; li.dataset.key = r.ts + r.event + r.text; items.push(li);
    });
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    // 只给新出现的消息播进场动画（定时刷新时旧消息不重播），首屏最多错开 12 条
    let n = 0;
    const fresh = items.filter(li => !S.seen.has(li.dataset.key));
    fresh.slice(-12).forEach(li => { li.classList.add("fresh"); li.style.setProperty("--d", (n++) * 40 + "ms"); });
    items.forEach(li => S.seen.add(li.dataset.key));
    box.replaceChildren(...items);
    if (atBottom || !box.dataset.ready) { box.scrollTop = box.scrollHeight; box.dataset.ready = "1"; }
  }

  async function refresh() {
    try { render(await api("/xiaoguang/status")); } catch (e) { handle(e); }
  }
  async function refreshLogs() {
    try { renderLogs((await api("/xiaoguang/logs?limit=80")).rows || []); } catch (e) { handle(e); }
  }
  function handle(e) {
    if (e.status === 401 || e.status === 403) notice('本机令牌失效了：请关掉这个页面，从小光启动器重新打开后台。', "需要重新打开");
    else if (e.status === 503) notice("小光的控制服务没有运行，请从启动器重新启动。", "控制服务未运行");
    else text("ctl-msg", e.message);
  }

  async function act(path, data, msg) {
    if (S.busy) return;
    S.busy = true; render(S.status); text("ctl-msg", msg);
    try {
      const r = await api(path, { method: "POST", data });
      text("ctl-msg", r.ok ? (r.message || "好了 ✓") : "没有成功：" + (r.message || r.detail || ""));
    } catch (e) { handle(e); text("ctl-msg", "没有成功：" + e.message); }
    finally { S.busy = false; await refresh(); }
  }

  // ---------- 哥哥：从当前小光号的好友里选（2026-10-31） ----------
  function renderFriends(r) {
    S.friends = r;
    const box = $("friends"), warn = $("owner-warn");
    if (!r.ok) { box.replaceChildren(); warn.hidden = false; warn.textContent = r.message || "读不到好友列表"; return; }
    warn.hidden = r.ownerIsFriend;
    warn.textContent = r.ownerIsFriend ? "" : `当前小光号还没加${T()}好友：先在游戏里加上，或者从下面重新选一个人当${T()}。`;
    box.replaceChildren(...(r.friends || []).map(f => {
      const li = document.createElement("li");
      li.className = "acct" + (f.owner ? " active" : "");
      const main = document.createElement("div"); main.className = "acct-main";
      const name = document.createElement("strong"); name.textContent = f.nickname;
      const meta = document.createElement("span"); meta.className = "acct-meta"; meta.textContent = f.uuid.slice(0, 8) + "…";
      main.append(name, meta);
      li.append(main);
      if (f.owner) {
        const tag = document.createElement("span"); tag.className = "acct-tag"; tag.textContent = T();
        li.append(tag);
      } else {
        const b = document.createElement("button");
        b.type = "button"; b.className = "button secondary small"; b.textContent = `设为${T()}`;
        b.addEventListener("click", () => setOwner(f));
        li.append(b);
      }
      return li;
    }));
  }

  async function refreshFriends() {
    text("owner-msg", "");
    try { renderFriends(await api("/xiaoguang/friends")); }
    catch (e) { renderFriends({ ok: false, message: e.message }); }
  }

  async function setOwner(f) {
    if (!confirm(`把「${f.nickname}」设为${T()}？`)) return;
    text("owner-msg", "正在设置…");
    try {
      const r = await api("/xiaoguang/owner", { method: "POST", data: { uuid: f.uuid } });
      text("owner-msg", r.message || `已设为${T()}`);
    } catch (e) { text("owner-msg", e.message); }
    refreshFriends(); refresh();
  }

  // ---------- 人设：切换 / 编辑 / 新建 / 删除（2026-10-31） ----------
  const P = { list: [], editing: null };

  function editPersona(p) {
    P.editing = p;
    $("persona-name").value = p ? p.name : "";
    $("persona-prompt").value = p ? p.prompt : "";
    const custom = p && !p.preset;
    $("b-persona-save").disabled = !custom;
    $("b-persona-del").disabled = !custom;
    $("b-persona-save").title = custom ? "" : "预置人设不能改，可以另存为新人设";
  }

  function renderPersonas(r) {
    P.list = r.personas || [];
    const box = $("personas");
    box.replaceChildren(...P.list.map(p => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "map"; b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(!!p.active));
      b.append(p.name);
      const sub = document.createElement("span"); sub.className = "sub";
      sub.textContent = p.active ? "正在使用" : p.preset ? "预置" : "自定义";
      b.append(sub);
      b.addEventListener("click", () => selectPersona(p));
      return b;
    }));
    const cur = P.editing && P.list.find(p => p.id === P.editing.id);
    editPersona(cur || P.list.find(p => p.active) || null);
  }

  async function refreshPersonas() {
    try { renderPersonas(await api("/xiaoguang/personas")); }
    catch (e) { text("persona-msg", e.message); }
  }

  async function selectPersona(p) {
    editPersona(p);
    if (p.active) return;
    text("persona-msg", "正在切换…");
    try {
      await api("/xiaoguang/personas/select", { method: "POST", data: { persona: p.id } });
      text("persona-msg", `已切换到「${p.name}」，下一句话就生效`);
    } catch (e) { text("persona-msg", e.message); }
    refreshPersonas();

  // ---------------- 聊天触发词（control/chat_trigger.json） ----------------
  async function loadTrigger() {
    try {
      const r = await api("/chat/trigger");
      $("trig-mode").value = r.mode === "all" ? "all" : "prefix";
      $("trig-prefix").value = r.prefix || "/";
    } catch (e) { /* 读不到就用默认 / */ }
  }
  async function saveTrigger() {
    try {
      const r = await api("/chat/trigger", { method: "POST",
        data: { mode: $("trig-mode").value, prefix: ($("trig-prefix").value || "/").trim() } });
      $("trig-msg").textContent = r.message || "已保存";
    } catch (e) {
      $("trig-msg").textContent = (e && e.message) || "保存失败";
    }
  }
  if ($("b-trig-save")) $("b-trig-save").addEventListener("click", saveTrigger);
  loadTrigger();
  }

  async function savePersona(asNew) {
    const name = $("persona-name").value.trim(), prompt = $("persona-prompt").value.trim();
    if (!name || !prompt) { text("persona-msg", "名字和人设内容都要填"); return; }
    const data = { name, prompt };
    if (!asNew && P.editing && !P.editing.preset) data.persona = P.editing.id;
    try {
      const r = await api("/xiaoguang/personas/save", { method: "POST", data });
      text("persona-msg", r.message || "已保存");
      P.editing = { id: r.id };
      refreshPersonas();
    } catch (e) { text("persona-msg", e.message); }
  }

  async function deletePersona() {
    const p = P.editing;
    if (!p || p.preset) return;
    if (!confirm(`删除人设「${p.name}」？`)) return;
    try {
      const r = await api("/xiaoguang/personas/delete", { method: "POST", data: { persona: p.id } });
      text("persona-msg", r.message || "已删除");
      P.editing = null;
      refreshPersonas();
    } catch (e) { text("persona-msg", e.message); }
  }

  // ---------- 账号：列表 / 切换 / 短信验证码登录新号（2026-10-31，原号被封后加） ----------
  const A = { pending: null, manual: null, platform: "android", ticket: null, retry: null };

  // ---------- W7：本机设备（首次登录自动注册；重新注册要二次确认） ----------
  function deviceLine(d) {
    if (!d) return "正在检查…";
    if (d.registered) {
      return "已注册（" + (d.device_name || "WindowsPC") + "，id 末 4 位 " +
        (d.device_id_tail || "----") + "）" + (d.platform === "ios" ? " · iOS 档" : "");
    }
    return d.message || "未注册（首次登录会自动注册）";
  }

  function renderDevice(r) {
    const d = (r && (r.current || r.device)) || {};
    text("device-status", deviceLine(d));
    if (r && r.pool) {
      $("device-status").title = "设备池：" + r.pool.total + " 台（安卓 " +
        r.pool.android + " / iOS " + r.pool.ios + "）";
    }
  }

  async function refreshDevice() {
    try { renderDevice(await api("/xiaoguang/api/device")); }
    catch (e) { text("device-status", "设备状态读取失败：" + e.message); }
  }

  async function ensureDevice() {
    try {
      const r = await api("/xiaoguang/api/device/ensure", { method: "POST", data: { platform: A.platform } });
      renderDevice(r);
      return r;
    } catch (e) {
      text("device-msg", e.message || "本机设备注册失败，请重试");
      return null;
    }
  }

  async function reRegisterDevice() {
    if (!confirm("重新注册会换一台设备，之后登录都用新设备；旧设备上的账号绑定会重新分配。继续？")) return;
    if (!confirm("确定现在重新注册？换设备可能触发网易安全验证（要用绑定手机发短信）。")) return;
    $("b-device-register").disabled = true;
    text("device-msg", "正在注册新设备…（只发一次请求）");
    try {
      const r = await api("/xiaoguang/api/device/register", { method: "POST",
        data: { platform: A.platform, confirm: true } });
      renderDevice(r);
      text("device-msg", r.message || "已换新设备");
    } catch (e) { text("device-msg", e.message || "注册失败，请稍后重试"); }
    finally { $("b-device-register").disabled = false; }
  }

  function renderAccounts(r) {
    S.accts = r;
    const box = $("accounts");
    const online = S.status && S.status.runner;
    const rows = (r.accounts || []).map(a => {
      const li = document.createElement("li");
      li.className = "acct" + (a.active ? " active" : "") + (a.bannedUntil ? " banned" : "");
      const main = document.createElement("div"); main.className = "acct-main";
      const name = document.createElement("strong"); name.textContent = a.alias || a.id;
      const meta = document.createElement("span"); meta.className = "acct-meta";
      meta.textContent = `${a.platform || "android"} · ${a.uuid}… · 添加于 ${a.created || "—"}`;
      main.append(name, meta);
      if (a.readyNote) {                                // 没准备好/好友没加：行里直接说原因
        const why = document.createElement("span"); why.className = "acct-meta";
        why.textContent = a.readyNote;
        main.append(why);
      }
      const state = document.createElement("span");
      state.className = "acct-state " + (a.bannedUntil ? "bad" : a.status === "ok" ? "good" : "warn");
      state.textContent = a.bannedUntil ? `封禁至 ${a.bannedUntil.slice(5, 16)}` : a.status === "ok" ? "正常" : "登录态异常";
      li.append(main, state);
      if (a.active) {
        const tag = document.createElement("span"); tag.className = "acct-tag"; tag.textContent = "当前使用";
        li.append(tag);
      } else {
        const b = document.createElement("button");
        b.type = "button"; b.className = "button secondary small"; b.textContent = "切换到这个号";
        b.disabled = !!a.bannedUntil || a.ready === false;   // 被封/没准备好不能点；在线也能切
        b.title = a.readyNote || (a.bannedUntil ? "这个号还在封禁中" : "");
        b.addEventListener("click", () => selectAccount(a.id, a.alias, online));
        li.append(b);
      }
      return li;
    });
    box.replaceChildren(...rows);
    text("acct-hint", "切换后小光会用新号重新上线（在线时原地重启）");
  }

  async function refreshAccounts() {
    try { renderAccounts(await api("/xiaoguang/accounts")); }
    catch (e) { text("acct-msg", e.message); }
  }

  async function selectAccount(id, alias, online) {
    if (online && !confirm("小光会用新号原地重新上线，约半分钟，确定？")) return;
    text("acct-msg", "正在切换…");
    try {
      const r = await api("/xiaoguang/accounts/select", { method: "POST", data: { account: id } });
      text("acct-msg", (r.message || "已切换") + `（${alias || id}）`);
    } catch (e) { text("acct-msg", e.message); }
    refreshAccounts(); refresh();
  }

  // 登录：手机号验证码 / 邮箱密码，安卓/iOS；网易风控（上行短信验证、设备风险）照原控制台的流程处理
  function loginDone(r) {
    text("login-msg", r.message || "登录成功");
    A.pending = null; A.retry = null;
    $("login-step2").hidden = true; $("risk-sms").hidden = true; $("risk-device").hidden = true;
    ["login-code", "login-mobile", "login-password"].forEach(id => { $(id).value = ""; });
    refreshAccounts(); refreshDevice();
  }

  function loginFailed(e, retry) {
    const b = e.body || {};
    $("risk-sms").hidden = $("risk-device").hidden = true;
    if (b.risk === "sms_verify" && b.verify) {          // 要用户自己编辑短信发到指定号码，发完点“我已发送”
      A.ticket = b.verify.ticket; A.retry = retry;
      text("risk-tips", b.verify.tips || "");
      text("risk-content", b.verify.content || "（网易没给验证内容，按提示操作）");
      text("risk-number", b.verify.number || "1069016373035");
      text("risk-alt", b.verify.alt || "");
      text("risk-msg", "");
      $("risk-sms").hidden = false;
      text("login-msg", "");
      return;
    }
    if (b.risk === "device_risk") {
      text("risk-device-tips", b.message || "");
      const a = $("risk-device-url");
      a.hidden = !(b.device && b.device.url);
      if (b.device && b.device.url) a.href = b.device.url;
      $("risk-device").hidden = false;
      text("login-msg", "");
      return;
    }
    text("login-msg", e.message);
  }

  async function sendCode() {
    const mobile = $("login-mobile").value.trim();
    if (!/^1[3-9]\d{9}$/.test(mobile)) { text("login-msg", "手机号格式不对（11 位）"); return; }
    text("login-msg", "正在准备本机设备…");
    if (!await ensureDevice()) { text("login-msg", $("device-msg").textContent || "本机设备不可用"); return; }
    $("b-send").disabled = true; text("login-msg", "正在发送验证码…");
    try {
      const r = await api("/xiaoguang/login/send-sms", { method: "POST", data: { mobile, platform: A.platform } });
      A.pending = r.pending; A.manual = r.manual;
      $("login-step2").hidden = false;
      $("login-code").placeholder = r.manual ? "发完短信后留空，直接点登录" : "短信验证码";
      text("login-msg", r.message || "验证码已发送");
      $("login-code").focus();
    } catch (e) { loginFailed(e, sendCode); }
    finally { setTimeout(() => { $("b-send").disabled = false; }, 30000); }
  }

  async function verifyCode() {
    if (!A.pending) { text("login-msg", "先获取验证码"); return; }
    const code = $("login-code").value.trim();
    if (!A.manual && !/^\d{4,8}$/.test(code)) { text("login-msg", "验证码格式不对"); return; }
    $("b-verify").disabled = true; text("login-msg", "正在登录…");
    try {
      loginDone(await api("/xiaoguang/login/verify", { method: "POST", data: code ? { pending: A.pending, code } : { pending: A.pending } }));
    } catch (e) { loginFailed(e, verifyCode); }
    finally { $("b-verify").disabled = false; }
  }

  async function emailLogin() {
    const email = $("login-email").value.trim(), password = $("login-password").value;
    if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,10}$/.test(email)) { text("login-msg", "邮箱格式不对"); return; }
    if (!password) { text("login-msg", "请填写密码"); return; }
    text("login-msg", "正在准备本机设备…");
    if (!await ensureDevice()) { text("login-msg", $("device-msg").textContent || "本机设备不可用"); return; }
    $("b-email").disabled = true; text("login-msg", "正在登录…（可能要十几秒）");
    try {
      loginDone(await api("/xiaoguang/login/email", { method: "POST", data: { email, password, platform: A.platform } }));
    } catch (e) { loginFailed(e, emailLogin); }
    finally { $("b-email").disabled = false; }
  }

  async function riskSent() {
    if (!A.ticket) { text("risk-msg", "验证已过期，请重新登录一次"); return; }
    $("b-risk-sent").disabled = true; text("risk-msg", "正在向网易确认…");
    try {
      const r = await api("/xiaoguang/login/confirm-sms", { method: "POST", data: { ticket: A.ticket } });
      text("risk-msg", r.message || "验证通过");
      $("risk-sms").hidden = true;
      if (r.account) { A.retry = null; await loginDone(r); }
      else if (A.retry) await A.retry();
    } catch (e) { text("risk-msg", e.message); }
    finally { $("b-risk-sent").disabled = false; }
  }

  function setLoginTab(email) {
    $("tab-sms").setAttribute("aria-selected", String(!email));
    $("tab-email").setAttribute("aria-selected", String(email));
    $("pane-sms").hidden = email; $("pane-email").hidden = !email;
    $("risk-sms").hidden = $("risk-device").hidden = true;
    text("login-msg", "");
  }

  function setPlatform(p) {
    A.platform = p;
    document.querySelectorAll("[data-platform]").forEach(b => b.setAttribute("aria-checked", String(b.dataset.platform === p)));
  }

  async function boot() {
    // 手机上顶部导航横向滚动，把当前的“小光 AI”滚进视野
    const nav = document.querySelector(".topbar nav");
    if (nav && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = nav.scrollWidth;
    try { render(await api("/xiaoguang/status")); } catch (e) { return handle(e); }
    $("panel").hidden = false;
    $("b-start").addEventListener("click", () => S.status && S.status.v === 2
      ? act("/xiaoguang/start", { map: S.status.home }, "正在回遇境…")
      : act("/xiaoguang/start", { map: S.map, join: $("join").checked }, "正在上线…"));
    $("b-restart").addEventListener("click", () => S.status && S.status.v === 2
      ? act("/xiaoguang/restart", {}, "正在原地重连…（会换房间）")
      : act("/xiaoguang/restart", { map: S.map, join: $("join").checked }, "正在重连…（会换房间）"));
    $("map-q").addEventListener("input", () => { if (S.status && S.status.v === 2) renderMapsV2(S.status); });
    $("b-stop").addEventListener("click", () => act("/xiaoguang/stop", {}, "正在下线…"));
    const restartSvc = $("b-restart-svc");
    if (restartSvc) restartSvc.addEventListener("click", async () => {
      if (!confirm("重启小光和后台？小光会下线再上线，大约半分钟；页面稍后自动刷新。")) return;
      restartSvc.disabled = true;
      try {
        await api("/xiaoguang/api/restart_service", { method: "POST", data: {} });
      } catch (e) { /* 进程重启时请求中断是正常的 */ }
      text("hero-sub", "小光和后台正在重启…（约半分钟）");
      setTimeout(() => window.location.reload(), 8000);
    });
    $("b-renew").addEventListener("click", () => act("/xiaoguang/renew", {}, "正在续期…（可能要等一会儿）"));
    const resetMemory = async scope => {                // 清空记忆（2026-10-31）
      const what = scope === "all" ? "全部记忆（包括对每个人的印象）" : "聊天上下文（最近聊的内容）";
      if (!confirm(`确定清空小光的${what}？清空后不能恢复。`)) return;
      text("mem-msg", "正在清空…");
      try { const r = await api("/xiaoguang/memory/reset", { method: "POST", data: { scope } }); text("mem-msg", (r.message || "已清空") + " ✓"); }
      catch (e) { handle(e); text("mem-msg", "没有成功：" + e.message); }
    };
    $("b-mem-chat").addEventListener("click", () => resetMemory("chat"));
    $("b-mem-all").addEventListener("click", () => resetMemory("all"));
    $("b-refresh").addEventListener("click", () => { refresh(); refreshLogs(); });
    $("b-send").addEventListener("click", sendCode);
    $("b-verify").addEventListener("click", verifyCode);
    $("login-code").addEventListener("keydown", e => { if (e.key === "Enter") verifyCode(); });
    $("b-email").addEventListener("click", emailLogin);
    $("login-password").addEventListener("keydown", e => { if (e.key === "Enter") emailLogin(); });
    $("b-risk-sent").addEventListener("click", riskSent);
    $("b-device-register").addEventListener("click", reRegisterDevice);
    $("b-copy-content").addEventListener("click", () => {
      const t = $("risk-content").textContent;
      (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(
        () => text("risk-msg", "已复制验证内容"), () => text("risk-msg", "复制失败，请手动输入"));
    });
    $("tab-sms").addEventListener("click", () => setLoginTab(false));
    $("tab-email").addEventListener("click", () => setLoginTab(true));
    document.querySelectorAll("[data-platform]").forEach(b => b.addEventListener("click", () => setPlatform(b.dataset.platform)));
    $("b-persona-new").addEventListener("click", () => savePersona(true));
    $("b-persona-save").addEventListener("click", () => savePersona(false));
    $("b-persona-del").addEventListener("click", deletePersona);
    refreshLogs();
    refreshAccounts();
    refreshDevice();
    refreshPersonas();
    refreshFriends();
    $("b-friends").addEventListener("click", refreshFriends);
    $("title-input").addEventListener("input", checkTitleSoon);
    $("title-input").addEventListener("keydown", e => { if (e.key === "Enter") saveTitle(); });
    $("b-title").addEventListener("click", saveTitle);
    setInterval(() => { if (!document.hidden) refreshAccounts(); }, 30000);
    setInterval(() => { if (!document.hidden) refresh(); }, 6000);
    setInterval(() => { if (!document.hidden) refreshLogs(); }, 10000);
  }
  boot();
})();
