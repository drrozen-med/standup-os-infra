/* Chat pane: turns a CCC conversation's events into a readable thread.
   Prose is a bubble; everything the agent did to get there (commands, output,
   thinking, turn notes) is a one-line row that opens on tap. */
(function () {
  'use strict';

  var MODES = ['send', 'steer', 'answer'];

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* enough markdown to read an agent reply: fenced code, inline code, bold, links */
  function md(text) {
    var out = esc(text);
    var fences = [];
    out = out.replace(/```([a-zA-Z0-9+#_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      fences.push('<pre data-lang="' + esc(lang) + '"><code>' + code.replace(/\n$/, '') + '</code></pre>');
      return '\u0000' + (fences.length - 1) + '\u0000';
    });
    out = out
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    out = out.split(/\n{2,}/).map(function (p) {
      if (/^\u0000\d+\u0000$/.test(p.trim())) return p.trim();
      return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).join('');
    return out.replace(/\u0000(\d+)\u0000/g, function (_, i) { return fences[+i]; });
  }

  function stamp(ts) {
    if (!ts) return '';
    var d = new Date(ts);
    if (isNaN(d)) return '';
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' +
        d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function size(n) {
    if (!n && n !== 0) return '';
    return n > 1e6 ? (n / 1e6).toFixed(1) + 'm' : n > 999 ? Math.round(n / 1000) + 'k' : String(n);
  }

  /* a row the user can open: one summary line, the detail underneath */
  function fold(icon, label, detail) {
    var wrap = document.createElement('div');
    wrap.className = 'fold-wrap';
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'fold';
    b.setAttribute('aria-expanded', 'false');
    b.innerHTML = '<span class="tw">' + icon + '</span><span class="f-lbl">' + label +
      '</span><span class="chev">' + ICONS.chev + '</span>';
    b.addEventListener('click', function () {
      var open = wrap.classList.toggle('open');
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    wrap.appendChild(b);
    if (detail) {
      var body = document.createElement('div');
      body.className = 'fold-body';
      body.textContent = detail;
      wrap.appendChild(body);
    }
    return wrap;
  }

  function bubble(side, html) {
    var row = document.createElement('div');
    row.className = 'row ' + (side === 'me' ? 'me' : 'them');
    var b = document.createElement('div');
    b.className = 'bubble';
    b.innerHTML = html;
    row.appendChild(b);
    return row;
  }

  function note(text) {
    var row = document.createElement('div');
    row.className = 'row them';
    row.innerHTML = '<div class="stamp">' + text + '</div>';
    return row;
  }

  function eventToNodes(ev, acc) {
    var t = ev.type;
    if (t === 'user_text') {
      var img = (ev.images || []).length;
      acc.push(bubble('me', md(ev.text || '') + (img ? '<div class="stamp">' + img + ' image' + (img > 1 ? 's' : '') + '</div>' : '')));
      return;
    }
    if (t === 'assistant') {
      (ev.blocks || []).forEach(function (b) {
        if (b.kind === 'text' && b.text) acc.push(bubble('them', md(b.text)));
        else if (b.kind === 'thinking') acc.push(fold(ICONS.think, '<b>Thought</b> ' + esc((b.text || '').slice(0, 120)), b.text || ''));
        else if (b.kind === 'tool_use') acc.push(fold(ICONS.run, '<b>' + esc(b.name || 'Tool') + '</b> ' + esc(b.detail || ''), b.detail || ''));
      });
      return;
    }
    if (t === 'tool_result') {
      var txt = ev.text || '';
      var head = txt.split('\n')[0].slice(0, 120) || 'Output';
      acc.push(fold(ICONS.out, '<b>Output</b> ' + esc(head), txt));
      return;
    }
    if (t === 'result') {
      var bits = [];
      if (ev.duration_ms) bits.push(Math.round(ev.duration_ms / 1000) + 's');
      var u = ev.token_usage || {};
      if (u.total_tokens) bits.push(size(u.total_tokens) + ' tokens');
      acc.push(note(ICONS.tick + ' Turn finished' + (bits.length ? ' &middot; ' + bits.join(' &middot; ') : '')));
      return;
    }
    if (t === 'system') acc.push(note(ICONS.dot + ' ' + esc(ev.text || ev.kind || 'event')));
  }

  var ICONS = {
    chev: '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M3.5 2l3 3-3 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    think: '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><circle cx="5" cy="5" r="3.2" stroke="currentColor" stroke-width="1.2"/></svg>',
    run: '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2.5 2.5L6 5l-3.5 2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    out: '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2.5 3h5M2.5 5h5M2.5 7h3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
    tick: '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 5.2L4 7.2 8 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    dot: '<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="2.4" fill="currentColor"/></svg>',
    send: '<svg width="17" height="17" viewBox="0 0 20 20" fill="none"><path d="M3 10h13M11 4.5L16.5 10 11 15.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>'
  };

  function render(events, thread) {
    thread.innerHTML = '';
    if (!events.length) {
      thread.innerHTML = '<div class="chat-empty">Nothing here yet.<br>Start the conversation below.</div>';
      return;
    }
    var acc = [];
    events.forEach(function (ev) { eventToNodes(ev, acc); });
    acc.forEach(function (n) { thread.appendChild(n); });
  }

  function Conversation(root, opts) {
    var sid = opts.sid;
    var thread = root.querySelector('.thread');
    var box = root.querySelector('.composer textarea');
    var send = root.querySelector('.composer .send');
    var timer = null;

    function load(scroll) {
      fetch('/api/conversations/' + encodeURIComponent(sid))
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) {
          render(d.events || [], thread);
          if (scroll !== false) thread.scrollTop = thread.scrollHeight;
          if (opts.onEngine) opts.onEngine(d.engine || '');
        })
        .catch(function () {
          thread.innerHTML = '<div class="chat-empty">This conversation could not be loaded.<br>' +
            'Open it in CCC instead.</div>';
        });
    }

    function post() {
      var text = box.value.trim();
      if (!text || send.disabled) return;
      send.disabled = true;
      fetch('/api/inject-input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, text: text, mode: 'send' })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok === false) throw new Error(d.error || 'send failed');
          box.value = '';
          autoGrow();
          load();
        })
        .catch(function (e) {
          if (opts.onError) opts.onError(e.message || 'Could not send');
        })
        .then(function () { send.disabled = false; });
    }

    function autoGrow() {
      box.style.height = 'auto';
      box.style.height = Math.min(box.scrollHeight, 180) + 'px';
    }

    box.addEventListener('input', autoGrow);
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); post(); }
    });
    send.addEventListener('click', post);

    this.refresh = load;
    this.poll = function (on, every) {
      if (timer) clearInterval(timer);
      if (on) timer = setInterval(load, every || 5000);
    };
    this.destroy = function () { if (timer) clearInterval(timer); };
    load();
  }

  window.NavChat = { Conversation: Conversation, md: md, render: render, size: size };
})();
