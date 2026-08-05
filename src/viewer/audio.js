// Ambience: a dark procedural score for the world — ambient hip-hop under heavy trap.
//
// Synthesised in the browser. There is no audio file, no cdn and no licence question, so
// it cannot block the page load and costs nothing to serve. Loops can be dropped in later
// (see BEDS below and README) and they take over automatically; the synth is the fallback,
// not a placeholder.
//
// The simulation has no idea this exists. Mood is read off the snapshot the viewer already
// has; no system is consulted and nothing is written back.

export const MOODS = ["calm", "active", "tense"];

// The old drone peaked at 0.16-0.21 of full scale with no limiter, which measured as a
// whisper against anything else running on the machine. The score now feeds a limiter, so
// the master can sit high without spitting on transients: 0.75 is about four times the old
// level, and the limiter is what makes that safe rather than merely loud.
export const DEFAULT_VOLUME = 0.75;

export function clampVolume(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return DEFAULT_VOLUME;
  return Math.min(1, Math.max(0, n));
}

// Where drop-in loops live, if any. Served by the /static mount, so a file at
// src/viewer/beds/calm.webm answers on /static/beds/calm.webm. Absent by default: the
// probe is a single HEAD-ish fetch on enable, never on page load.
export const BEDS_URL = "/static/beds";
const BED_FILES = { calm: "calm", active: "active", tense: "tense" };

// --- musical material --------------------------------------------------------
// Everything is derived from one scale so the layers cannot disagree. D phrygian: the
// flat second is what makes it read as dark rather than merely minor, and it is the
// interval trap leans on hardest.
const ROOT = 36.71; // D1, the 808's home
const PHRYGIAN = [0, 1, 3, 5, 7, 8, 10];
const semitone = (n) => Math.pow(2, n / 12);

// Per-mood arrangement. `hats` is subdivisions per beat, `drive` the waveshaper amount,
// `swing` how far offbeats are pushed late. Tempo stays put across moods — a score that
// changed speed with the world would read as a progress bar.
const BPM = 71; // halftime trap: the kick pattern implies 142
const PRESETS = {
  calm: {
    gain: 0.55, pad: 0.42, sub: 0.30, drums: 0.16, hats: 0, rolls: 0,
    cutoff: 620, q: 1.4, drive: 0.10, swing: 0.14, air: 0.020,
    chord: [0, 3, 7, 10], sparse: 0.45, delay: 0.34,
  },
  active: {
    gain: 0.72, pad: 0.34, sub: 0.52, drums: 0.44, hats: 2, rolls: 0.12,
    cutoff: 980, q: 2.0, drive: 0.26, swing: 0.16, air: 0.030,
    chord: [0, 3, 7, 10], sparse: 0.80, delay: 0.28,
  },
  tense: {
    gain: 0.86, pad: 0.30, sub: 0.68, drums: 0.60, hats: 4, rolls: 0.42,
    cutoff: 1500, q: 3.6, drive: 0.45, swing: 0.10, air: 0.045,
    // The flat second against the fifth is the dissonance the whole mood rests on.
    chord: [0, 1, 7, 10], sparse: 1.0, delay: 0.22,
  },
};

const MOOD_GLIDE = 2.6;

/** Is there a working web audio implementation here at all? */
export function supported() {
  return typeof window !== "undefined" && !!(window.AudioContext || window.webkitAudioContext);
}

/**
 * Pick a bed for a snapshot.
 *
 * Pure, and exported separately from the synth so it can be asserted without a speaker.
 * Anyone running for their life outranks everything else; otherwise the split is simply
 * how much of the population is doing something.
 */
export function moodFor(snapshot) {
  if (!snapshot || !snapshot.agents || snapshot.agents.length === 0) return "calm";
  const agents = snapshot.agents;
  const fleeing = agents.filter((a) => a.state === "FLEE").length;
  if (fleeing > 0) return "tense";
  const working = agents.filter(
    (a) => a.state === "GATHER" || a.state === "BUILD" || a.state === "SEEK_NEED"
  ).length;
  return working / agents.length >= 0.45 ? "active" : "calm";
}

