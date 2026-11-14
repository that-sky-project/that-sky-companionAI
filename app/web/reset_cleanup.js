/* Shared, authenticated resource cleanup UI. No network or filesystem access of its own. */
(function (global) {
  'use strict';
  const CATEGORIES = [
    ['terrain', '地形与游戏数据', true], ['catalog', '地图目录', true],
    ['names', '名称表', true], ['captures', '遇境录制登记（原始录制保留）', false],
    ['props', '道具形状（可单独补生成，不重做地形）', false]
  ];
  const PRESERVE = '即使全选也保留：账号与登录、AI 配置、主人与人设、聊天记忆。生成数据先移入本机回收区；原始录制文件始终保留，不会自动重新生成。';
  const GUIDE = '下一步：生成游戏数据 → 遇境采集 → 构建地图目录。请逐步点击，不会自动启动生成或采集。';
  const MODES = {
    cleanup: {title: '清理数据并从头生成', preview: 'preview', execute: 'execute', confirm: '确认清理所选数据/登记'},
    restore: {title: '恢复上次清理', preview: 'restore-preview', execute: 'restore', confirm: '确认恢复预览中的数据'},
    purge: {title: '彻底删除回收区', preview: 'purge-preview', execute: 'purge', confirm: '永久删除预览中的回收区数据'}
  };
  function formatBytes(value) {
    if (!Number.isFinite(value) || value < 0) return '未知大小';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let n = value, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i ? n.toFixed(2) : String(n)) + ' ' + units[i];
  }
  function expiry(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
    return typeof value === 'string' ? Date.parse(value) : NaN;
  }
  function offlineProblem(status) {
    if (!status || status.ok === false || typeof status.online !== 'boolean'
        || typeof status.runner !== 'boolean' || typeof status.chat !== 'boolean') return '无法确认小光已离线，请刷新状态后重试。';
    if (status.online || status.runner || status.chat || status.going || status.changing) return '请先让小光下线并停止聊天/换图，再预览或执行清理。';
    const capture = status.captureState;
    if (capture && !['idle', 'stopped', 'stale', 'error'].includes(typeof capture === 'string' ? capture : capture.state)) return '请先停止遇境采集，再进行数据清理。';
    return '';
  }
  function messageOf(error) {
    const doc = error && (error.data || error);
    const code = doc && doc.error_code;
    return (doc && typeof doc.message === 'string' ? doc.message : '操作失败，请刷新后重试。') + (code ? '（' + String(code) + '）' : '');
  }
  function mount(host, options) {
    if (!host || host.dataset.resetMounted === 'true') return null;
    host.dataset.resetMounted = 'true';
    const doc = host.ownerDocument || global.document;
    const state = {mode: 'cleanup', busy: false, plan: null, open: false, serial: 0};
    const fields = {};
    function node(tag, className, text) {
      const n = doc.createElement(tag);
      if (className) n.className = className;
      if (text !== undefined) n.textContent = text;
      return n;
    }
    function button(text, fn, className) {
      const b = node('button', 'xg-reset-button ' + (className || ''), text);
      b.type = 'button'; b.addEventListener('click', fn); return b;
    }
    const heading = node('h3', '', '重新准备本机游戏数据');
    const note = node('p', 'xg-reset-note', PRESERVE);
    const row = node('div', 'xg-reset-actions');
    const start = button(MODES.cleanup.title, () => open('cleanup'), 'xg-reset-danger');
    const restore = button(MODES.restore.title, () => open('restore'));
    const purge = button(MODES.purge.title, () => open('purge'));
    row.append(start, restore, purge);
    const feedback = node('p', 'xg-reset-feedback'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
    const guide = node('div', 'xg-reset-guide'); guide.hidden = true;
    const guideText=node('p', '', GUIDE);guide.append(guideText);const guideButtons={};
    const guideActions = node('div', 'xg-reset-actions');
    [['data', '1 · 生成游戏数据'], ['capture', '2 · 遇境采集'], ['catalog', '3 · 构建地图目录'], ['props', '补生成道具形状']].forEach(([step, label]) => {
      const action=button(label, () => { if (typeof options.navigate === 'function') options.navigate(step); });guideButtons[step]=action;guideActions.append(action);
    });
    guide.append(guideActions); host.append(heading, note, row, feedback, guide);

    const dialog = node('dialog', 'xg-reset-dialog');
    const title = node('h2'); title.id = (host.id || 'xg-reset') + '-title'; dialog.setAttribute('aria-labelledby', title.id);
    const intro = node('p', 'xg-reset-note', PRESERVE + ' 必须保持小光离线；后台执行前会再次核验。');
    const selection = node('fieldset', 'xg-reset-selection'); selection.append(node('legend', '', '选择要清理的类别'));
    CATEGORIES.forEach(([key, label, checked]) => {
      const wrap = node('label', 'xg-reset-choice');
      const input = node('input'); input.type = 'checkbox'; input.checked = checked; input.name = key;
      fields[key] = input; input.addEventListener('change', invalidate);
      wrap.append(input, node('span', '', label)); selection.append(wrap);
    });
    const all = button('全选清理类别', () => { CATEGORIES.forEach(([key]) => { fields[key].checked = true; }); invalidate(); });
    selection.append(all, node('p', 'xg-reset-note', '录制登记默认保留；勾选后仅清除录制登记，原始录制文件保留，需要新采集或明确重新登记。' + PRESERVE));
    const previewBox = node('section', 'xg-reset-preview'); previewBox.hidden = true;
    const previewTitle = node('h3', '', '操作预览（相对数据目录）');
    const list = node('ul', 'xg-reset-items');
    const total = node('p', 'xg-reset-total');
    const expires = node('p', 'xg-reset-note');
    previewBox.append(previewTitle, list, total, expires);
    const confirmation = node('label', 'xg-reset-confirm'); confirmation.hidden = true;
    const confirm = node('input'); confirm.type = 'checkbox'; confirm.addEventListener('change', sync);
    const confirmCopy = node('span'); confirmation.append(confirm, confirmCopy);
    const status = node('p', 'xg-reset-feedback'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const actions = node('div', 'xg-reset-actions');
    const cancel = button('取消', close);
    const preview = button('先预览目录与大小', previewPlan);
    const execute = button('确认清理', commit, 'xg-reset-danger'); execute.hidden = true;
    actions.append(cancel, preview, execute);
    dialog.append(title, intro, selection, previewBox, confirmation, status, actions);
    (doc.body || host).append(dialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });

    function say(text, error) { status.textContent = text || ''; status.setAttribute('role', error ? 'alert' : 'status'); status.dataset.error = String(!!error); }
    function sync() {
      [start, restore, purge, preview, cancel, all].forEach(b => { b.disabled = state.busy; });
      CATEGORIES.forEach(([key]) => { fields[key].disabled = state.busy; });
      confirm.disabled = state.busy;
      execute.disabled = state.busy || !state.plan || (!state.plan.items.length && state.mode !== 'cleanup') || !confirm.checked;
      dialog.setAttribute('aria-busy', String(state.busy));
      if (typeof options.onBusy === 'function') options.onBusy(state.busy);
    }
    function invalidate() {
      state.plan = null; confirm.checked = false; previewBox.hidden = confirmation.hidden = execute.hidden = true;
      say('请重新预览，目录和大小以本次后台返回为准。'); sync();
    }
    async function offline() {
      const reason = offlineProblem(await options.getStatus());
      if (reason) throw new Error(reason);
      if (typeof options.isBusy === 'function' && options.isBusy()) throw new Error('请先停止当前构建或采集任务，再进行数据清理。');
    }
    function open(mode) {
      if (state.busy || !MODES[mode]) return false;
      state.serial += 1; state.mode = mode; state.open = true; state.plan = null;
      CATEGORIES.forEach(([key, _label, checked]) => { fields[key].checked = checked; });
      title.textContent = MODES[mode].title;
      selection.hidden = mode !== 'cleanup'; previewBox.hidden = confirmation.hidden = execute.hidden = true;
      confirm.checked = false; preview.textContent = mode === 'purge' ? '预览将永久删除的回收区' : '先预览目录与大小';
      execute.textContent = MODES[mode].confirm;
      confirmCopy.textContent = mode === 'purge' ? '我已逐项核对以上目录和大小，确认永久删除，且知道此操作不可恢复。'
        : mode === 'restore' ? '我已核对预览，确认恢复上次清理的数据；存在冲突时应停止而非覆盖。'
          : '我已逐项核对预览，确认清理所选生成数据/录制登记；生成数据移入回收区，原始录制、账号、登录、AI 配置及聊天记忆保留。';
      say(mode === 'purge' ? '永久删除不可恢复。必须先预览，再勾选确认并单独点击永久删除。' : '尚未执行任何操作。先预览，再进行第二次明确确认。');
      if (typeof dialog.showModal === 'function') { if (!dialog.open) dialog.showModal(); }
      else dialog.setAttribute('open', '');
      sync(); preview.focus(); return true;
    }
    function close() {
      if (state.busy) return false;
      state.serial += 1; state.open = false; state.plan = null; confirm.checked = false;
      if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
      start.focus(); sync(); return true;
    }
    async function previewPlan() {
      if (state.busy || !state.open) return false;
      const chosen = Object.fromEntries(CATEGORIES.map(([key]) => [key, fields[key].checked === true]));
      if (state.mode === 'cleanup' && !Object.values(chosen).some(Boolean)) { say('请至少选择一个清理类别。', true); return false; }
      state.busy = true; state.plan = null; confirm.checked = false; previewBox.hidden = execute.hidden = confirmation.hidden = true; sync();
      say('正在确认离线状态并计算预览；尚未清理任何文件…');
      try {
        await offline();
        const result = await options.request('/api/wizard/cleanup/' + MODES[state.mode].preview, state.mode === 'cleanup' ? {selection: chosen} : {});
        if (!result || result.ok !== true) throw result || new Error('预览接口未返回结果。');
        if (typeof result.plan_id !== 'string' || !result.plan_id || !Array.isArray(result.items)
            || !Number.isFinite(result.total_bytes) || result.total_bytes < 0
            || result.items.some(item => !item || typeof item.path !== 'string' || !item.path
              || !Number.isFinite(item.bytes) || item.bytes < 0 || !Number.isInteger(item.files) || item.files < 0)
            || !Number.isFinite(expiry(result.expires_at)) || expiry(result.expires_at) <= Date.now()) throw new Error('预览数据无效或已过期，请重新预览。');
        while (list.firstChild) list.removeChild(list.firstChild);
        result.items.forEach(item => {
          const line = node('li');
          const path = node('code', 'xg-reset-path', String(item.path || '（未提供相对目录）'));
          const label = CATEGORIES.find(([key]) => key === item.category);
          line.append(node('strong', '', (label ? label[1] : String(item.category || '数据')) + '：'), path,
            node('span', 'xg-reset-item-size', formatBytes(item.bytes) + ' · ' + (Number.isInteger(item.files) ? item.files : '?') + ' 个文件 · ' + String(item.action || '')));
          list.append(line);
        });
        if (!result.items.length) list.append(node('li', '', state.mode === 'cleanup'
          ? '未找到可回收文件，仅重置所选资源的生成/录制状态。原始录制文件仍保留。'
          : '没有符合条件的数据目录。'));
        total.textContent = '合计：' + formatBytes(result.total_bytes) + '，共 ' + result.items.length + ' 项。';
        expires.textContent = '预览有效至：' + new Date(expiry(result.expires_at)).toLocaleString() + '；过期需重新预览。';
        state.plan = result;
        previewBox.hidden = false; confirmation.hidden = execute.hidden = result.items.length === 0 && state.mode !== 'cleanup';
        execute.textContent = result.items.length === 0 && state.mode === 'cleanup' ? '确认重置所选资源状态' : MODES[state.mode].confirm;
        if (state.mode === 'cleanup') confirmCopy.textContent = result.items.length === 0
          ? '我确认没有可回收文件，仅重置所选资源的生成/录制状态；随后需新生成、新采集或明确重新登记。账号、登录与聊天配置保留。'
          : '我已逐项核对预览，确认清理所选生成数据/录制登记；生成数据移入回收区，原始录制、账号、登录、AI 配置及聊天记忆保留。';
        say(result.message || '请逐项核对相对目录和大小，再勾选确认。预览不会修改文件。');
      } catch (error) { state.plan = null; say(messageOf(error), true); }
      finally { state.busy = false; sync(); }
      return !!state.plan;
    }
    async function commit() {
      if (state.busy || !state.open || !state.plan || (!state.plan.items.length && state.mode !== 'cleanup') || !confirm.checked) return false;
      if (expiry(state.plan.expires_at) <= Date.now()) { invalidate(); say('预览已过期，请重新预览后再确认。', true); return false; }
      const planId = state.plan.plan_id, mode = state.mode;
      const selected=state.plan.selection || Object.fromEntries(CATEGORIES.map(([key])=>[key,fields[key].checked===true]));
      state.busy = true; sync(); say('正在再次检查离线状态并执行已确认的计划，请勿重复点击…');
      let done = false;
      try {
        await offline();
        if (expiry(state.plan.expires_at) <= Date.now()) throw new Error('预览已过期，请重新预览后再确认。');
        const result = await options.request('/api/wizard/cleanup/' + MODES[mode].execute, {plan_id: planId, confirm: true});
        if (!result || result.ok !== true) throw result || new Error('操作接口未返回结果。');
        state.plan = null; confirm.checked = false; done = true;
        const warning = result.state_warning ? (typeof result.state_warning === 'string' ? result.state_warning
          : result.state_warning.message || '状态标记写入未完成，请重新核对资源状态后继续。') : '';
        const selection=result.selection || selected;
        const onlyProps=selection.props===true&&!['terrain','catalog','names','captures'].some(k=>selection[k]);
        const next=onlyProps?'下一步：单独补生成道具形状，不需要重做地形、录图或地图目录。':GUIDE;
        guideText.textContent=next;Object.entries(guideButtons).forEach(([key,button])=>{button.hidden=onlyProps&&key!=='props';});
        feedback.textContent = (result.message || (mode === 'purge' ? '回收区已按预览清除。' : mode === 'restore' ? '已恢复上次清理。' : '已清理所选数据。'))
          + (mode === 'purge' ? '' : ' ' + next) + (warning ? ' 警告：' + warning : '');
        feedback.setAttribute('role', warning ? 'alert' : 'status'); guide.hidden = mode === 'purge';
        try { if (typeof options.onSuccess === 'function') await options.onSuccess(mode, result); }
        catch (error) { feedback.textContent += ' 操作已完成，但状态刷新失败：' + messageOf(error); feedback.setAttribute('role', 'alert'); }
      } catch (error) { state.plan = null; confirm.checked = false; execute.hidden = confirmation.hidden = true; say(messageOf(error) + ' 如需重试，请先重新预览。', true); }
      finally { state.busy = false; sync(); if (done) close(); }
      return done;
    }
    sync();
    return {open, close, preview: previewPlan, commit, state,
      nodes: {start, restore, purge, dialog, fields, all, preview, execute, confirm, status, feedback, list, guide, selection, confirmation}};
  }
  global.XGDataCleanup = {mount, offlineProblem, formatBytes};
})(typeof window !== 'undefined' ? window : globalThis);
