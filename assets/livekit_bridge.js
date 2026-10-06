/*
 * Browser half of the Reflex <-> LiveKit bridge for avatar calls in English.
 *
 * What leaves this browser: the microphone (an audio track) and, up to 25
 * times a second, a 60-byte face packet with the 51 ARKit blendshapes and the
 * head pose (LiveKit data packet, topic "face", lossy). The camera picture
 * never leaves it: FaceTracker (face_tracker.js) reads it locally, and the
 * token only lets us publish the microphone, so the server refuses video.
 * For the subtitles, subtitles.js also sends the microphone to this app's own
 * backend (Whisper) and our subtitles to the room (topic "subtitle").
 *
 * LiveKitBridgeState drives this object with rx.call_script(...):
 *   window.livekitClient.connect(url, token, bridgeInputId, style)
 *   window.livekitClient.setMicrophone(enabled)
 *   window.livekitClient.setCamera(enabled)      (face tracking on / off)
 *   window.livekitClient.setStyle(style)
 *   window.livekitClient.startAudio()
 *   window.livekitClient.disconnect()
 * and receives room updates as JSON through a hidden <input> whose onChange
 * is bound to LiveKitBridgeState.handle_js_message.
 *
 * Reflex renders the tiles. Every animation frame this bridge draws (with
 * AvatarFace) each <canvas data-avatar-identity="..."> from that person's
 * latest face, smoothed; <canvas data-avatar-preview="<style>"> shows an idle
 * avatar (style picker), and <video data-avatar-camera> gets our camera.
 *
 * Face packet v1, 60 bytes: [0] 0xA1, [1] flags (1 face found, 2 camera on),
 * [2] style, [3] sequence number, [4..54] the blendshapes in AvatarFace.SHAPES
 * order as 0..255, [55..57] yaw, pitch, roll in degrees (int8), [58..59] head
 * x, y (int8, hundredths). Without a face, the jaw follows the voice.
 *
 * Needs /livekit-client.umd.js, /avatar_face.js and /face_tracker.js.
 */