// --- helpers -----------------------------------------------------------------

function noiseBuffer(ctx, seconds = 2) {
  const buffer = ctx.createBuffer(1, Math.floor(ctx.sampleRate * seconds), ctx.sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  return buffer;
}

// Soft asymmetric saturation. This is what makes the 808 read as "heavy" rather than as a
// loud sine — the harmonics it adds are what a small speaker actually reproduces, so the
// bass survives on a laptop instead of disappearing below its response.
function driveCurve(amount) {
  const n = 1024;
  const curve = new Float32Array(n);
  const k = amount * 60;
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / n - 1;
    curve[i] = ((1 + k) * x) / (1 + k * Math.abs(x));
  }
  return curve;
}

// A short exponential-decay noise burst, used as a cheap reverb impulse. Cheaper and more
// controllable than shipping an impulse file, and it is the psychedelic depth: a long
// dark tail behind a dry beat.
function reverbImpulse(ctx, seconds = 2.8, decay = 3.2) {
  const length = Math.floor(ctx.sampleRate * seconds);
  const impulse = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let channel = 0; channel < 2; channel++) {
    const data = impulse.getChannelData(channel);
    for (let i = 0; i < length; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
    }
  }
  return impulse;
}

export class Ambience {
  /**
   * @param {number} volume 0..1, the user-facing music level.
   */
  constructor(volume = DEFAULT_VOLUME) {
    this.ctx = null;
    this.enabled = false;
    this.mood = "calm";
    this.volume = clampVolume(volume);
    this.usingBeds = false;
    this.voices = [];
    this.master = null;
    this.timer = null;
    this._step = 0;
    this._nextNoteAt = 0;
  }

  /**
   * Build the graph and start playing.
   *
   * Must be called from a user gesture: browsers refuse to start an AudioContext without
   * one, and a context created too early sits suspended and silent. That constraint is
   * also why sound is off until somebody asks for it — there is no autoplay path here.
   */
  async enable() {
    if (this.enabled) return true;
    if (!supported()) return false;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctor();
    const ctx = this.ctx;
    const preset = PRESETS[this.mood] ?? PRESETS.calm;

    // Signal path, in order:
    //   voices -> mix -> shaper (drive) -> limiter -> master (user volume) -> speakers
    // The limiter is what lets this be loud without clipping. Without it, pushing the
    // master up far enough to be audible over a room made the 808 spit on transients.
    this.master = ctx.createGain();
    this.master.gain.value = 0;
    this.master.connect(ctx.destination);

    this.limiter = ctx.createDynamicsCompressor();
    this.limiter.threshold.value = -8;
    this.limiter.knee.value = 6;
    this.limiter.ratio.value = 12;
    this.limiter.attack.value = 0.004;
    this.limiter.release.value = 0.18;
    this.limiter.connect(this.master);

    this.shaper = ctx.createWaveShaper();
    this.shaper.curve = driveCurve(preset.drive);
    this.shaper.oversample = "2x";
    this.shaper.connect(this.limiter);

    this.mix = ctx.createGain();
    this.mix.gain.value = preset.gain;
    this.mix.connect(this.shaper);

    // Reverb send, and a modulated feedback delay for the psychedelic smear.
    this.reverb = ctx.createConvolver();
    this.reverb.buffer = reverbImpulse(ctx);
    this.reverbGain = ctx.createGain();
    this.reverbGain.gain.value = 0.5;
    this.reverb.connect(this.reverbGain).connect(this.mix);

    this.delay = ctx.createDelay(1.5);
    this.delay.delayTime.value = (60 / BPM) * 0.75; // dotted eighth, the classic dub throw
    this.delayFeedback = ctx.createGain();
    this.delayFeedback.gain.value = preset.delay;
    this.delayFilter = ctx.createBiquadFilter();
    this.delayFilter.type = "lowpass";
    this.delayFilter.frequency.value = 1800;
    this.delay.connect(this.delayFilter).connect(this.delayFeedback).connect(this.delay);
    this.delayFeedback.connect(this.mix);

    // Slow wobble on the delay time: tape flutter, and the main source of drift.
    this.wobble = ctx.createOscillator();
    this.wobble.frequency.value = 0.07;
    this.wobbleDepth = ctx.createGain();
    this.wobbleDepth.gain.value = 0.0035;
    this.wobble.connect(this.wobbleDepth).connect(this.delay.delayTime);
    this.wobble.start();

    this.noise = noiseBuffer(ctx);

    if (!(await this._startBeds(preset))) this._startPad(preset);

    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    // Short enough to be at level in about a second — a long swell reads as the page
    // being broken rather than as a fade.
    this.master.gain.setTargetAtTime(this.volume, ctx.currentTime, 0.3);
    this.enabled = true;

    if (!this.usingBeds) {
      this._step = 0;
      this._nextNoteAt = ctx.currentTime + 0.1;
      // A lookahead scheduler rather than one timer per note: setTimeout is far too
      // jittery for rhythm, so notes are queued ahead against the audio clock, which is
      // sample-accurate.
      this.timer = setInterval(() => this._schedule(), 25);
    }
    return true;
  }

