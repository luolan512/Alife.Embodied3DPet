import assert from 'node:assert/strict';
import {NaturalBlink, PetExpressionController} from '../../emotionAdapter/index';

const blink = new NaturalBlink(() => .5);
let clock = 0, activeStart = -1, activeEnd = -1, maximum = 0, pulses = 0, active = false;
for (let i = 0; i < 1200; i++) {
  clock += 1 / 120;
  const w = blink.update(1 / 120);
  assert.equal(w.blinkLeft, w.blinkRight);
  assert(w.blinkLeft >= 0 && w.blinkLeft <= 1);
  if (clock < 4.99) assert.equal(w.blinkLeft, 0);
  if (w.blinkLeft > 0 && !active) {pulses++; activeStart = clock;}
  if (w.blinkLeft === 0 && active) activeEnd = clock;
  active = w.blinkLeft > 0;
  maximum = Math.max(maximum, w.blinkLeft);
}
assert.equal(pulses, 1, 'one timed pulse, with no independent slow-blink scheduler');
assert.equal(maximum, 1);
assert(activeEnd - activeStart > .24 && activeEnd - activeStart < .28);
blink.reset();assert.equal(blink.update(0).blinkLeft, 0);

const at30 = new PetExpressionController(() => .5), at60 = new PetExpressionController(() => .5);
at30.set('happy', .65);at60.set('happy', .65);
let a = at30.update(0), b = at60.update(0);
for (let i = 0; i < 30; i++) a = at30.update(1 / 30);
for (let i = 0; i < 60; i++) b = at60.update(1 / 60);
assert(Math.abs(a.smile - b.smile) < 1e-12, 'smile smoothing is independent of frame rate');
assert(a.smile > .64 && a.smile <= .65);
at30.set('sad', 1);for (let i = 0; i < 90; i++) a = at30.update(1 / 30);
assert.equal(a.smile, 0, 'smile releases for non-happy emotions');

const expressions = new PetExpressionController(() => .5);
let count = 0;
for (let i = 0; i < 660; i++) {
  expressions.set(i % 2 ? 'happy' : 'angry', .8);
  const w = expressions.update(1 / 120);
  if (w.blinkLeft > 0) count++;
}
assert(count > 20, 'frequent emotion messages do not postpone natural blink');
const before = expressions.update(0);
assert.deepEqual(expressions.update(Number.NaN), before);
assert.deepEqual(expressions.update(-1), before);
expressions.set('happy', Number.NaN);
expressions.reset();assert.deepEqual(expressions.update(0), {smile: 0, blinkLeft: 0, blinkRight: 0});
console.log('PASS: one natural blink source, eyelid bounds and closure, frame-rate-independent smile, emotion changes preserve blink, invalid timing and reset.');
