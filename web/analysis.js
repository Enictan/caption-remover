/* Tru Studio — song analysis in the browser (port of music.py: tempo, beat grid, key, waveform, chorus start).
   Input: mono Float32Array at 22050 Hz. Runs in a Web Worker (or Node for tests). No dependencies. */
(function (root) {
  'use strict';
  const SR = 22050;
  const NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  const FLATS_MINOR = { 1: 'Db', 3: 'Eb', 8: 'Ab', 10: 'Bb' };
  const CAM_MAJ = { 0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7 };
  const CAM_MIN = { 9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7 };
  const TP_MAJ = [5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0];
  const TP_MIN = [5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0];
  const KK_MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
  const KK_MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];
  const wrap12 = (s) => ((s % 12) + 12) % 12;
  const isMinor = (m) => m === true || m === 'minor';
  const keyName = (t, m) => { const mi = isMinor(m); const n = mi ? (FLATS_MINOR[wrap12(t)] || NAMES[wrap12(t)]) : NAMES[wrap12(t)]; return `${n} ${mi ? 'minor' : 'major'}`; };
  const camelot = (t, m) => isMinor(m) ? `${CAM_MIN[wrap12(t)]}A` : `${CAM_MAJ[wrap12(t)]}B`;
  const relative = (t, m) => isMinor(m) ? [wrap12(t + 3), 'major'] : [wrap12(t - 3), 'minor'];

  // ---------------------------------------------------------------- FFT (radix-2, in place)
  const fftCache = {};
  function fft(n) {
    if (fftCache[n]) return fftCache[n];
    const lev = Math.round(Math.log2(n)), cos = new Float64Array(n / 2), sin = new Float64Array(n / 2), rev = new Uint32Array(n);
    for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos(2 * Math.PI * i / n); sin[i] = Math.sin(2 * Math.PI * i / n); }
    for (let i = 0; i < n; i++) { let x = i, r = 0; for (let j = 0; j < lev; j++) { r = (r << 1) | (x & 1); x >>= 1; } rev[i] = r; }
    const f = (re, im) => {
      for (let i = 0; i < n; i++) { const j = rev[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
      for (let size = 2; size <= n; size <<= 1) {
        const half = size >> 1, step = n / size;
        for (let i = 0; i < n; i += size) {
          for (let j = i, k = 0; j < i + half; j++, k += step) {
            const l = j + half, tre = re[l] * cos[k] + im[l] * sin[k], tim = -re[l] * sin[k] + im[l] * cos[k];
            re[l] = re[j] - tre; im[l] = im[j] - tim; re[j] += tre; im[j] += tim;
          }
        }
      }
    };
    return (fftCache[n] = f);
  }
  const hann = (n) => { const w = new Float64Array(n); for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n); return w; };

  /** magnitude STFT, centred (reflect padding) like librosa; returns frames of bins [lo, hi) */
  function stft(y, nfft, hop, lo, hi) {
    const n = y.length, pad = nfft >> 1, frames = 1 + Math.floor(n / hop), win = hann(nfft), F = fft(nfft);
    const re = new Float64Array(nfft), im = new Float64Array(nfft), out = new Array(frames);
    const at = (i) => (i >= 0 && i < n) ? y[i] : 0;   // zero padding, like librosa's default
    for (let f = 0; f < frames; f++) {
      const s = f * hop - pad;
      if (s >= 0 && s + nfft <= n) for (let i = 0; i < nfft; i++) { re[i] = y[s + i] * win[i]; im[i] = 0; }
      else for (let i = 0; i < nfft; i++) { re[i] = at(s + i) * win[i]; im[i] = 0; }
      F(re, im);
      const m = new Float32Array(hi - lo);
      for (let k = lo; k < hi; k++) m[k - lo] = Math.hypot(re[k], im[k]);
      out[f] = m;
    }
    return out;
  }

  // ---------------------------------------------------------------- small helpers
  function median(a, lo, hi, buf) {        // median of a[lo..hi) (copy into buf, quickselect)
    const n = hi - lo; for (let i = 0; i < n; i++) buf[i] = a[lo + i];
    let l = 0, r = n - 1; const k = n >> 1;
    while (l < r) {
      const p = buf[(l + r) >> 1]; let i = l, j = r;
      while (i <= j) { while (buf[i] < p) i++; while (buf[j] > p) j--; if (i <= j) { const t = buf[i]; buf[i] = buf[j]; buf[j] = t; i++; j--; } }
      if (k <= j) r = j; else if (k >= i) l = i; else break;
    }
    return buf[k];
  }
  const mean = (a) => { let s = 0; for (const v of a) s += v; return a.length ? s / a.length : 0; };
  function corr(a, b) {
    const ma = mean(a), mb = mean(b); let s = 0, sa = 0, sb = 0;
    for (let i = 0; i < a.length; i++) { const x = a[i] - ma, y = b[i] - mb; s += x * y; sa += x * x; sb += y * y; }
    return s / (Math.sqrt(sa * sb) + 1e-12);
  }
  const roll = (a, r) => a.map((_, i) => a[wrap12(i - r)]);

  /** harmonic part of a magnitude spectrogram (median-filter HPSS soft mask, like librosa.effects.harmonic) */
  function harmonic(S, margin, tk, fk) {
    const T = S.length, B = S[0].length, H = new Array(T), buf = new Float64Array(Math.max(tk, fk) + 2);
    const col = new Float64Array(T);
    const Hm = S.map(() => new Float32Array(B)), Pm = S.map(() => new Float32Array(B));
    for (let b = 0; b < B; b++) {
      for (let t = 0; t < T; t++) col[t] = S[t][b];
      for (let t = 0; t < T; t++) { const lo = Math.max(0, t - (tk >> 1)), hi = Math.min(T, t + (tk >> 1) + 1); Hm[t][b] = median(col, lo, hi, buf); }
    }
    for (let t = 0; t < T; t++) {
      const row = S[t];
      for (let b = 0; b < B; b++) { const lo = Math.max(0, b - (fk >> 1)), hi = Math.min(B, b + (fk >> 1) + 1); Pm[t][b] = median(row, lo, hi, buf); }
    }
    for (let t = 0; t < T; t++) {
      const o = new Float32Array(B);
      for (let b = 0; b < B; b++) { const h = Hm[t][b] ** 2, p = (margin * Pm[t][b]) ** 2; o[b] = S[t][b] * (h / (h + p + 1e-12)); }
      H[t] = o;
    }
    return H;
  }

  /** chroma from a magnitude STFT: each bin to its pitch class, weighted by closeness to the note centre */
  function chroma(S, nfft, lo, fmin, fmax) {
    const T = S.length, B = S[0].length, pc = new Int8Array(B), w = new Float32Array(B);
    for (let b = 0; b < B; b++) {
      const f = (lo + b) * SR / nfft;
      if (f < fmin || f > fmax) { pc[b] = -1; continue; }
      const midi = 69 + 12 * Math.log2(f / 440), near = Math.round(midi);
      pc[b] = wrap12(near); w[b] = Math.max(0, 1 - Math.abs(midi - near) * 1.6);
    }
    const out = new Array(T);
    for (let t = 0; t < T; t++) {
      const c = new Float64Array(12), row = S[t];
      for (let b = 0; b < B; b++) if (pc[b] >= 0) c[pc[b]] += row[b] * w[b];
      let mx = 0; for (let i = 0; i < 12; i++) mx = Math.max(mx, c[i]);
      for (let i = 0; i < 12; i++) c[i] = Math.log1p(10 * c[i] / (mx + 1e-9));
      out[t] = c;
    }
    return out;
  }
  const colMean = (C, a, b) => { a = Math.max(0, Math.min(C.length - 1, a)); b = Math.max(a + 1, Math.min(C.length, b)); const m = new Float64Array(12); for (let t = a; t < b; t++) for (let i = 0; i < 12; i++) m[i] += C[t][i]; for (let i = 0; i < 12; i++) m[i] /= (b - a); return m; };

  // ---------------------------------------------------------------- key
  function detectKey(y, beats) {
    const hop = 2048;
    // harmony: C2..C7, harmonic part only (drums out)
    let nf = 8192, lo = Math.floor(60 * nf / SR), hi = Math.ceil(2200 * nf / SR);
    const Sh = harmonic(stft(y, nf, hop, lo, hi), 3.0, 9, 31);
    const H = chroma(Sh, nf, lo, 65.4, 2093);
    const prof = Array.from(colMean(H, 0, H.length));
    const scores = [];
    for (const [minor, kk, tp] of [[false, KK_MAJ, TP_MAJ], [true, KK_MIN, TP_MIN]])
      for (let r = 0; r < 12; r++) scores.push([0.5 * corr(prof, roll(kk, r)) + 0.5 * corr(prof, roll(tp, r)), r, minor]);
    scores.sort((a, b) => b[0] - a[0]);
    const best = scores[0];
    const majRoot = !best[2] ? best[1] : wrap12(best[1] + 3), minRoot = wrap12(majRoot - 3);
    // bass: 65-260 Hz, stricter harmonic mask, finer frequency resolution
    nf = 16384; lo = Math.floor(60 * nf / SR); hi = Math.ceil(270 * nf / SR);
    const Sb = harmonic(stft(y, nf, hop, lo, hi), 8.0, 9, 15);
    const bass = chroma(Sb, nf, lo, 65.4, 261.6);
    const n = bass.length, fr = (t) => Math.floor(t * SR / hop);
    let first, last;
    if (beats && beats.length > 8) { first = colMean(bass, fr(beats[0]), Math.max(fr(beats[0]) + 1, fr(beats[2]))); last = colMean(bass, fr(beats[beats.length - 3]), Math.max(fr(beats[beats.length - 3]) + 1, fr(beats[beats.length - 1]) + 1)); }
    else { first = colMean(bass, 0, Math.max(1, (n / 20) | 0)); last = colMean(bass, n - Math.max(1, (n / 20) | 0), n); }
    const all = colMean(bass, 0, n), w = new Float64Array(12);
    for (let i = 0; i < 12; i++) w[i] = all[i] + first[i] + 0.6 * last[i];
    if (beats && beats.length > 40) {
      const cols = [];
      for (let i = 0; i < beats.length - 1; i += 16) cols.push(colMean(bass, fr(beats[i]), Math.max(fr(beats[i]) + 1, fr(beats[Math.min(i + 1, beats.length - 1)]))));
      if (cols.length) for (let i = 0; i < 12; i++) w[i] += 1.5 * mean(cols.map((c) => c[i]));
    }
    const tMaj = w[majRoot] + 0.5 * prof[majRoot], tMin = w[minRoot] + 0.5 * prof[minRoot];
    const cor = {}; for (const [c, r, m] of scores) cor[r + (m ? 'm' : 'M')] = c;
    const sMaj = cor[majRoot + 'M'] + 0.35 * (tMaj - tMin) / (tMaj + tMin + 1e-9);
    const sMin = cor[minRoot + 'm'] + 0.35 * (tMin - tMaj) / (tMaj + tMin + 1e-9);
    const minor = sMin > sMaj, root = minor ? minRoot : majRoot;
    const other = scores.find(([c, r, m]) => (r !== majRoot && r !== minRoot) || (r === majRoot && m) || (r === minRoot && !m))[0];
    const notesConf = Math.max(0, Math.min(1, (Math.max(sMaj, sMin) - other) * 5 + 0.35));
    const modeConf = Math.max(0, Math.min(1, Math.abs(sMaj - sMin) * 8));
    const nx = scores.find(([c, r]) => r !== majRoot && r !== minRoot);
    return { root, minor, notesConf, modeConf, next: nx ? [nx[1], nx[2]] : null };
  }
  function keyFields(root, minor, kconf, mconf, next, cap = true) {
    const mode = minor ? 'minor' : 'major', [rt, rm] = relative(root, mode);
    const alts = [{ tonic: rt, mode: rm, key: keyName(rt, rm) + ' (same notes)', camelot: camelot(rt, rm) }];
    if (next) { const nm = next[1] ? 'minor' : 'major'; alts.push({ tonic: next[0], mode: nm, key: keyName(next[0], nm), camelot: camelot(next[0], nm) }); }
    const conf = cap ? Math.min(kconf, 0.45 + 0.2 * mconf) : Math.min(kconf, 0.5 + 0.35 * mconf);
    return { tonic: root, mode, key: keyName(root, mode), camelot: camelot(root, mode), key_conf: +conf.toFixed(2), key_alts: alts };
  }

  // ---------------------------------------------------------------- tempo + beats (librosa.beat.beat_track style)
  function melFilters(nfft, nMels, fmax) {      // librosa.filters.mel defaults: Slaney mel scale + Slaney area normalisation
    const fsp = 200 / 3, minLogHz = 1000, minLogMel = minLogHz / fsp, step = Math.log(6.4) / 27;
    const hz2mel = (f) => f >= minLogHz ? minLogMel + Math.log(f / minLogHz) / step : f / fsp;
    const mel2hz = (m) => m >= minLogMel ? minLogHz * Math.exp(step * (m - minLogMel)) : m * fsp;
    const mMax = hz2mel(fmax), pts = []; for (let i = 0; i < nMels + 2; i++) pts.push(mel2hz(mMax * i / (nMels + 1)));
    const nb = nfft / 2 + 1, fl = [];
    for (let m = 0; m < nMels; m++) {
      const a = pts[m], c = pts[m + 1], b = pts[m + 2], norm = 2 / (b - a), wts = [];
      for (let k = 0; k < nb; k++) { const f = k * SR / nfft; const v = Math.max(0, Math.min((f - a) / (c - a), (b - f) / (b - c))); if (v > 0) wts.push([k, v * norm]); }
      fl.push(wts);
    }
    return fl;
  }
  function onsetEnvelope(y) {
    const nfft = 2048, hop = 512, S = stft(y, nfft, hop, 0, nfft / 2 + 1), fl = melFilters(nfft, 128, SR / 2);
    const T = S.length, M = fl.length, db = new Array(T);
    let top = -Infinity;
    for (let t = 0; t < T; t++) {
      const row = S[t], d = new Float32Array(M);
      for (let m = 0; m < M; m++) { let e = 0; for (const [k, v] of fl[m]) e += v * row[k] * row[k]; d[m] = 10 * Math.log10(Math.max(1e-10, e)); if (d[m] > top) top = d[m]; }
      db[t] = d;
    }
    for (let t = 0; t < T; t++) for (let m = 0; m < M; m++) db[t][m] = Math.max(db[t][m], top - 80);
    const env = new Float64Array(T), buf = new Float64Array(M), diff = new Float64Array(M);
    // librosa places frame t's flux 2 frames later (n_fft / (2 * hop)); match it so tempo and beats agree with the Mac app
    for (let t = 1; t < T - 2; t++) { for (let m = 0; m < M; m++) diff[m] = Math.max(0, db[t][m] - db[t - 1][m]); env[t + 2] = median(diff, 0, M, buf); }
    return { env, fps: SR / hop };
  }
  function estimateTempo(env, fps, startBpm = 110) {
    // librosa.feature.tempo: mean of a windowed, per-frame-normalised autocorrelation (8 s windows), with a tempo prior
    const n = env.length, win = Math.round(8 * fps), N = 1 << Math.ceil(Math.log2(2 * win)), F = fft(N), w = hann(win);
    const re = new Float64Array(N), im = new Float64Array(N), tg = new Float64Array(win);
    const half = win >> 1, step = 2; let cnt = 0;
    for (let c = 0; c < n; c += step) {
      re.fill(0); im.fill(0);
      for (let i = 0; i < win; i++) { const k = c - half + i; re[i] = (k >= 0 && k < n ? env[k] : 0) * w[i]; }
      F(re, im);
      for (let i = 0; i < N; i++) { re[i] = re[i] * re[i] + im[i] * im[i]; im[i] = 0; }
      F(re, im);                      // power spectrum -> autocorrelation (real, symmetric: forward FFT is fine)
      const z = Math.abs(re[0]) || 1;
      for (let lag = 0; lag < win; lag++) tg[lag] += re[lag] / z;
      cnt++;
    }
    let best = -Infinity, bl = 1;
    for (let lag = 1; lag < win; lag++) {
      const bpm = 60 * fps / lag; if (bpm > 320 || bpm < 30) continue;
      const v = Math.log1p(1e6 * Math.max(0, tg[lag] / cnt)) - 0.5 * ((Math.log2(bpm) - Math.log2(startBpm)) / 1.0) ** 2;
      if (v > best) { best = v; bl = lag; }
    }
    return 60 * fps / bl;
  }
  function trackBeats(env, fps, bpm, tightness = 100) {
    const n = env.length, period = fps * 60 / bpm;
    let sd = 0; const m = mean(env); for (const v of env) sd += (v - m) ** 2; sd = Math.sqrt(sd / Math.max(1, n - 1)) || 1;
    const on = Float64Array.from(env, (v) => v / sd);
    const half = Math.round(period), win = []; for (let i = -half; i <= half; i++) win.push(Math.exp(-0.5 * (i * 32 / period) ** 2));
    const local = new Float64Array(n);
    for (let i = 0; i < n; i++) { let s = 0; for (let j = -half; j <= half; j++) { const k = i + j; if (k >= 0 && k < n) s += on[k] * win[j + half]; } local[i] = s; }
    const cum = new Float64Array(n), back = new Int32Array(n).fill(-1);
    const wlo = Math.round(2 * period), whi = Math.round(period / 2);
    let first = true;
    for (let i = 0; i < n; i++) {
      let bs = -Infinity, bp = -1;
      for (let p = i - wlo; p <= i - whi; p++) { if (p < 0) continue; const s = cum[p] - tightness * Math.log((i - p) / period) ** 2; if (s > bs) { bs = s; bp = p; } }
      cum[i] = local[i] + (bp >= 0 ? bs : 0); back[i] = bp;
      if (first && local[i] < 0.01 * Math.max(...local.slice(0, i + 1))) { back[i] = -1; } else first = false;
    }
    // last beat: last local maximum of the cumulative score above half the median of maxima
    const maxima = []; for (let i = 1; i < n - 1; i++) if (cum[i] > cum[i - 1] && cum[i] >= cum[i + 1]) maxima.push(cum[i]);
    const med = maxima.length ? maxima.slice().sort((a, b) => a - b)[maxima.length >> 1] : 0;
    let lastI = n - 1; for (let i = n - 2; i > 0; i--) if (cum[i] > cum[i - 1] && cum[i] >= cum[i + 1] && cum[i] >= 0.5 * med) { lastI = i; break; }
    const beats = []; for (let i = lastI; i >= 0; i = back[i]) { beats.push(i); if (back[i] < 0) break; }
    beats.reverse();
    // trim weak beats at the edges
    const thr = 0.5 * Math.sqrt(mean(beats.map((b) => local[b] ** 2)));
    let a = 0, b = beats.length; while (a < b && local[beats[a]] <= thr) a++; while (b > a && local[beats[b - 1]] <= thr) b--;
    return beats.slice(a, b).map((f) => f / fps);
  }
  function tempoAndBeats(y) {
    const { env, fps } = onsetEnvelope(y);
    let bpm = estimateTempo(env, fps);
    const beats = trackBeats(env, fps, bpm);
    if (beats.length > 8) {     // refine: straight line through the beat grid
      const d = []; for (let i = 1; i < beats.length; i++) d.push(beats[i] - beats[i - 1]);
      const md = d.slice().sort((a, b) => a - b)[d.length >> 1], ks = [0], ts = [beats[0]];
      d.forEach((v, i) => { if (Math.abs(v - md) < 0.25 * md) { ks.push(i + 1); ts.push(beats[i + 1]); } });
      if (ks.length > 7) { const mk = mean(ks), mt = mean(ts); let num = 0, den = 0; for (let i = 0; i < ks.length; i++) { num += (ks[i] - mk) * (ts[i] - mt); den += (ks[i] - mk) ** 2; } const slope = num / den; if (slope > 0) bpm = 60 / slope; }
    }
    let conf = 0.5;
    if (beats.length > 8) { const d = []; for (let i = 1; i < beats.length; i++) d.push(beats[i] - beats[i - 1]); const m = mean(d); let s = 0; for (const v of d) s += (v - m) ** 2; conf = Math.max(0, Math.min(1, 1 - Math.sqrt(s / d.length) / (m + 1e-9) * 4)); }
    return { bpm, beats, conf };
  }

  // ---------------------------------------------------------------- waveform + chorus
  function peaks(y, perSec = 50) {
    const hop = Math.round(SR / perSec), nfft = 2048, S = stft(y, nfft, hop, 0, nfft / 2 + 1), T = S.length;
    const kLow = Math.ceil(250 * nfft / SR), kMid = Math.ceil(4000 * nfft / SR);
    const low = new Float64Array(T), mid = new Float64Array(T), high = new Float64Array(T);
    for (let t = 0; t < T; t++) { const r = S[t]; let a = 0, b = 0, c = 0; for (let k = 0; k < r.length; k++) { const e = r[k] * r[k]; if (k < kLow) a += e; else if (k < kMid) b += e; else c += e; } low[t] = Math.sqrt(a); mid[t] = Math.sqrt(b); high[t] = Math.sqrt(c); }
    const n = Math.min(T, Math.floor(y.length / hop) + 1), pk = new Float64Array(n);
    for (let i = 0; i < n; i++) { let m = 0; for (let j = i * hop; j < Math.min(y.length, (i + 1) * hop); j++) m = Math.max(m, Math.abs(y[j])); pk[i] = m; }
    const norm = (a) => { const s = Array.from(a.slice(0, n)).sort((x, z) => x - z), p = s[Math.min(s.length - 1, Math.floor(s.length * 0.995))] + 1e-9; return Array.from(a.slice(0, n), (v) => Math.round(Math.min(1, v / p) * 1000) / 1000); };
    return { per_sec: perSec, peak: norm(pk), low: norm(low), mid: norm(mid), high: norm(high) };
  }
  const snap = (t, beats) => { if (!beats || !beats.length) return t; let b = beats[0]; for (const x of beats) if (Math.abs(x - t) < Math.abs(b - t)) b = x; return b; };
  function hotStart(y, beats, length = 30) {
    const hop = SR >> 2, n = Math.floor(y.length / hop); if (n < 2) return 0;
    const rms = new Float64Array(n); for (let i = 0; i < n; i++) { let s = 0; for (let j = i * hop; j < (i + 1) * hop; j++) s += y[j] * y[j]; rms[i] = Math.sqrt(s / hop); }
    const w = Math.max(1, Math.round(length * 4)); if (n <= w) return 0;
    let s = 0; for (let i = 0; i < w; i++) s += rms[i]; let best = s, bi = 0;
    for (let i = w; i < n; i++) { s += rms[i] - rms[i - w]; if (s > best) { best = s; bi = i - w + 1; } }
    return Math.round(snap(bi / 4, beats) * 1000) / 1000;
  }

  /** everything the decks need about a song */
  function analyse(y, progress) {
    progress = progress || (() => {});
    const yy = y.length > SR * 900 ? y.subarray(0, SR * 900) : y;
    progress(0.1, 'Finding the beat…');
    const { bpm, beats, conf } = tempoAndBeats(yy);
    progress(0.45, 'Finding the key…');
    const k = detectKey(yy, beats);
    progress(0.85, 'Drawing the waveform…');
    const out = keyFields(k.root, k.minor, k.notesConf, k.modeConf, k.next);
    Object.assign(out, { bpm: Math.round(bpm * 10) / 10, tempo_conf: Math.round(conf * 100) / 100, duration: Math.round(y.length / SR * 100) / 100,
      beats: beats.slice(0, 20000).map((b) => Math.round(b * 1000) / 1000), hot_start: hotStart(yy, beats), key_source: 'mix' });
    out.peaks = peaks(yy);
    progress(1, 'Done');
    return out;
  }

  // ---------------------------------------------------------------- matching (music.match)
  const wrapS = (s) => wrap12(s + 6) - 6;
  function shiftsToFit(main, cand) {
    const mt = main.tonic, mm = main.mode, ct = cand.tonic, cm = cand.mode;
    const same = isMinor(cm) === isMinor(mm), base = same ? mt : relative(mt, mm)[0], kind0 = same ? 'same' : 'relative';
    const out = [];
    for (const [tr, kind] of [[base, kind0], [wrap12(base + 7), 'neighbour'], [wrap12(base + 5), 'neighbour']]) {
      const w = wrapS(tr - ct), opts = [...new Set([w, w + 12, w - 12])].sort((a, b) => Math.abs(a) - Math.abs(b));
      for (const s of opts) if (s >= -5 && s <= 5) { out.push({ semitones: s, kind }); break; }
    }
    out.sort((a, b) => (Math.abs(a.semitones) + (a.kind === 'neighbour' ? 1.5 : 0)) - (Math.abs(b.semitones) + (b.kind === 'neighbour' ? 1.5 : 0)));
    return out;
  }
  function tempoFit(mainBpm, candBpm) {
    let best = null;
    for (const [mult, feel] of [[1, 'normal'], [2, 'double time'], [0.5, 'half time']]) { const ratio = mainBpm / (candBpm * mult); if (!best || Math.abs(ratio - 1) < Math.abs(best.ratio - 1)) best = { ratio, feel }; }
    return best;
  }
  function match(main, cand) {
    const sh = shiftsToFit(main, cand), best = sh[0] || { semitones: 0, kind: 'none' }, tf = tempoFit(main.bpm, cand.bpm);
    const stretch = (tf.ratio - 1) * 100, s = best.semitones;
    const keyPen = Math.abs(s) * 9 + ({ neighbour: 6, relative: 2 }[best.kind] || 0) + (best.kind === 'none' ? 25 : 0);
    const conf = 0.5 * ((main.key_conf ?? 0.5) + (cand.key_conf ?? 0.5));
    const fit = Math.max(0, Math.min(100, Math.round(100 - keyPen - Math.abs(stretch) * 2.6 - (1 - conf) * 6)));
    const res = keyName(wrap12(cand.tonic + s), cand.mode);
    const why = best.kind === 'same' ? (s ? `lands on ${res}, the same key as your main song` : `already in ${res}, the same key as your main song`)
      : best.kind === 'relative' ? `${s ? 'lands on' : 'already in'} ${res}, which uses the same notes as ${main.key}`
      : best.kind === 'neighbour' ? `${s ? 'lands on' : 'already in'} ${res}, a neighbouring key that blends smoothly` : 'no close key within 5 semitones';
    return { fit, shift: s, kind: best.kind, why, alternatives: sh.slice(1).map((d) => d.semitones), tempo_ratio: +tf.ratio.toFixed(5), stretch_pct: +stretch.toFixed(2), feel: tf.feel };
  }

  const api = { _dbg: { estimateTempo, onsetEnvelope, trackBeats }, SR, analyse, match, keyName, camelot, relative, keyFields, detectKey, tempoAndBeats, peaks, hotStart };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.TruAnalysis = api;
  // as a Web Worker: {id, y} in -> {id, result} / progress out
  if (typeof WorkerGlobalScope !== 'undefined' && root instanceof WorkerGlobalScope) {
    root.onmessage = (e) => {
      const { id, y } = e.data;
      try { const r = analyse(y, (p, msg) => root.postMessage({ id, progress: p, msg })); root.postMessage({ id, result: r }); }
      catch (err) { root.postMessage({ id, error: String(err && err.message || err) }); }
    };
  }
})(typeof self !== 'undefined' ? self : this);
