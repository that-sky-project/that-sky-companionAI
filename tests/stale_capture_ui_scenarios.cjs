'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ROOT = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(ROOT, 'app/web/desktop.js'), 'utf8');
const begin = source.indexOf('// RESOURCE_WORKFLOW_MODEL_BEGIN');
const end = source.indexOf('// RESOURCE_WORKFLOW_MODEL_END');
assert(begin >= 0 && end > begin);
const context = vm.createContext({});
vm.runInContext(source.slice(begin, end) + '\nglobalThis.model=resourceStatus;', context);
const model = context.model;
const results = [];
function test(name, fn) { fn(); results.push({name, ok: true}); }
const pending = () => ({ok: true, account: 'fixture-a', gameVersion: '0.16.8',
  steps: [{id: 'capture', done: false}, {id: 'data', done: false}]});
const stale = [{record_dir: 'CandleSpace_auto_old', scene: 'CandleSpace',
  version: '0.16.7', apk_build: 421589}];
const staleCap = () => ({state: 'stale', worker_pid: null, recorded: [], stale,
  current_version: '0.16.8', stale_version: '0.16.7',
  message: '游戏已更新到 0.16.8，之前的遇境录制是 0.16.7 的，需要用 MuMu 重新录一次遇境（只需遇境一张）。'});
const currentCap = () => ({state: 'ready', worker_pid: null, recorded: ['CandleSpace'],
  stale: [], current_version: '0.16.8',
  last_registered: {record_dir: 'CandleSpace_auto_new', scene: 'CandleSpace', account: 'fixture-a'}});

test('stale recording is not shown as capture success', () => {
  const view = model.captureView(staleCap(), pending());
  assert.notEqual(view.state, 'success');
  assert.doesNotMatch(view.title, /采集成功/);
  assert.match(view.title, /重新录制遇境/);
  assert.match(view.message, /游戏已更新到 0\.16\.8/);
  assert.match(view.message, /之前的遇境录制是 0\.16\.7 的/);
  assert.match(view.message, /需要用 MuMu 重新录一次遇境（只需遇境一张）/);
});
test('stale recording wins over an old green catalog flag', () => {
  const oldGreen = {ok: true, account: 'fixture-a', gameVersion: '0.16.8',
    steps: [{id: 'capture', done: true, detail: {account: 'fixture-a',
      result: {ok: true, account: 'fixture-a', maps: 115, variants: 1}}}, {id: 'data', done: true}]};
  const view = model.captureView(staleCap(), oldGreen);
  assert.notEqual(view.state, 'success');
  assert.match(view.message, /重新录一次遇境/);
});
test('catalog build is blocked with the same stale reason', () => {
  const view = model.catalogView({...pending(), capture: {stale, recorded: []}}, null);
  assert.notEqual(view.state, 'success');
  assert.match(view.message, /游戏已更新到 0\.16\.8/);
  assert.match(view.message, /0\.16\.7/);
  assert.match(view.detail, /重新录/);
});
test('current-version recording still reaches capture success', () => {
  const view = model.captureView(currentCap(), pending());
  assert.equal(view.state, 'success');
  assert.match(view.title, /已自动登记/);
});
test('new game version asks for game-data rebuild without a new login', () => {
  const summary = {...pending(), gameData: {needs_rebuild: true, current_version: '0.16.8',
    stale: [{version: '0.16.7', dir: '0.16.7-421589'}]},
    steps: [{id: 'capture', done: false}, {id: 'data', done: false, needs_rebuild: true}]};
  const view = model.taskView({ok: true, running: false, task: null}, summary);
  assert.match(view.title, /新版本需要重新生成游戏数据/);
  assert.match(view.message, /游戏已更新到 0\.16\.8/);
  assert.match(view.message, /0\.16\.7/);
  assert.match(view.detail, /不用重新登录/);
});
test('current-version catalog is still certified normally', () => {
  const ready = {ok: true, account: 'fixture-a', gameVersion: '0.16.8',
    catalog: {account: 'fixture-a', certified: true, maps: 115, variants: 1},
    steps: [{id: 'capture', done: true, detail: {account: 'fixture-a',
      result: {ok: true, account: 'fixture-a', maps: 115, variants: 1}}}, {id: 'data', done: true}]};
  assert.equal(model.catalogView(ready, null).state, 'success');
});
console.log(JSON.stringify({passed: results.length, failed: 0, tests: results.map(row => row.name)}, null, 2));
