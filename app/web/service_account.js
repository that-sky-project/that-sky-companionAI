(function (root) {
  'use strict';
  function expiryText(value) {
    if (!value) return '未设置';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '待确认' : date.toLocaleString('zh-CN', {hour12: false});
  }
  function subscriptionView(subscription) {
    if (!subscription || typeof subscription.enforcement !== 'boolean') {
      return {billing: '订阅状态暂不可用', state: '待查询', expiry: ''};
    }
    if (subscription.exempt === true) return {billing: '管理员测试账号', state: '无限时长', expiry: ''};
    const billing = '请联系管理员获取密钥';
    const item = subscription.entitlement;
    if (!item) return {billing, state: '', expiry: ''};
    if (item.status === 'paused') return {billing, state: '已暂停', expiry: '有效期暂停计时，请联系管理员恢复'};
    const active = item.active === true && Number(item.remaining_seconds) > 0;
    return {billing, state: active ? '已开通' : '已到期', expiry: '有效期至：' + expiryText(item.expires_at)};
  }
  const MESSAGES = {
    client_running_stop_first: '小光正在运行，请先停止运行，再登录、注册、退出或重置服务密码。',
    invalid_email: '请填写有效的服务邮箱。', invalid_password: '新服务密码须为8至30个字符，含大写字母、小写字母和符号；不可含空白或控制字符，不强制数字。',
    email_verification_required: '请先完成对应邮箱的验证码验证。',
    email_challenge_expired: '本次验证码已过期或客户端已重启，请重新发送。',
    verification_service_changed: '服务地址已变化，请在当前服务重新发送验证码并验证邮箱。',
    invalid_verification_code: '验证码格式不正确。',
    identity_session_invalid: '服务登录已失效，请重新登录。',
    identity_session_required: '请注册或登录小光服务账号。',
    auth_service_unavailable: '暂时无法连接服务，请稍后重试。',
    identity_owner_mismatch: '服务用户编号不一致，请停止操作并联系管理员。',
    too_many_pending_challenges: '待验证请求过多，请稍后重试。',
    rate_limited: '请求过于频繁，请稍后重试。',
    email_delivery_unavailable: '验证邮件暂时发送失败，请稍后重试或联系管理员检查邮箱服务。',
    invalid_credentials: '邮箱或密码不正确。',
    identity_credentials_invalid: '邮箱或密码不正确。',
    email_already_registered: '此邮箱已注册，可直接登录或找回密码。',
    redeem_code_unavailable: '密钥无效、已使用或已过期，请联系管理员。',
    invalid_redeem_code: '请输入有效密钥。',
    redeem_rate_limited: '兑换请求过于频繁，请稍后重试。'
  };
  function errorText(code) { return MESSAGES[code] || '操作未完成，请检查输入后重试；仍失败时联系管理员。'; }
  const NEW_PASSWORD_HINT = '8至30个字符，须含大写字母、小写字母和符号，不强制数字';
  function validNewPassword(value) {
    return typeof value === 'string' && Array.from(value).length >= 8 && Array.from(value).length <= 30 &&
      /[A-Z]/.test(value) && /[a-z]/.test(value) && /[\p{P}\p{S}]/u.test(value) &&
      !/[\s\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
  }
  root.XGServiceAccountView = {subscriptionView, expiryText, errorText, validNewPassword};
  if (typeof document === 'undefined') return;

  function mount() {
    if (document.querySelector('.xg-service-account')) return;
    const wizardMount = document.getElementById('wizard-auth-service-mount');
    const host = document.getElementById('service-account-mount') || wizardMount;
    if (!host) return;
    const card = document.createElement('section');
    card.className = 'settings-card glass-strong xg-service-account';
    card.innerHTML = `
      <div class="xg-service-heading"><h3>小光服务账号</h3><span class="badge" data-sa="badge">未登录</span></div>
      <p class="mini-note">用于小光登录和订阅；与网易游戏账号分开，游戏登录仍在原游戏账号页。</p>
      <div class="auth-status glass-inset" data-sa="summary">正在读取服务登录状态…</div>
      <p class="mini-note" data-sa="service-url"></p>
      <p class="mini-note" data-sa="busy" hidden>小光正在运行：可查看状态；登录、注册、退出和重置密码需先停止运行。</p>
      <div data-sa="identity" hidden>
        <label class="field">我的用户编号 <span class="xg-service-copy"><input data-sa="owner" readonly autocomplete="off"><button class="btn" type="button" data-sa="copy">复制</button></span></label>
        <p class="mini-note" data-sa="email"></p>
        <div class="glass-inset xg-service-subscription"><strong data-sa="billing"></strong><p data-sa="subscription-state"></p><p data-sa="expiry"></p></div>
        <form data-sa="redeem-form" hidden novalidate>
          <label class="field">密钥<input data-sa="redeem-code" type="password" autocomplete="off" spellcheck="false" maxlength="128" placeholder="输入管理员提供的密钥"></label>
          <button class="btn btn-primary" type="submit" data-sa="redeem">兑换</button>
        </form>
      </div>
      <div class="xg-service-tabs" data-sa="auth-tabs" hidden role="tablist" aria-label="小光服务账号操作">
        <button type="button" class="btn" data-mode="login" role="tab">登录</button>
        <button type="button" class="btn" data-mode="register" role="tab">注册</button>
        <button type="button" class="btn" data-mode="reset" role="tab">忘记密码</button>
      </div>
      <p class="mini-note" data-sa="reset-help" hidden>忘记密码：填写小光服务邮箱 → 发送并验证邮箱验证码 → 设置新密码（8至30个字符，含大写、小写和符号） → 重置后重新登录。</p>
      <form data-sa="form" hidden novalidate>
        <label class="field">小光服务邮箱 <input data-sa="input-email" type="email" autocomplete="username" maxlength="254" placeholder="不是网易游戏邮箱登录入口" required></label>
        <div data-sa="verification" hidden>
          <button type="button" class="btn" data-sa="send-code">发送邮箱验证码</button>
          <label class="field">邮箱验证码 <input data-sa="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="6位数字"></label>
          <button type="button" class="btn" data-sa="verify">验证邮箱</button><span class="mini-note" data-sa="verification-status"></span>
        </div>
        <label class="field"><span data-sa="password-label">服务密码</span><input data-sa="password" type="password" autocomplete="current-password" minlength="1" maxlength="256" placeholder="输入已有服务密码" required></label>
        <button type="submit" class="btn btn-primary btn-block" data-sa="submit">登录小光服务</button>
      </form>
      <div class="xg-service-actions"><button class="btn" type="button" data-sa="refresh">刷新状态</button><button class="btn" type="button" data-sa="logout" hidden>退出本机服务账号</button></div>
      <p class="form-error" role="status" aria-live="polite" data-sa="message"></p>`;
    host.prepend(card);
    const style = document.createElement('style');
    style.textContent = '.xg-service-account{padding:18px;margin:12px 0;border:1px solid rgba(255,255,255,.22);border-radius:18px;background:rgba(255,255,255,.10);backdrop-filter:blur(14px)}.xg-service-heading,.xg-service-tabs,.xg-service-actions,.xg-service-copy{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.xg-service-heading{justify-content:space-between}.xg-service-account .field{display:flex;flex-direction:column;gap:6px;margin:12px 0}.xg-service-copy input{flex:1;min-width:180px}.xg-service-account input{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid rgba(160,174,190,.45);border-radius:10px;background:rgba(255,255,255,.10);color:inherit}.xg-service-tabs,.xg-service-actions{margin:12px 0}.xg-service-tabs [aria-selected=true]{box-shadow:inset 0 -2px currentColor}.xg-service-account [hidden]{display:none!important}.xg-service-subscription{padding:12px;border-radius:12px}.xg-service-account button:disabled{opacity:.55;cursor:not-allowed}';
    document.head.append(style);

    const el = name => card.querySelector('[data-sa="' + name + '"]');
    let mode = 'login', info = {}, statusLoaded = false, pending = false, challenge = null, verified = false, cooldown = 0;
    const notify = message => { el('message').textContent = message; };
    async function api(action, body) {
      const response = await fetch('/api/service-account/' + action, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(errorText(result.error));
      return result;
    }
    function render() {
      const authenticated = info.authenticated === true;
      const showSignIn = statusLoaded && !authenticated;
      el('auth-tabs').hidden = !showSignIn;
      el('form').hidden = !showSignIn;
      el('badge').textContent = !statusLoaded ? '查询中' : (authenticated ? '已登录' : '未登录');
      el('summary').textContent = !statusLoaded ? '正在读取服务登录状态…' : (authenticated ? '服务邮箱已验证，登录有效。' : '使用小光服务邮箱注册或登录。');
      if (info.error) el('summary').textContent = errorText(info.error);
      el('service-url').textContent = info.service_url ? '服务地址：' + info.service_url : '';
      el('busy').hidden = !info.busy;
      el('identity').hidden = !info.owner_id;
      el('owner').value = typeof info.owner_id === 'string' ? info.owner_id : '';
      el('email').textContent = info.email ? '服务邮箱：' + info.email : '';
      const subscription = subscriptionView(info.subscription);
      el('billing').textContent = subscription.billing;
      el('subscription-state').textContent = subscription.state;
      el('expiry').textContent = subscription.expiry;
      el('subscription-state').hidden = !subscription.state;
      el('expiry').hidden = !subscription.expiry;
      const canRedeem = info.authenticated === true && !(info.subscription && info.subscription.exempt === true);
      el('redeem-form').hidden = !canRedeem;
      el('redeem').disabled = pending || !canRedeem;
      el('redeem-code').disabled = pending || !canRedeem;
      if (!canRedeem) el('redeem-code').value = '';
      el('verification').hidden = mode === 'login';
      el('reset-help').hidden = !showSignIn || mode !== 'reset';
      el('password-label').textContent = mode === 'login' ? '服务密码（支持原有密码）' : (mode === 'reset' ? '新服务密码：' : '注册密码：') + NEW_PASSWORD_HINT;
      el('password').minLength = mode === 'login' ? 1 : 8;
      el('password').maxLength = mode === 'login' ? 256 : 60;
      el('password').placeholder = mode === 'login' ? '输入已有服务密码' : NEW_PASSWORD_HINT;
      el('password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
      el('submit').textContent = mode === 'login' ? '登录小光服务' : (mode === 'register' ? '注册并登录' : '重置密码');
      el('submit').disabled = !showSignIn || pending || !!info.busy || (mode !== 'login' && !verified);
      el('logout').hidden = !(authenticated || info.configured || info.legacy);
      el('logout').disabled = pending || !!info.busy;
      el('refresh').disabled = pending;
      el('send-code').disabled = pending || cooldown > Date.now();
      el('send-code').textContent = cooldown > Date.now() ? Math.ceil((cooldown - Date.now()) / 1000) + ' 秒后重发' : '发送邮箱验证码';
      el('verify').disabled = pending || !challenge || verified;
      el('verification-status').textContent = verified ? '邮箱已验证' : '';
      card.querySelectorAll('[data-mode]').forEach(button => {
        button.setAttribute('aria-selected', String(button.dataset.mode === mode));
        button.disabled = pending;
      });
    }
    async function refresh() {
      try {
        info = await api('status'); statusLoaded = true; render();
        const routingStatus = Object.freeze({authenticated: info.authenticated === true, legacy: info.legacy === true});
        root.XGServiceAccountStatus = routingStatus;
        root.dispatchEvent(new CustomEvent('xg-service-account-status', {detail: routingStatus}));
      }
      catch (error) { notify(error.message); }
    }
    async function perform(operation) {
      if (pending) return;
      pending = true; notify('正在处理…'); render();
      try { await operation(); }
      catch (error) { notify(error.message || '操作未完成，请稍后重试。'); }
      finally { pending = false; render(); }
    }
    function invalidateVerification() {
      challenge = null; verified = false; el('code').value = ''; render();
    }
    card.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
      mode = button.dataset.mode; el('password').value = ''; invalidateVerification(); notify('');
    }));
    el('input-email').addEventListener('input', invalidateVerification);
    el('send-code').addEventListener('click', () => perform(async () => {
      const result = await api('send-code', {email: el('input-email').value.trim(), purpose: mode === 'reset' ? 'reset' : 'register'});
      challenge = result.challenge_id; verified = false;
      cooldown = Date.now() + Number(result.retry_after || 60) * 1000;
      notify('验证码已发送，请检查邮箱。');
    }));
    el('verify').addEventListener('click', () => perform(async () => {
      try {
        const result = await api('verify', {challenge_id: challenge, code: el('code').value.trim()});
        verified = result.verified === true;
        notify(verified ? '邮箱验证完成，请继续提交。' : '验证未完成，请重试。');
      } finally { el('code').value = ''; }
    }));
    el('form').addEventListener('submit', event => {
      event.preventDefault();
      perform(async () => {
        try {
          if (info.busy) throw new Error(MESSAGES.client_running_stop_first);
          if (mode !== 'login' && !validNewPassword(el('password').value)) throw new Error(MESSAGES.invalid_password);
          if (mode !== 'login' && !verified) throw new Error(MESSAGES.email_verification_required);
          if (mode === 'login' && (Array.from(el('password').value).length < 1 || Array.from(el('password').value).length > 128)) throw new Error(MESSAGES.invalid_credentials);
          if (mode === 'login') {
            await api('login', {email: el('input-email').value.trim(), password: el('password').value});
            notify('小光服务登录成功。');
          } else if (mode === 'register') {
            await api('register', {email: el('input-email').value.trim(), password: el('password').value,
                                   link_existing: false});
            notify('注册成功，已登录小光服务。');
          } else {
            await api('reset-password', {new_password: el('password').value});
            mode = 'login';
            notify('若此邮箱已注册，密码已更新，请重新登录。');
          }
          invalidateVerification();
          await refresh();
          root.dispatchEvent(new Event('xg-service-account-changed'));
        } finally { el('password').value = ''; el('code').value = ''; }
      });
    });
    el('redeem-form').addEventListener('submit', event => {
      event.preventDefault();
      perform(async () => {
        try {
          if (info.authenticated !== true) throw new Error(MESSAGES.identity_session_required);
          const code = el('redeem-code').value.trim();
          el('redeem-code').value = '';
          if (!code || code.length > 128) throw new Error(MESSAGES.invalid_redeem_code);
          const result = await api('redeem', {code});
          if (result.subscription) { info.subscription = result.subscription; render(); }
          await refresh();
          notify(result.replayed ? '此密钥已兑换到当前账号，未重复增加时长。' : '兑换完成，时长已绑定当前服务账号。');
          root.dispatchEvent(new Event('xg-service-account-changed'));
        } finally { el('redeem-code').value = ''; }
      });
    });
    root.addEventListener('pagehide', () => { el('redeem-code').value = ''; });
    el('refresh').addEventListener('click', refresh);
    el('logout').addEventListener('click', () => {
      if (info.busy) return notify(MESSAGES.client_running_stop_first);
      if (!root.confirm('退出本机小光服务账号？仅清本地登录及运行凭据，地形、录图和其他配置保留。')) return;
      perform(async () => {
        await api('logout', {});
        mode = 'login';
        el('password').value = ''; invalidateVerification(); await refresh();
        notify('已退出本机服务账号，地形和录图未删除。');
        root.dispatchEvent(new Event('xg-service-account-changed'));
      });
    });
    el('copy').addEventListener('click', async () => {
      const value = el('owner').value;
      if (!value) return;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(value);
        else { el('owner').select(); if (!document.execCommand('copy')) throw new Error('copy'); }
        notify('用户编号已复制。');
      } catch (_) { el('owner').select(); notify('请复制已选中的用户编号。'); }
    });
    render(); refresh();
    root.setInterval(() => { if (cooldown > Date.now() - 1000) render(); }, 1000);
    root.setInterval(() => { if (!pending && card.getClientRects().length) refresh(); }, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})(typeof window === 'undefined' ? globalThis : window);
