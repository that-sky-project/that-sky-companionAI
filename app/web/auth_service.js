/* NetEase login readiness and pending security verification stay in the local UI. */
(() => {
  const panel = document.createElement('details');
  const mount = document.getElementById('wizard-auth-service-mount');
  panel.className = 'xg-auth-service-panel';
  if (!mount) panel.style.cssText = 'margin:12px;padding:12px;border:1px solid #888;border-radius:10px;';
  panel.innerHTML = `<summary>网易账号安全验证与自动续期</summary>
    <p>请先登录小光服务账号，再使用网易游戏账号登录。游戏邮箱与手机号的自动续期由私有服务托管。</p>
    <p role="status" data-status></p><div data-pending></div>`;
  if (mount) mount.append(panel);
  else document.body.prepend(panel);
  const $ = s => panel.querySelector(s);
  const status = text => { $('[data-status]').textContent = text; };
  async function api(path, body) {
    const r = await fetch('/api/auth-service/' + path, {
      method: body ? 'POST' : 'GET', credentials: 'same-origin',
      headers: body ? {'Content-Type':'application/json'} : {},
      ...(body ? {body: JSON.stringify(body)} : {})
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.message || j.error || '操作未完成');
    return j;
  }
  async function continueJob(id) {
    try {
      const job = await api('job', {job_id:id});
      const box = $('[data-pending]'); box.replaceChildren();
      const text = document.createElement('p');
      const ch = job.challenge || {};
      text.textContent = ch.kind === 'upload_sms'
        ? `请向 ${ch.number || ''} 发送短信：${ch.content || ''}` : '请输入收到的短信验证码';
      box.append(text);
      const code = document.createElement('input');
      code.autocomplete = 'one-time-code'; code.inputMode = 'numeric';
      if (ch.kind !== 'upload_sms') box.append(code);
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = ch.kind === 'upload_sms' ? '我已发送，继续验证' : '提交验证码';
      box.append(button);
      button.onclick = async () => {
        button.disabled = true;
        try {
          await api('continue', {job_id:id, kind:ch.kind, code:code.value});
          code.value = ''; status('验证完成，凭据已更新。'); await refresh();
        } catch (e) { status(e.message); button.disabled = false; }
      };
    } catch (e) { status(e.message); }
  }
  async function refresh() {
    try {
      const info = await api('status');
      if (!info.configured) {
        $('[data-pending]').replaceChildren();
        status('请先注册或登录小光服务账号，再使用网易游戏账号登录。');
        return;
      }
      const r = await api('accounts');
      status('已连接，账号登录和自动续期由私有服务负责。');
      $('[data-pending]').replaceChildren();
      for (const a of r.accounts || []) {
        if (a.pending_login) {
          panel.open = true;
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = `${a.alias || '小光账号'}：继续安全验证`;
          button.onclick = () => continueJob(a.pending_login);
          $('[data-pending]').append(button);
        } else if (a.status === 'reauth_required') {
          panel.open = true;
          const note = document.createElement('p');
          note.textContent = `${a.alias || '小光账号'}：长期凭据已不可用，请在原登录页面重新完成验证。`;
          $('[data-pending]').append(note);
        }
      }
    } catch (e) { status(e.message); }
  }
  window.addEventListener("xg-service-account-changed", refresh);
  refresh();
  setInterval(() => { if (!panel.open) refresh(); }, 30000);
})();
