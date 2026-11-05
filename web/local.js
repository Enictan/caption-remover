/* Tru Studio Music for iPad (web app): everything the Mac app's local server does for the music studio,
   done in the browser instead. The studio UI is the same code as the Mac app; it talks to "/api/…" and this
   file answers those calls on the device: songs and memory in IndexedDB, analysis in a worker, renders offline. */
(function () {
  'use strict';
  window.WEBAPP = true;
  document.documentElement.classList.add('webapp');
  const VERSION = window.WEBAPP_VERSION || 'web';

  // ---------------------------------------------------------------- storage (IndexedDB)
  const DB = (() => {
    let p = null;
    const open = () => p || (p = new Promise((res, rej) => {
      const r = indexedDB.open('tru-studio-music', 1);
      r.onupgradeneeded = () => { const d = r.result; d.createObjectStore('tracks', { keyPath: 'id' }); d.createObjectStore('blobs'); d.createObjectStore('peaks'); };
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    }));
    const tx = async (store, mode, fn) => { const d = await open(); return new Promise((res, rej) => { const t = d.transaction(store, mode), s = t.objectStore(store); const q = fn(s); t.oncomplete = () => res(q && q.result); t.onerror = () => rej(t.error); t.onabort = () => rej(t.error); }); };
    return {
      get: (s, k) => tx(s, 'readonly', (st) => st.get(k)),
      put: (s, v, k) => tx(s, 'readwrite', (st) => k === undefined ? st.put(v) : st.put(v, k)),
      del: (s, k) => tx(s, 'readwrite', (st) => st.delete(k)),
      all: (s) => tx(s, 'readonly', (st) => st.getAll()),
    };
  })();
  try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (e) {}

  // ---------------------------------------------------------------- settings (per device)
  const SETTINGS_DEFAULT = { keys_open: false, keys_names: true, keys_scale: 'sel', keys_octave: 3, keys_type: false, keys_vol: 0.6, quantize: true, link_markers: 'bars' };
  const settings = () => { let s = {}; try { s = JSON.parse(localStorage.getItem('settings') || '{}'); } catch (e) {} return Object.assign({}, SETTINGS_DEFAULT, s); };
  const saveSettings = (b) => { const s = Object.assign(settings(), b); try { localStorage.setItem('settings', JSON.stringify(s)); } catch (e) {} return s; };

  // ---------------------------------------------------------------- analysis worker
  let worker = null, wid = 0; const waiting = {};
  function analyseMono(y, onProgress) {
    if (!worker) {
      worker = new Worker('analysis.js');
      worker.onmessage = (e) => { const w = waiting[e.data.id]; if (!w) return; if (e.data.progress != null) { w.p && w.p(e.data.progress, e.data.msg); return; } delete waiting[e.data.id]; e.data.error ? w.rej(new Error(e.data.error)) : w.res(e.data.result); };
    }
    return new Promise((res, rej) => { const id = ++wid; waiting[id] = { res, rej, p: onProgress }; worker.postMessage({ id, y }, [y.buffer]); });
  }

  // ---------------------------------------------------------------- decoding (one decode per song per session)
  const decoded = {};
  async function decodeBlob(id) {
    if (decoded[id]) return decoded[id];
    const blob = await DB.get('blobs', id); if (!blob) throw new Error('This song is no longer on this iPad');
    const buf = await ac().decodeAudioData(await blob.arrayBuffer());
    decoded[id] = buf; return buf;
  }
  async function toMono22k(buf) {
    const n = Math.max(1, Math.ceil(buf.duration * 22050)), oc = new OfflineAudioContext(1, n, 22050);
    const s = oc.createBufferSource(); s.buffer = buf; s.connect(oc.destination); s.start();
    return (await oc.startRendering()).getChannelData(0).slice();
  }

  // ---------------------------------------------------------------- tracks
  const tracks = {};       // id -> track (as the studio expects it, no peaks)
  const pub = (t) => { const o = Object.assign({}, t); delete o.peaks; o.stem_names = []; return o; };
  function applyKey(t, tonic, mode) {
    const A = TruAnalysis, [rt, rm] = A.relative(tonic, mode);
    Object.assign(t, { tonic, mode, key: A.keyName(tonic, mode), camelot: A.camelot(tonic, mode), key_conf: 1, key_user: true, key_source: 'you',
      key_alts: [{ tonic: rt, mode: rm, key: A.keyName(rt, rm) + ' (same notes)', camelot: A.camelot(rt, rm) }] });
  }
  async function addFile(file) {
    const id = Math.random().toString(16).slice(2, 12);
    await DB.put('blobs', file, id);
    try {
      const buf = await ac().decodeAudioData(await file.arrayBuffer());
      decoded[id] = buf;
      const mono = await toMono22k(buf);
      const a = await analyseMono(mono);
      const peaks = a.peaks; delete a.peaks;
      a.duration = Math.round(buf.duration * 100) / 100;
      const name = (file.name || 'Song').replace(/\.[^.]+$/, '');
      const t = Object.assign(a, { id, name, detected: { tonic: a.tonic, mode: a.mode }, mem: {}, added: Date.now() });
      await DB.put('peaks', peaks, id); await DB.put('tracks', t);
      tracks[id] = t; return pub(t);
    } catch (e) { await DB.del('blobs', id); throw new Error("Couldn't read that audio file (" + (e.message || e) + ')'); }
  }

  // ---------------------------------------------------------------- rendering (save deck) + WAV + export sheet
  async function renderDeck(id, semis, ratio) {
    const buf = await decodeBlob(id), sr = buf.sampleRate, len = Math.ceil(buf.duration / ratio * sr);
    const oc = new OfflineAudioContext(2, len + sr, sr);
    const node = await SignalsmithStretch(oc, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2] });
    let lat = 0; try { lat = await node.latency(); } catch (e) {}
    const s = oc.createBufferSource(); s.buffer = buf; s.playbackRate.value = ratio; s.connect(node); node.connect(oc.destination);
    node.schedule({ active: true, semitones: semis - 12 * Math.log2(ratio), outputTime: 0 }); s.start(0);
    const out = await oc.startRendering(), skip = Math.round(lat * sr);
    return { chans: [0, 1].map((c) => out.getChannelData(Math.min(c, out.numberOfChannels - 1)).subarray(skip, skip + len)), sr };
  }
  function wav(chans, sr) {
    const n = chans[0].length, nc = chans.length, b = new ArrayBuffer(44 + n * nc * 2), v = new DataView(b);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); v.setUint32(4, 36 + n * nc * 2, true); w(8, 'WAVE'); w(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, nc, true);
    v.setUint32(24, sr, true); v.setUint32(28, sr * nc * 2, true); v.setUint16(32, nc * 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * nc * 2, true);
    let o = 44; for (let i = 0; i < n; i++) for (let c = 0; c < nc; c++) { const x = Math.max(-1, Math.min(1, chans[c][i])); v.setInt16(o, x < 0 ? x * 32768 : x * 32767, true); o += 2; }
    return new Blob([b], { type: 'audio/wav' });
  }
  function exportSheet(blob, filename) {
    let el = document.getElementById('webExport');
    if (!el) { el = document.createElement('div'); el.id = 'webExport'; document.body.appendChild(el); }
    const mb = (blob.size / 1048576).toFixed(1), file = new File([blob], filename, { type: blob.type });
    const canShare = !!(navigator.canShare && navigator.canShare({ files: [file] }));
    el.innerHTML = `<div class="wxcard"><b>Ready to save</b><div class="wxname">${filename.replace(/</g, '&lt;')}</div><div class="wxsub">${mb} MB · WAV</div>
      <div class="wxbtns"><button class="btn sm" data-x="close">Close</button>${canShare ? '<button class="btn sm gold" data-x="share">Save or share…</button>' : '<button class="btn sm gold" data-x="dl">Download</button>'}</div>
      <div class="wxsub">${canShare ? 'Choose “Save to Files”, or send it straight to CapCut, LumaFusion or AirDrop.' : 'It goes to your Downloads folder.'}</div></div>`;
    el.style.display = 'flex';
    el.onclick = async (e) => {
      const x = e.target.closest('[data-x]'); if (!x && e.target !== el) return;
      if (!x || x.dataset.x === 'close') { el.style.display = 'none'; return; }
      if (x.dataset.x === 'share') { try { await navigator.share({ files: [file], title: filename }); el.style.display = 'none'; } catch (err) { if (err && err.name !== 'AbortError') toast('Sharing failed: ' + err.message); } }
      if (x.dataset.x === 'dl') { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 60000); el.style.display = 'none'; }
    };
  }
  const safeName = (s) => String(s).replace(/[\\/:*?"<>|]/g, '').trim() || 'mix';

  // ---------------------------------------------------------------- recorder
  const recs = {};

  // ---------------------------------------------------------------- the local "server"
  const J = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  const ERR = (msg, status = 400) => J({ detail: msg }, status);
  const body = (init) => { try { return typeof init.body === 'string' ? JSON.parse(init.body) : {}; } catch (e) { return {}; } };
  async function handle(url, init) {
    const u = new URL(url, location.href), p = u.pathname.replace(/^.*?\/api\//, '/api/'), m = (init.method || 'GET').toUpperCase();
    let mt;
    if (p === '/api/settings') return J(m === 'POST' ? saveSettings(body(init)) : settings());
    if (p === '/api/system') return J({ version: VERSION, device: 'ipad', dev: false });
    if (p.startsWith('/api/update') || p.startsWith('/api/link') || p.startsWith('/api/mixsync')) return J({ connected: false, installed: false, results: {}, state: 'idle' });
    if (p === '/api/music/stem_models') return J({ models: [], default: '' });
    if ((mt = p.match(/^\/api\/music\/stems\/([^/]+)/))) return J({ state: 'none' });
    if (p === '/api/music/upload') {
      const f = init.body && init.body.get ? init.body.get('file') : null; if (!f) return ERR('No file');
      try { return J(await addFile(f)); } catch (e) { return ERR(e.message); }
    }
    if ((mt = p.match(/^\/api\/music\/peaks\/([^/]+)/))) { const pk = await DB.get('peaks', mt[1]); return pk ? J(pk) : ERR('No waveform', 404); }
    if ((mt = p.match(/^\/api\/music\/mem\/([^/]+)/))) {
      const t = tracks[mt[1]]; if (!t) return ERR('No such song', 404);
      const b = body(init), ok = {}; ['hot', 'bpm_factor', 'eq'].forEach((k) => { if (k in b) ok[k] = b[k]; });
      t.mem = Object.assign({}, t.mem, ok); await DB.put('tracks', t); return J(t.mem);
    }
    if ((mt = p.match(/^\/api\/music\/key\/([^/]+)/))) {
      const t = tracks[mt[1]]; if (!t) return ERR('No such song', 404); const b = body(init), tonic = ((+b.tonic % 12) + 12) % 12;
      applyKey(t, tonic, b.mode); t.mem = Object.assign({}, t.mem, { key: { tonic, mode: b.mode } }); await DB.put('tracks', t); return J(pub(t));
    }
    if (p === '/api/music/match') {
      const b = body(init), main = tracks[b.main]; if (!main) return J({ matches: [] });
      return J({ matches: (b.cands || []).map((c) => tracks[c] && Object.assign(TruAnalysis.match(main, tracks[c]), { id: c })).filter(Boolean) });
    }
    if ((mt = p.match(/^\/api\/music\/([0-9a-f]+)$/)) && m === 'DELETE') {
      delete tracks[mt[1]]; delete decoded[mt[1]]; await Promise.all([DB.del('tracks', mt[1]), DB.del('blobs', mt[1]), DB.del('peaks', mt[1])]); return J({ ok: true });
    }
    if (p === '/api/music/save_deck') {
      const b = body(init), t = tracks[b.id]; if (!t) return ERR('No such song', 404);
      const semis = +b.semis || 0, ratio = +b.ratio || 1;
      try {
        const r = await renderDeck(b.id, semis, ratio), tag = [];
        if (semis) tag.push(`${semis > 0 ? '+' : ''}${semis} st`);
        if (Math.abs(ratio - 1) > 1e-4) tag.push(`${(t.bpm * ratio).toFixed(1)} BPM`);
        tag.push(TruAnalysis.keyName(((t.tonic + Math.round(semis)) % 12 + 12) % 12, t.mode));
        const fn = `${safeName(t.name)} (${tag.join(', ')}).wav`;
        exportSheet(wav(r.chans, r.sr), fn);
        return J({ output: fn, filename: fn });
      } catch (e) { return ERR('Saving failed: ' + (e.message || e)); }
    }
    if (p === '/api/music/rec/start') { const id = Math.random().toString(16).slice(2, 10); recs[id] = { sr: body(init).sr || 48000, parts: [] }; return J({ id }); }
    if ((mt = p.match(/^\/api\/music\/rec\/([^/]+)\/chunk/))) { const r = recs[mt[1]]; if (r && init.body) r.parts.push(init.body instanceof Float32Array ? init.body : new Float32Array(init.body)); return J({ ok: true }); }
    if ((mt = p.match(/^\/api\/music\/rec\/([^/]+)\/stop/))) {
      const r = recs[mt[1]]; delete recs[mt[1]]; if (!r) return ERR('No such recording', 404);
      let n = 0; r.parts.forEach((a) => (n += a.length / 2));
      const L = new Float32Array(n), R = new Float32Array(n); let o = 0;
      r.parts.forEach((a) => { for (let i = 0; i < a.length; i += 2) { L[o] = a[i]; R[o] = a[i + 1]; o++; } });
      let s = 0; while (s < n && Math.abs(L[s]) < 1e-4 && Math.abs(R[s]) < 1e-4) s++;
      if (s >= n) return ERR('Nothing was playing while recording');
      s = Math.max(0, s - Math.round(r.sr / 20));
      const d = new Date(), pad = (x) => String(x).padStart(2, '0');
      const fn = `Mix ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}.${pad(d.getMinutes())}.wav`;
      exportSheet(wav([L.subarray(s), R.subarray(s)], r.sr), fn);
      return J({ output: fn, filename: fn, seconds: (n - s) / r.sr });
    }
    if (p === '/api/music/reveal' || p === '/api/open_folder') return J({ ok: true });
    return ERR('Not available in the iPad version', 404);
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (/^\/api\/|^[a-z]+:\/\/[^/]+\/(?:[^?#]*\/)?api\//i.test(url) && (url.startsWith('/api/') || url.indexOf(location.origin) === 0))
      return handle(url, init || {}).catch((e) => ERR(String(e && e.message || e), 500));
    return realFetch(input, init);
  };
  window.WEB = { DB, tracks, decodeBlob, handle };

  // ---------------------------------------------------------------- hook into the studio once its code has run
  window.addEventListener('DOMContentLoaded', async () => {
    // the decks decode songs here instead of asking the server for WAV copies
    window.decodePcm = (t) => decodeBlob(t.id);
    try { $('pVer') && ($('pVer').textContent = VERSION); } catch (e) {}
    setMode('music');
    // the library you built on this iPad
    try {
      const list = (await DB.all('tracks')).sort((a, b) => (a.added || 0) - (b.added || 0));
      list.forEach((t) => { tracks[t.id] = t; if (t.mem && t.mem.key) applyKey(t, t.mem.key.tonic, t.mem.key.mode); LIB.tracks.push(pub(t)); });
      refitLibrary();
    } catch (e) { toast("Couldn't open your library: " + e.message); }
    // stems: coming to the iPad next
    const rs = Deck.prototype.renderStems;
    Deck.prototype.renderStems = function () {
      if (this.tab !== 'stems') return rs.call(this);
      const box = this.q('.pbody'), ex = this.q('.pextra'); if (ex) ex.textContent = '';
      box.innerHTML = this.t ? '<div class="splitbox"><div class="note"><b style="color:var(--tx)">Stem splitting is coming to the iPad next.</b> For now, split stems in the Mac app.</div></div>' : '';
    };
    Object.values(DECKS).forEach((d) => d.renderPads());
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
  });
})();
