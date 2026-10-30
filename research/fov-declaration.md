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
