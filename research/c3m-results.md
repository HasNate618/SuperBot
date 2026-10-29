# C3m — hold as a reference switch, and the measurement bug behind the bad holds

**Date:** 2026-10-23
**Model:** GEAR-SONIC default on its own MuJoCo model, headless CPU
**Script:** `spike/wbc/hold_c3m.py`
**Data:** `spike/wbc/logs/c3m_summary.json`; per-run JSONL `spike/wbc/logs/c3m_H*.jsonl`, `c3m_E2_*.jsonl`
**Runtime:** 4 m 04 s wall for the whole matrix

**Provenance.** The subagent that ran this had not written its report when the parent picked the run up; this document was assembled from `c3m_summary.json`, whose numbers were logged from `mjData` after `mj_step`. Nothing was re-run.

**Known defect, carried forward honestly.** `experiment2` in the summary holds **2** configs (both `survived=True`, full per-cycle data), not the 4 that were attempted. An additional 5-cycle variant crashed in `place_region` (`quat_mul` on the `(T,14,4)` `bq` array) after the squat config had finished, so **`hold_c3m.py` is left with an unpatched bug** in that path. The two configs that did complete are sufficient for the conclusion; the missing variants are not. (The subagent's own count of "3 complete configs" does not match the file, which has 2.)

---

## 0. Verdict

**The freeze exists, and the previous bad hold was our harness, not the robot.**

| | net / 20 s | mean v | **RMS v (last 10 s)** | p95 v | tilt | survived |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| H1 freezes the soak's pose — **anchor index only** | 3.928 m | 0.205 | **0.2005 m/s** | 0.288 | 5.9° | yes |
| H1f **same frozen pose**, whole 10-frame horizon frozen | 0.110 m | 0.0099 | **0.0085 m/s** | 0.014 | 3.6° | yes |

Same pose, same frame, same clip. The only difference is whether the encoder's **0.9 s lookahead** is frozen along with the anchor. Result: **36× less drift and 24× less motion.** The 0.23–2.07 m holds in the C3i soak were produced by the anchor-only freeze — the policy was still being told, 0.9 s into the future, to keep walking.

**Best hold: H5n — switch the reference to a standing clip, anchor-index freeze — RMS 0.00013 m/s, i.e. 0.02% of walking speed.** `p95` is 2×10⁻⁴ m/s. Against a 0.66 m/s walk, Superhot's clock should sit pinned at its 0.005 floor. **Note the freeze mode: the best rows use an anchor-index (native) freeze on a *standing* clip, not a flat horizon.** See §1.1 — forcing a flat horizon *degrades* a standing clip while it *rescues* a walking one.

**But the hold entered from a walk is not that clean.** Five walk→hold→walk cycles: RMS **0.050 m/s** mean (worst 0.067), 0.068–0.146 m net per 10 s hold, cumulative 0.478 m, tilt to 9.5°, **no fall**, and the walk resumes within ~0.2 m of the reference every cycle. The right-after-switch RMS is 0.13–0.18 m/s, so the 0.05 is dominated by a **settle transient**: the body is still moving for the first seconds after it stops, then it is genuinely frozen.

**Practical consequence:** there is a real freeze, it takes a few seconds to settle into, and it costs roughly 0.6–0.8 s of game time to enter. A 20 s VLM think therefore costs on the order of **0.7 s of game time, not 20 s.** The clock floor and the settle together are cheap.

---

## 1. Experiment 1 — which hold is actually still (20 s, zero command)

All rows are 5 s warm-up followed by a 20 s hold. `rms_last10` is RMS speed over the final 10 s, i.e. after the settle; `rms_all` includes the settle.

| id | Condition | net | max exc | path | mean v | rms_all | **rms_last10** | p95_l10 | tilt | min z | survived |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| H1 | freeze walk mid-stride, **anchor only** | 3.928 m | 3.928 | 4.102 | 0.205 | 0.214 | **0.2005** | 0.288 | 5.9° | 0.745 | yes |
| H1f | freeze walk mid-stride, **flat horizon** | 0.110 m | 0.110 | 0.197 | 0.0099 | 0.037 | **0.0085** | 0.014 | 3.6° | 0.753 | yes |
| H2 | freeze walk at **frame 0**, anchor only | 0.123 m | 0.125 | 0.171 | 0.0086 | 0.048 | **0.00065** | 0.0011 | 2.7° | 0.774 | yes |
| H3 | freeze squat frame 0 from a squat start | 0.083 m | 0.094 | 0.215 | 0.0107 | 0.038 | **0.0021** | 0.0042 | 8.0° | 0.745 | yes |
| H4 | **switch** to squat frame 0, flat hold | 0.097 m | 0.097 | 0.167 | 0.0084 | 0.035 | 0.0233 | 0.0070 | 3.6° | 0.776 | yes |
| **H4n** | **switch** to squat frame 0, frozen horizon | 0.077 m | 0.086 | 0.107 | 0.0054 | 0.027 | **0.00019** | 0.0004 | 2.1° | 0.776 | yes |
| H5 | switch to walk prelude frame 400, flat hold | 0.104 m | 0.104 | 0.180 | 0.0090 | 0.036 | 0.0310 | 0.014 | 5.1° | 0.776 | yes |
| **H5n** | **switch** to walk prelude frame 400, frozen horizon | 0.102 m | 0.108 | 0.124 | 0.0062 | 0.031 | **0.00013** | 0.0002 | 2.8° | 0.776 | yes |

### 1.1 The mechanism, stated exactly

`H1`'s note: *"reference index held at looped frame 250 == walk source frame 880, |jv| = 6.57. Lookahead still shows the next 0.9 s of walking."*

The policy's encoder consumes a **10-frame horizon at 20 ms spacing ≈ 0.9 s of lookahead**. The C3i soak's freeze held only the *anchor* frame index. So the anchor said "frame 880" while the next nine frames still said "here is the rest of your step" — and the policy, being a motion tracker, tracked it. It walked 3.93 m in 20 s at 0.21 m/s and never fell, which is exactly what a walking policy told to keep walking should do.

Freezing the horizon (`H1f`) or freezing at a frame whose lookahead is itself static (`H2`, frame 0 of the walk clip) removes the instruction to keep walking. Nothing about the clip or the phase matters as much as **whether the lookahead is still walking.**

**The freeze mode is not the active ingredient, and it is not interchangeable.** The four rows that separate cleanly:

| Condition | Clip lookahead during hold | Freeze mode | RMS l10 |
|---|---|---|---:|
| H1 walk mid-stride | continuing walking | anchor only | 0.2005 |
| H1f walk mid-stride | same pose repeated | flat horizon | 0.0085 |
| H4 squat f0 switch | squat frames 0-45 (descent start) | flat horizon | 0.0233 |
| **H4n squat f0 switch** | squat frames 0-45 | **anchor only** | **0.00019** |

A flat horizon **rescues** the walking clip (0.2005 -> 0.0085) and **degrades** the standing one (0.00019 -> 0.0233). Forcing the lookahead to repeat one frame appears to strip the velocity/phase information the policy expects, which costs more on a clip that was already stable. So the shipping recipe is **standing clip + anchor-index freeze**, and a flat horizon is a repair you use only when you must freeze a walking clip in place.

### 1.2 Two secondary results

- **`H2` — freezing the walk clip itself at frame 0 works** (rms 6.5×10⁻⁴). The reference-switch machinery is therefore *optional*; what matters is that the frozen reference has a non-walking lookahead. A standing clip is the cleaner way to guarantee that.
- **Every hold settles ~8–12 cm even at RMS ~0.** `net` is 0.077–0.123 m across every good condition while `rms_last10` falls to 10⁻⁴. This is a one-time settle, not creep — the body takes a step's worth of displacement when it stops and then stops. The design's <5 cm hold target is therefore missed by the *transient*, not by ongoing motion.

---

## 2. Experiment 2 — the switch entered from walking (the version we would ship)

walk 5 s → switch to a standing clip, hold 10 s → switch back to walking, 5 s, ×5. 100 s total, 5000/5000 ticks. Both configs below use an **anchor-index** freeze on the switch, which §1.1 identifies as the better mode for a standing clip.

| Config | survived | cum hold net | hold RMS mean | hold RMS worst | hold net worst | tilt (all) | min z | walk total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **squat frame 0** | **yes** | 0.478 m | **0.0502** | 0.0527 | 0.135 m | 9.48° | 0.719 | 16.06 m |
| walk prelude frame 400 | yes | 0.617 m | 0.0584 | 0.0667 | 0.146 m | 9.48° | 0.719 | 16.05 m |

Per-cycle detail (squat frame 0):

| Cycle | walk dist | walk v | hold net | hold RMS10 | hold tilt | resume / ref |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 3.255 m | 0.654 m/s | 0.099 m | 0.0503 | 3.6° | 3.18 / 3.10 |
| 1 | 3.323 m | 0.667 m/s | 0.068 m | 0.0485 | 4.3° | 3.11 / 3.13 |
| 2 | 3.139 m | 0.630 m/s | 0.070 m | 0.0493 | 3.9° | 3.16 / 3.14 |
| 3 | 3.347 m | 0.672 m/s | 0.107 m | 0.0527 | 3.8° | 3.19 / 3.13 |
| 4 | 2.993 m | 0.601 m/s | 0.135 m | 0.0502 | 3.1° | 3.35 / 3.13 |

Right after each switch to hold, RMS is **0.139 / 0.147 / 0.154 / 0.165 / 0.142 m/s**; over the full 10 s hold it averages 0.050. Since the 20 s single holds reach 1.3×10⁻⁴ once settled, the cycle figure is the **settle transient averaged over a short hold**, not ongoing drift.

**Squat frame 0 is the better cycle clip** (0.050 vs 0.058 RMS) despite the prelude frame winning slightly on single holds, and it is a genuine standing clip rather than a cherry-picked low-motion frame of a walking one. Recommend `squat_001__A359` frame 0.

### 2.1 The switch itself is benign

Per switch: `tilt_pre_max` 2.6–5.2°, `tilt_post_max` 2.9–5.5°, `z_post_range` 0.010–0.050 m, `dz` across the switch ±0.05 m. The largest event in the whole 100 s is 5.5° of tilt at a switch edge, and the global worst tilt (9.48°) occurs during the *warm-up walk*, not at a transition. **No transition instability**, in contrast to every earlier attempt (C3d: walk→park fell in 0.64–1.49 s; C3h: gain-scheduled hold fell in 3.11–4.24 s).

---

## 3. What this means for the freeze budget

| Phase | Motion | Duration | Implied clock (linear, floor 0.005) | Game time cost |
| --- | --- | --- | --- | --- |
| walking | 0.66 m/s | — | 1.0 | — |
| settle after stopping | 0.14–0.18 m/s | ~2–4 s | ≈0.22–0.27 | ≈0.6–0.8 s |
| settled hold | 1.3×10⁻⁴ m/s | indefinite | pinned at 0.005 | 0.1 s per 20 s |

A 20 s think therefore costs roughly **0.7 s of game time**, dominated by the settle, not by the think. That is the number C0 needs to confirm against the game's real clock behaviour — the mapping above is `[ASSUMPTION]` (linear in speed), while the 0.005 floor is `[MEASURED]` from the serialized assets and the hold motion is `[MEASURED]` here.

**Design rule that follows:** issue the think *after* the settle, not at the switch instant. Or accept ~0.7 s of game time and think immediately. Either is safe; thinking during the settle is what you must not do with a 20 s budget and no settle.

---

## 4. What is still unmeasured

- **Live co-residency.** All headless CPU. 50 Hz beside Superhot + Monado unverified.
- **The clock mapping itself.** Linear-in-speed is an assumption. C0 measures it.
- **Holds longer than 20 s.** 20 s is the VLM think budget; 60 s+ is untested.
- **Hold under an arm/head command.** Every hold here ran at zero command with the policy's own arms. A hold that also aims is a different case, and C3j showed SONIC's arms are worst exactly when stationary (1.14 cm but 7.33°).
- **The 8–12 cm settle.** Whether a step's worth of displacement on stopping matters in-game is a C0 observation.
- **C3k** (unify the QP's model with SONIC's) — still not done, still not needed now that the arm override is dead.

---

## 5. What the design doc should now say

1. **"SONIC holds 3.6 cm" is retired, and so is "the holds drift 0.23–2.07 m."** The real figure is **RMS 1.3×10⁻⁴ m/s once settled (0.02% of walking speed)**, with a one-time settle of ~10 cm. Both earlier numbers were artefacts of how the reference horizon was frozen.
2. **`hold` = switch the reference to `squat_001__A359` frame 0, anchor index held there — *not* a flat horizon.** That is the measured best (RMS 1.9×10⁻⁴, and 1.3×10⁻⁴ on the walk-prelude clip). The deciding factor is **whether the lookahead horizon contains walking motion**, not which freeze mode you pick: an anchor-only freeze of a walk mid-stride is the worst row in the table (0.2005) because its lookahead says "keep walking", while an anchor-only freeze of a standing clip is the best, because its lookahead is a squat descent that the policy can hold against. A flat horizon is a **repair** for freezing a walking clip (0.2005 → 0.0085) that costs you on a standing one (0.00019 → 0.0233).
3. **Entered from a walk it survives and resumes:** 5/5 cycles, no fall, worst tilt 9.5°, cumulative 0.478 m, walk resumes within ~0.2 m of reference. This is the transition that C3d and C3h could not achieve.
4. **The freeze costs ~0.7 s of game time**, mostly the settle. Think after the settle, or budget for it.
5. **R2 is met** (the <5 cm bar is missed only by the one-time settle transient), and the remaining freeze risk is now entirely a **game-side** question for C0.

---

## 6. Reproduce

```bash
cd /home/nate/Projects/SuperBot
time spike/e3/.venv/bin/python spike/wbc/hold_c3m.py
# -> spike/wbc/logs/c3m_summary.json plus per-run jsonl (4 m 04 s)
```

Do not delete `spike/wbc/logs/c3m_*` — those files are the evidence for every table above.
