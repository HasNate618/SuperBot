# What the body view costs on the GPU

Measured 2026-10-29, RTX 4060 Laptop (8 GB), body view at 640×480, SONIC policy
pinned to one ONNX thread each (see `a6aec89`).

## Why this was measured

The body view was described as running on the Mesa software renderer at
~9.4 ms/frame for a second angle. That number drove a plan to "switch to
hardware EGL" once the GPU came back. The measurement below says there is
nothing to switch.

## Method

`spike/wbc/body_view.py` writes `body_state.json` once per render tick, so ticks
were counted from its mtime changes rather than inferred. Each run: 8 s wall
clock, `--fps 120` so pacing was not the limit, CPU-only sampling from `/proc`
(no `nvidia-smi` polling inside the timed window — see the retraction below).

Backend identity came from the running process's own memory map, which is not
an inference:

| library                | mappings                 |
| ---------------------- | ------------------------ |
| `libnvidia-*`          | 50                       |
| `libglvnd` `libEGL`    | 5                        |
| `libEGL_mesa`          | 5 (vendor enumeration only) |
| `swrast` / `llvmpipe`  | **0**                    |

## Results

| configuration                    | ticks / 8 s | achieved Hz |
| -------------------------------- | ----------- | ----------- |
| `--no-render` (policy only)      | 367         | 45.8        |
| one view (`three_quarter`)       | 365         | 45.6        |
| two views (`three_quarter,side`) | 357         | 44.5        |

Derived per-tick cost: policy alone **21.8 ms**, and the second view adds
**~0.6 ms**. Rendering is no longer anywhere near the critical path.

## What this changes

- **No code change.** The body view already renders on the 4060. The earlier
  "software renderer" state was the pre-GPU machine, not a configuration.
- **The ceiling is the policy, not the renderer**: 45.8 Hz policy-only, 44.5 Hz
  with two views, against a 50 Hz design target. The body is ~8% under target on
  this box with two angles enabled.
- **Memory matters for co-residency**: the viewer's RSS is **948 MB** while it
  holds the SONIC weights. On an 8 GB card that is a real slice of the budget
  R6 has to fit inside.

## Retraction

A first pass measured 13.4 Hz and looked like a renderer problem. It was caused
by the sampler: `nvidia-smi` and `ps` were spawned every 50 ms inside the timed
window, which starved the process being measured. The sampler is the
instrument; a slow instrument reads as a slow subject. Numbers above come from a
sampler that only reads `/proc` and stat data.

## Limits

- One machine, one resolution (640×480). The per-view cost scales with pixels;
  a larger tile would not stay free.
- Ticks counted from file mtime are a cadence measure, not a breakdown of
  encoder/decoder/physics. The 21.8 ms policy figure is a total, not a split.
- Not yet tested while the game is running — that is R6.
