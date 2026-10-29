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
