# Strike timing: what "punch" can mean on this body

The question: can `punch` be a **strike** (reach and return) instead of a held
pose? The contract already supports it — `duration_ms > 0` with `preempt=false`
makes a command ballistic and non-preemptible in `CommandBus`, and expiry falls
back to `hold`. What was missing was any measurement of what that looks like.

## Method

One duration per **fresh process**, no GPU (`body_view --no-render`), control
loop at 50 Hz, state sampled every 20 ms:

1. load the model, send `hold`, settle 3 s
2. record the wrist's forward (x) position as the baseline
3. send one `BodyCommand` with `authority=AUTH_R_HAND`, `ttl_ms = duration_ms`,
   `duration_ms` as shown, target = `punch_target("right")`
4. sample the peak over the window, then wait 2 s and record where it settles

Each row is a separate process, so no row inherits the previous row's baseline.
An earlier sweep in a single process produced 3–7 cm peaks that were an artefact
of drift between rows, and is the reason this method exists.

## Results

| duration | peak forward | at window end | after 2 s settle |
| ---: | ---: | ---: | ---: |
| 200 ms | **4.8 cm** | 4.8 cm | −3.8 cm |
| 400 ms | **14.3 cm** | 14.1 cm | −4.3 cm |
| 800 ms | **14.4 cm** | 14.4 cm | −0.3 cm |
| 1600 ms | **14.6 cm** | 14.6 cm | −2.6 cm |
| sustained (2 s TTL, `duration_ms=0`) | **14.6 cm** | 14.6 cm | −0.4 cm |

## What this says

- **Full reach arrives by 400 ms.** 400 / 800 / 1600 / sustained all land within
  0.3 cm of each other at ~14.5 cm forward. A strike does not need a long window.
- **200 ms is a partial jab**: 4.8 cm, about a third of the reach. Below 400 ms
  the duration sets the depth, which is a usable knob for a short vs full strike.
- **~14.5 cm is the reach limit again**, consistent with the ~0.15 m envelope
  measured on the classical probe. The number did not change; only the timing did.
- **The settle figure is noise, not drift.** It ranges −0.3 to −4.3 cm with no
  order in the duration, so a single strike does not reliably return to its
  starting pose and does not accumulate either. Distinguishing those two needs a
  repeated-strike test, which has not been run.

## Consequence for the verb layer

`punch` should send `duration_ms=400, ttl_ms=400` for a full strike, and the same
with a longer duration for a jab. Today's button path sends `duration_ms=0,
ttl_ms=2000`, which is a **held reach**, not a strike: the arm goes out and stays
out for two seconds. That is the difference the design needs to fix.

MEASURED 2026-10-29, headless CPU, `spike/wbc/body_view.py --no-render`.

---

# Addendum 2026-10-30: the forward envelope, measured on the correct axis

**Retraction first.** Every "reach saturates around 0.15 m" figure in this file and in
`research/c3m-results.md` was taken through `forward_m`, which was not forward. `hand_target`
added `lateral_m` to +X and `forward_m` to +Y, so those sweeps moved the wrist **across the
body** — the axis the policy is worst on (8.63 cm / 28.33 deg at 30 deg off-axis). The old
number described cross-body travel, not forward reach. It is retired.

**Method.** Fresh `SonicExecutor`, offline, no GPU, no game, no live session. `hold` for 120
ticks to settle, then 200 ticks on one `AUTH_R_HAND` command, then 100 ticks of `hold` before
the next point. Head point in the root frame is (+0.038, +0.009, +0.401); the clip's right
wrist is (+0.084, -0.180, -0.252). Gaze angle is the wrist's elevation below the head's
horizontal, in degrees.

## Forward tracking works

| `forward_m` | achieved x | achieved z | residual | gaze |
|---|---|---|---|---|
| 0.10 | 0.220 | -0.221 | 0.140 | 74 |
| 0.15 | 0.222 | -0.229 | 0.125 | 74 |
| 0.20 | 0.256 | -0.219 | 0.139 | 71 |
| 0.25 | 0.304 | -0.204 | 0.153 | 66 |
| 0.30 | 0.361 | -0.184 | 0.175 | 61 |
| 0.35 | 0.411 | -0.152 | 0.204 | 56 |

(`up_m = -0.10`.) Achieved x rises monotonically with the command, which is the direct proof the
axis fix landed: before it, x did not move at all. Forward reach reaches **0.41 m achieved** at
`forward_m = 0.35` with no fall — well past the retired 0.15 m figure.

## The hand is low, not out of view

`aim_target` uses `up_m = 0.12`, i.e. wrist z = -0.132. That is **0.53 m below the head**, which
puts the wrist about **69 deg below the horizontal** — outside any plausible vertical FOV. That is
why the operator reports "the fov is too small to see the hand": the pose, not the projection.

Raising it brings the hand into view without head pitch:

| `forward_m` | `up_m` | achieved x | achieved z | residual | gaze |
|---|---|---|---|---|---|
| 0.35 | +0.30 | 0.512 | 0.101 | **0.096** | 32 |
| 0.35 | +0.40 | 0.543 | 0.224 | 0.136 | 19 |
| 0.35 | +0.45 | 0.538 | 0.280 | 0.134 | 14 |
| **0.35** | **+0.50** | **0.540** | **0.305** | **0.121** | **11** |
| 0.35 | +0.55 | 0.530 | 0.375 | 0.127 | 3 |
| 0.25 | +0.45 | 0.526 | 0.335 | 0.240 | 8 |
| 0.15 | +0.45 | 0.359 | 0.323 | 0.225 | 14 |

**`forward_m=0.35, up_m=0.50` is the first pose measured to be both in view (11 deg below the eye
line) and reachable (residual 0.121, no fall).** No grid point fell.

## `SATURATED` at this tolerance means almost nothing

`DEFAULT_TOLERANCE_M` is **0.02 m**, and the clip-anchored bias for any offset target is 0.05-0.24
m, so `FLAG_SATURATED` (bit 0) is set on **every** point above — including the best one. An earlier
note read "aim 0.22 m → flags=1, target exceeds ~0.20 m reach" as evidence of unreachable
distance. It was evidence of a 0.02 m tolerance, not of a reach limit. Read the residual, not the
flag, and never quote the flag alone as a capability claim.

## Not done here

- `aim_target`'s `up_m=0.12` default is unchanged by this measurement on purpose: `aim` is a
  measured verb and moving it is a behaviour change. The `reach` verb already accepts
  `--up-m 0.50`, so the pose is available today without retuning anything.
- Lateral reach and the left hand were not swept.
