const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const file = path.join(__dirname, "../app/web/wizard.js");
const source = fs.readFileSync(file, "utf8").replace(
  'if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);\n  else boot();',
  'window.test = {bindAction, refresh, renderCapture, checkMumu};');
const nodes = new Map();
function el(id) {
  if (!nodes.has(id)) nodes.set(id, {
    hidden: true, disabled: false, textContent: "", dataset: {},
    classList: {contains: name => name === "wz-result" && id === "wz-mumu-result"},
    addEventListener: (name, fn) => { el(id)[name] = fn; }
  });
  return nodes.get(id);
}
const window = {addEventListener() {}};
const document = {getElementById: el};
let fakeMumu;
vm.runInNewContext(source, {
  window, document, setTimeout, clearTimeout, AbortController,
  fetch: async (route) => {
    if (route === "/api/wizard/mumu" && fakeMumu) return {ok:true, status:200, json:async () => fakeMumu};
    throw new Error("synthetic details: connection reset");
  }
});
(async () => {
  const {bindAction, refresh, renderCapture, checkMumu} = window.test;
  const button = el("wz-mumu-check");
  button.textContent = "检查 MuMu";
  let count = 0;
  bindAction("wz-mumu-check", async () => {
    if (++count === 1) {
      button.disabled = true;
      throw new Error("masked stack");
    }
    return true;
  }, "wz-mumu-result");
  await button.click();
  assert.equal(button.disabled, false);
  assert.match(button.textContent, /^重试/);
  assert.match(el("wz-mumu-result").textContent, /重试/);
  assert.doesNotMatch(el("wz-mumu-result").textContent, /masked stack/);
  await button.click();
  assert.equal(button.textContent, "检查 MuMu");
  assert.equal(count, 2);
  assert.equal((await refresh()).ok, false);
  assert.equal(el("wz-summary-error").hidden, false);
  assert.match(el("wz-summary-error-text").textContent, /重试读取向导/);
  renderCapture({state:"error", message:"不支持 USB 真机", last_error_code:"device_scope",
    last_error:"raw log", last_error_detail:{code:"device_scope", text:"fixture detail"}});
  assert.equal(el("wz-cap-msg").textContent, "不支持 USB 真机");
  assert.match(el("wz-cap-detail").textContent, /错误码：device_scope/);
  el("wz-adb").value = "";
  fakeMumu = {ok:false, android_label:"安卓 15", page_size:16384,
    page_size_message:"此页大小未验证录制", reason:"root_user_build",
    message:"adb root 不可用", serial:"emulator-5554"};
  assert.equal(await checkMumu(), false);
  const diagnostics = el("wz-mumu-result").textContent;
  assert.match(diagnostics, /安卓 15/);
  assert.match(diagnostics, /16 KiB/);
  assert.match(diagnostics, /此页大小未验证录制/);
  assert.match(diagnostics, /root_user_build/);
  assert.doesNotMatch(diagnostics, /undefined/);
  renderCapture({state:"ready", mumu:{serial:"emulator-5554", android_label:"安卓 15", pid:413, root:true}});
  assert.equal(el("wz-cap-mumu").textContent, "emulator-5554 · 安卓 15 · 进程 413 · Root 有");
})().catch(error => { console.error(error); process.exitCode = 1; });