(function () {
  if (window.livekitClient) return;

  // The SDK disconnects by itself when the page goes away (reload, close);
  // that is not a dropped connection, so don't report it to the backend.
  let leaving = false;
  window.addEventListener('beforeunload', () => { leaving = true; });
  window.addEventListener('pagehide', () => { leaving = true; });

  const TOPIC = 'face';
  const PACKET_VERSION = 0xa1;
  const SHAPE_COUNT = 51;
  const PACKET_SIZE = 4 + SHAPE_COUNT + 5;
  const FLAG_FACE = 1;
  const FLAG_CAMERA = 2;
  const SEND_INTERVAL_MS = 40;
  // An unchanged face is sent again only this often.
  const KEEPALIVE_MS = 1000;
  // Without packets for this long, someone's avatar goes idle.
  const STALE_MS = 1500;
  const DEG = 180 / Math.PI;
  // Avatar styles; packets carry the index (avatar_face.js loads first).
  const styles = () => window.AvatarFace.STYLES;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // The backend hands us its own URL (ws://localhost:8000 locally,
  // wss://<sub>.reflex-ddns.com behind re-ddns); the SDK appends /rtc.
  function signalUrl(url) {
    const u = new URL(url, window.location.href);
    if (u.protocol === 'http:') u.protocol = 'ws:';
    if (u.protocol === 'https:') u.protocol = 'wss:';
    if (window.location.protocol === 'https:' && u.protocol === 'ws:') u.protocol = 'wss:';
    return u.toString().replace(/\/$/, '');
  }

  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || '?').slice(0, 2);
    return letters.toUpperCase();
  }

  function avatarColor(seed) {
    let hue = 0;
    for (const ch of String(seed)) hue = (hue * 31 + ch.codePointAt(0)) % 360;
    return 'hsl(' + hue + ' 65% 50%)';
  }

  function errorText(error) {
    return (error && error.message) || String(error);
  }

  function audioHolder() {
    let holder = document.getElementById('livekit-audio-holder');
    if (!holder) {
      holder = document.createElement('div');
      holder.id = 'livekit-audio-holder';
      holder.style.display = 'none';
      document.body.appendChild(holder);
    }
    return holder;
  }

  // --- face packets -----------------------------------------------------------

  function encode(face, flags, style, seq) {
    const out = new Uint8Array(PACKET_SIZE);
    const view = new DataView(out.buffer);
    out[0] = PACKET_VERSION;
    out[1] = flags;
    out[2] = Math.max(0, styles().indexOf(style));
    out[3] = seq & 0xff;
    for (let i = 0; i < SHAPE_COUNT; i++) out[4 + i] = Math.round(clamp(face.shapes[i] || 0, 0, 1) * 255);
    const at = 4 + SHAPE_COUNT;
    view.setInt8(at, Math.round(clamp(face.yaw * DEG, -90, 90)));
    view.setInt8(at + 1, Math.round(clamp(face.pitch * DEG, -90, 90)));
    view.setInt8(at + 2, Math.round(clamp(face.roll * DEG, -90, 90)));
    view.setInt8(at + 3, Math.round(clamp(face.x, -1, 1) * 100));
    view.setInt8(at + 4, Math.round(clamp(face.y, -1, 1) * 100));
    return out;
  }

  function decode(bytes) {
    if (!(bytes instanceof Uint8Array) || bytes.length < PACKET_SIZE || bytes[0] !== PACKET_VERSION) return null;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const shapes = new Float32Array(SHAPE_COUNT);
    for (let i = 0; i < SHAPE_COUNT; i++) shapes[i] = bytes[4 + i] / 255;
    const at = 4 + SHAPE_COUNT;
    return {
      flags: bytes[1],
      style: styles()[bytes[2]] || 'human',
      seq: bytes[3],
      face: {
        shapes,
        yaw: view.getInt8(at) / DEG,
        pitch: view.getInt8(at + 1) / DEG,
        roll: view.getInt8(at + 2) / DEG,
        x: view.getInt8(at + 3) / 100,
        y: view.getInt8(at + 4) / 100,
      },
    };
  }

  // Same face, flags and style (the sequence number aside).
  function samePacket(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (i !== 3 && a[i] !== b[i]) return false;
    return true;
  }

  // --- drawn avatars ----------------------------------------------------------

  // One person's avatar: the latest face received (target) and the face drawn
  // (current), which follows the target smoothly at the display's frame rate.
  function makeAvatar(style) {
    return {
      style: style || 'human',
      target: window.AvatarFace.neutral(),
      current: window.AvatarFace.neutral(),
      face: false,
      camera: false,
      at: 0,
      seq: -1,
      packets: 0,
      bytes: 0,
      voice: 0,
      nextBlink: performance.now() + 800 + Math.random() * 3000,
      blinkUntil: 0,
    };
  }

  // Without a fresh tracked face the avatar idles: it blinks now and then,
  // breathes, and its lips follow the voice.
  function animateAvatar(av, now, dt) {
    const A = window.AvatarFace;
    const fresh = now - av.at < STALE_MS;
    const tracked = fresh && av.face;
    const cur = av.current;
    const goal = av.target;
    const k = 1 - Math.exp(-dt / 0.05);
    for (let i = 0; i < SHAPE_COUNT; i++) {
      cur.shapes[i] += ((tracked ? goal.shapes[i] : 0) - cur.shapes[i]) * k;
    }
    if (!tracked) {
      const jaw = A.INDEX.jawOpen;
      // Packets without a face still carry the jaw (moved by the voice).
      const voiceJaw = fresh ? goal.shapes[jaw] : av.voice * 0.7;
      cur.shapes[jaw] += (voiceJaw - cur.shapes[jaw]) * (1 - Math.exp(-dt / 0.04));
      if (now > av.nextBlink) {
        av.blinkUntil = now + 150;
        av.nextBlink = now + 2500 + Math.random() * 3500;
      }
      if (now < av.blinkUntil) {
        cur.shapes[A.INDEX.eyeBlinkLeft] = 1;
        cur.shapes[A.INDEX.eyeBlinkRight] = 1;
      }
    }
    const kp = 1 - Math.exp(-dt / 0.08);
    for (const key of ['yaw', 'pitch', 'roll', 'x', 'y']) {
      let g = tracked ? goal[key] : 0;
      if (!tracked && key === 'y') g = Math.sin(now / 900) * 0.03;
      cur[key] += (g - cur[key]) * kp;
    }
  }

  function paint(canvas, face, opts) {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cw = Math.round(w * dpr);
    const ch = Math.round(h * dpr);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    window.AvatarFace.draw(ctx, w, h, face, opts);
  }

  window.livekitClient = {
    room: null,
    audioInterval: null,
    bridgeInputId: null,
    style: 'human',
    // identity -> avatar (ours too); style -> idle avatar of the style picker.
    avatars: new Map(),
    previews: new Map(),
    tracker: null,
    cameraOn: false,
    lastFace: null,
    lastFaceAt: 0,
    injected: null,
    injectedUntil: 0,
    faceStatus: '',
    sendTimer: null,
    seq: 0,
    lastPacket: null,
    lastSentAt: 0,
    mic: null,
    sent: { packets: 0, bytes: 0 },
    lastFrame: 0,

    async connect(url, token, bridgeInputId, style) {
      this.bridgeInputId = bridgeInputId;
      const LK = window.LivekitClient;
      if (!LK || !window.AvatarFace || !window.FaceTracker) {
        this.sendStatus({ type: 'error', message: 'The call scripts failed to load' });
        return;
      }
      this.style = styles().includes(style) ? style : 'human';
      if (this.room) await this.disconnect();

      const room = new LK.Room({
        audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        publishDefaults: { audioPreset: LK.AudioPresets.speech },
      });
      this.room = room;
      // Our words become subtitles (subtitles.js); everyone's show on their tiles.
      if (window.liveSubtitles) window.liveSubtitles.attach(room, url, token);
      const E = LK.RoomEvent;
      const refresh = () => this.updateParticipants();

      room
        .on(E.Connected, () => this.sendStatus({ status: 'Connected' }))
        .on(E.Reconnecting, () => this.sendStatus({ status: 'Reconnecting...' }))
        .on(E.Reconnected, () => this.sendStatus({ status: 'Connected' }))
        .on(E.ParticipantConnected, refresh)
        .on(E.ParticipantDisconnected, (p) => {
          this.avatars.delete(p.identity);
          refresh();
        })
        .on(E.ActiveSpeakersChanged, refresh)
        .on(E.TrackMuted, refresh)
        .on(E.TrackUnmuted, refresh)
        .on(E.LocalTrackPublished, (pub) => {
          if (pub.kind === 'audio') {
            this.watchMicrophone(pub.track);
            if (window.liveSubtitles) window.liveSubtitles.listen(pub.track);
          }
          refresh();
        })
        .on(E.TrackSubscribed, (track) => {
          // Keep remote audio elements in the DOM (as LiveKit's own demo does).
          if (track.kind === 'audio') audioHolder().appendChild(track.attach());
          refresh();
        })
        .on(E.TrackUnsubscribed, (track) => {
          track.detach().forEach((el) => el.remove());
          refresh();
        })
        .on(E.DataReceived, (payload, participant, kind, topic) => this.receiveFace(payload, participant, topic))
        .on(E.AudioPlaybackStatusChanged, () => {
          this.sendStatus({ audio_blocked: !room.canPlaybackAudio });
        })
        .on(E.MediaDevicesError, (e) => {
          this.sendStatus({ type: 'warning', message: 'Microphone error: ' + errorText(e) });
        })
        .on(E.Disconnected, () => {
          // Ignore the event caused by our own disconnect() (leave / rejoin).
          if (this.room !== room || leaving) return;
          this.room = null;
          this.cleanUp();
          this.sendStatus({ status: 'Disconnected', participants: [] });
        });

      try {
        await room.connect(signalUrl(url), token);
      } catch (error) {
        console.error('LiveKit connection error:', error);
        if (this.room === room) this.room = null;
        this.sendStatus({ type: 'error', message: errorText(error) });
        return;
      }
      this.avatars.set(room.localParticipant.identity, makeAvatar(this.style));

      try {
        await room.localParticipant.setMicrophoneEnabled(true);
      } catch (error) {
        // Stay in the room as a listener (no mic, or permission denied).
        this.sendStatus({ type: 'warning', message: 'Joined without microphone: ' + errorText(error) });
      }
      if (this.room !== room) return;  // left while the browser asked for permissions

      this.sendTimer = setInterval(() => this.sendFace(), SEND_INTERVAL_MS);
      this.startAudioVisualizer();
      this.updateParticipants();
      this.sendStatus({ status: 'Connected', audio_blocked: !room.canPlaybackAudio });
      // The call is on while the camera starts and the face tracker loads.
      this.setCamera(true);
    },

    cleanUp() {
      clearInterval(this.sendTimer);
      this.sendTimer = null;
      this.stopAudioVisualizer();
      if (this.tracker) this.tracker.stop();
      this.tracker = null;
      this.cameraOn = false;
      this.lastFace = null;
      this.injected = null;
      this.lastPacket = null;
      this.unwatchMicrophone();
      if (window.liveSubtitles) window.liveSubtitles.detach();
      this.avatars.clear();
      this.setFaceStatus('');
    },

    async disconnect() {
      const room = this.room;
      this.room = null;
      this.cleanUp();
      if (room) await room.disconnect();
      audioHolder().replaceChildren();
    },

    async setMicrophone(enabled) {
      if (!this.room) return;
      try {
        await this.room.localParticipant.setMicrophoneEnabled(enabled);
      } catch (error) {
        this.sendStatus({ type: 'warning', message: 'Microphone error: ' + errorText(error) });
      }
      this.updateParticipants();
    },

    // The camera only feeds the face tracker on this device. Off releases it.
    async setCamera(enabled) {
      if (!this.room) return;
      if (!enabled) {
        if (this.tracker) this.tracker.stop();
        this.tracker = null;
        this.cameraOn = false;
        this.lastFace = null;
        this.setFaceStatus('Camera off');
        this.updateParticipants();
        return;
      }
      if (this.tracker) return;
      const tracker = new window.FaceTracker({
        onFrame: (face) => {
          if (this.tracker !== tracker) return;
          this.lastFace = face;
          this.lastFaceAt = performance.now();
        },
        onStatus: (status, message) => {
          if (this.tracker === tracker) this.setFaceStatus(message);
        },
      });
      this.tracker = tracker;
      this.cameraOn = true;
      this.updateParticipants();
      try {
        await tracker.start();
      } catch (error) {
        if (this.tracker !== tracker) return;
        this.tracker = null;
        this.cameraOn = false;
        this.sendStatus({ type: 'warning', message: 'Camera unavailable: ' + errorText(error) });
        this.updateParticipants();
      }
    },

    setStyle(style) {
      if (!styles().includes(style)) return;
      this.style = style;
      this.lastPacket = null;  // send it right away
    },

    async startAudio() {
      if (this.mic && this.mic.context.state !== 'running') this.mic.context.resume().catch(() => {});
      if (window.liveSubtitles) window.liveSubtitles.resume();
      if (!this.room) return;
      await this.room.startAudio();
      this.sendStatus({ audio_blocked: !this.room.canPlaybackAudio });
    },

    // --- our face ---------------------------------------------------------------

    // Microphone loudness (0..1): opens the jaw while no face is tracked.
    watchMicrophone(track) {
      this.unwatchMicrophone();
      try {
        const context = new (window.AudioContext || window.webkitAudioContext)();
        const source = context.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
        const analyser = context.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        if (context.state !== 'running') context.resume().catch(() => {});
        this.mic = { context, analyser, data: new Float32Array(analyser.fftSize), level: 0 };
      } catch (error) {
        console.warn('No microphone level:', error);
      }
    },

    unwatchMicrophone() {
      if (this.mic) this.mic.context.close().catch(() => {});
      this.mic = null;
    },

    micLevel() {
      const mic = this.mic;
      const local = this.room && this.room.localParticipant;
      if (!mic || !local || !local.isMicrophoneEnabled) return 0;
      mic.analyser.getFloatTimeDomainData(mic.data);
      let sum = 0;
      for (const v of mic.data) sum += v * v;
      const goal = clamp((Math.sqrt(sum / mic.data.length) - 0.01) * 9, 0, 0.8);
      mic.level += (goal - mic.level) * (goal > mic.level ? 0.6 : 0.3);
      return mic.level;
    },

    // Every SEND_INTERVAL_MS: our face to everyone (and to our own tile).
    sendFace() {
      const room = this.room;
      if (!room) return;
      const A = window.AvatarFace;
      const now = performance.now();
      const voice = this.micLevel();
      let flags = this.cameraOn ? FLAG_CAMERA : 0;
      let face;
      if (this.injected && now < this.injectedUntil) {
        face = this.injected;
        flags |= FLAG_FACE;
      } else if (this.lastFace && now - this.lastFaceAt < 500) {
        face = this.lastFace;
        flags |= FLAG_FACE;
      } else {
        face = A.neutral();
        face.shapes[A.INDEX.jawOpen] = voice;
      }
      const me = this.avatars.get(room.localParticipant.identity);
      if (me) {
        Object.assign(me, { target: face, face: !!(flags & FLAG_FACE), camera: this.cameraOn, style: this.style, at: now, voice });
      }
      const packet = encode(face, flags, this.style, this.seq);
      if (now - this.lastSentAt < KEEPALIVE_MS && samePacket(packet, this.lastPacket)) return;
      this.seq = (this.seq + 1) & 0xff;
      packet[3] = this.seq;
      this.lastPacket = packet;
      this.lastSentAt = now;
      this.sent.packets += 1;
      this.sent.bytes += packet.length;
      room.localParticipant.publishData(packet, { reliable: false, topic: TOPIC }).catch(() => {});
    },

    receiveFace(payload, participant, topic) {
      if (topic !== TOPIC || !participant) return;
      const msg = decode(payload);
      if (!msg) return;
      let av = this.avatars.get(participant.identity);
      if (!av) {
        av = makeAvatar(msg.style);
        this.avatars.set(participant.identity, av);
      }
      // Lossy packets may arrive out of order: keep only newer ones.
      if (av.seq >= 0 && ((msg.seq - av.seq) & 0xff) >= 128) return;
      const camera = !!(msg.flags & FLAG_CAMERA);
      const announce = av.packets === 0 || av.camera !== camera;
      Object.assign(av, {
        seq: msg.seq, target: msg.face, face: !!(msg.flags & FLAG_FACE), camera, style: msg.style,
        at: performance.now(), packets: av.packets + 1, bytes: av.bytes + payload.length,
      });
      if (announce) this.updateParticipants();
    },

    // For tests and demos: use `values` (blendshape name -> 0..1, and yaw,
    // pitch, roll, x, y) as our face for `ms`, instead of the camera's.
    injectFace(values, ms = 2000) {
      const A = window.AvatarFace;
      const face = A.neutral();
      for (const [key, value] of Object.entries(values || {})) {
        if (key in A.INDEX) face.shapes[A.INDEX[key]] = value;
        else if (key in face) face[key] = value;
      }
      this.injected = face;
      this.injectedUntil = performance.now() + ms;
    },

    // What this browser knows of someone's avatar, by display name (tests, debugging).
    faceOf(name) {
      const room = this.room;
      if (!room) return null;
      const everyone = [room.localParticipant, ...room.remoteParticipants.values()];
      const p = everyone.find((x) => (x.name || x.identity) === name);
      const av = p && this.avatars.get(p.identity);
      if (!av) return null;
      const A = window.AvatarFace;
      const shapes = {};
      A.SHAPES.forEach((n, i) => { shapes[n] = Math.round(av.target.shapes[i] * 1000) / 1000; });
      return {
        style: av.style, face: av.face, camera: av.camera, packets: av.packets, bytes: av.bytes,
        shapes, emotion: A.emotion(av.target),
        yaw: av.target.yaw, pitch: av.target.pitch, roll: av.target.roll,
      };
    },

    // --- drawing ----------------------------------------------------------------

    animate(now) {
      requestAnimationFrame((t) => this.animate(t));
      if (!window.AvatarFace) return;
      const dt = this.lastFrame ? Math.min(0.1, (now - this.lastFrame) / 1000) : 0.016;
      this.lastFrame = now;
      const room = this.room;
      if (room) {
        room.remoteParticipants.forEach((p) => {
          const av = this.avatars.get(p.identity);
          if (av) av.voice = p.audioLevel || 0;
        });
      }
      this.avatars.forEach((av) => animateAvatar(av, now, dt));
      document.querySelectorAll('canvas[data-avatar-identity]').forEach((canvas) => {
        const av = this.avatars.get(canvas.dataset.avatarIdentity);
        paint(canvas, av ? av.current : null, {
          style: av ? av.style : 'human',
          seed: canvas.dataset.avatarSeed || '',
          mirror: canvas.dataset.avatarMirror === '1',
          voice: av ? av.voice : 0,
        });
      });
      document.querySelectorAll('canvas[data-avatar-preview]').forEach((canvas) => {
        const style = canvas.dataset.avatarPreview;
        let av = this.previews.get(style);
        if (!av) {
          av = makeAvatar(style);
          this.previews.set(style, av);
        }
        animateAvatar(av, now, dt);
        paint(canvas, av.current, { style, seed: canvas.dataset.avatarSeed || style, badge: false });
      });
      const stream = this.tracker && this.tracker.stream;
      document.querySelectorAll('video[data-avatar-camera]').forEach((video) => {
        if (video.srcObject !== stream) video.srcObject = stream || null;
      });
      if (window.liveSubtitles) window.liveSubtitles.render();
    },

    startAudioVisualizer() {
      this.stopAudioVisualizer();
      this.audioInterval = setInterval(() => {
        if (!this.room) return;
        const updateBar = (p) => {
          const el = document.getElementById('vol-' + p.identity);
          if (!el) return;
          const level = p.audioLevel || 0;
          let width = Math.min(100, level * 100 * 5);
          if (width > 0 && width < 5) width = 5;
          el.style.width = width + '%';
        };
        updateBar(this.room.localParticipant);
        this.room.remoteParticipants.forEach(updateBar);
      }, 50);
    },

    stopAudioVisualizer() {
      if (this.audioInterval) {
        clearInterval(this.audioInterval);
        this.audioInterval = null;
      }
    },

    // --- Reflex state -------------------------------------------------------------

    setFaceStatus(message) {
      if (message === this.faceStatus) return;
      this.faceStatus = message;
      this.sendStatus({ face_status: message });
    },

    updateParticipants() {
      const room = this.room;
      if (!room) return;
      const describe = (p, isLocal) => {
        const name = p.name || p.identity;
        const av = this.avatars.get(p.identity);
        return {
          identity: p.identity,
          name: name,
          initials: initials(name),
          avatar_color: avatarColor(name),
          is_speaking: p.isSpeaking,
          is_local: isLocal,
          is_muted: !p.isMicrophoneEnabled,
          camera_on: isLocal ? this.cameraOn : !!(av && av.camera),
        };
      };
      const participants = [describe(room.localParticipant, true)];
      room.remoteParticipants.forEach((p) => participants.push(describe(p, false)));
      this.sendStatus({
        participants: participants,
        is_muted: !room.localParticipant.isMicrophoneEnabled,
        is_camera_off: !this.cameraOn,
      });
    },

    sendStatus(data) {
      const input = document.getElementById(this.bridgeInputId);
      if (!input) return;
      // React ignores plain value assignments; use the native setter + input event.
      const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setValue.call(input, JSON.stringify(data));
      input.dispatchEvent(new Event('input', { bubbles: true }));
    },
  };

  requestAnimationFrame((t) => window.livekitClient.animate(t));
})();
