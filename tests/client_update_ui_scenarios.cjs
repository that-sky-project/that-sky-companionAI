'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..', 'app', 'web');
const html = fs.readFileSync(path.join(root, 'desktop.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'desktop.js'), 'utf8')
  .split('// CLIENT_UPDATE_UI_BEGIN')[1].split('// CLIENT_UPDATE_UI_END')[0];
assert.match(html, /启动时自动检查/);
assert.match(html, /检查更新/);
assert.match(html, /跳过此版本/);
assert.match(html, /稍后/);
assert.match(html, /去下载/);

function element() {
  const listeners = {};
  return {
    open: false, disabled: false, textContent: '',
    addEventListener: (event, handler) => { listeners[event] = handler; },
    dispatch: (event) => listeners[event]?.({preventDefault() {}}),
    showModal() { this.open = true; },
    close() { this.open = false; },
    set innerHTML(_) { throw new Error('remote HTML rendered'); },
  };
}

const hit = [];
const payload = '<img src=a onerror=alert(1)>';
const available = {
  status: 'available', current: 'v1.0.5-r10.2-terrainfix.18', auto_check: false,
  release: {tag: 'v1.0.5-r10.2-terrainfix.19', title: payload, notes: payload},
};
let manual = available;
let initialPoll = 0;
const window = {...element(), setTimeout, pywebview: {api: {
  update_get: async () => ++initialPoll === 1 ? {status: 'checking', auto_check: false} : available,
  update_check: async () => manual,
  update_set_auto: async (enabled) => { hit.push(['auto', enabled]); return {ok: true, auto_check: enabled}; },
  update_open_release: async (...args) => { hit.push(['open', ...args]); return true; },
  update_skip: async (...args) => { hit.push(['skip', ...args]); return true; },
  update_later: async (...args) => { hit.push(['later', ...args]); return true; },
}}};
const elements = new Map();
const document = {readyState: 'complete', getElementById(id) {
  assert.ok(html.includes(`id='${id}'`), id);
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
}};
vm.runInNewContext(script, {window, document});
const read = (id) => elements.get('client-update-' + id);

(async () => {
  assert.equal(read('check').disabled, true);
  for (let i = 0; i < 50 && !read('dialog').open; i++) await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(read('auto').checked, false);
  assert.equal(read('dialog').open, true);
  assert.equal(read('heading').textContent, '发现新版本 v1.0.5-r10.2-terrainfix.19（当前 v1.0.5-r10.2-terrainfix.18）');
  assert.equal(read('release-title').textContent, payload);
  assert.equal(read('notes').textContent, payload);
  assert.equal(read('status').textContent, '');
  await read('later').dispatch('click');
  assert.deepEqual(hit.pop(), ['later']);
  assert.equal(read('dialog').open, false);

  read('auto').checked = true;
  await read('auto').dispatch('change');
  assert.deepEqual(hit.pop(), ['auto', true]);
  assert.equal(read('auto').disabled, false);

  await read('check').dispatch('click');
  await read('open').dispatch('click');
  assert.deepEqual(hit.pop(), ['open']);
  await read('check').dispatch('click');
  await read('skip').dispatch('click');
  assert.deepEqual(hit.pop(), ['skip']);

  manual = {status: 'silent'};
  await read('check').dispatch('click');
  assert.equal(read('check').disabled, false);
  assert.equal(read('dialog').open, false);
  assert.equal(read('status').textContent, '');

  manual = {status: 'no_update'};
  await read('check').dispatch('click');
  assert.match(read('status').textContent, /没有需要提示的新版本/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
