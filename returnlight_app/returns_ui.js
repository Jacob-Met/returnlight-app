(function () {
  'use strict';
  var TOKEN = document.body.dataset.token, SIM = document.body.dataset.sim === '1';
  var main = document.getElementById('rt-main'), sheet = document.getElementById('rt-sheet'), toastEl = document.getElementById('rt-toast');
  var state = { filter: 'all', picks: { item: null, receipt: null }, inHand: true, poll: null, tab: 'item' };
  var MARKS = { refunded: 'Refunded', waiting: 'Waiting for the parcel', decision: 'Needs a decision', reading: 'Reading' };

  function h(tag, attrs) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') e.className = attrs[k]; else if (k === 'text') e.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), attrs[k]); else if (attrs[k] !== false && attrs[k] != null) e.setAttribute(k, attrs[k]);
    });
    [].slice.call(arguments, 2).reduce(function (a, c) { return a.concat(c); }, []).forEach(function (c) { if (c == null) return; e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }
  function money(c) { return '$' + (c / 100).toFixed(2); }
  function api(path, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Returnlight-Token': TOKEN }, body: JSON.stringify(body) };
    return fetch(path, opt).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.ok === false) throw new Error(j.error || 'Something went wrong'); return j; }); });
  }
  function toast(t) { toastEl.textContent = t; toastEl.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(function () { toastEl.hidden = true; }, 3500); }
  function stopPoll() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }
  function go(hash) { location.hash = hash; }

  // ---------------------------------------------------------------- home
  function home() {
    stopPoll(); main.textContent = '';
    main.appendChild(h('h1', { text: 'Returns' }));
    main.appendChild(h('p', { class: 'lead', text: 'Start from what you have in hand. The photo and the receipt are enough.' }));
    var chipsBox = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter' }), list = h('div', { class: 'list', id: 'rt-list' });
    main.appendChild(chipsBox); main.appendChild(list);
    main.appendChild(h('button', { class: 'btn primary fab', id: 'rt-start', text: 'Start a return', onclick: function () { go('#new'); } }));
    api('/api/returns').then(function (d) {
      var rows = d.returns, counts = { all: rows.length, decision: 0, waiting: 0, refunded: 0 };
      rows.forEach(function (r) { if (counts[r.status] !== undefined) counts[r.status]++; });
      [['all', 'All'], ['decision', 'Needs a decision'], ['waiting', 'Waiting for the parcel'], ['refunded', 'Refunded']].forEach(function (c) {
        chipsBox.appendChild(h('button', { class: 'chip', type: 'button', 'aria-pressed': String(state.filter === c[0]), text: c[1] + ' ' + counts[c[0]], onclick: function () { state.filter = c[0]; home(); } }));
      });
      var shown = rows.filter(function (r) { return state.filter === 'all' || r.status === state.filter; });
      if (!shown.length) list.appendChild(h('div', { class: 'empty', text: rows.length ? 'Nothing here.' : 'No returns yet. Start one with a photo of the item and its receipt.' }));
      shown.forEach(function (r) {
        var th = h('div', { class: 'thumb' });
        if (r.phase === 'ready' && r.has_photo) th.appendChild(h('img', { src: '/api/returns/' + r.id + '/item.img', alt: '' })); else th.textContent = r.status === 'refunded' ? '✓' : '📦';
        list.appendChild(h('button', { class: 'row', type: 'button', onclick: function () { go('#r/' + r.id); } }, th,
          h('div', {}, h('b', { text: r.item || 'Reading…' }), h('small', { text: (r.buyer || 'Customer') + ' · ' + new Date(r.created).toLocaleDateString() }), h('div', {}, h('span', { class: 'mk ' + r.status, text: MARKS[r.status] || r.status }))),
          h('div', { class: 'amt', text: r.cents ? money(r.cents) : '' })));
      });
    }).catch(function (e) { list.appendChild(h('div', { class: 'empty', text: e.message })); });
  }

  // ---------------------------------------------------------------- start
  function shrink(file, cb) {
    function fromCanvas(src, w, hh) {
      var s = Math.min(1, 1280 / Math.max(w, hh)), c = document.createElement('canvas'); c.width = Math.round(w * s); c.height = Math.round(hh * s);
      c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
      c.toBlob(function (b) { var fr = new FileReader(); fr.onload = function () { cb(fr.result.split(',')[1], URL.createObjectURL(b)); }; fr.readAsDataURL(b); }, 'image/jpeg', 0.85);
    }
    if (file.type.indexOf('video') === 0) {
      var v = document.createElement('video'); v.muted = true; v.playsInline = true; v.src = URL.createObjectURL(file);
      v.onloadeddata = function () { v.currentTime = Math.min(1, (v.duration || 1) / 2); };
      v.onseeked = function () { fromCanvas(v, v.videoWidth, v.videoHeight); };
    } else {
      var im = new Image(); im.onload = function () { fromCanvas(im, im.naturalWidth, im.naturalHeight); }; im.src = URL.createObjectURL(file);
    }
  }
  function tile(kind, icon, title, sub) {
    var t = h('label', { class: 'tile' + (state.picks[kind] ? ' has' : ''), id: 'rt-tile-' + kind });
    if (state.picks[kind]) { t.appendChild(h('img', { src: state.picks[kind].url, alt: title })); t.appendChild(h('span', { class: 'cap', text: title + ' ✓' })); }
    else { t.appendChild(h('div', {}, h('div', { class: 'ic', text: icon }), h('b', { text: title }), h('small', { text: sub }))); }
    var inp = h('input', { type: 'file', accept: kind === 'item' ? 'image/*,video/*' : 'image/*', id: 'rt-file-' + kind, 'aria-label': title });
    inp.addEventListener('change', function () { if (!inp.files[0]) return; shrink(inp.files[0], function (b64, url) { state.picks[kind] = { b64: b64, url: url }; start(); }); });
    t.appendChild(inp); return t;
  }
  function start() {
    stopPoll(); main.textContent = '';
    main.appendChild(h('p', {}, h('a', { href: '#', text: '‹ Returns', onclick: function (e) { e.preventDefault(); go(''); home(); } })));
    main.appendChild(h('h1', { text: 'Start a return' }));
    main.appendChild(h('p', { class: 'lead', text: 'A photo or short video of the item, and a photo of the receipt or order email.' }));
    main.appendChild(h('div', { class: 'tiles' }, tile('item', '📷', 'The item', 'Photo or short video'), tile('receipt', '🧾', 'The receipt', 'Photo or order email')));
    main.appendChild(h('div', { class: 'seg', role: 'group', 'aria-label': 'Where is the item' },
      h('button', { type: 'button', id: 'rt-inhand', 'aria-pressed': String(state.inHand), text: 'Item is here', onclick: function () { state.inHand = true; start(); } }),
      h('button', { type: 'button', id: 'rt-bypost', 'aria-pressed': String(!state.inHand), text: 'Coming by post', onclick: function () { state.inHand = false; start(); } })));
    var ready = state.picks.item && state.picks.receipt;
    main.appendChild(h('div', { class: 'stack' },
      h('button', { class: 'btn primary full', id: 'rt-read', type: 'button', disabled: ready ? false : 'disabled', text: 'Read it', onclick: function () { submit(false); } }),
      h('button', { class: 'btn quiet full', id: 'rt-sample', type: 'button', text: 'Try with the sample photos', onclick: function () { submit(true); } })));
    if (SIM) main.appendChild(h('p', {}, h('span', { class: 'simtag', text: 'Simulated demo: no real PayPal call' })));
  }
  function submit(sample) {
    var body = { in_hand: state.inHand, sample: sample };
    if (!sample) { body.item = state.picks.item.b64; body.receipt = state.picks.receipt.b64; }
    main.textContent = '';
    main.appendChild(h('div', { class: 'working' }, h('div', { class: 'spin' }), h('b', { text: 'Reading the receipt and looking at the item…' }), h('small', { text: 'This happens on this machine. Nothing is sent anywhere.' })));
    api('/api/returns', body).then(function (d) { state.picks = { item: null, receipt: null }; state.tab = 'item'; go('#r/' + d.id); }).catch(function (e) { toast(e.message); start(); });
  }

  // ---------------------------------------------------------------- the card
  function place(el, box) { el.style.left = box[0] * 100 + '%'; el.style.top = box[1] * 100 + '%'; el.style.width = box[2] * 100 + '%'; el.style.height = box[3] * 100 + '%'; }
  function stage(r) {
    var wrap = h('div', { class: 'wrap' }), kind = state.tab;
    var img = h('img', { src: '/api/returns/' + r.id + '/' + kind + '.img', alt: kind === 'item' ? 'Photo of the item with what was noticed marked' : 'Photo of the receipt with the matching line marked' });
    wrap.appendChild(img);
    if (kind === 'item' && !r.facts.marks_hidden) {
      r.facts.marks.forEach(function (m, i) { var b = h('div', { class: 'box ' + m.kind }, h('i', { text: String(i + 1) })); place(b, m.box); wrap.appendChild(b); });
    } else if (kind === 'receipt' && r.eval.line && r.eval.line.box) {
      var b = h('div', { class: 'box line' }, h('i', { text: 'Matching line' })); place(b, r.eval.line.box); b.firstChild.style.background = '#b8860b'; wrap.appendChild(b);
    }
    return h('div', { class: 'stage' }, wrap);
  }
  function card(r) {
    stopPoll(); main.textContent = '';
    var ev = r.eval, f = r.facts, refunded = !!r.refund && r.refund.state === 'COMPLETED';
    main.appendChild(h('p', {}, h('a', { href: '#', text: '‹ Returns', onclick: function (e) { e.preventDefault(); go(''); home(); } })));
    var left = h('div', { class: 'stagecol' });
    var hasPics = r.pictures && r.pictures.item;
    if (hasPics) {
      left.appendChild(h('div', { class: 'tabs', role: 'group', 'aria-label': 'Picture' },
        h('button', { type: 'button', 'aria-pressed': String(state.tab === 'item'), text: 'Item', onclick: function () { state.tab = 'item'; card(r); } }),
        h('button', { type: 'button', 'aria-pressed': String(state.tab === 'receipt'), text: 'Receipt', onclick: function () { state.tab = 'receipt'; card(r); } })));
      left.appendChild(stage(r));
    }
    var b = h('div', { class: 'body' });
    b.appendChild(h('span', { class: 'mk ' + r.status, text: MARKS[r.status] }));
    b.appendChild(h('h2', { text: (r.buyer ? r.buyer + ' · ' : '') + f.item }));
    if (f.summary && !f.marks_hidden) b.appendChild(h('p', { class: 'lead', text: 'I noticed: ' + f.summary }));
    else if (f.summary) b.appendChild(h('p', { class: 'lead', text: 'Noted as you said: ' + COND(r) }));
    if (f.marks.length && !f.marks_hidden) b.appendChild(h('div', { class: 'said' }, f.marks.map(function (m, i) { return h('span', { text: (i + 1) + ' · ' + m.label }); })));
    if (r.analysis === 'typed') b.appendChild(h('div', { class: 'banner err', text: 'The local model was not available. Tell me about the item below, and enter the payment.' }));
    b.appendChild(h('div', { class: 'amount-label', style: null }, h('small', { text: 'Refund' })));
    b.appendChild(h('div', { class: 'big', id: 'rt-amount', text: money(ev.cents) }));
    var facts = h('div', { class: 'facts' });
    if (ev.line) {
      var sel = h('select', { id: 'rt-line', 'aria-label': 'Order line', onchange: function (e) { api('/api/returns/' + r.id + '/setline', { line: +e.target.value }).then(function (d) { card(d.return); }); } });
      f.lines.forEach(function (l, i) { var o = h('option', { value: i, text: l.name + (l.qty > 1 ? ' × ' + l.qty : '') + ' · ' + money(l.cents * l.qty) }); if (i === f.line) o.selected = true; sel.appendChild(o); });
      facts.appendChild(h('div', { class: 'fact ok', id: 'rt-f-line' }, h('span', { class: 'ic', text: '🧾' }), h('div', {}, h('small', { text: 'Matches this line on the receipt' + (f.order_id ? ' · order ' + f.order_id : '') }), sel)));
    } else facts.appendChild(h('div', { class: 'fact bad', id: 'rt-f-line' }, h('span', { class: 'ic', text: '🧾' }), h('div', {}, h('small', { text: 'Order line' }), f.lines.length ? pickLine(r) : h('b', { text: 'Not found on the receipt' }))));
    if (ev.window) facts.appendChild(h('div', { class: 'fact ' + (ev.window.inside ? 'ok' : 'bad'), id: 'rt-f-window' }, h('span', { class: 'ic', text: '📅' }), h('div', {}, h('small', { text: 'Return window' }), h('b', { text: 'Day ' + ev.window.days + ' of ' + ev.window.limit + (ev.window.inside ? ' · inside the window' : ' · outside the window') }))));
    else facts.appendChild(h('div', { class: 'fact warn', id: 'rt-f-window' }, h('span', { class: 'ic', text: '📅' }), h('div', {}, h('small', { text: 'Return window' }), h('b', { text: 'No purchase date found' }))));
    facts.appendChild(h('div', { class: 'fact ' + (f.condition === 'used' || (f.condition === 'damaged' && !f.not_buyer_fault) ? 'warn' : 'ok'), id: 'rt-f-cond' }, h('span', { class: 'ic', text: '🔍' }), h('div', {}, h('small', { text: 'Condition' }), h('b', { text: COND(r) }))));
    b.appendChild(facts);
    ev.notes.forEach(function (n) { b.appendChild(h('div', { class: 'banner err', text: n })); });
    if (r.said.length) b.appendChild(h('div', { class: 'said' }, r.said.map(function (s) { return h('span', { text: '“' + s + '”' }); })));
    if (refunded) {
      b.appendChild(h('div', { class: 'banner ok', id: 'rt-done', text: 'Refunded ' + money(r.refund.cents) + (r.refund.refund_id ? ' · PayPal ref ' + r.refund.refund_id : '') + (r.simulated ? ' (simulated)' : '') }));
    } else {
      b.appendChild(sayBox(r));
      if (r.refund && r.refund.state === 'UNKNOWN') b.appendChild(h('div', { class: 'banner err', text: 'PayPal did not confirm. Check the refund case before doing anything else.' }));
      b.appendChild(actions(r));
      b.appendChild(payment(r));
    }
    var c = h('div', { class: 'card wide', id: 'rt-card' }, left, b);
    if (!hasPics) { c.className = 'card'; c.removeChild(left); }
    main.appendChild(c);
  }
  function COND(r) {
    var f = r.facts, t = r.conditions[f.condition] || f.condition;
    if (f.condition === 'damaged') t = f.not_buyer_fault ? 'Damaged on arrival' : 'Damaged';
    var why = r.reasons[f.reason || ''];
    return f.reason ? t + ' · ' + why : t;
  }
  function pickLine(r) {
    var s = h('select', { 'aria-label': 'Pick the order line', onchange: function (e) { if (e.target.value !== '') api('/api/returns/' + r.id + '/setline', { line: +e.target.value }).then(function (d) { card(d.return); }); } }, h('option', { value: '', text: 'Pick the line…' }));
    r.facts.lines.forEach(function (l, i) { s.appendChild(h('option', { value: i, text: l.name + ' · ' + money(l.cents) })); }); return s;
  }
  function sayBox(r) {
    var inp = h('input', { id: 'rt-say', type: 'text', placeholder: 'Correct me: “it was opened but unused”', maxlength: '300', 'aria-label': 'Correct what I read' });
    function send(t) { t = (t || inp.value).trim(); if (t.length < 2) return; api('/api/returns/' + r.id + '/say', { text: t }).then(function (d) { card(d.return); }).catch(function (e) { toast(e.message); }); }
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
    var mic = h('button', { class: 'round', type: 'button', id: 'rt-mic', 'aria-label': 'Say a correction', text: '🎙', onclick: function () { record(mic, r); } });
    return h('div', {}, h('div', { class: 'say' }, inp, mic, h('button', { class: 'round', type: 'button', id: 'rt-say-go', 'aria-label': 'Apply', text: '↵', onclick: function () { send(); } })),
      h('div', { class: 'sug' }, ['It was opened but unused', 'Wrong size, not damaged', 'It was used'].map(function (t) { return h('button', { type: 'button', text: t, onclick: function () { send(t); } }); })));
  }
  function record(btn, r) {
    if (btn._rec) { btn._rec.stop(); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { toast('Recording is not available here. Type instead.'); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var chunks = [], rec = new MediaRecorder(stream); btn._rec = rec; btn.classList.add('on');
      rec.ondataavailable = function (e) { chunks.push(e.data); };
      rec.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); }); btn._rec = null; btn.classList.remove('on');
        var fr = new FileReader(); fr.onload = function () {
          api('/api/returns/' + r.id + '/hear', { audio: fr.result.split(',')[1] }).then(function (d) { toast('Heard: ' + d.heard); card(d.return); }).catch(function (e) { toast(e.message); });
        }; fr.readAsDataURL(new Blob(chunks));
      };
      rec.start(); setTimeout(function () { if (btn._rec) btn._rec.stop(); }, 12000);
    }).catch(function () { toast('Microphone not allowed. Type instead.'); });
  }
  function actions(r) {
    var ev = r.eval, box = h('div', { class: 'stack' });
    if (r.parcel !== 'received') {
      box.appendChild(h('div', { class: 'banner', text: 'Waiting for the parcel. The refund unlocks when it arrives.' }));
      box.appendChild(h('button', { class: 'btn primary full', id: 'rt-parcel', type: 'button', text: 'The parcel has arrived', onclick: function () { api('/api/returns/' + r.id + '/parcel', {}).then(function (d) { card(d.return); }); } }));
      return box;
    }
    var can = r.facts.capture_id && ev.cents > 0;
    box.appendChild(h('button', { class: 'btn full ' + (ev.clear ? 'primary' : 'warn'), id: 'rt-refund', type: 'button', disabled: can ? false : 'disabled', text: (ev.clear ? 'Refund ' : 'Refund anyway · ') + money(ev.cents) + (r.buyer ? ' to ' + r.buyer : ''), onclick: function () { confirmSheet(r); } }));
    if (!can) box.appendChild(h('small', { class: 'muted', text: r.facts.capture_id ? 'Nothing to refund.' : (r.facts.payment_note || 'Link the PayPal payment below first.') }));
    return box;
  }
  function payment(r) {
    var d = h('details', { class: 'det', open: !r.facts.capture_id ? 'open' : false }, h('summary', { text: r.facts.capture_id ? 'Payment ' + r.facts.capture_id + ' · paid ' + money(r.facts.paid_cents || 0) : 'Payment: not linked' }));
    var inp = h('input', { id: 'rt-capture', placeholder: 'PayPal capture ID', 'aria-label': 'PayPal capture ID' });
    d.appendChild(inp);
    d.appendChild(h('button', { class: 'btn full', type: 'button', text: 'Check payment', onclick: function () { api('/api/returns/' + r.id + '/payment', { capture_id: inp.value }).then(function (x) { card(x.return); }).catch(function (e) { toast(e.message); }); } }));
    return d;
  }

  // the deliberate part: a sheet, then press and hold
  function confirmSheet(r) {
    var ev = r.eval; sheet.textContent = ''; sheet.hidden = false;
    var p = h('div', { class: 'panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Confirm refund' });
    p.appendChild(h('h2', { text: 'Send ' + money(ev.cents) + (r.buyer ? ' to ' + r.buyer : '') + '?' }));
    p.appendChild(h('p', { class: 'lead', text: r.facts.item + ' · PayPal payment ' + r.facts.capture_id + '. This goes through Returnlight’s refund case and cannot be undone.' + (r.simulated ? ' Simulated: no real PayPal call.' : '') }));
    if (!ev.clear) ev.notes.forEach(function (n) { p.appendChild(h('div', { class: 'banner err', text: n })); });
    var err = h('div', { class: 'banner err', hidden: 'hidden' });
    var fill = h('div', { class: 'fill' }), btn = h('button', { class: 'hold' + (ev.clear ? '' : ' warn'), id: 'rt-hold', type: 'button' }, fill, h('span', { text: 'Press and hold to send ' + money(ev.cents) }));
    var t0 = 0, raf = 0, done = false;
    function tick() { var k = Math.min(1, (performance.now() - t0) / 1200); fill.style.width = k * 100 + '%'; if (k >= 1) fire(); else raf = requestAnimationFrame(tick); }
    function begin(e) { if (done) return; e.preventDefault && e.preventDefault(); t0 = performance.now(); cancelAnimationFrame(raf); raf = requestAnimationFrame(tick); }
    function end() { if (done) return; cancelAnimationFrame(raf); fill.style.width = '0'; }
    function fire() {
      done = true; btn.disabled = true; btn.lastChild.textContent = 'Sending…';
      api('/api/returns/' + r.id + '/refund', { held: true, cents: ev.cents, override: !ev.clear }).then(function (d) { sheet.hidden = true; toast('Refund sent'); card(d.return); })
        .catch(function (e) { done = false; btn.disabled = false; fill.style.width = '0'; btn.lastChild.textContent = 'Press and hold to send ' + money(ev.cents); err.textContent = e.message; err.hidden = false; });
    }
    btn.addEventListener('pointerdown', begin); ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (n) { btn.addEventListener(n, end); });
    btn.addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && !e.repeat) begin(e); }); btn.addEventListener('keyup', end);
    p.appendChild(err); p.appendChild(h('div', { class: 'stack' }, btn, h('button', { class: 'btn quiet full', id: 'rt-cancel', type: 'button', text: 'Not now', onclick: function () { sheet.hidden = true; } })));
    sheet.appendChild(p); btn.focus();
  }

  // ---------------------------------------------------------------- routing
  function open(id) {
    stopPoll(); main.textContent = ''; main.appendChild(h('div', { class: 'working' }, h('div', { class: 'spin' }), h('b', { text: 'Reading the receipt and looking at the item…' }), h('small', { text: 'On this machine only.' })));
    function load() { api('/api/returns/' + id).then(function (d) { if (d.return.phase === 'ready') { stopPoll(); card(d.return); } }).catch(function (e) { stopPoll(); main.textContent = e.message; }); }
    load(); state.poll = setInterval(load, 1200);
  }
  function route() {
    sheet.hidden = true; var hs = location.hash;
    if (hs === '#new') start(); else if (/^#r\/[a-f0-9]{16}$/.test(hs)) open(hs.slice(3)); else home();
  }
  window.addEventListener('hashchange', route); route();
})();
