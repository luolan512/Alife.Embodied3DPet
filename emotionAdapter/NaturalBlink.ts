export interface BlinkWeights {
  blinkLeft: number;
  blinkRight: number;
}

/** One timed eyelid pulse. Independent of emotion, facial models and body motion. */
export class NaturalBlink {
  private countdown: number;
  private age: number | null = null;
  private weights: BlinkWeights = {blinkLeft: 0, blinkRight: 0};

  constructor(private random: () => number = Math.random) {
    this.countdown = this.interval();
  }

  update(seconds: number): BlinkWeights {
    if (!Number.isFinite(seconds) || seconds <= 0) return {...this.weights};
    const dt = Math.min(seconds, .25);
    if (this.age === null) {
      this.countdown -= dt;
      if (this.countdown > 0) return {...this.weights};
      this.age = -this.countdown;
    } else {
      this.age += dt;
    }

    // Close quickly, briefly hold, then reopen more slowly (260 ms total).
    const closing = .07, held = .03, opening = .16;
    const curve = (progress: number) => .5 - .5 * Math.cos(Math.PI * Math.max(0, Math.min(1, progress)));
    let weight: number;
    if (this.age < closing) weight = curve(this.age / closing);
    else if (this.age < closing + held) weight = 1;
    else if (this.age < closing + held + opening) weight = 1 - curve((this.age - closing - held) / opening);
    else {
      this.countdown = Math.max(.001, this.interval() - (this.age - closing - held - opening));
      this.age = null;
      weight = 0;
    }
    this.weights = {blinkLeft: weight, blinkRight: weight};
    return {...this.weights};
  }

  reset(): void {
    this.age = null;
    this.weights = {blinkLeft: 0, blinkRight: 0};
    this.countdown = this.interval();
  }

  private interval(): number {
    const sample = this.random();
    return 3 + 4 * (Number.isFinite(sample) ? Math.max(0, Math.min(1, sample)) : .5);
  }
}