  /** Optional drop-in loops. Absent by default; the synth carries the score if so. */
  async _startBeds(preset) {
    if (typeof fetch !== "function") return false;
    try {
      const response = await fetch(`${BEDS_URL}/manifest.json`, { cache: "no-store" });
      if (!response.ok) return false;
      const manifest = await response.json();
      // Ships empty. An empty manifest is the normal state and means "synthesise" — it
      // exists so the probe gets a 200 rather than a 404, because a failed fetch prints a
      // console error even when it is caught, and a clean console is worth a two-byte file.
      if (!MOODS.every((mood) => typeof manifest[mood] === "string" && manifest[mood])) {
        return false;
      }
      const ctx = this.ctx;
      const entries = await Promise.all(
        MOODS.map(async (mood) => {
          const name = manifest[mood] ?? `${BED_FILES[mood]}.webm`;
          const file = await fetch(`${BEDS_URL}/${name}`);
          if (!file.ok) throw new Error(`missing bed: ${name}`);
          return [mood, await ctx.decodeAudioData(await file.arrayBuffer())];
        })
      );
      this.bedNodes = {};
      for (const [mood, buffer] of entries) {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.loop = true;
        const gain = ctx.createGain();
        // All three run together and are crossfaded, so a mood change is seamless rather
        // than a restart from the top of a different file.
        gain.gain.value = mood === this.mood ? 1 : 0;
        source.connect(gain).connect(this.mix);
        source.start();
        this.bedNodes[mood] = { source, gain };
      }
      this.usingBeds = true;
      return true;
    } catch {
      // Any failure at all falls back to the synth. A missing or malformed loop must not
      // cost the visitor their sound.
      this.bedNodes = null;
      return false;
    }
  }

