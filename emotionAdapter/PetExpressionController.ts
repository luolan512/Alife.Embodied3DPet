import {NaturalBlink, type BlinkWeights} from './NaturalBlink';

export interface PetExpressionWeights extends BlinkWeights {
  smile: number;
}

/** Small renderer helper: natural blink plus the smile used by the VRM fallback. */
export class PetExpressionController {
  private blink: NaturalBlink;
  private targetSmile = 0;
  private smile = 0;

  constructor(random: () => number = Math.random) {
    this.blink = new NaturalBlink(random);
  }

  set(emotion: string, intensity: number): void {
    const strength = Number.isFinite(intensity) ? Math.max(0, Math.min(1, intensity)) : 0;
    this.targetSmile = emotion === 'happy' ? strength : 0;
  }

  update(seconds: number): PetExpressionWeights {
    const dt = Number.isFinite(seconds) ? Math.max(0, Math.min(.25, seconds)) : 0;
    this.smile += (this.targetSmile - this.smile) * (1 - Math.exp(-dt * 8));
    if (this.targetSmile === 0 && this.smile < .0001) this.smile = 0;
    return {smile: this.smile, ...this.blink.update(dt)};
  }

  reset(): void {
    this.targetSmile = 0;
    this.smile = 0;
    this.blink.reset();
  }
}
