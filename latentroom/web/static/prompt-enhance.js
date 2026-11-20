// Enhance prompt: the button by the Prompt label. The text encoder rewrites a short idea
// (any language) into a full English prompt; Undo puts the user's text back. Errors are plain
// sentences for toast(); the user's text is never lost.
import { $, api, toast } from './ui.js';

export const enhancePrompt = async prompt => (await api('/api/prompt/enhance', { prompt })).prompt;

// running(): a picture job of ours is queued or running. edit(): references or From a picture,
// where the official rewrite prompt (new pictures) does not fit, so there is no button.
// Returns sync(): call it when the prompt, the mode or the jobs change.
export function initEnhance({ app, running, edit }) {
  const b = $('#enh'), u = $('#enh-undo'), p = $('#prompt');
  let model = false, busy = false, before = null, after = null; // model: missing until /api/models answers
  const why = () => !model ? 'Needs the text encoder (Models tab).' : !p.value.trim() ? 'Write an idea first.'
    : running() ? 'Wait for the picture to finish.' : '';
  const label = t => { b.lastChild.textContent = t; };
  function sync() {
    const w = busy ? '' : why();
    b.hidden = edit(); b.title = w; b.setAttribute('aria-disabled', String(busy || !!w));
    u.hidden = b.hidden || busy || before === null || p.value !== after; // edited after: it is the user's text now
  }
  async function check() { // the catalogue (re)loaded: is the text encoder on disk? (same check as the engine: by kind)
    try {
      const { installed } = await api('/api/models');
      model = (app.files || []).some(f => f.kind === 'llm' && installed.includes(f.id));
    } catch { /* keep the last known state */ }
    sync();
  }
  function put(text) { p.value = text; p.dispatchEvent(new Event('input')); }
  b.onclick = async () => {
    if (busy) return;
    const w = why(); if (w) return toast(w); // a phone has no tooltips: a tap says it
    const idea = p.value;
    busy = true; p.readOnly = true; b.setAttribute('aria-busy', 'true'); label('Enhancing…'); sync();
    window.dispatchEvent(new Event('lr-busy')); // the VRAM meter reads live while it runs
    try {
      const out = await enhancePrompt(idea);
      if (p.value === idea) { before = idea; after = out; put(out); } // a Re-develop meanwhile wins
    } catch (e) { toast(e.message); } finally {
      busy = false; p.readOnly = false; b.removeAttribute('aria-busy'); label('Enhance prompt'); sync();
    }
  };
  u.onclick = () => { const t = before; before = after = null; put(t); p.focus(); };
  p.addEventListener('input', sync);
  window.addEventListener('lr-model', check);
  return sync;
}