  _startPad(preset) {
    const ctx = this.ctx;
    this.padFilter = ctx.createBiquadFilter();
    this.padFilter.type = "lowpass";
    this.padFilter.frequency.value = preset.cutoff;
    this.padFilter.Q.value = preset.q;
    this.padGain = ctx.createGain();
    this.padGain.gain.value = preset.pad;
    this.padFilter.connect(this.padGain).connect(this.mix);
    this.padGain.connect(this.reverb);

    this.lfo = ctx.createOscillator();
    this.lfo.frequency.value = 0.045;
    this.lfoDepth = ctx.createGain();
    this.lfoDepth.gain.value = 260;
    this.lfo.connect(this.lfoDepth).connect(this.padFilter.frequency);
    this.lfo.start();

    // The pad is the ambient half: a wide, slowly beating chord two octaves up from the
    // 808 so the two never fight for the same space.
    this.voices = preset.chord.map((step, index) => {
      const osc = ctx.createOscillator();
      osc.type = index === 0 ? "triangle" : "sawtooth";
      osc.frequency.value = ROOT * 4 * semitone(PHRYGIAN[step % 7] + 12 * Math.floor(step / 7));
      osc.detune.value = index % 2 === 0 ? 6 : -7;
      const gain = ctx.createGain();
      gain.gain.value = index === 0 ? 0.32 : 0.17;
      osc.connect(gain).connect(this.padFilter);
      osc.start();
      return { osc, gain, step };
    });

    // Air: filtered noise well under the music, so the silence between hits is never
    // digitally empty.
    const air = ctx.createBufferSource();
    air.buffer = this.noise;
    air.loop = true;
    this.airFilter = ctx.createBiquadFilter();
    this.airFilter.type = "bandpass";
    this.airFilter.frequency.value = 5200;
    this.airGain = ctx.createGain();
    this.airGain.gain.value = preset.air;
    air.connect(this.airFilter).connect(this.airGain).connect(this.mix);
    air.start();
    this.air = air;
  }

  // --- the beat --------------------------------------------------------------

  _schedule() {
    if (!this.ctx || !this.enabled) return;
    const preset = PRESETS[this.mood] ?? PRESETS.calm;
    const beat = 60 / BPM;
    const stepLength = beat / 4; // sixteenths
    while (this._nextNoteAt < this.ctx.currentTime + 0.2) {
      this._playStep(this._step % 32, this._nextNoteAt, preset, stepLength);
      this._step++;
      this._nextNoteAt += stepLength;
    }
  }

  _playStep(step, when, preset, stepLength) {
    const beat = step % 4;
    const bar = Math.floor(step / 16);
    // Swing: odd sixteenths land late, which is what stops a grid from sounding typed in.
    const at = when + (step % 2 === 1 ? stepLength * preset.swing : 0);

    // Kick on 1 and the classic trap pickup before 3.
    if (step % 16 === 0 || step % 16 === 10) this._kick(at, preset);
    // Snare halftime: beat 3 of each bar, which is what makes 142 feel like 71.
    if (step % 16 === 8) this._snare(at, preset);

    if (preset.hats > 0 && step % (4 / preset.hats) === 0) {
      this._hat(at, preset, 0.5);
    }
    // Rolls: the trap signature. A burst of fast hats at the end of a phrase.
    if (preset.rolls > 0 && step % 16 === 14 && Math.random() < preset.rolls) {
      const divisions = 6;
      for (let i = 0; i < divisions; i++) {
        this._hat(at + (stepLength / divisions) * i, preset, 0.34);
      }
    }

    // 808: root on the downbeat, then a scale move. Sparse in calm, insistent in tense.
    if (step % 16 === 0 || (step % 16 === 6 && Math.random() < preset.sparse)) {
      const degree = step % 16 === 0 ? 0 : [3, 5, 6][bar % 3];
      this._eight0eight(at, preset, degree, stepLength * (step % 16 === 0 ? 6 : 4));
    }
  }

  _eight0eight(at, preset, degree, length) {
    const ctx = this.ctx;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    const target = ROOT * semitone(PHRYGIAN[degree % 7]);
    // The pitch glide into the note is the 808's whole character.
    osc.frequency.setValueAtTime(target * 1.5, at);
    osc.frequency.exponentialRampToValueAtTime(target, at + 0.06);
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(preset.sub, at + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + length);
    osc.connect(gain).connect(this.mix);
    osc.start(at);
    osc.stop(at + length + 0.05);
  }

