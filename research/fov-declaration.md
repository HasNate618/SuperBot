# Can we declare the field of view instead of measuring it?

`superbot/projection.py` refuses to invent a FOV, and `wire.fill` wrote none
(`per_view_data_valid = False`), so the compositor's field of view has been unknown.
But we are the ones *emitting* the HMD data, so the FOV could be a stated number
rather than something inferred from images. The question is whether this build's
Monado honours what we write.

## CITED — the fields exist and are documented as meaningful

Upstream `src/xrt/drivers/remote/r_interface.h`, read from
<https://monado.pages.freedesktop.org/monado/r__interface_8h_source.html>
(accessed 2026-10-30):

```c
#define R_HEADER_VALUE (*(uint64_t *)"mndrmt3\0")

struct r_head_data
{
    struct
    {
        struct xrt_fov fov;     //!< The field of view values of this view.
        struct xrt_pose pose;   //!< The pose of this view relative to r_head_data::center.
        uint32_t _pad;          //!< Padded to fov(16) + pose(16 + 12) + 4 = 48
    } views[2];

    struct xrt_pose center;     //!< The centre of the head, in OpenXR terms the view space.
    bool per_view_data_valid;   //!< Is the per view data valid and should be used?
    bool _pad0, _pad1, _pad2;   //!< pose(16 + 12) bool(1) + pad(3) = 32.
};
```

This also independently corroborates `superbot/wire.py`: the same header, the same
`views[2]` layout, the same controller struct (including `trigger_value`,
`squeeze_value`, `hand_curl[5]` and the 16-byte click/touch block). The wire format
is not our invention.

## ASSUMPTION — that the driver acts on it

The *consuming* code path was not readable: no Monado source is in the Nix store, the
installed binary is behind a wrapper
(`/nix/store/rzjn31yz67rbhqlb8vl3rz8z34bxm8mw-monado-psvr2-psvr2-work-steamvr-2026-06-04`),
and raw GitHub source returned 502 through the fetch path. A doxygen field description
saying "should be used" is documentation, not a measurement.

So this is a verified **contract** with unverified **behaviour**.

## The measurement that settles it

Two runs with very different fields of view, body held still, one capture each:

- 60 deg horizontal vs 110 deg horizontal.
- If Monado honours the declaration: same scene, visibly more of it in the 110 deg
  frame, i.e. a wide-angle view. Record MEASURED.
- If the frames are essentially identical: the flag is ignored on this build. Record
  the negative result, leave the default `per_view_data_valid = False` in the shipped
  path, and take the FOV from the hand-in-image fit instead (plan Task 5).

## What was implemented, pending that measurement

`wire.fill(..., fov=(half_h, half_v))` in radians: validates finite and positive,
writes all four half-angles symmetrically on both views, and sets
`per_view_data_valid = True`. Default `None` changes nothing, so every earlier
measurement still holds. `Frame`-pose fields are left zeroed - the view poses are a
separate assumption this task does not make.

Five tests in `tests/test_wire.py:DeclaredFov` pin our side, including that declaring
a FOV does not move the head pose (otherwise a changed image could not be attributed
to the field of view).

## MEASURED 2026-10-31 — the declaration is honoured, and changing it live is not safe

First run where the declaration actually reached the driver. Three earlier "looks the
same" reports were not measurements: `config.toml` had no `fov` key, so nothing was
declared, and one of them was `mode=hold`, which runs the *scripted* `c0` client and
never passes FOV flags at all. The relay's own log is the admission ticket:

```
pose_relay: declaring fov 110.0 x 78.0 deg full (55.0/39.0 half)
```

Operator observation, immediately after that line appeared: **the view briefly
widened.** So `r_head_data::per_view_data_valid` + the per-view `fov` block are acted
on by this build - the ASSUMPTION above is now MEASURED, at least qualitatively
(eyeball, one frame, no capture; see the gap at the end).

And then Superhot died, taking Steam with it. The dashboard afterwards:

```
mode: body | monado True | weston True | steam False | game False
```

Monado and Weston survived; the OpenXR client did not. Monado's last words are a
client tearing down its space:

```
ERROR [ipc_server_objects_destroy_xspc] Client tried to destroy non-existent space!
ERROR [ipc_handle_space_destroy] ipc_server_objects_destroy_xspc failed:
      XRT_ERROR_IPC_FAILURE
 INFO [client_disconnected] Client 4 disconnected
```

### What this does and does not establish

MEASURED: the driver honours a declared field of view.

ASSUMPTION, the leading explanation: the change was applied to a *live* session. The
game and SteamVR had already created swapchains against the previous view
configuration, then the relay restarted mid-session and Monado began reporting a
different one. OpenXR clients are expected to react to
`XR_TYPE_EVENT_DATA_VIEW_CONFIGURATION_CHANGED` by recreating swapchains; SteamVR on
Proton evidently does not do that gracefully. The predicted consequence is exactly
what happened: a moment of correct wide rendering, then the client dies.

NOT established: that 110 deg is itself too wide. Nothing here tests a session
started with the declaration in place, which is the next run. If a from-start session
is stable at 110, the live-change hypothesis stands; if it also dies, the value or the
aspect (110x78 = 1.41, not 16:9) is implicated.

### Separate defect found in the same log

`monado.log` reached 33.5 MB in 45 minutes, from ~200,000 repetitions of

```
ERROR [u_device_ni_set_output] Function set_output is not implemented for 'Remote <side> Controller'
```

That is ~74/s, i.e. per controller per frame: the packet drives something that makes
Monado attempt a haptic output call every frame. Body mode only. Harmless to the
render, but it is 44 MB/hour of disk and CPU per session, and it buries real errors.
Not yet diagnosed; not the crash.
