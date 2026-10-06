/*
 * Live subtitles for English calls, and translating the words you pick in them.
 *
 * Speaking: our microphone track (the one published to LiveKit) also goes,
 * downsampled to 16 kHz PCM by subtitle_worklet.js, over a WebSocket to this
 * app's backend (/stt?token=...), where Whisper turns it into text. What comes
 * back is our subtitle: shown here, and passed on to the room as LiveKit data
 * packets (topic "subtitle", reliable):
 *
 *   {"v": 1, "seg": 12, "text": "How was your weekend?", "final": true}
 *
 * A segment's text grows while it is spoken ("final": false) and settles at
 * the pause that ends it ("final": true).
 *
 * Reading: everyone's subtitles show as a caption on their avatar tile
 * (<div data-subtitle-identity="...">) and as lines in the transcript
 * (<div id="subtitle-log">). Select words there and right-click (or tap the
 * Translate chip on a touch screen): /translate asks the backend's local LLM,
 * with the line they were said in as context, for the language picked in
 * <select data-translate-to> (default 繁體中文).
 *
 * livekit_bridge.js drives it: attach(room, url, token), listen(micTrack),
 * resume(), render() every animation frame, and detach().
 */
(function () {
  if (window.liveSubtitles) return;

  const TOPIC = 'subtitle';
  // A caption stays this long after its last words.
  const CAPTION_MS = 6000;
  const CAPTION_CHARS = 140;
  const MAX_LINES = 300;
  // Audio the socket has not sent yet; beyond this, chunks are dropped (slow link).
  const MAX_BUFFERED = 64000;
  const SCRIPT = document.currentScript;
  const WORKLET_URL = '/subtitle_worklet.js' + (SCRIPT ? new URL(SCRIPT.src, location.href).search : '');
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  const STATUS_TEXT = {
    downloading: 'Downloading the speech model (first start only)…',
    loading: 'Loading the speech model…',
    offline: 'Subtitles reconnecting…',
  };

  function baseUrl(url, scheme) {
    const u = new URL(url, window.location.href);
    const secure = u.protocol === 'https:' || u.protocol === 'wss:' || window.location.protocol === 'https:';
    u.protocol = scheme === 'ws' ? (secure ? 'wss:' : 'ws:') : (secure ? 'https:' : 'http:');
    return u.toString().replace(/\/$/, '');
  }

  // The newest words of a long caption, cut at a word.
  function tail(text) {
    if (text.length <= CAPTION_CHARS) return text;
    const cut = text.slice(-CAPTION_CHARS);
    const space = cut.indexOf(' ');
    return '…' + (space > 0 && space < 30 ? cut.slice(space + 1) : cut);
  }

  window.liveSubtitles = {
    room: null,
    url: '',
    token: '',
    socket: null,
    retry: null,
    retryMs: 1000,
    audio: null,
    track: null,
    phase: '',
    message: '',
    // Server segment numbers restart with each socket; ours never repeat.
    segs: new Map(),
    nextSeg: 0,
    // The transcript, oldest first: {key, identity, name, text, final, el}.
    lines: [],
    byKey: new Map(),
    // identity -> {text, at}: what each tile's caption shows.
    captions: new Map(),
    logEl: null,

    attach(room, url, token) {
      this.detach();
      this.room = room;
      this.url = url;
      this.token = token;
      this.onData = (payload, participant, kind, topic) => this.receive(payload, participant, topic);
      room.on(window.LivekitClient.RoomEvent.DataReceived, this.onData);
      this.connect();
    },

    detach() {
      if (this.room && this.onData) this.room.off(window.LivekitClient.RoomEvent.DataReceived, this.onData);
      this.room = null;
      clearTimeout(this.retry);
      this.retry = null;
      const socket = this.socket;
      this.socket = null;
      if (socket) socket.close();
      this.stopAudio();
      this.segs.clear();
      this.lines = [];
      this.byKey.clear();
      this.captions.clear();
      if (this.logEl) this.logEl.replaceChildren();
      this.setPhase('');
      closeTranslateUi();
    },

    // --- our voice -> /stt -> our subtitles ------------------------------------------

    connect() {
      if (!this.room || this.socket) return;
      const ws = new WebSocket(baseUrl(this.url, 'ws') + '/stt?token=' + encodeURIComponent(this.token));
      ws.binaryType = 'arraybuffer';
      this.socket = ws;
      ws.onopen = () => { this.retryMs = 1000; };
      ws.onmessage = (ev) => this.fromServer(ev.data);
      ws.onclose = () => {
        if (this.socket !== ws) return;
        this.socket = null;
        this.segs.clear();
        if (!this.room) return;
        this.setPhase('offline');
        this.retry = setTimeout(() => this.connect(), this.retryMs);
        this.retryMs = Math.min(15000, this.retryMs * 2);
      };
    },

    fromServer(data) {
      let msg;
      try { msg = JSON.parse(data); } catch (e) { return; }
      if (msg.type === 'status') {
        this.setPhase(msg.phase === 'ready' ? '' : msg.phase, msg.message || '');
        return;
      }
      const room = this.room;
      if (msg.type !== 'subtitle' || !room) return;
      if (!this.segs.has(msg.seg)) this.segs.set(msg.seg, this.nextSeg++);
      const seg = this.segs.get(msg.seg);
      if (msg.final) this.segs.delete(msg.seg);
      const me = room.localParticipant;
      this.show(me.identity, me.name || me.identity, seg, msg.text, !!msg.final);
      const packet = encoder.encode(JSON.stringify({ v: 1, seg, text: msg.text, final: !!msg.final }));
      me.publishData(packet, { reliable: true, topic: TOPIC }).catch(() => {});
    },

    // Our microphone track, as published to LiveKit (same echo cancellation and noise suppression).
    async listen(track) {
      this.stopAudio();
      this.track = track;
      const media = track && track.mediaStreamTrack;
      if (!media) return;
      let context;
      try {
        context = new (window.AudioContext || window.webkitAudioContext)();
        await context.audioWorklet.addModule(WORKLET_URL);
        if (this.track !== track) { context.close().catch(() => {}); return; }
        const source = context.createMediaStreamSource(new MediaStream([media]));
        const node = new AudioWorkletNode(context, 'subtitle-pcm');
        // A silent path to the speakers keeps the worklet running.
        const sink = context.createGain();
        sink.gain.value = 0;
        source.connect(node);
        node.connect(sink);
        sink.connect(context.destination);
        node.port.onmessage = (ev) => this.sendAudio(ev.data);
        if (context.state !== 'running') context.resume().catch(() => {});
        this.audio = { context, source, node };
      } catch (error) {
        if (context) context.close().catch(() => {});
        console.warn('No subtitles for our microphone:', error);
      }
    },

    stopAudio() {
      if (this.audio) {
        this.audio.node.port.onmessage = null;
        this.audio.context.close().catch(() => {});
      }
      this.audio = null;
      this.track = null;
    },

    resume() {
      if (this.audio && this.audio.context.state !== 'running') this.audio.context.resume().catch(() => {});
    },

    sendAudio(buffer) {
      const ws = this.socket;
      const me = this.room && this.room.localParticipant;
      // Muted: nothing is sent, and the server closes the segment being spoken.
      if (!ws || ws.readyState !== WebSocket.OPEN || !me || !me.isMicrophoneEnabled) return;
      if (ws.bufferedAmount > MAX_BUFFERED) return;
      ws.send(buffer);
    },

    setPhase(phase, message) {
      this.phase = phase || '';
      this.message = message || '';
      const text = this.phase === 'error' ? this.message : (STATUS_TEXT[this.phase] || '');
      document.querySelectorAll('[data-subtitle-status]').forEach((el) => {
        if (el.textContent !== text) el.textContent = text;
      });
    },

    // --- everyone's subtitles ----------------------------------------------------------

    receive(payload, participant, topic) {
      if (topic !== TOPIC || !participant) return;
      let msg;
      try { msg = JSON.parse(decoder.decode(payload)); } catch (e) { return; }
      if (!msg || typeof msg.text !== 'string' || typeof msg.seg !== 'number') return;
      this.show(participant.identity, participant.name || participant.identity, msg.seg, msg.text, !!msg.final);
    },

    show(identity, name, seg, text, final) {
      const key = identity + '#' + seg;
      let line = this.byKey.get(key);
      if (line && line.final && !final) return;  // a partial that arrived after its final
      if (!text) {
        // The segment turned out to hold no words: take its partial back.
        if (line) this.drop(line);
        const caption = this.captions.get(identity);
        if (caption && caption.key === key) this.captions.delete(identity);
        return;
      }
      if (!line) {
        line = { key, identity, name, text: '', final: false, el: null };
        this.lines.push(line);
        this.byKey.set(key, line);
        while (this.lines.length > MAX_LINES) this.drop(this.lines[0]);
      }
      line.text = text;
      line.final = final;
      this.captions.set(identity, { key, text, at: performance.now() });
      this.paint(line);
    },

    drop(line) {
      const at = this.lines.indexOf(line);
      if (at >= 0) this.lines.splice(at, 1);
      this.byKey.delete(line.key);
      if (line.el) line.el.remove();
    },

    log() {
      const el = document.getElementById('subtitle-log');
      if (el !== this.logEl) {
        // A new transcript element (page change, view switch): fill it again.
        this.logEl = el;
        if (el) {
          el.replaceChildren();
          for (const line of this.lines) {
            line.el = null;
            this.paint(line, el);
          }
        }
      }
      return el;
    },

    paint(line, log = this.log()) {
      if (!log) return;
      const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 32 && !selectionIn(log);
      if (!line.el || line.el.parentNode !== log) {
        line.el = document.createElement('p');
        line.el.className = 'subtitle-line';
        line.el.dataset.identity = line.identity;
        const who = document.createElement('span');
        who.className = 'subtitle-who';
        const said = document.createElement('span');
        said.className = 'subtitle-said';
        line.el.append(who, said);
        log.appendChild(line.el);
      }
      line.el.classList.toggle('subtitle-partial', !line.final);
      line.el.firstChild.textContent = line.name;
      if (line.el.lastChild.textContent !== line.text) line.el.lastChild.textContent = line.text;
      if (follow) log.scrollTop = log.scrollHeight;
    },

    // Every animation frame: the captions on the tiles, and the status line.
    render() {
      this.log();
      const now = performance.now();
      document.querySelectorAll('[data-subtitle-identity]').forEach((el) => {
        const caption = this.captions.get(el.dataset.subtitleIdentity);
        const text = caption && now - caption.at < CAPTION_MS ? tail(caption.text) : '';
        let span = el.firstElementChild;
        if (!span) {
          span = document.createElement('span');
          span.className = 'subtitle-caption';
          el.appendChild(span);
        }
        if (span.textContent !== text && !selectionIn(span)) span.textContent = text;
      });
      if (this.phase) this.setPhase(this.phase, this.message);
    },

    // For tests: the transcript as {name, text, final}.
    transcript() {
      return this.lines.map((line) => ({ name: line.name, identity: line.identity, text: line.text, final: line.final }));
    },
  };

  // --- translating the words picked in the subtitles -------------------------------------

  const ui = { menu: null, popup: null, chip: null, request: 0 };

  function selectionIn(el) {
    const sel = window.getSelection();
    return !!(sel && !sel.isCollapsed && sel.rangeCount && el.contains(sel.getRangeAt(0).commonAncestorContainer));
  }

  // The words selected in a subtitle, with the line they were said in (the context).
  function picked() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const text = sel.toString().replace(/\s+/g, ' ').trim();
    if (!text) return null;
    const range = sel.getRangeAt(0);
    const node = range.commonAncestorContainer;
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    const host = el && el.closest('#subtitle-log, [data-subtitle-identity]');
    if (!host) return null;
    const line = el.closest('.subtitle-line');
    let context = text;
    if (line) context = line.querySelector('.subtitle-said').textContent;
    else if (host.matches('[data-subtitle-identity]')) context = host.textContent;
    return { text, context, rect: range.getBoundingClientRect() };
  }

  function target() {
    const select = document.querySelector('select[data-translate-to]');
    if (select && select.value) {
      const option = select.options[select.selectedIndex];
      return { code: select.value, label: option ? option.textContent : select.value };
    }
    return { code: 'zh-TW', label: '繁體中文' };
  }

  function closeTranslateUi() {
    for (const key of ['menu', 'popup', 'chip']) {
      if (ui[key]) ui[key].remove();
      ui[key] = null;
    }
  }

  function place(el, x, y) {
    document.body.appendChild(el);
    const r = el.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
    el.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
  }

  function showMenu(x, y, pick) {
    closeTranslateUi();
    const menu = document.createElement('div');
    menu.className = 'subtitle-menu';
    menu.setAttribute('role', 'menu');
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.textContent = 'Translate to ' + target().label;
    item.addEventListener('click', () => translate(pick));
    menu.appendChild(item);
    ui.menu = menu;
    place(menu, x, y);
    item.focus();
  }

  function showChip(pick) {
    if (ui.chip) ui.chip.remove();
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'subtitle-chip';
    chip.textContent = 'Translate';
    chip.addEventListener('click', () => translate(pick));
    ui.chip = chip;
    place(chip, pick.rect.left, pick.rect.bottom + 8);
  }

  function showPopup(pick, to, result) {
    if (ui.menu) ui.menu.remove();
    if (ui.chip) ui.chip.remove();
    ui.menu = ui.chip = null;
    if (!ui.popup) {
      ui.popup = document.createElement('div');
      ui.popup.className = 'subtitle-popup';
      ui.popup.setAttribute('role', 'dialog');
      ui.popup.setAttribute('aria-label', 'Translation');
    }
    const popup = ui.popup;
    popup.replaceChildren();
    const head = document.createElement('div');
    head.className = 'subtitle-popup-head';
    const lang = document.createElement('span');
    lang.textContent = to.label;
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', closeTranslateUi);
    head.append(lang, close);
    const source = document.createElement('div');
    source.className = 'subtitle-popup-source';
    source.textContent = pick.text;
    const out = document.createElement('div');
    out.className = 'subtitle-popup-text';
    if (!result) {
      out.textContent = 'Translating…';
      out.dataset.state = 'busy';
    } else if (result.error) {
      out.textContent = result.error;
      out.dataset.state = 'error';
    } else {
      out.textContent = result.text;
      out.dataset.state = 'done';
      out.lang = to.code;
    }
    popup.append(head, source, out);
    const below = pick.rect.bottom + 10;
    place(popup, pick.rect.left, below + 160 > window.innerHeight ? pick.rect.top - 170 : below);
  }

  async function translate(pick) {
    const client = window.liveSubtitles;
    const to = target();
    const request = ++ui.request;
    showPopup(pick, to, null);
    let result;
    try {
      const resp = await fetch(baseUrl(client.url, 'http') + '/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: client.token, text: pick.text, context: pick.context, target: to.code }),
      });
      const data = await resp.json().catch(() => ({}));
      result = resp.ok ? { text: data.translation } : { error: data.error || 'Translation failed (' + resp.status + ').' };
    } catch (error) {
      result = { error: 'Translation failed: ' + ((error && error.message) || error) };
    }
    if (request === ui.request && ui.popup) showPopup(pick, to, result);
  }

  document.addEventListener('contextmenu', (ev) => {
    const pick = picked();
    if (!pick) return;  // the browser's own menu everywhere else
    ev.preventDefault();
    showMenu(ev.clientX, ev.clientY, pick);
  });

  // Touch screens have no right click: a chip appears under the selection.
  const coarse = window.matchMedia ? window.matchMedia('(pointer: coarse)') : null;
  let chipTimer = null;
  document.addEventListener('selectionchange', () => {
    if (!coarse || !coarse.matches) return;
    clearTimeout(chipTimer);
    chipTimer = setTimeout(() => {
      const pick = picked();
      if (pick) showChip(pick);
      else if (ui.chip) { ui.chip.remove(); ui.chip = null; }
    }, 350);
  });

  document.addEventListener('pointerdown', (ev) => {
    for (const key of ['menu', 'popup', 'chip']) {
      if (ui[key] && ui[key].contains(ev.target)) return;
    }
    if (ui.menu || ui.popup) closeTranslateUi();
  }, true);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && (ui.menu || ui.popup)) closeTranslateUi();
  });

  const style = document.createElement('style');
  style.textContent = [
    '[data-subtitle-identity]{position:absolute;left:8px;right:8px;bottom:38px;display:flex;justify-content:center;pointer-events:none;z-index:2}',
    '.subtitle-caption{display:inline-block;max-width:100%;padding:4px 10px;border-radius:8px;background:rgba(15,23,42,.74);color:#fff;',
    'font-size:15px;line-height:1.35;text-align:center;pointer-events:auto;user-select:text;-webkit-user-select:text}',
    '.subtitle-caption:empty{display:none}',
    // Our own tile shows the camera picture in its bottom right corner.
    '.participant-tile[data-local="1"][data-camera="on"] [data-subtitle-identity]{right:calc(24% + 16px)}',
    '#subtitle-log{user-select:text;-webkit-user-select:text}',
    '#subtitle-log:empty::before{content:"What people say appears here as they speak.";color:#9ca3af;font-size:13px}',
    '#subtitle-log .subtitle-line{margin:0 0 6px;font-size:15px;line-height:1.45;color:#111827}',
    '#subtitle-log .subtitle-who{font-weight:600;color:#6d28d9;margin-right:6px;user-select:none;-webkit-user-select:none}',
    '#subtitle-log .subtitle-partial .subtitle-said{color:#6b7280}',
    '.subtitle-menu{position:fixed;z-index:2000;background:#fff;border:1px solid #e5e7eb;border-radius:10px;',
    'box-shadow:0 12px 32px rgba(15,23,42,.18);padding:4px;font:14px Inter,system-ui,sans-serif}',
    '.subtitle-menu button{display:block;width:100%;text-align:left;border:0;background:none;padding:8px 12px;border-radius:6px;cursor:pointer;color:#111827}',
    '.subtitle-menu button:hover,.subtitle-menu button:focus{background:#f5f3ff;color:#6d28d9;outline:none}',
    '.subtitle-chip{position:fixed;z-index:2000;border:0;border-radius:999px;background:#7c3aed;color:#fff;padding:6px 14px;',
    'font:600 14px Inter,system-ui,sans-serif;box-shadow:0 8px 20px rgba(124,58,237,.35)}',
    '.subtitle-popup{position:fixed;z-index:2000;width:min(320px,calc(100vw - 16px));background:#fff;border:1px solid #e5e7eb;',
    'border-radius:12px;box-shadow:0 16px 40px rgba(15,23,42,.2);padding:10px 12px 12px;font-family:Inter,system-ui,sans-serif}',
    '.subtitle-popup-head{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:600;color:#6d28d9}',
    '.subtitle-popup-head button{border:0;background:none;font-size:18px;line-height:1;color:#6b7280;cursor:pointer;padding:0 2px}',
    '.subtitle-popup-source{margin-top:6px;font-size:13px;color:#6b7280}',
    '.subtitle-popup-text{margin-top:4px;font-size:18px;line-height:1.4;color:#111827;user-select:text}',
    '.subtitle-popup-text[data-state=busy]{color:#9ca3af;font-size:14px}',
    '.subtitle-popup-text[data-state=error]{color:#b91c1c;font-size:14px}',
  ].join('');
  document.head.appendChild(style);
})();
