/*
 * Passkey ceremonies: sign in (usernameless, username-first and autofill),
 * first registration from a setup link, and adding another passkey.
 *
 * Every navigator.credentials call starts from a click, which iOS requires
 * inside an installed web app.
 */
(function () {
  'use strict';

  const b64 = {
    toBuf(s) {
      const pad = '='.repeat((4 - (s.length % 4)) % 4);
      const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
      const out = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out.buffer;
    },
    fromBuf(buf) {
      const bytes = new Uint8Array(buf);
      let s = '';
      for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    },
  };

  function csrf() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRF-Token': csrf() },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ')');
    return data;
  }

  function creationOptions(o) {
    const pk = Object.assign({}, o);
    pk.challenge = b64.toBuf(o.challenge);
    pk.user = Object.assign({}, o.user, { id: b64.toBuf(o.user.id) });
    pk.excludeCredentials = (o.excludeCredentials || []).map((c) => Object.assign({}, c, { id: b64.toBuf(c.id) }));
    return pk;
  }

  function requestOptions(o) {
    const pk = Object.assign({}, o);
    pk.challenge = b64.toBuf(o.challenge);
    pk.allowCredentials = (o.allowCredentials || []).map((c) => Object.assign({}, c, { id: b64.toBuf(c.id) }));
    return pk;
  }

  function registrationJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: b64.fromBuf(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: b64.fromBuf(r.clientDataJSON),
        attestationObject: b64.fromBuf(r.attestationObject),
        transports: typeof r.getTransports === 'function' ? r.getTransports() : [],
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      authenticatorAttachment: cred.authenticatorAttachment || null,
    };
  }

  function assertionJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: b64.fromBuf(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: b64.fromBuf(r.clientDataJSON),
        authenticatorData: b64.fromBuf(r.authenticatorData),
        signature: b64.fromBuf(r.signature),
        userHandle: r.userHandle ? b64.fromBuf(r.userHandle) : null,
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      authenticatorAttachment: cred.authenticatorAttachment || null,
    };
  }

  function friendly(err) {
    if (!err) return 'Something went wrong.';
    if (err.name === 'NotAllowedError') return 'Cancelled, or the passkey timed out. Try again.';
    if (err.name === 'InvalidStateError') return 'This device already has a passkey for Fiscus.';
    if (err.name === 'SecurityError') return 'Passkeys need this site on HTTPS at the configured address.';
    return err.message || String(err);
  }

  function say(root, msg, kind) {
    const box = root.querySelector('[data-passkey-status]');
    if (!box) return;
    box.textContent = msg || '';
    box.className = msg ? 'alert ' + (kind === 'ok' ? 'alert-ok' : 'alert-error') : 'hidden';
  }

  function defaultName() {
    const ua = navigator.userAgent;
    if (/iPhone/.test(ua)) return 'iPhone';
    if (/iPad/.test(ua)) return 'iPad';
    if (/Android/.test(ua)) return 'Android phone';
    if (/Mac/.test(ua)) return 'Mac';
    if (/Windows/.test(ua)) return 'Windows PC';
    return 'Passkey';
  }

  let conditionalAbort = null;

  async function signIn(root, username, conditional) {
    if (conditionalAbort && !conditional) conditionalAbort.abort();
    const next = root.getAttribute('data-next') || '/';
    const start = await post('/auth/passkey/options', username ? { username } : {});
    const opts = { publicKey: requestOptions(start.options) };
    if (conditional) {
      conditionalAbort = new AbortController();
      opts.mediation = 'conditional';
      opts.signal = conditionalAbort.signal;
    }
    const cred = await navigator.credentials.get(opts);
    const done = await post('/auth/passkey/verify', { handle: start.handle, credential: assertionJSON(cred), next });
    window.location.href = done.redirect || '/';
  }

  async function register(root, startUrl, finishUrl, extra) {
    const start = await post(startUrl, extra || {});
    const cred = await navigator.credentials.create({ publicKey: creationOptions(start.options) });
    const nameInput = root.querySelector('[name="passkey_name"]');
    const name = (nameInput && nameInput.value.trim()) || defaultName();
    const done = await post(finishUrl, { handle: start.handle, credential: registrationJSON(cred), name });
    window.location.href = done.redirect || '/';
  }

  function supported() {
    return !!(window.PublicKeyCredential && navigator.credentials);
  }

  function bind(root) {
    root.querySelectorAll('[data-passkey-login]').forEach((el) => {
      if (el.dataset.bound) return;
      el.dataset.bound = '1';
      if (!supported()) {
        say(el, 'This browser does not support passkeys. Open Fiscus in another browser, or on your phone.');
        return;
      }
      el.querySelectorAll('[data-passkey-go]').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
          e.preventDefault();
          say(el, '');
          btn.disabled = true;
          const input = el.querySelector('[name="username"]');
          const username = btn.hasAttribute('data-with-username') && input ? input.value.trim() : '';
          if (btn.hasAttribute('data-with-username') && !username) {
            say(el, 'Enter your username first.');
            btn.disabled = false;
            if (input) input.focus();
            return;
          }
          try {
            await signIn(el, username, false);
          } catch (err) {
            say(el, friendly(err));
            btn.disabled = false;
          }
        });
      });
      // Autofill sign-in where the browser offers it.
      if (PublicKeyCredential.isConditionalMediationAvailable) {
        PublicKeyCredential.isConditionalMediationAvailable().then((ok) => {
          if (ok) signIn(el, '', true).catch(() => {});
        });
      }
    });

    root.querySelectorAll('[data-passkey-register]').forEach((el) => {
      if (el.dataset.bound) return;
      el.dataset.bound = '1';
      const btn = el.querySelector('[data-passkey-go]');
      if (!btn) return;
      if (!supported()) {
        say(el, 'This browser does not support passkeys. Open the link on your phone or another browser.');
        btn.disabled = true;
        return;
      }
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        say(el, '');
        btn.disabled = true;
        const extra = {};
        el.querySelectorAll('[data-extra]').forEach((i) => { extra[i.name] = i.value; });
        try {
          await register(el, el.getAttribute('data-start'), el.getAttribute('data-finish'), extra);
        } catch (err) {
          say(el, friendly(err));
          btn.disabled = false;
        }
      });
      const nameInput = el.querySelector('[name="passkey_name"]');
      if (nameInput && !nameInput.value) nameInput.value = defaultName();
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bind(document);
    if (window.htmx) window.htmx.onLoad((el) => bind(el));
  });
})();
