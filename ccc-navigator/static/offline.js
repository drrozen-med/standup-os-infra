// Offline UX for the Navigator: registers the service worker (sw.js), shows a clear banner when the page is
// running on a saved copy, and holds the few writes that are safe to replay (decision answers, question
// answers) until the Mac is reachable again. Loaded first in <head> so it wraps every later fetch().
(function () {
  'use strict';
  var OUTBOX_KEY = 'ccc-nav-outbox';
  var REPLAYABLE = ['/api/decisions/answer', '/api/ask/answer'];
  var origFetch = window.fetch.bind(window);
  var state = { offline: false, cachedAt: 0, lastLive: Date.now(), flushing: false, note: '' };
  var bar = null, probeTimer = null, checkingConnection = null;

  function ls(op, k, v) { try { return op === 'get' ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { return null; } }
  function outbox() { try { return JSON.parse(ls('get', OUTBOX_KEY) || '[]'); } catch (e) { return []; } }
  function saveOutbox(l) { ls('set', OUTBOX_KEY, JSON.stringify(l)); render(); }
  function hhmm(t) { var d = new Date(t); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  function ago(t) {
    var m = Math.max(0, Math.round((Date.now() - t) / 60000));
    return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago';
  }

  function ensureBar() {
    if (bar || !document.body) return bar;
    var css = document.createElement('style');
    css.textContent = '#nav-offline{position:fixed;top:0;left:0;right:0;z-index:99999;padding:calc(env(safe-area-inset-top,0px) + 6px) 14px 8px;' +
      'font:600 13px/1.35 system-ui,sans-serif;text-align:center;color:#1a1200;background:#f0b429;box-shadow:0 2px 8px rgba(0,0,0,.25);display:none}' +
      '#nav-offline.ok{background:#2f9e5b;color:#fff}#nav-offline small{display:block;font-weight:500;opacity:.85}' +
      'body.nav-offline{padding-top:54px}';
    document.head.appendChild(css);
    bar = document.createElement('div'); bar.id = 'nav-offline'; bar.setAttribute('role', 'status');
    document.body.insertBefore(bar, document.body.firstChild);
    return bar;
  }
  function render() {
    if (!ensureBar()) return;
    var q = outbox().length;
    document.body.classList.toggle('nav-offline', state.offline);
    if (state.offline) {
      bar.className = '';
      bar.innerHTML = 'Offline: showing the saved copy' +
        '<small>' + (state.cachedAt ? 'Saved ' + hhmm(state.cachedAt) + ' (' + ago(state.cachedAt) + ')' : 'Last live ' + hhmm(state.lastLive)) +
        ' · reconnects by itself' + (q ? ' · ' + q + ' answer' + (q > 1 ? 's' : '') + ' waiting to send' : '') + '</small>';
      bar.style.display = 'block';
    } else if (state.note) {
      bar.className = 'ok'; bar.textContent = state.note; bar.style.display = 'block';
    } else if (q) {
      bar.className = ''; bar.textContent = q + ' answer' + (q > 1 ? 's' : '') + ' waiting to send'; bar.style.display = 'block';
    } else bar.style.display = 'none';
  }
  function flash(msg) { state.note = msg; render(); setTimeout(function () { state.note = ''; render(); }, 4000); }

  function goOffline(cachedAt) {
    state.cachedAt = cachedAt || state.cachedAt;
    if (!state.offline) { state.offline = true; startProbe(); }
    render();
  }
  function goOnline() {
    var was = state.offline;
    state.offline = false; state.cachedAt = 0; state.lastLive = Date.now(); stopProbe();
    if (was) flushOutbox().then(function (n) { flash(n ? 'Back online · sent ' + n + ' queued answer' + (n > 1 ? 's' : '') : 'Back online'); });
    else render();
  }

  // A cached response, or one failed request, can just mean a slow server (the Mac is busy), not a lost
  // connection. Only a refused/failed connection or a 5xx (Mac asleep, tunnel down) counts as "down", and
  // it has to fail twice in a row. A probe that is merely slow means the Mac is reachable, so stay online.
  function probeOnce() {
    var timeout = new Promise(function (resolve) { setTimeout(function () { resolve('slow'); }, 15000); });
    var net = origFetch('/api/version', { cache: 'no-store' })
      .then(function (r) { return r.status < 500 ? 'up' : 'down'; }, function () { return 'down'; });
    return Promise.race([net, timeout]);
  }
  function verifyConnection(cachedAt) {
    if (checkingConnection) return checkingConnection;
    var started = Date.now();
    checkingConnection = probeOnce()
      .then(function (a) {
        if (a !== 'down') return a;
        return new Promise(function (r) { setTimeout(r, 3000); }).then(probeOnce);   // one retry before believing it
      })
      .then(function (res) {
        if (res !== 'down') goOnline();
        else if (state.lastLive <= started) goOffline(cachedAt);
      })
      .then(function () { checkingConnection = null; }, function () { checkingConnection = null; });
    return checkingConnection;
  }

  // While offline, poke the real network (the SW never answers /api/version) until it answers.
  function startProbe() {
    if (probeTimer) return;
    probeTimer = setInterval(function () {
      origFetch('/api/version', { cache: 'no-store' }).then(function (r) { if (r.status < 500) goOnline(); }).catch(function () {});
    }, 8000);
  }
  function stopProbe() { if (probeTimer) { clearInterval(probeTimer); probeTimer = null; } }

  function flushOutbox() {
    if (state.flushing) return Promise.resolve(0);
    state.flushing = true;
    var sent = 0;
    function next() {
      var list = outbox();
      if (!list.length) return Promise.resolve();
      var item = list[0];
      return origFetch(item.path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: item.body })
        .then(function (r) {
          if (r.status >= 500) throw new Error('server');   // still unwell: keep it, retry later
          saveOutbox(outbox().slice(1)); if (r.ok) sent++;    // 4xx = rejected for good (already answered, gone): drop it
          return next();
        });
    }
    return next().catch(function () {}).then(function () { state.flushing = false; return sent; });
  }

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    var path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    return origFetch(input, init).then(function (res) {
      if (method === 'GET') {
        if (res.headers.get('x-nav-from-cache')) verifyConnection(Number(res.headers.get('x-nav-cached-at')) || 0);
        else if (res.status < 500 && state.offline && path !== '/api/version') goOnline();
        else if (res.status < 500) state.lastLive = Date.now();
        return res;
      }
      // a write that the Mac (or the front door) answered with 5xx did not happen: treat like offline
      return res.status >= 500 && res.status !== 501 ? queueOrFail(path, init) : res;
    }, function (err) {
      if (method === 'GET') { verifyConnection(); throw err; }
      return queueOrFail(path, init);
    });
  };

  function jsonRes(obj, status) { return new Response(JSON.stringify(obj), { status: status, headers: { 'content-type': 'application/json' } }); }
  function queueOrFail(path, init) {
    verifyConnection();
    if (REPLAYABLE.indexOf(path) < 0) {
      var msg = 'Could not reach Navigator. Please try again.';
      if (window.toast) try { window.toast(msg); } catch (e) {}
      return Promise.resolve(jsonRes({ ok: false, offline: true, error: msg }, 503));
    }
    var body = (init && init.body) || '{}', parsed = {};
    try { parsed = JSON.parse(body); } catch (e) {}
    var l = outbox(); l.push({ path: path, body: body, at: Date.now() }); saveOutbox(l);
    // answer shaped like the real one so the page treats it as done
    return Promise.resolve(jsonRes({ ok: true, queued: true, delivery: 'queued, sends when the Mac is back',
      answer: { choice: parsed.choice || '', note: parsed.note || '' } }, 200));
  }

  window.addEventListener('online', function () { origFetch('/api/version', { cache: 'no-store' }).then(function (r) { if (r.status < 500) goOnline(); }).catch(function () {}); });
  window.addEventListener('offline', function () { verifyConnection(); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && state.offline) origFetch('/api/version', { cache: 'no-store' }).then(function (r) { if (r.status < 500) goOnline(); }).catch(function () {});
  });
  document.addEventListener('DOMContentLoaded', function () { render(); if (outbox().length && !state.offline) flushOutbox().then(render); });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {}); });
  }
})();
