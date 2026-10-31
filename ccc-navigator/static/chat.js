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
  /* Direction of one line. Quoted text ("...", “...”) is left out, so an English line that quotes Hebrew
     stays left-to-right (and the reverse); the first letter of what remains decides, as in Gmail/Slack.
     A line that is all quote falls back to whichever script has more letters. Markup is skipped. */
  var RTL = /[\u0590-\u05FF\u0600-\u06FF]/, LTR = /[A-Za-z\u00C0-\u024F]/;
  function dirOf(line) {
    var t = String(line || '').replace(/&quot;/g, '"').replace(/<[^>]*>|&[a-z#0-9]+;/gi, ' ');
    var bare = t.replace(/"[^"]*"|“[^”]*”|«[^»]*»|„[^“”]*[“”]/g, ' ');
    var m = bare.match(/[A-Za-z\u00C0-\u024F\u0590-\u05FF\u0600-\u06FF]/);
    if (m) return RTL.test(m[0]) ? 'rtl' : 'ltr';
    var r = (t.match(/[\u0590-\u05FF\u0600-\u06FF]/g) || []).length, l = (t.match(/[A-Za-z\u00C0-\u024F]/g) || []).length;
    return r > l ? 'rtl' : r || l ? 'ltr' : 'auto';
  }

  /* a quoted phrase is laid out as one unit in its own direction, so a Hebrew quote inside an English
     line (or the reverse) does not scramble the words and punctuation around it */
  function isolateQuotes(line) {
    return line.replace(/&quot;((?:(?!&quot;)[^<])+)&quot;/g, function (_, q) {
      return '&quot;<bdi dir="' + dirOf(q) + '">' + q + '</bdi>&quot;';
    });
  }

  function md(text) {
    var out = esc(text);
    var fences = [];
    out = out.replace(/```([a-zA-Z0-9+#_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      fences.push('<pre dir="ltr" data-lang="' + esc(lang) + '"><code>' + code.replace(/\n$/, '') + '</code></pre>');
      return '\u0000' + (fences.length - 1) + '\u0000';
    });
    out = out
      .replace(/`([^`\n]+)`/g, '<code dir="ltr">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    out = out.split(/\n{2,}/).map(function (p) {
      if (/^\u0000\d+\u0000$/.test(p.trim())) return p.trim();
      // each line picks its own direction (Hebrew right-to-left, English left-to-right)
      return '<p>' + p.split('\n').map(function (l) { return '<span class="ln" dir="' + dirOf(l) + '">' + (isolateQuotes(l) || '&nbsp;') + '</span>'; }).join('') + '</p>';
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
      if (/^\[Automatic check-in/.test(ev.text || '')) { acc.push(note(ICONS.dot + ' Automatic check-in')); return; }   // the long standing prompt is not a message from Uri
      var img = (ev.images || []).length;
      acc.push(bubble('me', md(ev.text || '') + (img ? '<div class="stamp">' + img + ' image' + (img > 1 ? 's' : '') + '</div>' : '')));
      return;
    }
    if (t === 'assistant') {
      (ev.blocks || []).forEach(function (b) {
        if (b.kind === 'text' && b.text) acc.push(bubble('them', md(b.text)));
        else if (b.kind === 'thinking') acc.push(step(fold(ICONS.think, '<b>Thought</b> ' + esc((b.text || '').slice(0, 120)), b.text || '')));
        else if (b.kind === 'tool_use') acc.push(step(fold(ICONS.run, '<b>' + esc(b.name || 'Tool') + '</b> ' + esc(b.detail || ''), b.detail || ''), b.name));
      });
      return;
    }
    if (t === 'tool_result') {
      var txt = ev.text || '';
      var head = txt.split('\n')[0].slice(0, 120) || 'Output';
      acc.push(step(fold(ICONS.out, '<b>Output</b> ' + esc(head), txt)));
      return;
    }
    if (t === 'result') {
      var bits = [];
      if (ev.duration_ms) bits.push(Math.round(ev.duration_ms / 1000) + 's');
      var u = ev.token_usage || {};
      if (u.total_tokens) bits.push(size(u.total_tokens) + ' tokens');
      acc.push(step(note(ICONS.tick + ' Turn finished' + (bits.length ? ' &middot; ' + bits.join(' &middot; ') : '')), '', ev.duration_ms));
      return;
    }
    if (t === 'system') acc.push(step(note(ICONS.dot + ' ' + esc(ev.text || ev.kind || 'event'))));
  }

  /* tool calls, outputs, thoughts and status lines are "steps": render() folds each run of them into one line */
  function step(node, tool, ms) {
    node.dataset.step = tool ? 'tool' : '1';
    if (ms) node.dataset.ms = String(ms);
    return node;
  }

  function stepGroup(nodes, id, open) {
    var tools = nodes.filter(function (n) { return n.dataset.step === 'tool'; }).length;
    var ms = nodes.reduce(function (a, n) { return a + (+n.dataset.ms || 0); }, 0);
    var bits = [tools ? tools + ' step' + (tools > 1 ? 's' : '') : nodes.length + ' update' + (nodes.length > 1 ? 's' : '')];
    if (ms) bits.push(Math.round(ms / 1000) + 's');
    var wrap = document.createElement('div');
    wrap.className = 'steps' + (open ? ' open' : '');
    wrap.dataset.id = id;
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'steps-h';
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
    b.innerHTML = '<span class="f-lbl">Worked &middot; ' + bits.join(' &middot; ') + '</span><span class="chev">' + ICONS.chev + '</span>';
    var body = document.createElement('div');
    body.className = 'steps-body';
    nodes.forEach(function (n) { body.appendChild(n); });
    wrap.appendChild(b); wrap.appendChild(body);
    return wrap;
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

  /* what the session is doing right now, read off the end of the transcript (null = idle) */
  function activityOf(ev, live) {
    var l = ev[ev.length - 1];
    if (!l) return null;
    var label = null;
    if (l.type === 'user_text' || l.type === 'tool_result') label = 'Thinking';
    else if (l.type === 'assistant') {
      var b = (l.blocks || [])[(l.blocks || []).length - 1];
      if (!b) return null;
      if (b.kind === 'tool_use') label = 'Running ' + (b.name || 'a tool');
      else if (b.kind === 'thinking') label = 'Thinking';
    }
    if (!label) return null;
    var at = Date.parse(l.ts || '');
    if (at && Date.now() - at > (live ? 15 * 60e3 : 90e3)) return null;   // a stale tail is a stalled or finished session, not a busy one
    return { label: label, since: at || Date.now() };
  }
  function elapsed(since) {
    var s = Math.max(0, Math.round((Date.now() - since) / 1000));
    return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  }
  function normText(t) { return String(t || '').replace(/\s+/g, ' ').trim(); }

  function pendNode(p) {
    var row = bubble('me', md(p.text));
    row.classList.add('pend', p.state);
    row.dataset.live = '1'; row.dataset.pid = String(p.id);
    var st = document.createElement('div');
    st.className = 'stamp pstate';
    if (p.state === 'sending') st.innerHTML = 'Sending&hellip;';
    else if (p.state === 'sent') st.innerHTML = ICONS.tick + ' Sent';
    else st.innerHTML = '<span class="bad">Not sent</span>' + (p.err ? ' &middot; ' + esc(p.err) : '') +
      ' <button type="button" data-retry>Retry</button><button type="button" data-edit>Edit</button>';
    row.appendChild(st);
    return row;
  }

  function typingNode(a) {
    var row = document.createElement('div');
    row.className = 'row them';
    row.dataset.live = '1';
    row.innerHTML = '<div class="typing" role="status" aria-live="polite" data-label="' + esc(a.label) + '">' +
      '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
      '<span class="t-lbl">' + esc(a.label) + '&hellip;</span><span class="t-el">' + elapsed(a.since) + '</span></div>';
    return row;
  }

  // ---- fast switching: cache of recent conversations + prefetch
  var TAIL = 80, CACHE_MAX = 24, FRESH_MS = 15000;
  var cache = new Map(), inflight = new Map();
  function urlFor(sid, engine) {
    return engine === 'pi' ? '/api/pi/transcript?sid=' + encodeURIComponent(sid) : '/api/conversations/' + encodeURIComponent(sid);
  }
  function fetchConv(sid, engine) {
    if (inflight.has(sid)) return inflight.get(sid);
    var p = fetch(urlFor(sid, engine))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) {
        cache.delete(sid); cache.set(sid, { data: d, at: Date.now() });
        while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
        inflight.delete(sid); return d;
      })
      .catch(function (e) { inflight.delete(sid); throw e; });
    inflight.set(sid, p); return p;
  }
  function prefetch(sid, engine) {
    var c = cache.get(sid);
    if (!sid || inflight.has(sid) || (c && Date.now() - c.at < FRESH_MS)) return;
    fetchConv(sid, engine).catch(function () {});
  }
  function sig(events) { var l = events[events.length - 1]; return events.length + ':' + (l ? JSON.stringify(l).length : 0); }

  function render(events, thread, showAll) {
    thread.innerHTML = '';
    var hidden = 0;
    if (!showAll && events.length > TAIL) { hidden = events.length - TAIL; events = events.slice(-TAIL); }
    if (hidden) {
      var more = document.createElement('button');
      more.type = 'button'; more.className = 'chat-more';
      more.textContent = 'Show ' + hidden + ' earlier messages';
      more.setAttribute('data-more', '1');
      thread.appendChild(more);
    }
    if (!events.length) {
      thread.innerHTML = '<div class="chat-empty">Nothing here yet.<br>Start the conversation below.</div>';
      return;
    }
    var open = thread._openSteps || (thread._openSteps = new Set());
    var run = [], runId = '';
    function flush() {
      if (run.length) thread.appendChild(stepGroup(run, runId, open.has(runId)));
      run = [];
    }
    events.forEach(function (ev, i) {
      var acc = [];
      eventToNodes(ev, acc);
      acc.forEach(function (n) {
        if (n.dataset.step) { if (!run.length) runId = 'e' + (hidden + i); run.push(n); }
        else { flush(); thread.appendChild(n); }
      });
    });
    flush();
  }

  function Conversation(root, opts) {
    var sid = opts.sid;
    var engine = opts.engine || '';
    var isPi = engine === 'pi';
    var key = opts.key || (engine ? engine + ':' + sid : sid);
    var thread = root.querySelector('.thread');
    var box = root.querySelector('.composer textarea');
    var send = root.querySelector('.composer .send');
    var askCard = root.querySelector('#askCard');
    var askPoll = null;
    var askState = null;
    var timer = null;

    var piWatch = null;
    function watchPiRun(run) {      // a Pi turn takes a while: refresh the thread until its run finishes
      if (piWatch) clearInterval(piWatch);
      var until = Date.now() + 10 * 60 * 1000;
      piWatch = setInterval(function () {
        cache.delete(sid); load(false);
        fetch('/api/pi/run?id=' + encodeURIComponent(run)).then(function (r) { return r.json(); }).then(function (d) {
          if ((d && d.done) || Date.now() > until) { clearInterval(piWatch); piWatch = null; cache.delete(sid); load(false); if (box) box.placeholder = ''; }
        }).catch(function () {});
      }, 3000);
    }

    var showAll = false, lastSig = null, current = null;
    function paint(d, scroll) {
      var ev = d.events || [];
      var s = sig(ev) + ':' + showAll;
      if (s === lastSig) return;                       // nothing new: leave the DOM (and scroll) alone
      var atEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
      var keep = thread.scrollTop;
      lastSig = s; render(ev, thread, showAll); current = ev;
      reconcile(); decorate();
      // a reader already at the bottom follows new replies, including on background polls (scroll === false)
      if (atEnd || (scroll !== false && keep === 0)) thread.scrollTop = thread.scrollHeight; else thread.scrollTop = keep;
      if (opts.onEngine) opts.onEngine(d.engine || '');
    }
    function load(scroll) {
      var c = cache.get(sid);
      if (c && !lastSig) { paint(c.data, true); thread.scrollTop = thread.scrollHeight; }   // instant from cache
      if (c && Date.now() - c.at < 1500 && lastSig) return;
      fetchConv(sid, engine)
        .then(function (d) { paint(d, scroll); })
        .catch(function () {
          if (!lastSig) thread.innerHTML = '<div class="chat-empty">This conversation could not be loaded.<br>' +
            'Open it in CCC instead.</div>';
        });
    }
    thread.addEventListener('click', function (e) {
      var sh = e.target.closest && e.target.closest('.steps-h');
      if (sh) {
        var g = sh.parentNode, on = g.classList.toggle('open');
        sh.setAttribute('aria-expanded', on ? 'true' : 'false');
        if (on) thread._openSteps.add(g.dataset.id); else thread._openSteps.delete(g.dataset.id);
        return;
      }
      if (e.target.closest && e.target.closest('[data-more]')) { showAll = true; lastSig = null; if (current) paint({ events: current }, false); var top = thread.scrollTop; thread.scrollTop = top; }
    });

    /* ---- sent messages show at once, then "Thinking..." until the reply lands ---- */
    var pending = [], seq = 0, pollT = null, tick = null;
    function countUser(text) {
      var all = 0, same = 0, n = normText(text);
      (current || []).forEach(function (e) { if (e.type === 'user_text') { all++; if (normText(e.text) === n) same++; } });
      return { all: all, same: same };
    }
    function reconcile() {
      pending = pending.filter(function (p) {
        if (p.state !== 'sent') return true;
        var n = countUser(p.text), age = Date.now() - p.sentAt;
        if (n.same > p.baseSame) return false;                         // our message is in the transcript now
        if (age > 45000 && n.all > p.baseAll) return false;            // transcript changed the wording: stop waiting
        return age < 5 * 60e3;
      });
    }
    function activity() {
      var waiting = pending.filter(function (p) { return p.state === 'sent'; });
      if (waiting.length) return { label: 'Thinking', since: waiting[0].at };
      var a = current ? activityOf(current, opts.live) : null;
      if (!a && piWatch) a = { label: 'Pi is working', since: Date.now() };
      return a;
    }
    function decorate() {
      if (!lastSig && !pending.length) return;
      var atEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
      Array.prototype.slice.call(thread.querySelectorAll('[data-live]')).forEach(function (n) { n.remove(); });
      var empty = thread.querySelector('.chat-empty');
      if (empty && pending.length) empty.remove();
      pending.forEach(function (p) { thread.appendChild(pendNode(p)); });
      var a = activity();
      if (a) { thread._since = a.since; thread.appendChild(typingNode(a)); }
      if (atEnd) thread.scrollTop = thread.scrollHeight;
    }
    function busy() { return pending.length > 0 || !!activity(); }

    function hideAskCard() {
      if (askCard) { askCard.style.display = 'none'; askCard.innerHTML = ''; }
      askState = null;
    }

    function renderAskCard(data) {
      if (!askCard) return;
      if (!data || !data.source) { hideAskCard(); return; }
      if (engine === 'codex') { hideAskCard(); return; }
      askCard.innerHTML = '';
      askCard.className = 'ask-card dcard';
      if (data.source === 'wrap') {
        var ask = data.ask || '';
        var actions = data.actions || [];
        var q = document.createElement('div');
        q.className = 'dq';
        q.dir = 'auto';
        q.textContent = ask;
        askCard.appendChild(q);
        var opts = document.createElement('div');
        opts.className = 'dopts';
        actions.forEach(function (a, idx) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'dopt' + (idx === 0 ? '' : '');
          if (idx === 0) b.style.fontWeight = '700';
          b.textContent = a.label || '';
          b.addEventListener('click', function () { answerAsk(data.source, a.label, a.message); });
          opts.appendChild(b);
        });
        var other = document.createElement('button');
        other.type = 'button';
        other.className = 'dopt other';
        other.textContent = 'Other…';
        other.addEventListener('click', function () {
          if (box) { box.focus(); }
        });
        opts.appendChild(other);
        askCard.appendChild(opts);
        askCard.style.display = 'flex';
        askState = { source: data.source };
        return;
      }
      if (data.source === 'menu' || data.source === 'relay') {
        var questions = data.questions || [];
        var qwrap = document.createElement('div');
        qwrap.style.display = 'flex';
        qwrap.style.flexDirection = 'column';
        qwrap.style.gap = '12px';
        var selections = [];
        questions.forEach(function (qq, qi) {
          var qqel = document.createElement('div');
          qqel.style.display = 'flex';
          qqel.style.flexDirection = 'column';
          qqel.style.gap = '8px';
          var qt = document.createElement('div');
          qt.className = 'dq';
          qt.dir = 'auto';
          qt.textContent = qq.question || qq.header || '';
          qqel.appendChild(qt);
          var opts = document.createElement('div');
          opts.className = 'dopts';
          var optsArr = qq.options || [];
          var multi = !!qq.multiSelect;
          selections[qi] = multi ? [] : null;
          optsArr.forEach(function (o) {
            var ob = document.createElement('button');
            ob.type = 'button';
            ob.className = 'dopt';
            var ot = document.createElement('div');
            ot.dir = 'auto';
            ot.textContent = o.label || '';
            ob.appendChild(ot);
            if (o.description) {
              var od = document.createElement('div');
              od.style.fontSize = '11px';
              od.style.color = 'var(--muted)';
              od.dir = 'auto';
              od.textContent = o.description;
              ob.appendChild(od);
            }
            ob.addEventListener('click', function () {
              if (multi) {
                var idx = selections[qi].indexOf(o.label);
                if (idx >= 0) { selections[qi].splice(idx, 1); ob.style.outline = 'none'; }
                else { selections[qi].push(o.label); ob.style.outline = '2px solid var(--primary)'; }
              } else {
                selections[qi] = o.label;
                // reset others
                var btns = opts.querySelectorAll('.dopt');
                btns.forEach(function (x) { x.style.outline = 'none'; });
                ob.style.outline = '2px solid var(--primary)';
              }
            });
            opts.appendChild(ob);
          });
          qqel.appendChild(opts);
          qwrap.appendChild(qqel);
        });
        askCard.appendChild(qwrap);
        var bottom = document.createElement('div');
        bottom.className = 'dopts';
        if (questions.some(function (q){ return q.multiSelect; })) {
          var sendm = document.createElement('button');
          sendm.type = 'button';
          sendm.className = 'dopt';
          sendm.textContent = 'Send';
          sendm.addEventListener('click', function () {
            var ans = [];
            questions.forEach(function (qq, qi) {
              if (qq.multiSelect) {
                ans.push({ index: qi, text: (selections[qi] || []).join(', ') });
              } else if (selections[qi]) {
                ans.push({ index: qi, text: selections[qi] });
              }
            });
            answerAsk(data.source, null, null, ans);
          });
          bottom.appendChild(sendm);
        }
        var other = document.createElement('button');
        other.type = 'button';
        other.className = 'dopt other';
        other.textContent = 'Other…';
        other.addEventListener('click', function () {
          var ob = document.createElement('div');
          ob.className = 'dother';
          ob.innerHTML = '<textarea dir="auto" rows="2" placeholder="Your answer…"></textarea><div class="drow"><button type="button" class="dopt dsend">Send</button><button type="button" class="linkbtn">Cancel</button></div>';
          askCard.appendChild(ob);
          var ta = ob.querySelector('textarea');
          ta.focus();
          ob.querySelector('.dsend').addEventListener('click', function () {
            answerAsk(data.source, null, ta.value);
          });
          ob.querySelector('.linkbtn').addEventListener('click', function () { ob.remove(); });
          ta.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); answerAsk(data.source, null, ta.value); }
          });
        });
        bottom.appendChild(other);
        askCard.appendChild(bottom);
        askCard.style.display = 'flex';
        askState = { source: data.source, questions: questions };
        return;
      }
      hideAskCard();
    }

    function answerAsk(source, label, message, answers) {
      var text = message || label || null;
      if (answers) text = null;
      fetch('/api/ask/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key, source: source, text: text, answers: answers })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok || d.error) { throw new Error((d && d.error) || ('HTTP ' + r.status)); }
          return d;
        });
      }).then(function () {
        hideAskCard();
        var sent = text || (answers ? (function(){ var parts=[]; (answers||[]).forEach(function(a){ if(a.text) parts.push(a.text); }); return parts.join(', '); })() : (label||''));
        var p = { id: ++seq, text: sent, state: 'sent', sentAt: Date.now() };
        p.at = Date.now(); pending.push(p);
        decorate(); thread.scrollTop = thread.scrollHeight;
        cache.delete(sid); lastSig = null;
        load();
        schedule();
      }).catch(function (e) {
        if (typeof toast === 'function') toast('Not sent: ' + (e.message || e));
      });
    }

    function pollAsk() {
      if (engine === 'codex') { hideAskCard(); return; }
      fetch('/api/ask?key=' + encodeURIComponent(key))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { renderAskCard(d); })
        .catch(function () {});
    }

    function schedule() {
      if (pollT) clearTimeout(pollT);
      pollT = setTimeout(function () {
        if (!document.hidden) { load(false); pollAsk(); }
        schedule();
      }, busy() ? 2000 : 8000);
    }
    tick = setInterval(function () {      // elapsed-time counter; rebuild only when the state itself changed
      var t = thread.querySelector('.typing'), a = activity();
      if (!!t !== !!a || (t && a && t.getAttribute('data-label') !== a.label)) { decorate(); return; }
      if (t && a) { var el = t.querySelector('.t-el'); if (el) el.textContent = elapsed(a.since); }
    }, 1000);

    function post(retry) {
      var text = retry ? retry.text : box.value.trim();
      if (!text) return;
      var p = retry || { id: ++seq, text: text };
      var n = countUser(text);
      p.at = Date.now(); p.baseSame = n.same; p.baseAll = n.all; p.state = 'sending'; p.err = '';
      if (!retry) { pending.push(p); box.value = ''; autoGrow(); }
      decorate(); thread.scrollTop = thread.scrollHeight; schedule();
      fetch(isPi ? '/api/pi/send' : '/api/inject-input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(isPi ? { sid: sid, text: text } : { session_id: sid, text: text, mode: 'send' })
      })
        .then(function (r) {
          // a 502 from the CCC proxy is {"error": ...} with no "ok": treat any HTTP error as a failed send
          return r.json().catch(function () { return {}; }).then(function (d) {
            if (!r.ok || (d && d.ok === false)) throw new Error((d && d.error) || 'send failed (HTTP ' + r.status + ')');
            return d;
          });
        })
        .then(function (d) {
          p.state = 'sent'; p.sentAt = Date.now();
          if (isPi && d.run) { box.placeholder = 'Pi is working on it...'; watchPiRun(d.run); }
          if (d && d.warning && opts.onError) opts.onError(d.warning);
          cache.delete(sid); lastSig = null; load();
          decorate(); schedule();
        })
        .catch(function (e) {
          p.state = 'failed'; p.err = e.message || 'Could not send';
          decorate();
          if (opts.onError) opts.onError(p.err);
        });
    }

    function autoGrow() {
      box.style.height = 'auto';
      box.style.height = Math.min(box.scrollHeight, 180) + 'px';
    }

    thread.addEventListener('click', function (e) {
      var row = e.target.closest && e.target.closest('.pend');
      if (!row) return;
      var p = pending.filter(function (x) { return String(x.id) === row.dataset.pid; })[0];
      if (!p) return;
      if (e.target.closest('[data-retry]')) post(p);
      else if (e.target.closest('[data-edit]')) {
        pending = pending.filter(function (x) { return x !== p; });
        if (!box.value) { box.value = p.text; autoGrow(); }
        box.focus(); decorate();
      }
    });

    box.addEventListener('input', autoGrow);
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); post(); }
    });
    send.addEventListener('click', function () { post(); });
    document.addEventListener('visibilitychange', onVisible);
    function onVisible() { if (!document.hidden) load(false); }

    this.refresh = load;
    this.poll = function () {};            // the conversation now keeps itself fresh (see schedule)
    this.destroy = function () {
      if (timer) clearInterval(timer);
      if (piWatch) clearInterval(piWatch);
      if (pollT) clearTimeout(pollT);
      if (tick) clearInterval(tick);
      if (askPoll) clearInterval(askPoll);
      document.removeEventListener('visibilitychange', onVisible);
    };
    pollAsk();
    if (askPoll) clearInterval(askPoll);
    askPoll = setInterval(pollAsk, busy() ? 2000 : 8000);
    schedule();
    load();
  }

  window.NavChat = { prefetch: prefetch, Conversation: Conversation, md: md, render: render, size: size };
})();
