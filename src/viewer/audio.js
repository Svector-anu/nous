// Ambience: three procedural beds for the world, synthesised in the browser.
//
// Nothing is fetched. There is no audio file, no cdn, and no licence to worry about —
// the sound is a handful of oscillators through a filter, so it cannot block the page
// load and costs nothing to serve. That is the whole reason for doing it this way.
//
// The simulation has no idea this exists. Mood is read off the snapshot the viewer
// already has; no system is consulted and nothing is written back.

export const MOODS = ["calm", "active", "tense"];

// Each bed is the same graph with different tuning. Crossfading between two full graphs
// would double the voice count for a sound nobody is listening to closely; retargeting
// the parameters gets the same drift for a third of the work.
//
// The intervals are what carry the mood. Calm is a root, a fifth and an octave — an open
// consonance. Active nudges the octave a few cents sharp so the two beat against each
// other, which reads as motion. Tense uses a tritone and a minor second, the two most
// unsettled intervals available, plus a resonant filter that makes the beating audible.
const PRESETS = {
  calm:   { root: 55.0, ratios: [1, 1.5, 2],      detune: 4,  cutoff: 320, q: 0.7, lfoRate: 0.05, lfoDepth: 90,  noise: 0.000, gain: 0.16 },
  active: { root: 61.7, ratios: [1, 1.5, 2.006],  detune: 7,  cutoff: 620, q: 1.1, lfoRate: 0.22, lfoDepth: 240, noise: 0.015, gain: 0.19 },
  tense:  { root: 49.0, ratios: [1, 1.414, 1.06], detune: 11, cutoff: 480, q: 4.5, lfoRate: 0.60, lfoDepth: 330, noise: 0.045, gain: 0.21 },
};

// How long a mood takes to become the new one. Long on purpose: a bed that snapped
// between states would announce the state machine rather than the world.
const MOOD_GLIDE = 3.5;

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

// A short burst of white noise, looped. Used at very low gain as air rather than as a
// sound in its own right — it stops the oscillators from sounding like a test tone.
function noiseBuffer(ctx) {
  const frames = ctx.sampleRate * 2;
  const buffer = ctx.createBuffer(1, frames, ctx.sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < frames; i++) data[i] = Math.random() * 2 - 1;
  return buffer;
}

export class Ambience {
  constructor() {
    this.ctx = null;
    this.enabled = false;
    this.mood = "calm";
    this.voices = [];
    this.master = null;
    this.filter = null;
    this.noiseGain = null;
    this.lfo = null;
    this.lfoDepth = null;
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

    this.master = ctx.createGain();
    this.master.gain.value = 0;
    this.master.connect(ctx.destination);

    this.filter = ctx.createBiquadFilter();
    this.filter.type = "lowpass";
    this.filter.frequency.value = preset.cutoff;
    this.filter.Q.value = preset.q;
    this.filter.connect(this.master);

    // Slow sweep of the filter, so the bed breathes instead of sitting still.
    this.lfo = ctx.createOscillator();
    this.lfo.frequency.value = preset.lfoRate;
    this.lfoDepth = ctx.createGain();
    this.lfoDepth.gain.value = preset.lfoDepth;
    this.lfo.connect(this.lfoDepth).connect(this.filter.frequency);
    this.lfo.start();

    this.voices = preset.ratios.map((ratio, index) => {
      const osc = ctx.createOscillator();
      osc.type = index === 0 ? "sine" : "triangle";
      osc.frequency.value = preset.root * ratio;
      // Alternating detune keeps the pair beating rather than drifting the whole stack
      // the same way, which would just sound out of tune.
      osc.detune.value = index % 2 === 0 ? preset.detune : -preset.detune;
      const gain = ctx.createGain();
      gain.gain.value = index === 0 ? 0.5 : 0.28;
      osc.connect(gain).connect(this.filter);
      osc.start();
      return { osc, gain, ratio };
    });

    const noise = ctx.createBufferSource();
    noise.buffer = noiseBuffer(ctx);
    noise.loop = true;
    this.noiseGain = ctx.createGain();
    this.noiseGain.gain.value = preset.noise;
    noise.connect(this.noiseGain).connect(this.filter);
    noise.start();
    this.noise = noise;

    // Chrome starts a context suspended until a gesture is seen; resume is a no-op when
    // it is already running.
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});

    this.master.gain.setTargetAtTime(preset.gain, ctx.currentTime, 1.2);
    this.enabled = true;
    return true;
  }

  /** Fade out and tear the graph down. Silence should cost nothing to keep. */
  disable() {
    if (!this.enabled || !this.ctx) {
      this.enabled = false;
      return;
    }
    const ctx = this.ctx;
    this.master.gain.setTargetAtTime(0, ctx.currentTime, 0.25);
    const dying = { ctx, voices: this.voices, lfo: this.lfo, noise: this.noise };
    setTimeout(() => {
      for (const voice of dying.voices) { try { voice.osc.stop(); } catch {} }
      try { dying.lfo.stop(); } catch {}
      try { dying.noise.stop(); } catch {}
      dying.ctx.close().catch(() => {});
    }, 900);
    this.ctx = null;
    this.voices = [];
    this.master = null;
    this.filter = null;
    this.enabled = false;
  }

  /** Glide to a different bed. Silent and free when sound is off. */
  setMood(mood) {
    if (!MOODS.includes(mood)) return;
    this.mood = mood;
    if (!this.enabled || !this.ctx) return;
    const preset = PRESETS[mood];
    const now = this.ctx.currentTime;
    const glide = MOOD_GLIDE / 3;

    this.filter.frequency.setTargetAtTime(preset.cutoff, now, glide);
    this.filter.Q.setTargetAtTime(preset.q, now, glide);
    this.lfo.frequency.setTargetAtTime(preset.lfoRate, now, glide);
    this.lfoDepth.gain.setTargetAtTime(preset.lfoDepth, now, glide);
    this.noiseGain.gain.setTargetAtTime(preset.noise, now, glide);
    this.master.gain.setTargetAtTime(preset.gain, now, glide);
    this.voices.forEach((voice, index) => {
      const ratio = preset.ratios[index] ?? voice.ratio;
      voice.osc.frequency.setTargetAtTime(preset.root * ratio, now, glide);
      voice.osc.detune.setTargetAtTime(index % 2 === 0 ? preset.detune : -preset.detune, now, glide);
    });
  }
}
