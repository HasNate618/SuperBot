#!/usr/bin/env python3
"""Decode grim PNG frames, track the blue cube, and correlate pose steps."""

import csv
import struct
import sys
import zlib
from pathlib import Path


def read_png(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    idat = bytearray()
    w = h = bitdepth = colortype = interlace = None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bitdepth, colortype, comp, filt, interlace = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"IDAT":
            idat.extend(chunk)
        elif typ == b"IEND":
            break
    assert bitdepth == 8 and interlace == 0, (bitdepth, interlace)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colortype]
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, channels, bytes(out)


def blue_stats(w, h, ch, pix):
    count = 0
    sx = sy = 0
    minx = w
    maxx = -1
    for y in range(h):
        base = y * w * ch
        for x in range(w):
            i = base + x * ch
            r, g, b = pix[i], pix[i + 1], pix[i + 2]
            if b > 140 and r < 90 and g < 90:
                count += 1
                sx += x
                sy += y
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
    if count:
        return count, sx / count, sy / count, minx, maxx
    return 0, float("nan"), float("nan"), -1, -1


def frame_diff(w, h, ch, a, b, step=3):
    n = 0
    s = 0
    for y in range(0, h, step):
        base = y * w * ch
        for x in range(0, w, step):
            i = base + x * ch
            s += abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2])
            n += 3
    return s / n


def load_pose(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append((float(r["wall"]), float(r["yaw_deg"])))
    return rows


def main():
    frame_csv = sys.argv[1] if len(sys.argv) > 1 else "frames.csv"
    pose_csv = sys.argv[2] if len(sys.argv) > 2 else "/tmp/e2_pose_meas.csv"
    out_csv = sys.argv[3] if len(sys.argv) > 3 else "frame_metrics.csv"

    frames = []
    with open(frame_csv) as f:
        for r in csv.DictReader(f):
            frames.append((int(r["idx"]), float(r["t_start"]), float(r["t_end"])))

    prev = None
    metrics = []
    for idx, ts, te in frames:
        w, h, ch, pix = read_png(f"frames/f{idx:04d}.png")
        count, cx, cy, minx, maxx = blue_stats(w, h, ch, pix)
        diff = 0.0 if prev is None else frame_diff(w, h, ch, prev, pix)
        prev = pix
        metrics.append((idx, ts, te, count, cx, cy, minx, maxx, diff))
        print(f"f{idx:04d} t={te:.3f} blue={count:5d} cx={cx:7.2f} cy={cy:7.2f} "
              f"x=[{minx:3d},{maxx:3d}] diff={diff:6.2f}", flush=True)

    with open(out_csv, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["idx", "t_start", "t_end", "blue_count", "cx", "cy", "minx", "maxx", "diff"])
        wr.writerows(metrics)

    poses = load_pose(pose_csv)
    steps = []
    last = None
    for wall, yaw in poses:
        if last is not None and (yaw > 0) != (last > 0):
            steps.append((wall, yaw))
        last = yaw
    print("\npose steps:")
    for wall, yaw in steps:
        print(f"  wall={wall:.6f} yaw={yaw:+.2f}")

    loop0, loop1 = frames[0][1], frames[-1][2]
    print(f"\nloop {loop0:.6f}..{loop1:.6f} ({len(frames)} frames, {len(frames)/(loop1-loop0):.2f} fps)")

    print("\nstep -> first changed frame:")
    for wall, yaw in steps:
        if wall < loop0 or wall > loop1:
            continue
        pre = [m for m in metrics if m[2] <= wall]
        if not pre:
            continue
        base = pre[-1]
        found = None
        for m in metrics:
            if m[2] <= wall:
                continue
            dcx = abs(m[4] - base[4]) if m[3] and base[3] else 999
            dcount = abs(m[3] - base[3])
            if dcx > 30 or dcount > 500 or m[8] > 4.0:
                found = m
                break
        if found is None:
            print(f"  step {wall:.6f} yaw={yaw:+.1f}: NO CHANGE DETECTED")
            continue
        lo = max(0.0, found[1] - wall)
        hi = found[2] - wall
        print(f"  step {wall:.6f} yaw={yaw:+.1f}: frame f{found[0]:04d} "
              f"latency {lo*1000:.1f}..{hi*1000:.1f} ms (mid {(lo+hi)/2*1000:.1f}) "
              f"blue {base[3]}->{found[3]} cx {base[4]:.1f}->{found[4]:.1f} diff {found[8]:.2f}")


if __name__ == "__main__":
    main()