  _kick(at, preset) {
    const ctx = this.ctx;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.setValueAtTime(180, at);
    osc.frequency.exponentialRampToValueAtTime(46, at + 0.09);
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(preset.drums, at);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.22);
    osc.connect(gain).connect(this.mix);
    osc.start(at);
    osc.stop(at + 0.25);
  }

  _snare(at, preset) {
    const ctx = this.ctx;
    const source = ctx.createBufferSource();
    source.buffer = this.noise;
    const filter = ctx.createBiquadFilter();
    filter.type = "bandpass";
    filter.frequency.value = 1900;
    filter.Q.value = 0.8;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(preset.drums * 0.8, at);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
    source.connect(filter).connect(gain).connect(this.mix);
    // A snare into a long dark tail is most of the psychedelia.
    gain.connect(this.reverb);
    source.start(at, Math.random(), 0.3);
    source.stop(at + 0.2);
  }

  _hat(at, preset, level) {
    const ctx = this.ctx;
    const source = ctx.createBufferSource();
    source.buffer = this.noise;
    const filter = ctx.createBiquadFilter();
    filter.type = "highpass";
    filter.frequency.value = 7800;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(preset.drums * level * 0.5, at);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.045);
    source.connect(filter).connect(gain).connect(this.mix);
    source.start(at, Math.random(), 0.08);
    source.stop(at + 0.06);
  }

  // --- controls --------------------------------------------------------------

  /** User-facing music level, 0..1. Applies live and survives a mute. */
  setVolume(value) {
    this.volume = clampVolume(value);
    if (this.enabled && this.master && this.ctx) {
      this.master.gain.setTargetAtTime(this.volume, this.ctx.currentTime, 0.05);
    }
    return this.volume;
  }

  /** Glide to a different bed. Silent and free when sound is off. */
  setMood(mood) {
    if (!MOODS.includes(mood)) return;
    const changed = mood !== this.mood;
    this.mood = mood;
    if (!this.enabled || !this.ctx) return;
    const preset = PRESETS[mood];
    const now = this.ctx.currentTime;
    const glide = MOOD_GLIDE / 3;

    this.mix.gain.setTargetAtTime(preset.gain, now, glide);
    this.delayFeedback.gain.setTargetAtTime(preset.delay, now, glide);
    if (changed) this.shaper.curve = driveCurve(preset.drive);

    if (this.usingBeds && this.bedNodes) {
      for (const name of MOODS) {
        this.bedNodes[name].gain.gain.setTargetAtTime(name === mood ? 1 : 0, now, glide);
      }
      return;
    }

    if (this.padFilter) {
      this.padFilter.frequency.setTargetAtTime(preset.cutoff, now, glide);
      this.padFilter.Q.setTargetAtTime(preset.q, now, glide);
      this.padGain.gain.setTargetAtTime(preset.pad, now, glide);
      this.airGain.gain.setTargetAtTime(preset.air, now, glide);
      this.voices.forEach((voice, index) => {
        const step = preset.chord[index] ?? voice.step;
        const hz = ROOT * 4 * semitone(PHRYGIAN[step % 7] + 12 * Math.floor(step / 7));
        voice.osc.frequency.setTargetAtTime(hz, now, glide);
      });
    }
  }

  /** Fade out and tear the graph down. Silence should cost nothing to keep. */
  disable() {
    if (!this.enabled || !this.ctx) {
      this.enabled = false;
      return;
    }
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    const ctx = this.ctx;
    this.master.gain.setTargetAtTime(0, ctx.currentTime, 0.2);
    const dying = {
      ctx,
      stoppable: [
        ...this.voices.map((v) => v.osc),
        this.lfo, this.wobble, this.air,
        ...(this.bedNodes ? MOODS.map((m) => this.bedNodes[m].source) : []),
      ].filter(Boolean),
    };
    setTimeout(() => {
      for (const node of dying.stoppable) { try { node.stop(); } catch {} }
      dying.ctx.close().catch(() => {});
    }, 800);
    this.ctx = null;
    this.voices = [];
    this.master = null;
    this.bedNodes = null;
    this.usingBeds = false;
    this.enabled = false;
  }
}

