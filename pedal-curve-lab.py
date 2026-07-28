#!/usr/bin/env python3
"""
Pedal Curve Lab for Simagic - precise editing of Simagic pedal response curves.

An independent tool: not affiliated with, endorsed by, or supported by Simagic.

SimPro Manager stores presets in a SQLite DB as protobuf blobs. Its own curve
editor only lets you drag control points, so you get whatever value the drag
lands on; the underlying storage is float64. This tool edits those values
directly.

    python pedal-curve-lab.py            # launch GUI
    python pedal-curve-lab.py --live     # launch straight to the Live tab
    python pedal-curve-lab.py --selftest # verify encode/decode round-trip
    python pedal-curve-lab.py --hidtest  # dump decoded live HID reports

Only the selected axis block is re-encoded; every other byte of the preset is
copied verbatim. The DB is backed up before any write.

Everything the program writes - settings, the axis map, DB backups - goes in
its own folder next to the script or the executable, so a packaged copy is
portable and can be cleared out by deleting what it made.

The Live tab reads the pedals' HID input report directly, which carries both the
post-curve output the game sees and the pre-curve input, so the real transfer
function can be measured rather than assumed.

Built against a P1000 but not tied to it: the pedal device is auto-picked from
all present HID devices (overridable from the Live tab), the report layout is
read from each device's own descriptor, and the pre-curve input bytes are
located by the movement-based Identify step rather than assumed. Anything
genuinely P1000-specific is confined to the LEGACY_* constants.
"""

import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import datetime

# Dialog titles use the short form; only the window title carries the "for
# Simagic" descriptor, so dropping that later is a one-line change.
APP_NAME = "Pedal Curve Lab"
APP_TITLE = "%s for Simagic" % APP_NAME

# Read by the Makefile to name the build and tag the release, so the version
# is stated once and the two can never disagree.
APP_VERSION = "0.1.0"


def app_dir():
    """The folder this program keeps its own files in.

    A packaged build is a folder the user unzipped and can delete wholesale,
    so its state belongs beside the executable - `__file__` there points into
    the unpacked bundle, which is temporary and not somewhere anyone would
    think to look. Running from source it is simply the script's own folder.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_db_path():
    """Newest user.db under any Simpro* install; the Simpro3 path if none.

    Pinned to a version folder this would go stale the day SimPro 4 ships;
    globbing mirrors how find_simpro_exe() already locates the executable.
    """
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Simagic")
    hits = glob.glob(os.path.join(root, "Simpro*", "storage", "user.db"))
    if hits:
        return max(hits, key=os.path.getmtime)
    return os.path.join(root, "Simpro3", "storage", "user.db")


DB_PATH = _find_db_path()

AXES_CACHE = os.path.join(app_dir(), "pedal-curve-lab.axes.json")

# UI state that should survive a restart (the Y markers, and the pivot each
# curve was last written from). Kept apart from the axis map, which is device
# pairing rather than preference.
SETTINGS_PATH = os.path.join(app_dir(), "pedal-curve-lab.settings.json")

# DB backups land here rather than beside user.db: everything this program
# creates then sits in the one folder, which can be cleared out or carried to
# another machine in one move, and the Simagic install is left as it was.
BACKUP_DIR = os.path.join(app_dir(), "backup")

# Axis ids as they appear in field 27.1. Names verified against the P1000 UI by
# matching each block's lo/hi range to the sliders shown for each pedal. Other
# Simagic sets may assign ids differently, so anything not listed is shown
# neutrally as "Axis N" rather than guessed at.
AXIS_NAMES = {1: "Clutch", 3: "Brake", 4: "Throttle"}

# Each axis stores four curve points. Only the first three are real control
# points (fixed at 25/50/75% travel, adjustable output); the fourth is the
# curve's end and is (100,100) in every stock and user preset.
ENDPOINT = (100.0, 100.0)

# Both charts plot the same thing, so they share one set of axis titles.
AXIS_X, AXIS_Y = "pedal travel % (after deadzone)", "output %"

# The shape of the HID input report is read from each device's own descriptor
# at connect time: report length, which Generic Desktop axes it carries (those
# are the post-curve outputs the game sees) and their logical ranges. The
# pre-curve inputs, however, live in a vendor-defined blob (usage page 0xFF00)
# that the descriptor deliberately leaves unstructured, so those are found by
# scanning the report for little-endian u16 windows that track pedal movement
# during Identify.
#
# For reference, the P1000 (the device this tool was first built against)
# reports:
#   byte  0      report id 0x01
#   bytes 1,3,5  uint16 LE output axes (Rz, Ry, Rx), 0-4095, post-curve
#   bytes 7-8    constant 0x80 0x20
#   bytes 9,11,13,15  uint16 LE, pre-curve input, 0-4095
# The legacy constants below describe that layout; they are used only to
# migrate an old axis-pairing cache and as a hint when scoring devices.
LEGACY_VIDPID = ("vid_0483", "pid_0525")
LEGACY_OUT_USAGES = (0x35, 0x34, 0x33)       # Rz, Ry, Rx at bytes 1, 3, 5
LEGACY_OUT_OFFSETS = (1, 3, 5)
LEGACY_IN_OFFSETS = (9, 11, 13, 15)

# Movement below this fraction of full scale is treated as noise during
# Identify (the legacy threshold was 300 counts of 4095).
MIN_TRAVEL_FRAC = 0.07

# A sweep stops on its own after this long. One slow press and release is the
# whole procedure and takes a few seconds; anything past this is a recording
# left running, which only dilutes the per-travel averages with samples taken
# at rest.
SWEEP_TIMEOUT = 8

GD_PAGE, VENDOR_PAGE = 0x01, 0xFF00
GD_AXIS_USAGES = tuple(range(0x30, 0x3A))    # X..Wheel, incl. sliders/dials
GD_USAGE_NAMES = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry",
                  0x35: "Rz", 0x36: "Slider", 0x37: "Dial", 0x38: "Wheel"}


# --------------------------------------------------------------------------
# protobuf wire format
# --------------------------------------------------------------------------

def read_varint(b, i):
    result = shift = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not x & 0x80:
            return result, i
        shift += 7


def write_varint(v):
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def fields(b):
    """Yield (field_no, wire_type, start, end, payload_start, payload_end).

    b[start:end] is the whole field including its key, so untouched fields can
    be copied byte-for-byte.
    """
    out = []
    i = 0
    while i < len(b):
        start = i
        key, i = read_varint(b, i)
        wt, fn = key & 7, key >> 3
        ps = i
        if wt == 0:
            _, i = read_varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            ln, i = read_varint(b, i)
            ps = i
            i += ln
        else:
            raise ValueError("unsupported wire type %d at offset %d" % (wt, start))
        if i > len(b):
            raise ValueError("truncated field at offset %d" % start)
        out.append((fn, wt, start, i, ps, i))
    return out


def enc_f64(fn, val):
    return write_varint(fn << 3 | 1) + struct.pack("<d", float(val))


def enc_msg(fn, payload):
    return write_varint(fn << 3 | 2) + write_varint(len(payload)) + payload


def first_field(b, fn, wt):
    for t in fields(b):
        if t[0] == fn and t[1] == wt:
            return t
    return None


def replace_submsg(buf, fn, new_payload):
    """Rebuild buf with the first length-delimited field `fn` swapped out."""
    out = bytearray()
    done = False
    for f, wt, s, e, ps, pe in fields(buf):
        if not done and f == fn and wt == 2:
            out += enc_msg(fn, new_payload)
            done = True
        else:
            out += buf[s:e]
    if not done:
        raise ValueError("field %d not found" % fn)
    return bytes(out)


# --------------------------------------------------------------------------
# preset model
# --------------------------------------------------------------------------
# Layout of one axis block, discovered by walking the wire format:
#   27            repeated per-axis block
#    .1  varint   axis id
#    .2 .3 .7     nested wrappers down to the "core" message
#         .5  f64   range low  (omitted when 0)
#         .6  f64   range high
#         .8  msg   control point, repeated: .1 = x%, .2 = y%
#                   (one empty .8 entry precedes the points)
#         .12 .13   raw ADC calibration bounds

CORE_PATH = (2, 3, 7)


def _core_of(block):
    buf = block
    for fn in CORE_PATH:
        t = first_field(buf, fn, 2)
        if t is None:
            return None
        buf = buf[t[4]:t[5]]
    return buf


def parse_axes(blob):
    """-> [ {index, axis_id, name, lo, hi, points, lo_present, hi_present} ]"""
    axes = []
    for idx, (fn, wt, s, e, ps, pe) in enumerate(fields(blob)):
        if fn != 27 or wt != 2:
            continue
        block = blob[ps:pe]
        t = first_field(block, 1, 0)
        axis_id = read_varint(block, t[4])[0] if t else None
        core = _core_of(block)
        if core is None:
            continue

        lo = hi = None
        points = []
        for f, w, cs, ce, cps, cpe in fields(core):
            if f == 5 and w == 1:
                lo = struct.unpack("<d", core[cps:cps + 8])[0]
            elif f == 6 and w == 1:
                hi = struct.unpack("<d", core[cps:cps + 8])[0]
            elif f == 8 and w == 2 and cpe > cps:
                sub = core[cps:cpe]
                x = first_field(sub, 1, 1)
                y = first_field(sub, 2, 1)
                if x and y:
                    points.append((
                        struct.unpack("<d", sub[x[4]:x[4] + 8])[0],
                        struct.unpack("<d", sub[y[4]:y[4] + 8])[0],
                    ))
        axes.append({
            "field_index": idx,
            "axis_id": axis_id,
            "name": AXIS_NAMES.get(axis_id, "Axis %s" % axis_id),
            "lo": 0.0 if lo is None else lo,
            "hi": 100.0 if hi is None else hi,
            "lo_present": lo is not None,
            "hi_present": hi is not None,
            "points": points,
        })
    return axes


def _build_core(core, lo, hi, points):
    """Re-emit the core message with new range + control points.

    Fields 5/6 are inserted in field-number order when the original omitted
    them (protobuf drops defaults, so lo=0 is simply absent).
    """
    out = bytearray()
    want = {5: lo, 6: hi}
    written = set()
    pt = 0

    def flush_before(fn):
        for n in (5, 6):
            if n not in written and want[n] is not None and fn > n:
                out.extend(enc_f64(n, want[n]))
                written.add(n)

    for f, w, s, e, ps, pe in fields(core):
        flush_before(f)
        if f in (5, 6) and w == 1:
            if want[f] is not None:
                out.extend(enc_f64(f, want[f]))
            written.add(f)
            continue
        if f == 8 and w == 2 and pe > ps and pt < len(points):
            x, y = points[pt]
            pt += 1
            out.extend(enc_msg(8, enc_f64(1, x) + enc_f64(2, y)))
            continue
        out.extend(core[s:e])

    for n in (5, 6):
        if n not in written and want[n] is not None:
            out.extend(enc_f64(n, want[n]))
    return bytes(out)


def patch_axis(blob, field_index, lo, hi, points):
    """Return a new preset blob with one axis block rewritten."""
    out = bytearray()
    for idx, (fn, wt, s, e, ps, pe) in enumerate(fields(blob)):
        if idx != field_index:
            out += blob[s:e]
            continue

        block = blob[ps:pe]
        # descend to the core, rebuild, then re-wrap on the way back up
        bufs = [block]
        for f in CORE_PATH:
            t = first_field(bufs[-1], f, 2)
            bufs.append(bufs[-1][t[4]:t[5]])
        new = _build_core(bufs[-1], lo, hi, points)
        for depth in range(len(CORE_PATH) - 1, -1, -1):
            new = replace_submsg(bufs[depth], CORE_PATH[depth], new)
        out += enc_msg(27, new)
    return bytes(out)


# --------------------------------------------------------------------------
# curve evaluation
# --------------------------------------------------------------------------
# The firmware's interpolation between control points is not documented. Both
# candidates are provided so a recorded sweep can show which one the pedals
# actually use; the Live tab overlays both and reports which fits better.

def _catmull(p0, p1, p2, p3, t):
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (2 * p1 + (-p0 + p2) * t
                  + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)


def curve_polyline(points, mode="linear", steps=240):
    """Dense [(x, y)] through (0,0) plus the control points, in percent."""
    pts = [(0.0, 0.0)] + sorted((float(x), float(y)) for x, y in points)
    if len(pts) < 2:
        return [(0.0, 0.0), (100.0, 100.0)]
    if mode == "linear":
        return pts
    pad = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(len(pad) - 3):
        p0, p1, p2, p3 = pad[i], pad[i + 1], pad[i + 2], pad[i + 3]
        n = max(2, steps // (len(pad) - 3))
        for s in range(n + 1):
            t = s / n
            out.append((_catmull(p0[0], p1[0], p2[0], p3[0], t),
                        _catmull(p0[1], p1[1], p2[1], p3[1], t)))
    return out


def curve_eval(points, x, mode="linear"):
    """Curve output at input x (both in percent)."""
    poly = curve_polyline(points, mode)
    if x <= poly[0][0]:
        return poly[0][1]
    if x >= poly[-1][0]:
        return poly[-1][1]
    for i in range(len(poly) - 1):
        x0, y0 = poly[i]
        x1, y1 = poly[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return poly[-1][1]


def slope_between(p_from, p_to):
    """Output % gained per pedal travel % between two points."""
    dx = p_to[0] - p_from[0]
    return 0.0 if dx == 0 else (p_to[1] - p_from[1]) / dx


def find_regression(samples, tol=0.5, bin_pct=1.0):
    """Where recorded output falls while pedal travel rises.

    Returns (from%, to%, drop%) for the worst run, or None. Samples are binned
    by travel and averaged first, so a press-and-release sweep is judged as a
    function of travel rather than of time, and sensor noise is smoothed out.
    """
    if len(samples) < 20:
        return None
    bins = {}
    for x, y in samples:
        bins.setdefault(int(x / bin_pct), []).append(y)
    profile = [(k * bin_pct, sum(v) / len(v)) for k, v in sorted(bins.items())]
    if len(profile) < 5:
        return None
    peak_x, peak_y = profile[0]
    worst, span = 0.0, None
    for x, y in profile:
        if y > peak_y:
            peak_x, peak_y = x, y
        if peak_y - y > worst:
            worst, span = peak_y - y, (peak_x, x)
    return (span[0], span[1], worst) if span and worst >= tol else None


def _hermite(t, ya, yb, ma, mb, h):
    """Cubic Hermite on one span: endpoint values ya/yb, tangents ma/mb."""
    t2 = t * t
    t3 = t2 * t
    return ((2 * t3 - 3 * t2 + 1) * ya + (t3 - 2 * t2 + t) * h * ma
            + (-2 * t3 + 3 * t2) * yb + (t3 - t2) * h * mb)


def pivot_slope_limit(px, py):
    """Largest pivot slope that keeps the curve rising on both sides.

    Fritsch-Carlson: a Hermite knot stays monotone while its tangent is at
    most three times the chord of *each* neighbouring span.
    """
    px = min(max(float(px), 1.0), 99.0)
    py = min(max(float(py), 0.0), 100.0)
    c0 = py / px
    c1 = (100.0 - py) / (100.0 - px)
    return max(0.0, 3.0 * min(c0, c1))


def pivot_curve_y(px, py, slope, x):
    """Cubic Hermite through (0,0), (px,py), (100,100) with a prescribed
    tangent at the pivot, so the curve leaves it at the same slope both ways.

    That shared tangent is the symmetry: equal distances either side of the
    pivot start out with the same steepness, then bend to meet the fixed ends.
    """
    px = min(max(float(px), 1.0), 99.0)
    py = min(max(float(py), 0.0), 100.0)
    c0 = py / px
    c1 = (100.0 - py) / (100.0 - px)
    s = max(0.0, min(float(slope), pivot_slope_limit(px, py)))
    if x <= px:
        m0 = max(0.0, min(2.0 * c0 - s, 3.0 * c0))
        return _hermite(x / px, 0.0, py, m0, s, px)
    m1 = max(0.0, min(2.0 * c1 - s, 3.0 * c1))
    return _hermite((x - px) / (100.0 - px), py, 100.0, s, m1, 100.0 - px)


def pivot_curve_points(points, px, py, slope):
    """Best 25/50/75 fit to the pivot curve - it is sampled at those exact X,
    which is the closest the three tunable points can get.

    Sampled values are kept as they come out of the model. The preset stores
    float64, so rounding them to whole percent here would throw away most of
    the slope's effect: at a mid-travel pivot one 0.01 step of slope moves
    these samples by about 0.06%, so whole percents would swallow fifteen
    consecutive steps and only then jump.
    """
    out = []
    prev = 0.0
    for x in [x for x, _y in points[:-1]]:
        y = max(prev, min(100.0, pivot_curve_y(px, py, slope, x)))
        out.append((float(x), y))
        prev = y
    out += [ENDPOINT] * (len(points) - len(out))
    return out


def _pivot_fit_error(pts, px, py, slope):
    return sum((pivot_curve_y(px, py, slope, x) - y) ** 2 for x, y in pts)


def fit_pivot_slope(pts, px, py, scan=64, refine=60):
    """The pivot slope whose curve fits `pts` best, by least squares.

    Solved numerically rather than in closed form because the model clamps its
    end tangents, which makes the error piecewise-quadratic: a bare ternary
    search can settle inside the wrong piece. A coarse scan brackets the true
    minimum first, then the search runs inside that bracket only.
    """
    lim = pivot_slope_limit(px, py)
    if lim <= 0.0 or not pts:
        return 0.0
    err = lambda s: _pivot_fit_error(pts, px, py, s)
    at = lambda i: lim * i / scan
    best = min(range(scan + 1), key=lambda i: err(at(i)))
    a, b = at(max(0, best - 1)), at(min(scan, best + 1))
    for _ in range(refine):
        m1, m2 = a + (b - a) / 3.0, b - (b - a) / 3.0
        if err(m1) <= err(m2):
            b = m2
        else:
            a = m1
    return (a + b) / 2.0


def fit_pivot_from_points(points):
    """(px, py, slope) of the pivot curve closest to an existing point list.

    The pivot itself is placed on the middle control point - with the X grid
    fixed at 25/50/75 that is the only point the model can pass through
    exactly - and the slope is then fitted to the rest. Given points that this
    same model produced, the fit returns the slope that made them, so a curve
    written from this tab reads back with the numbers it was created from.
    """
    edit = [(float(x), float(y)) for x, y in points[:-1]]
    if not edit:
        return 50.0, 50.0, 1.0
    px, py = edit[len(edit) // 2]
    return px, py, fit_pivot_slope(edit, px, py)


def curve_slope_at(points, x, h=1.0, mode="catmull"):
    """Local slope of the curve at x: output % gained per travel %."""
    a, b = max(0.0, x - h), min(100.0, x + h)
    if b <= a:
        return 0.0
    return (curve_eval(points, b, mode) - curve_eval(points, a, mode)) / (b - a)


def measured_slope_at(samples, x, h=2.5, tol=1.5):
    """Local slope of a recorded sweep at x, or None if it lacks data there.

    Averages the samples either side of x rather than differencing raw points,
    which would be dominated by sensor noise at this resolution.
    """
    def avg(centre):
        ys = [y for xx, y in samples if abs(xx - centre) <= tol]
        return sum(ys) / len(ys) if ys else None

    a, b = max(0.0, x - h), min(100.0, x + h)
    ya, yb = avg(a), avg(b)
    if ya is None or yb is None or b <= a:
        return None
    return (yb - ya) / (b - a)


# --------------------------------------------------------------------------
# slope colouring
# --------------------------------------------------------------------------
# Curves are drawn in bands of constant colour rather than a smooth gradient.
# Steps make "this stretch is steeper than that one" a comparison instead of a
# guess, and they keep a curve to a handful of canvas items rather than one per
# sample. The band boundaries land on round slopes, so the colour can be read
# back as a number.

SLOPE_BANDS = 10                 # 0.2 wide
SLOPE_SPAN = 2.0                 # anything steeper reads as the top band


def slope_band(m):
    """Band index for a slope. Band 5 begins at exactly 1.00, the linear one."""
    t = max(0.0, min(SLOPE_SPAN, float(m))) / SLOPE_SPAN
    return min(SLOPE_BANDS - 1, int(t * SLOPE_BANDS))


def mix_colour(c1, c2, t):
    """Blend two #rrggbb strings; t=0 gives c1, t=1 gives c2."""
    t = max(0.0, min(1.0, float(t)))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(a[k] + (b[k] - a[k]) * t))
                                   for k in range(3))


def band_palette(slow, base, fast):
    """One colour per band, taken at the slope the band starts at.

    The band starting at 1.00 gets `base` exactly, so a linear curve is drawn
    in the series' own established colour and every other shade reads as a
    departure from it: darker below linear, brighter above. That keeps the two
    series apart by hue while colour still carries the slope.
    """
    out = []
    for i in range(SLOPE_BANDS):
        s = i * SLOPE_SPAN / SLOPE_BANDS
        out.append(mix_colour(slow, base, s) if s <= 1.0
                   else mix_colour(base, fast, s - 1.0))
    return out


def slope_vs_linear(m):
    """How a slope compares with the linear baseline: (1 - m) * 100 percent."""
    pct = (1.0 - m) * 100.0
    if abs(pct) < 0.5:
        return "standard"
    return "%.0f%% %s" % (abs(pct), "slower" if pct > 0 else "faster")


def curve_segments(points):
    """[(from%, to%, slope, vs-linear text)] for every piece, origin included.

    The first and last pieces have no direct lever - they follow from the first
    point's output and from having to reach 100 - so they are worth showing.
    """
    chain = [(0.0, 0.0)] + [(float(x), float(y)) for x, y in points]
    out = []
    for a, b in zip(chain, chain[1:]):
        m = slope_between(a, b)
        out.append((a[0], b[0], m, slope_vs_linear(m)))
    return out


def points_to_slopes(points):
    """Points -> (output % of the first point, slope of each later segment)."""
    edit = points[:-1]
    if not edit:
        return 0.0, []
    return edit[0][1], [slope_between(edit[i], edit[i + 1])
                        for i in range(len(edit) - 1)]


def slopes_to_points(points, y_first, slopes):
    """(first output %, slopes) -> full point list on the same fixed X grid.

    Each Y is clamped so the curve can never fall or exceed 100. It is kept as
    a float: a slope times a 25% span rarely lands on a whole percent, and
    rounding it there would mean the stored curve did not have the slope that
    was asked for.
    """
    xs = [x for x, _ in points[:-1]]
    if not xs:
        return list(points)
    y = max(0.0, min(100.0, float(y_first)))
    out = [(xs[0], y)]
    for k, s in enumerate(slopes):
        if k + 1 >= len(xs):
            break
        y = out[-1][1] + s * (xs[k + 1] - xs[k])
        out.append((xs[k + 1], max(out[-1][1], min(100.0, y))))
    out += [ENDPOINT] * (len(points) - len(out))
    return out


def curve_x_of(travel, lo, hi):
    """Real pedal travel % -> curve-domain input %.

    Charts are drawn in the curve's own 0-100 domain, matching SimPro's graph
    and the control points' fixed 25/50/75. Measured sweeps arrive as real
    travel, so they are mapped through here before being plotted.
    """
    if hi <= lo:
        return 0.0
    return max(0.0, min(100.0, (travel - lo) * 100.0 / (hi - lo)))


def fmt_num(v, places=10):
    """A number as the shortest text that still carries it: 44.0 -> "44",
    23.25 -> "23.25", 0.78000000000001 -> "0.78".

    Fields hold text, so this is what decides the resolution actually written
    to the DB - hence ten places, which is finer than the float64 the preset
    stores can meaningfully resolve over a 0-100 range, so nothing a curve
    model produces is lost on the way through a widget. Trailing zeros are
    dropped, so a value that happens to be whole still reads as one rather
    than as false precision, and the arithmetic dust left by a fit
    (0.78000000000001) collapses back to the number that made it.
    """
    s = "%.*f" % (places, float(v))
    return s.rstrip("0").rstrip(".") if "." in s else s


def predicted_output(axis, input_pct, mode="linear"):
    """Expected output % for a given pedal travel %, per the stored preset.

    lo/hi are treated as an input trim: travel below lo reads 0, travel above
    hi reads full, and the span between them is what the curve is applied to.
    """
    lo, hi = float(axis["lo"]), float(axis["hi"])
    if hi <= lo:
        return 0.0
    t = (float(input_pct) - lo) / (hi - lo) * 100.0
    return curve_eval(axis["points"], max(0.0, min(100.0, t)), mode)


# --------------------------------------------------------------------------
# database access
# --------------------------------------------------------------------------

def load_presets(db_path=DB_PATH):
    """Presets whose blob carries pedal curve data, i.e. field-27 axis blocks.

    The DB is shared by every Simagic product SimPro manages, so on a rig with
    a wheel base its presets sit in the same table under another productUUID.
    Filtering by whether the blob parses (rather than by a hardcoded product
    id) keeps any pedal model in and everything unparseable out - a preset the
    tool cannot parse is also one it must never rewrite.
    """
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    con.text_factory = bytes
    rows = con.execute(
        "select id, presetName, presetData, typeof(presetData), presetUUID, "
        "productUUID from preset order by id"
    ).fetchall()
    con.close()
    out = []
    for pid, name, data, storage, uuid, product in rows:
        try:
            axes = parse_axes(data)
        except Exception:
            axes = []
        if not any(a["points"] for a in axes):
            continue
        out.append({
            "id": pid,
            "name": name.decode("utf-8", "replace"),
            "blob": data,
            "storage": storage.decode() if isinstance(storage, bytes) else storage,
            "uuid": uuid.decode("utf-8", "replace") if isinstance(uuid, bytes) else uuid,
            "product": product.decode("utf-8", "replace")
                       if isinstance(product, bytes) else str(product),
        })
    return out


def preset_labels(presets):
    """Combobox label per preset. The productUUID is only surfaced when the
    filtered list still spans more than one product, which should not happen
    for curve presets but must not be invisible if it does."""
    multi = len({p["product"] for p in presets}) > 1
    return ["%d  %s%s" % (p["id"], p["name"],
                          "  [product %s]" % p["product"] if multi else "")
            for p in presets]


def load_selected_preset_uuids(db_path=DB_PATH):
    """presetUUIDs SimPro records as loaded, most device-specific first.

    There is one 'selected_preset' row per product (pedals, wheel base, ...),
    so every row is returned and the caller picks the first uuid that matches
    a preset it actually loaded - that is what scopes the lookup to the
    pedals without knowing their productUUID up front.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"),
                              uri=True)
        rows = con.execute(
            "select settingValue from setting where settingKey='selected_preset'"
            " order by (deviceUUID is not null and deviceUUID != '0') desc"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def pick_selected_preset(presets, db_path=DB_PATH):
    """Index into presets of the one SimPro has loaded, or None."""
    for active in load_selected_preset_uuids(db_path):
        for n, p in enumerate(presets):
            if p["uuid"] == active:
                return n
    return None


def backup_db(db_path=DB_PATH):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, "user_precurve_%s.db" % stamp)
    shutil.copy2(db_path, dest)
    return dest


def save_preset(preset_id, blob, storage, db_path=DB_PATH):
    """Write the blob back, preserving the original storage class.

    SimPro binds these blobs as TEXT (with embedded NULs). CAST(? AS TEXT)
    relabels a blob parameter as text without touching the bytes, so the row
    keeps the exact shape the app wrote.
    """
    con = sqlite3.connect(db_path)
    sql = ("update preset set presetData = CAST(? AS TEXT) where id = ?"
           if storage == "text" else
           "update preset set presetData = ? where id = ?")
    con.execute(sql, (sqlite3.Binary(blob), preset_id))
    con.commit()
    con.close()


def simpro_running():
    """Names of any running SimPro Manager UI processes (simdaemon excluded)."""
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    return sorted({m.group(0) for m in re.finditer(r"simpro\w*", out, re.I)})


def _taskkill_targets(names):
    """['/im', 'simpro3.exe', ...] - one taskkill call covers every image."""
    out = []
    for n in names:
        out += ["/im", n if n.lower().endswith(".exe") else n + ".exe"]
    return out


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p), ("lpVerb", wt.LPCWSTR),
                ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wt.LPCWSTR), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", wt.DWORD), ("hIcon", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)]


def _taskkill_elevated(names, timeout=30.0):
    """Run taskkill through UAC. -> (launched_ok, detail).

    SimPro Manager runs elevated, so a taskkill from this (normal) process is
    refused outright: Windows only lets you end a process whose integrity level
    is no higher than your own. ShellExecuteEx with the "runas" verb asks for
    admin rights for that one command, which is far less intrusive than making
    the whole editor require elevation.
    """
    if sys.platform != "win32":
        return False, "elevation is only implemented on Windows"
    args = " ".join(["/f", "/t"] + _taskkill_targets(names))
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100        # NOCLOSEPROCESS | NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "taskkill.exe"
    info.lpParameters = args
    info.nShow = 0                   # SW_HIDE - the console window is noise
    # use_last_error, so the declined-prompt case can be told apart from a
    # real failure; plain windll would leave get_last_error() at 0.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wt.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:              # ERROR_CANCELLED
            return False, ("The Windows admin prompt was declined, so SimPro "
                           "Manager is still running.")
        return False, "could not start an elevated taskkill (error %d)" % err
    k = ctypes.windll.kernel32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    if info.hProcess:
        k.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        k.CloseHandle(info.hProcess)
    return True, ""


def _simagic_dirs_from_registry():
    """Simagic install folders named by the uninstall entries, if any.

    The Simpro3 entry records no InstallLocation but its DisplayIcon points at
    simdaemon.exe inside the same Simagic folder, so the tree can be found from
    that even on an install that did not go to the default location.
    """
    out = []
    try:
        import winreg
    except ImportError:
        return out
    roots = ((winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                except OSError:
                    continue
                if "simpro" not in str(name).lower():
                    continue
                for value in ("InstallLocation", "DisplayIcon"):
                    try:
                        v = str(winreg.QueryValueEx(sub, value)[0]).strip('" ')
                    except OSError:
                        continue
                    v = v.split(",")[0]          # DisplayIcon may carry ",0"
                    if not v:
                        continue
                    d = os.path.dirname(v) if os.path.splitext(v)[1] else v
                    # ...\Simagic\Daemon -> ...\Simagic
                    for cand in (d, os.path.dirname(d)):
                        if cand and os.path.isdir(cand) and cand not in out:
                            out.append(cand)
        finally:
            key.Close()
    return out


def find_simpro_exe():
    """Full path of the SimPro Manager executable, or None.

    The running process cannot be asked: it is elevated, so a normal process
    reads its image path back empty. The install is located on disk instead.
    """
    bases = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        root = os.environ.get(var)
        if root:
            bases.append(os.path.join(root, "Simagic"))
    bases += _simagic_dirs_from_registry()

    seen = set()
    for base in bases:
        if not base or base.lower() in seen:
            continue
        seen.add(base.lower())
        exact = os.path.join(base, "Simpro3", "bin", "simpro3.exe")
        if os.path.isfile(exact):
            return exact
        for pat in (os.path.join("Simpro*", "bin", "simpro*.exe"),
                    os.path.join("Simpro*", "simpro*.exe"),
                    os.path.join("*", "Simpro*", "bin", "simpro*.exe")):
            hits = sorted(h for h in glob.glob(os.path.join(base, pat))
                          if os.path.isfile(h))
            if hits:
                return hits[0]
    return None


def start_simpro(exe):
    """Launch SimPro Manager. -> (ok, detail).

    ShellExecute rather than Popen: the executable's manifest asks for
    administrator, so a plain CreateProcess is refused with "requires
    elevation" - only the shell raises the consent prompt.
    """
    if sys.platform != "win32":
        return False, "only implemented on Windows"
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.LPCWSTR,
                                      wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
    # Default verb, so the manifest decides whether to elevate - the same thing
    # the Start menu shortcut does.
    rc = int(shell32.ShellExecuteW(None, None, exe, None,
                                   os.path.dirname(exe), 1) or 0)  # SW_SHOWNORMAL
    if rc > 32:
        return True, ""
    if rc == 5:                      # SE_ERR_ACCESSDENIED
        return False, "the Windows admin prompt was declined"
    return False, "ShellExecute returned %d for %s" % (rc, exe)


def _wait_simpro_gone(timeout):
    deadline = time.time() + timeout
    while True:
        if not simpro_running():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def kill_simpro(timeout=10.0):
    """Force-close the SimPro Manager UI. -> (ok, detail).

    Deliberately a hard kill: a clean exit is exactly what makes SimPro write
    its own in-memory copy of the preset back over ours. simdaemon is left
    alone, so the pedals keep working on whatever is already loaded.

    Tries without elevation first (cheap, and no prompt when SimPro happens not
    to be running as admin), then escalates. taskkill's exit code is not
    trusted either way - with a multi-process app a child can already be gone
    by the time its own kill runs - so success is decided by re-checking the
    process list.
    """
    names = simpro_running()
    if not names:
        return True, ""
    detail = ""
    try:
        r = subprocess.run(["taskkill", "/f", "/t"] + _taskkill_targets(names),
                           capture_output=True, text=True, timeout=20)
        detail = ((r.stdout or "") + (r.stderr or "")).strip()
        plain_ok = r.returncode == 0
    except Exception as exc:
        detail, plain_ok = str(exc), False

    # A refusal is immediate, so only give the plain attempt a moment to take
    # effect before asking for admin rights.
    if _wait_simpro_gone(timeout if plain_ok else 1.5):
        return True, ""

    launched, edetail = _taskkill_elevated(simpro_running() or names)
    if _wait_simpro_gone(timeout if launched else 0):
        return True, ""
    left = simpro_running()
    if not left:
        return True, ""
    return False, (edetail or detail
                   or "still running: %s" % ", ".join(left))


# --------------------------------------------------------------------------
# HID reader
# --------------------------------------------------------------------------
# Every SetupAPI/kernel32 call below declares restype/argtypes. Without that,
# ctypes truncates returned 64-bit handles to a 32-bit int and enumeration
# silently finds nothing.

class _GUID(ctypes.Structure):
    _fields_ = [("D1", ctypes.c_ulong), ("D2", ctypes.c_ushort),
                ("D3", ctypes.c_ushort), ("D4", ctypes.c_ubyte * 8)]


class _DEVINTERFACE(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("guid", _GUID), ("Flags", wt.DWORD),
                ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD),
                ("hEvent", ctypes.c_void_p)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort), ("VersionNumber", ctypes.c_ushort)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort),
                ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort * 17),
                ("NumberLinkCollectionNodes", ctypes.c_ushort),
                ("NumberInputButtonCaps", ctypes.c_ushort),
                ("NumberInputValueCaps", ctypes.c_ushort),
                ("NumberInputDataIndices", ctypes.c_ushort),
                ("NumberOutputButtonCaps", ctypes.c_ushort),
                ("NumberOutputValueCaps", ctypes.c_ushort),
                ("NumberOutputDataIndices", ctypes.c_ushort),
                ("NumberFeatureButtonCaps", ctypes.c_ushort),
                ("NumberFeatureValueCaps", ctypes.c_ushort),
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


class _HIDP_VALUE_CAPS(ctypes.Structure):
    # The trailing union (Range/NotRange) is flattened to the four u16 pairs
    # it is made of; U1 is Usage/UsageMin, U2 is UsageMax when IsRange.
    _fields_ = [("UsagePage", ctypes.c_ushort), ("ReportID", ctypes.c_ubyte),
                ("IsAlias", ctypes.c_ubyte), ("BitField", ctypes.c_ushort),
                ("LinkCollection", ctypes.c_ushort), ("LinkUsage", ctypes.c_ushort),
                ("LinkUsagePage", ctypes.c_ushort), ("IsRange", ctypes.c_ubyte),
                ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
                ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte), ("BitSize", ctypes.c_ushort),
                ("ReportCount", ctypes.c_ushort), ("Reserved2", ctypes.c_ushort * 5),
                ("UnitsExp", ctypes.c_ulong), ("Units", ctypes.c_ulong),
                ("LogicalMin", ctypes.c_long), ("LogicalMax", ctypes.c_long),
                ("PhysicalMin", ctypes.c_long), ("PhysicalMax", ctypes.c_long),
                ("U1", ctypes.c_ushort), ("U2", ctypes.c_ushort),
                ("U3", ctypes.c_ushort), ("U4", ctypes.c_ushort),
                ("U5", ctypes.c_ushort), ("U6", ctypes.c_ushort),
                ("U7", ctypes.c_ushort), ("U8", ctypes.c_ushort)]


HIDP_STATUS_SUCCESS = 0x00110000
HIDP_INPUT = 0

_INVALID_HANDLE = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1


def _win32():
    k = ctypes.windll.kernel32
    s = ctypes.windll.setupapi
    h = ctypes.windll.hid
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                              wt.DWORD, wt.DWORD, ctypes.c_void_p]
    k.CreateEventW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                           ctypes.c_void_p, ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    k.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, wt.BOOL]
    k.CancelIo.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.ResetEvent.argtypes = [ctypes.c_void_p]
    s.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    s.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                       ctypes.c_void_p, wt.DWORD]
    s.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, wt.DWORD,
                                              ctypes.c_void_p]
    s.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
        ctypes.c_void_p, ctypes.c_void_p]
    s.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    h.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.ULONG]
    h.HidD_GetManufacturerString.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             wt.ULONG]
    h.HidD_GetSerialNumberString.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             wt.ULONG]
    h.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    h.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
    h.HidP_GetUsageValue.argtypes = [ctypes.c_int, ctypes.c_ushort,
                                     ctypes.c_ushort, ctypes.c_ushort,
                                     ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.c_void_p, ctypes.c_char_p, wt.ULONG]
    return k, s, h


def _hid_interface_paths():
    """Device interface paths of every present HID collection."""
    if sys.platform != "win32":
        return []
    k, s, h = _win32()
    guid = _GUID()
    h.HidD_GetHidGuid(ctypes.byref(guid))
    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
    hdev = s.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                  DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == _INVALID_HANDLE:
        return []
    did = _DEVINTERFACE()
    did.cbSize = ctypes.sizeof(did)
    i, out = 0, []
    try:
        while s.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i,
                                            ctypes.byref(did)):
            need = wt.DWORD()
            s.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0,
                                               ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 on x64, 6 on x86.
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = \
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if s.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf,
                                                  need.value, ctypes.byref(need),
                                                  None):
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            i += 1
    finally:
        s.SetupDiDestroyDeviceInfoList(hdev)
    return out


def hid_device_info(path):
    """Descriptor summary of one HID interface, or None if it cannot be read.

    Opened with no access rights: attributes, strings and the preparsed report
    descriptor are all available on a query-only handle, so a device held
    exclusively by a game or driver still describes itself here.
    """
    k, _s, h = _win32()
    OPEN_EXISTING, SHARE_RW = 3, 0x3
    handle = k.CreateFileW(path, 0, SHARE_RW, None, OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE:
        return None
    try:
        attrs = _HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        h.HidD_GetAttributes(ctypes.c_void_p(handle), ctypes.byref(attrs))
        strs = []
        for fn in (h.HidD_GetProductString, h.HidD_GetManufacturerString,
                   h.HidD_GetSerialNumberString):
            b = ctypes.create_unicode_buffer(256)
            strs.append(b.value.strip()
                        if fn(ctypes.c_void_p(handle), b, 512) else "")
        pp = ctypes.c_void_p()
        if not h.HidD_GetPreparsedData(ctypes.c_void_p(handle),
                                       ctypes.byref(pp)):
            return None
        try:
            caps = _HIDP_CAPS()
            if h.HidP_GetCaps(pp, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                return None
            out_axes, vendor_max, vendor_bytes = [], 0, 0
            n = ctypes.c_ushort(caps.NumberInputValueCaps)
            if n.value:
                arr = (_HIDP_VALUE_CAPS * n.value)()
                if h.HidP_GetValueCaps(HIDP_INPUT, arr, ctypes.byref(n),
                                       pp) == HIDP_STATUS_SUCCESS:
                    for v in arr[:n.value]:
                        lm = v.LogicalMax
                        if lm <= 0:
                            lm = (1 << min(31, v.BitSize)) - 1
                        if (v.UsagePage == GD_PAGE and not v.IsRange
                                and v.U1 in GD_AXIS_USAGES):
                            out_axes.append((v.U1, float(lm), v.ReportID))
                        elif v.UsagePage >= VENDOR_PAGE:
                            vendor_bytes += v.BitSize * v.ReportCount // 8
                            vendor_max = max(vendor_max, lm)
        finally:
            h.HidD_FreePreparsedData(pp)
        return {
            "path": path,
            "vid": attrs.VendorID, "pid": attrs.ProductID,
            "product": strs[0], "mfr": strs[1], "serial": strs[2],
            "usage_page": caps.UsagePage, "usage": caps.Usage,
            "report_len": caps.InputReportByteLength,
            "out_axes": out_axes,          # [(usage, logical_max, report_id)]
            "vendor_bytes": vendor_bytes,
            "vendor_max": float(vendor_max),
        }
    finally:
        k.CloseHandle(ctypes.c_void_p(handle))


def score_device(info):
    """How likely an HID interface is to be the Simagic pedals.

    Ranking, not filtering: even a bad guess lands in the dropdown where it
    can be corrected, so the score only has to put the right device first on
    an ordinary rig, not be certain.
    """
    s = 0
    text = ("%s %s" % (info["product"], info["mfr"])).lower()
    if "simagic" in text:
        s += 8
    if "pedal" in text or re.search(r"\bp\d{3,4}\b", text):
        s += 2
    if info["usage_page"] == GD_PAGE and info["usage"] in (0x04, 0x05):
        s += 2
    s += min(3, len(info["out_axes"]))
    if info["vendor_bytes"] >= 2:
        s += 2                   # room for a pre-curve input blob
    low = info["path"].lower()
    if all(t in low for t in LEGACY_VIDPID):
        s += 1
    return s


def enum_pedal_candidates():
    """Plausible pedal devices, best first.

    Kept deliberately loose - any joystick/gamepad-ish interface with analog
    axes qualifies - because this list also feeds the device dropdown, whose
    whole point is overriding a wrong auto-pick.
    """
    out = []
    for path in _hid_interface_paths():
        info = hid_device_info(path)
        if not info or not info["out_axes"]:
            continue
        gamey = info["usage_page"] == GD_PAGE and info["usage"] in (0x04, 0x05)
        text = ("%s %s" % (info["product"], info["mfr"])).lower()
        if not gamey and "simagic" not in text:
            continue
        out.append(info)
    out.sort(key=score_device, reverse=True)
    return out


def device_key(info):
    """Stable identity for remembering a choice or a pairing across sessions.

    The serial keeps the key stable across USB ports; without one, the
    interface path stands in (stable per port, the best available)."""
    return "%04x:%04x:%s" % (info["vid"], info["pid"],
                             info["serial"] or info["path"].lower())


def device_label(info):
    name = " ".join(x for x in (info["mfr"], info["product"]) if x) or "unnamed"
    return "%s  (%04x:%04x%s)" % (name, info["vid"], info["pid"],
                                  ", sn %s" % info["serial"]
                                  if info["serial"] else "")


class HidReader:
    """Background reader for a pedal device's HID input report.

    Overlapped reads on a worker thread; the Tk thread only ever drains the
    queue, so a silent device can never freeze the UI.

    Which bytes mean what is taken from the device's own report descriptor:
    outputs are decoded through HidP_GetUsageValue against the preparsed
    data, so no byte offset is ever assumed. Only the vendor blob carrying
    the pre-curve inputs stays opaque; Identify finds those bytes by
    movement (see finish_identify).
    """

    def __init__(self, info=None, maxlen=20000):
        self.info = info                 # from hid_device_info(); None = auto
        self.path = info["path"] if info else None
        self.queue = queue.Queue(maxsize=maxlen)
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self._pp = None                  # preparsed data, freed in stop()

    def start(self):
        if self.info is None:
            cands = enum_pedal_candidates()
            if not cands:
                self.error = "no HID device with analog axes found"
                return False
            self.info = cands[0]
        self.path = self.info["path"]
        if not self.info["out_axes"]:
            self.error = "device reports no analog axes"
            return False
        k, _s, h = _win32()
        handle = k.CreateFileW(self.path, 0, 0x3, None, 3, 0, None)
        if handle == _INVALID_HANDLE:
            self.error = "cannot query device"
            return False
        pp = ctypes.c_void_p()
        ok = h.HidD_GetPreparsedData(ctypes.c_void_p(handle), ctypes.byref(pp))
        k.CloseHandle(ctypes.c_void_p(handle))
        if not ok:
            self.error = "cannot read report descriptor"
            return False
        self._pp = pp
        self.report_len = self.info["report_len"]
        self.out_axes = list(self.info["out_axes"])
        # One report id carries the axes; on a device with several input
        # reports the others (buttons, vendor status) are simply skipped.
        self.report_id = self.out_axes[0][2]
        self._out_max = {u: lm for u, lm, _rid in self.out_axes}
        self.in_full = self.info["vendor_max"] or max(self._out_max.values())
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._pp:
            _k, _s, h = _win32()
            h.HidD_FreePreparsedData(self._pp)
            self._pp = None

    def decode(self, raw):
        """-> {usage: raw value} for the output axes, or None.

        HidP against the preparsed descriptor rather than fixed offsets, so
        any report layout Windows can parse is decoded correctly.
        """
        if self._pp is None or len(raw) != self.report_len:
            return None
        if raw[0] != self.report_id:
            return None
        _k, _s, h = _win32()
        val = ctypes.c_ulong()
        out = {}
        for usage, _lm, _rid in self.out_axes:
            if h.HidP_GetUsageValue(HIDP_INPUT, GD_PAGE, 0, usage,
                                    ctypes.byref(val), self._pp, raw,
                                    len(raw)) == HIDP_STATUS_SUCCESS:
                out[usage] = val.value
        return out or None

    @staticmethod
    def u16(raw, off):
        return raw[off] | (raw[off + 1] << 8)

    def windows(self, raw):
        """Every little-endian u16 window after the report id: the search
        space in which Identify locates the pre-curve inputs."""
        return {o: self.u16(raw, o) for o in range(1, len(raw) - 1)}

    def out_max(self, usage):
        return self._out_max.get(usage, 4095.0)

    def out_pct(self, usage, v):
        return v / self.out_max(usage) * 100.0

    def in_pct(self, v):
        return v / self.in_full * 100.0

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        k, _s, _h = _win32()
        GENERIC_READ = 0x80000000
        SHARE_RW = 0x3
        OPEN_EXISTING = 3
        FILE_FLAG_OVERLAPPED = 0x40000000
        handle = k.CreateFileW(self.path, GENERIC_READ, SHARE_RW, None,
                               OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if handle == _INVALID_HANDLE:
            self.error = "cannot open device (in use exclusively?)"
            return
        # The driver's default ring is 32 reports; at the ~190 Hz this device
        # actually emits that is only ~170 ms of slack, so a UI hitch during a
        # sweep would silently drop samples. Each handle gets its own buffer,
        # so this costs nothing elsewhere.
        try:
            hid = ctypes.windll.hid
            hid.HidD_SetNumInputBuffers.argtypes = [ctypes.c_void_p, wt.ULONG]
            hid.HidD_SetNumInputBuffers(ctypes.c_void_p(handle), 512)
        except Exception:
            pass
        event = k.CreateEventW(None, True, False, None)
        buf = ctypes.create_string_buffer(self.report_len)
        got = wt.DWORD()
        try:
            while not self._stop.is_set():
                ovl = _OVERLAPPED()
                ovl.hEvent = event
                k.ResetEvent(ctypes.c_void_p(event))
                k.ReadFile(ctypes.c_void_p(handle), buf, self.report_len, None,
                           ctypes.byref(ovl))
                if k.WaitForSingleObject(ctypes.c_void_p(event), 250) != 0:
                    k.CancelIo(ctypes.c_void_p(handle))
                    continue
                if not k.GetOverlappedResult(ctypes.c_void_p(handle),
                                             ctypes.byref(ovl),
                                             ctypes.byref(got), False):
                    continue
                raw = buf.raw[:got.value]
                try:
                    self.queue.put_nowait((time.perf_counter(), raw))
                except queue.Full:
                    try:
                        self.queue.get_nowait()
                        self.queue.put_nowait((time.perf_counter(), raw))
                    except queue.Empty:
                        pass
        finally:
            k.CloseHandle(ctypes.c_void_p(event))
            k.CloseHandle(ctypes.c_void_p(handle))

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out


def load_axis_map():
    """-> (per-device pairings, legacy flat pairing or {}).

    Format 2 keys pairings by device (see device_key), so a cached pairing
    can never silently apply to different hardware:
        {"format": 2, "devices": {key: {axis_id: {"out_usage": int,
                                                  "out_off": int|None,
                                                  "in_off": int|None}}}}
    A format-1 file (flat {axis_id: {"out": i, "in": j}}, index-based) is
    returned separately; connect() converts it once the legacy P1000 it must
    have described is actually present.
    """
    # utf-8-sig, not utf-8: these files are small enough to hand-edit, and a
    # Windows editor that adds a BOM would otherwise make the file silently
    # unreadable - the pairing would appear to have been forgotten.
    try:
        with open(AXES_CACHE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return {}, {}
    if isinstance(data, dict) and data.get("format") == 2:
        devices = {}
        for key, axes in (data.get("devices") or {}).items():
            try:
                devices[key] = {int(a): dict(m) for a, m in axes.items()}
            except Exception:
                continue
        return devices, {}
    try:
        return {}, {int(k): v for k, v in data.items()}
    except Exception:
        return {}, {}


def migrate_legacy_map(flat):
    """Index-based P1000 pairing -> format 2 usages and byte offsets."""
    out = {}
    for aid, m in flat.items():
        try:
            out[int(aid)] = {"out_usage": LEGACY_OUT_USAGES[m["out"]],
                             "out_off": LEGACY_OUT_OFFSETS[m["out"]],
                             "in_off": LEGACY_IN_OFFSETS[m["in"]]}
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def save_axis_map(devices):
    try:
        with open(AXES_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"format": 2,
                       "devices": {key: {str(a): m for a, m in axes.items()}
                                   for key, axes in devices.items()}},
                      fh, indent=2)
        return True
    except Exception:
        return False


def load_settings():
    """Saved UI preferences, or {} when the file is missing or unreadable."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception:
        return False


def hidtest(seconds=15):
    """Headless check of the reader, mirroring how the device was first probed."""
    cands = enum_pedal_candidates()
    print("candidate devices (best first):")
    for c in cands:
        print("  score %2d  %s" % (score_device(c), device_label(c)))
        print("            %d axes (%s)  report %dB  vendor blob %dB"
              % (len(c["out_axes"]),
                 " ".join(GD_USAGE_NAMES.get(u, "0x%02x" % u)
                          for u, _lm, _rid in c["out_axes"]),
                 c["report_len"], c["vendor_bytes"]))
    if not cands:
        print("  none")
        return 1
    r = HidReader(cands[0])
    if not r.start():
        print("FAILED: %s" % r.error)
        return 1
    usages = [u for u, _lm, _rid in r.out_axes]
    print("\nusing: %s" % device_label(r.info))
    print("path:  %s" % r.path)
    print("reading for %ds - press the pedals\n" % seconds)
    t0 = time.time()
    n = 0
    last = 0.0
    olo, ohi = {}, {}
    wlo, whi = {}, {}
    try:
        while time.time() - t0 < seconds:
            for _ts, raw in r.drain():
                d = r.decode(raw)
                if not d:
                    continue
                n += 1
                for u, v in d.items():
                    olo[u] = min(olo.get(u, v), v)
                    ohi[u] = max(ohi.get(u, v), v)
                for o, v in r.windows(raw).items():
                    wlo[o] = min(wlo.get(o, v), v)
                    whi[o] = max(whi.get(o, v), v)
                now = time.time()
                if now - last > 0.2:
                    last = now
                    print("  out %s   %s"
                          % (" ".join("%s=%4d" % (GD_USAGE_NAMES.get(u, u), d[u])
                                      for u in usages),
                             raw.hex(" ")))
            time.sleep(0.01)
    finally:
        r.stop()
    print("\n%d reports, %.0f Hz" % (n, n / max(1e-9, time.time() - t0)))
    if n == 0:
        print("\nFAILED: no reports decoded")
        return 1
    print("output axes travel: %s"
          % ", ".join("%s %d..%d" % (GD_USAGE_NAMES.get(u, u), olo[u], ohi[u])
                      for u in usages))
    moved = [(o, wlo[o], whi[o]) for o in sorted(wlo)
             if whi[o] - wlo[o] >= MIN_TRAVEL_FRAC * r.in_full]
    print("u16 windows that moved (candidate pre-curve inputs incl. outputs):")
    for o, a, b in moved:
        print("  byte %2d: %4d..%4d" % (o, a, b))
    print("\nHID READER OK")
    return 0


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def model_selftest():
    """Curve models must read back as the numbers that made them.

    This is the property the DB round-trip below cannot see: the points can
    survive a write byte-for-byte and the editor still show different
    parameters next time, which is exactly what whole-percent storage plus a
    chord-slope "fit" used to do.

    Only a pivot sitting on the middle control point is checked. Elsewhere the
    model has three free parameters and three points to read them from, so
    several pivots describe one curve equally well - that is what the GUI
    remembers its last saved pivot for.
    """
    grid = [(25.0, 0.0), (50.0, 0.0), (75.0, 0.0), ENDPOINT]
    failures = 0

    for py in (20.0, 33.3, 44.0, 50.0, 61.5, 80.0):
        for want in (0.05, 0.3, 0.78, 1.0, 1.37, 2.0):
            s = min(want, pivot_slope_limit(50.0, py))
            pts = pivot_curve_points(grid, 50.0, py, s)
            gx, gy, gs = fit_pivot_from_points(pts)
            if abs(gx - 50.0) > 1e-9 or abs(gy - py) > 1e-9 or abs(gs - s) > 1e-6:
                print("FAIL pivot round-trip: (50, %g, %g) read back as "
                      "(%g, %g, %g)" % (py, s, gx, gy, gs))
                failures += 1

    # One step of the slope spinner has to move the stored points, or the
    # field needs many clicks before the curve changes shape at all.
    for py in (30.0, 44.0, 70.0):
        a = pivot_curve_points(grid, 50.0, py, 0.78)
        b = pivot_curve_points(grid, 50.0, py, 0.79)
        if max(abs(p[1] - q[1]) for p, q in zip(a, b)) < 1e-4:
            print("FAIL pivot resolution: one 0.01 slope step changed nothing "
                  "at pivot output %g" % py)
            failures += 1

    for y1 in (10.0, 23.7, 35.0):
        for want in ([0.5, 1.5], [1.0, 1.0], [0.83, 1.21]):
            pts = slopes_to_points(grid, y1, want)
            gy1, got = points_to_slopes(pts)
            if abs(gy1 - y1) > 1e-9 or any(abs(a - b) > 1e-9
                                           for a, b in zip(want, got)):
                print("FAIL slope round-trip: %g %s read back as %g %s"
                      % (y1, want, gy1, got))
                failures += 1

    print("model round-trip: %s\n"
          % ("PASS" if not failures else "%d FAILURES" % failures))
    return failures


def selftest():
    failures = model_selftest()
    presets = load_presets()
    print("DB: %s" % DB_PATH)
    print("%d presets\n" % len(presets))

    for p in presets:
        axes = parse_axes(p["blob"])
        for ax in axes:
            # 1. identity: re-encoding unchanged values must be byte-identical
            same = patch_axis(p["blob"], ax["field_index"],
                              ax["lo"] if ax["lo_present"] else None,
                              ax["hi"] if ax["hi_present"] else None,
                              ax["points"])
            if same != p["blob"]:
                print("FAIL identity: preset %d %s" % (p["id"], ax["name"]))
                failures += 1
                continue

            # 2. mutation: changed values must read back exactly
            new_pts = [(x, round(y * 0.5 + 3.14159, 6)) for x, y in ax["points"]]
            mutated = patch_axis(p["blob"], ax["field_index"], 2.5, 91.25, new_pts)
            got = parse_axes(mutated)[
                [a["field_index"] for a in parse_axes(mutated)].index(ax["field_index"])
            ]
            if got["lo"] != 2.5 or got["hi"] != 91.25 or got["points"] != new_pts:
                print("FAIL mutation: preset %d %s" % (p["id"], ax["name"]))
                failures += 1
                continue

            # 3. every other axis in the same blob must be untouched
            others_before = [a for a in axes if a["field_index"] != ax["field_index"]]
            others_after = [a for a in parse_axes(mutated)
                            if a["field_index"] != ax["field_index"]]
            if [(a["lo"], a["hi"], a["points"]) for a in others_before] != \
               [(a["lo"], a["hi"], a["points"]) for a in others_after]:
                print("FAIL bleed: preset %d %s" % (p["id"], ax["name"]))
                failures += 1

        print("preset %-3d %-28s prod %-10s %d axes  %s"
              % (p["id"], p["name"][:28], p["product"], len(axes),
                 " ".join("%s[%g-%g]" % (a["name"][:3], a["lo"], a["hi"]) for a in axes)))

    print("\n%s" % ("ALL PASS" if failures == 0 else "%d FAILURES" % failures))
    return 1 if failures else 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter import font as tkfont

    BG, FG, ACCENT, GRID = "#1b1b1f", "#e6e6e6", "#e03030", "#33333a"

    # Tk is not DPI-aware by default, so Windows bitmap-stretches the window
    # (blurry text, clipped columns). Opt in and scale Tk to match.
    scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            scale = 1.0

    root = tk.Tk()
    root.title(APP_TITLE)
    root.configure(bg=BG)
    if scale != 1.0:
        root.tk.call("tk", "scaling", scale * 1.3333)
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=max(8, int(round(abs(f.cget("size")) * scale))))
            except Exception:
                pass
    # real geometry is computed from the finished layout, just before mainloop

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground="#26262c")
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TFrame", background=BG)
    style.configure("TLabelframe", background=BG, foreground=FG)
    style.configure("TLabelframe.Label", background=BG, foreground=FG)
    style.configure("TButton", padding=6)
    style.configure("Hint.TLabel", foreground="#8a8a95")
    style.configure("Warn.TLabel", foreground="#f0b429")
    style.configure("TEntry", foreground=FG, insertcolor=FG)
    style.configure("TCheckbutton", background=BG, foreground=FG,
                    focuscolor=BG, indicatorforeground=BG)
    style.map("TCheckbutton",
              background=[("active", BG)],
              foreground=[("active", FG)],
              indicatorbackground=[("selected", ACCENT), ("!selected", "#26262c")])
    style.configure("TSpinbox", foreground=FG, insertcolor=FG,
                    fieldbackground="#26262c", background="#33333a",
                    arrowcolor=FG, arrowsize=int(11 * scale))
    style.map("TCombobox",
              fieldbackground=[("readonly", "#26262c")],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", "#26262c")],
              selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", "#26262c")
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 7), background="#26262c",
                    foreground="#9a9aa5")
    style.map("TNotebook.Tab",
              background=[("selected", BG)], foreground=[("selected", FG)])

    state = {"presets": [], "axes": [], "preset": None, "axis": None, "dirty": False}
    live_refresh = {"fn": None}
    sync = {"busy": False, "last": None}

    # Every tab uses the same header/footer heights and side-panel width, so the
    # chart lands in an identical rectangle and does not jump when you switch.
    HEADER_H = int(64 * scale)
    FOOTER_H = int(92 * scale)   # must clear the Live tab's HID inspector
    SIDE_W = int(430 * scale)
    MEAS = "#35d0e0"
    MARKER = "#f2e14c"
    # Slope shades for each series. Both ramps pass through the series' own
    # colour at slope 1.00, so a linear curve looks exactly as it always did,
    # and neither ramp strays into the yellow of the markers or the blue of the
    # pivot handle.
    STORED_PAL = band_palette("#7d2b34", ACCENT, "#ff9f6b")
    MEAS_PAL = band_palette("#17505f", MEAS, "#c9fbff")

    # Declared up here because the editor charts overlay the recorded sweep too.
    # "map" is the pairing for the connected device only; "maps" holds every
    # device ever identified, keyed by device_key, and is what goes to disk.
    _maps, _legacy_map = load_axis_map()
    live = {"reader": None, "maps": _maps, "legacy": _legacy_map, "map": {},
            "devkey": None, "devices": [], "samples": [], "rec": False,
            "rec_t0": 0.0, "rec_left": None,
            "raw": None, "prev": None, "n": 0, "t0": 0.0, "rate": 0.0,
            "ident": None, "axes": [], "axis": None, "stats": [], "hexat": 0.0,
            "drawat": 0.0, "regress": None}

    _sweep_cache = {"key": None, "data": []}

    def sweep_in_curve_domain():
        """Recorded (travel, output) mapped into the charts' curve domain.

        Uses the deadzone that was in force when the sweep was taken, so
        editing Low/High afterwards does not slide the measured data around.
        Cached, because a single tooltip asks for it several times.
        """
        lo, hi = live.get("sweep_lohi") or (0.0, 100.0)
        key = (len(live["samples"]), lo, hi)
        if _sweep_cache["key"] != key:
            _sweep_cache["key"] = key
            _sweep_cache["data"] = [(curve_x_of(t, lo, hi), y)
                                    for t, y in live["samples"]]
        return _sweep_cache["data"]

    def measured_at(xv, tol=1.0):
        """Mean recorded output around a given curve-domain %, or None."""
        ys = [y for x, y in sweep_in_curve_domain() if abs(x - xv) <= tol]
        return sum(ys) / len(ys) if ys else None

    def tooltip_rows(points, xv):
        """Value + slope for the curve, and for the sweep when one exists."""
        oy = curve_eval(points, xv, "catmull")
        os_ = curve_slope_at(points, xv)
        rows = [("output", ACCENT, oy, "%.2f %%" % oy),
                ("slope", ACCENT, None,
                 "%.2f  %s" % (os_, slope_vs_linear(os_)))]
        if live["samples"]:
            s = sweep_in_curve_domain()
            my = measured_at(xv)
            ms = measured_slope_at(s, xv)
            rows.append(("measured", MEAS, my,
                         "-" if my is None else "%.2f %%" % my))
            rows.append(("meas. slope", MEAS, None,
                         "-" if ms is None else
                         "%.2f  %s" % (ms, slope_vs_linear(ms))))
        return rows

    def draw_legend(c, measured=None):
        """Top-left legend for any chart.

        Shared so all four tabs name the same two lines the same way: the red
        one is always the curve as stored, the blue one is always measurement.
        `measured` overrides the default rule of showing that entry only when
        a sweep has been recorded. With slope colouring on, each swatch becomes
        that series' ramp and a shared scale is printed underneath, so the
        shades can be read back as slopes.
        """
        x0, y0, _x1, _y1 = c.hover_geom
        rows = [("stored curve", ACCENT, STORED_PAL)]
        if bool(live["samples"]) if measured is None else measured:
            rows.append(("measured", MEAS, MEAS_PAL))
        banded = slope_colour.get()
        seg = int(round(5 * scale))
        bar = seg * SLOPE_BANDS
        lx, ly = x0 + 8, y0 + 8
        for label, col, pal in rows:
            if banded:
                for i, shade in enumerate(pal):
                    c.create_rectangle(lx + i * seg, ly - 3,
                                       lx + (i + 1) * seg, ly + 3,
                                       fill=shade, outline="")
                tx = lx + bar + 6
            else:
                c.create_line(lx, ly, lx + 16, ly, fill=col, width=2)
                tx = lx + 22
            c.create_text(tx, ly, text=label, anchor="w", fill="#8a8a95",
                          font=("Segoe UI", 7))
            ly += 13
        if banded:
            # End ticks are anchored inwards so neither can run into the bar's
            # neighbours - the right one would otherwise sit under "slope".
            for v, anchor in ((0.0, "w"), (1.0, "center"), (SLOPE_SPAN, "e")):
                c.create_text(lx + bar * v / SLOPE_SPAN, ly, anchor=anchor,
                              text="%g%s" % (v, "+" if v >= SLOPE_SPAN else ""),
                              fill="#6d6d78", font=("Segoe UI", 7))
            c.create_text(lx + bar + 6, ly, text="slope", anchor="w",
                          fill="#6d6d78", font=("Segoe UI", 7))

    _meas_band_cache = {"key": None, "bands": {}, "step": 2.0}

    def measured_bands(step=2.0):
        """Slope band per travel bin for the recorded sweep. -> (bands, step).

        Binned and cached rather than differenced per sample: at this sampling
        rate neighbouring samples differ by sensor noise, not by slope, and a
        per-dot lookup over the whole sweep would be quadratic.
        """
        s = sweep_in_curve_domain()
        key = (len(s), step, live.get("sweep_lohi"))
        if _meas_band_cache["key"] != key:
            binned = {}
            for x, y in s:
                binned.setdefault(int(x / step), []).append(y)
            avg = {k: sum(v) / len(v) for k, v in binned.items()}
            ks = sorted(avg)
            bands = {}
            for i, k in enumerate(ks):
                a = ks[max(0, i - 1)]
                b = ks[min(len(ks) - 1, i + 1)]
                dx = (b - a) * step
                bands[k] = slope_band((avg[b] - avg[a]) / dx if dx > 0 else 1.0)
            _meas_band_cache.update(key=key, bands=bands, step=step)
        return _meas_band_cache["bands"], _meas_band_cache["step"]

    def measured_scatter(c, px, py):
        """Draw the recorded sweep; shared by all four charts."""
        s = sweep_in_curve_domain()
        stride = max(1, len(s) // 1200)
        banded = slope_colour.get()
        bands, step = measured_bands() if banded else ({}, 1.0)
        for i in range(0, len(s), stride):
            xv, yv = s[i]
            col = (MEAS_PAL[bands.get(int(xv / step), 5)] if banded else MEAS)
            c.create_oval(px(xv) - 1.5, py(yv) - 1.5, px(xv) + 1.5, py(yv) + 1.5,
                          outline="", fill=col)

    def draw_curve(c, px, py, points):
        """The stored curve, in runs of constant slope band.

        Consecutive samples sharing a band are emitted as one polyline, so a
        typical curve costs a handful of canvas items instead of one per
        sample. Runs start on the previous run's last point, so the bands meet
        without gaps.
        """
        poly = curve_polyline(points, "catmull")
        if not slope_colour.get():
            flat = []
            for x, y in poly:
                flat += [px(x), py(y)]
            c.create_line(*flat, fill=ACCENT, width=2)
            return
        runs = []
        prev = None
        for a, b in zip(poly, poly[1:]):
            dx = b[0] - a[0]
            band = slope_band((b[1] - a[1]) / dx) if dx > 1e-9 else prev
            if band is None:
                band = slope_band(1.0)
            if band != prev:
                runs.append((band, [a]))
                prev = band
            runs[-1][1].append(b)
        for band, pts in runs:
            flat = []
            for x, y in pts:
                flat += [px(x), py(y)]
            c.create_line(*flat, fill=STORED_PAL[band], width=3)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    tab_curve = ttk.Frame(nb)
    tab_pivot = ttk.Frame(nb)
    tab_slope = ttk.Frame(nb)
    tab_live = ttk.Frame(nb)
    nb.add(tab_curve, text="3-point curve")
    nb.add(tab_pivot, text="Pivot curve")
    nb.add(tab_slope, text="Slope curve")
    nb.add(tab_live, text="Live / Verify")
    PIVOT_COL = "#7f8cff"        # the draggable pivot handle

    # ---- layout ---------------------------------------------------------
    tab_curve.grid_rowconfigure(0, minsize=HEADER_H, weight=0)
    tab_curve.grid_rowconfigure(1, weight=1)
    tab_curve.grid_rowconfigure(2, minsize=FOOTER_H, weight=0)
    tab_curve.grid_columnconfigure(0, weight=1)

    head = ttk.Frame(tab_curve)
    head.grid(row=0, column=0, sticky="nsew")
    top = ttk.Frame(head, padding=(12, 10, 12, 4))
    top.pack(fill="x")
    ttk.Label(top, text="Preset").grid(row=0, column=0, sticky="w")
    preset_cb = ttk.Combobox(top, state="readonly", width=38)
    preset_cb.grid(row=0, column=1, padx=(6, 18))
    ttk.Label(top, text="Pedal").grid(row=0, column=2, sticky="w")
    axis_cb = ttk.Combobox(top, state="readonly", width=24)
    axis_cb.grid(row=0, column=3, padx=6)

    warn = ttk.Label(head, text="", style="Warn.TLabel", padding=(12, 0))
    warn.pack(fill="x")

    body = ttk.Frame(tab_curve, padding=12)
    body.grid(row=1, column=0, sticky="nsew")

    side_px = int(430 * scale)
    canvas = tk.Canvas(body, width=int(430 * scale), height=int(430 * scale),
                       bg="#111114", highlightthickness=1, highlightbackground=GRID)
    canvas.grid(row=0, column=0, sticky="nsew")

    side = ttk.Frame(body, padding=(18, 0, 0, 0))
    side.grid(row=0, column=1, sticky="n")
    # Only the plot column absorbs extra space; the controls column is pinned to
    # the same width on every tab so the chart edge lines up.
    body.grid_columnconfigure(0, weight=1)
    body.grid_columnconfigure(1, weight=0, minsize=SIDE_W)
    body.grid_rowconfigure(0, weight=1)

    # The deadzone and the Y markers are whole percentages, so those entries
    # only accept 0-100 digits.
    def only_pct(proposed):
        return proposed == "" or (proposed.isdigit() and int(proposed) <= 100)

    vpct = (root.register(only_pct), "%P")

    def only_pct_frac(proposed):
        if proposed in ("", "."):
            return True
        try:
            return 0.0 <= float(proposed) <= 100.0
        except ValueError:
            return False

    vpctf = (root.register(only_pct_frac), "%P")

    def pct_spin(parent, var, width=6):
        """Whole 0-100 percent field with step arrows."""
        return ttk.Spinbox(parent, textvariable=var, from_=0, to=100,
                           increment=1, width=width, wrap=False,
                           validate="key", validatecommand=vpct)

    def pct_spin_frac(parent, var, width=7, increment=0.1):
        """0-100 percent field that also accepts fractions of a percent.

        No -format is given, so a value set from code keeps whatever precision
        it arrived with - a curve generated from a pivot lands on values like
        23.25 - and a typed one is taken as typed. Only the arrows quantise,
        stepping a tenth at a time.
        """
        return ttk.Spinbox(parent, textvariable=var, from_=0, to=100,
                           increment=increment, width=width, wrap=False,
                           validate="key", validatecommand=vpctf)

    def to_tenth(v):
        """Clamped to 0-100 and snapped to the drag/step resolution."""
        return round(max(0.0, min(100.0, float(v))) * 10.0) / 10.0

    # Up to three horizontal reference lines, drawn on every chart. All three
    # tabs bind their fields to these same variables, so the values stay in
    # step across tabs without any syncing code. They are restored from disk on
    # startup, since a reference line is usually a target you keep coming back
    # to across sessions rather than a one-off.
    settings = load_settings()
    marker_vars = [tk.StringVar(), tk.StringVar(), tk.StringVar()]
    for _var, _saved in zip(marker_vars, settings.get("markers") or []):
        _txt = str(_saved).strip()
        if _txt.isdigit() and int(_txt) <= 100:
            _var.set(_txt)

    # Shared by every chart, and remembered like the markers are. Off unless
    # asked for: the banding answers "how steep is it here", which is a
    # question you go looking for, and until you do the plain two-colour chart
    # is the easier one to read a curve off.
    slope_colour = tk.BooleanVar(
        value=bool(settings.get("slope_colour", False)))

    def marker_values():
        """Output % of each marker that is set, blanks and junk skipped."""
        out = []
        for v in marker_vars:
            s = v.get().strip()
            if not s:
                continue
            try:
                y = float(s)
            except ValueError:
                continue
            if 0.0 <= y <= 100.0:
                out.append(y)
        return out

    def marker_box(parent):
        """Chart controls that belong to every tab: the Y markers and the
        slope colouring. Built per tab but bound to one set of variables."""
        f = ttk.Labelframe(parent, text=" Chart display ", padding=10)
        for i, var in enumerate(marker_vars):
            ttk.Label(f, text="M%d" % (i + 1)).grid(
                row=0, column=i * 2, sticky="w", padx=(0 if i == 0 else 10, 0))
            pct_spin(f, var, width=5).grid(row=0, column=i * 2 + 1, padx=(4, 0))
        ttk.Label(f, text="Y markers: output %, blank = off", style="Hint.TLabel"
                  ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(f, text="Clear",
                   command=lambda: [v.set("") for v in marker_vars]
                   ).grid(row=1, column=4, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Checkbutton(f, text="Colour curves by slope",
                        variable=slope_colour).grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))
        return f

    rng = ttk.Labelframe(side, text=" Deadzone ", padding=10)
    rng.pack(fill="x")
    lo_var, hi_var = tk.StringVar(), tk.StringVar()
    ttk.Label(rng, text="Low %").grid(row=0, column=0, sticky="w")
    pct_spin(rng, lo_var).grid(row=0, column=1, padx=6, pady=3)
    ttk.Label(rng, text="High %").grid(row=1, column=0, sticky="w")
    pct_spin(rng, hi_var).grid(row=1, column=1, padx=6, pady=3)

    pts_frame = ttk.Labelframe(side, text=" Control points ", padding=10)
    pts_frame.pack(fill="x", pady=(12, 0))
    ttk.Label(pts_frame, text="pedal travel %",
              style="Hint.TLabel").grid(row=0, column=1)
    ttk.Label(pts_frame, text="output %", style="Hint.TLabel").grid(row=0, column=2)
    point_vars = []

    seg_frame = ttk.Labelframe(
        side, text=" Every segment vs linear (straight-line approximation) ",
        padding=10)
    seg_frame.pack(fill="x", pady=(12, 0))
    seg_label = ttk.Label(seg_frame, text="", justify="left",
                          font=("Consolas", int(9 * scale)))
    seg_label.pack(anchor="w")
    ttk.Label(seg_frame, style="Hint.TLabel", justify="left",
              wraplength=int(360 * scale),
              text="Each row is the straight line between two control points. "
                   "The stored curve bends between them, so hover the chart for "
                   "the true slope at a given travel."
              ).pack(anchor="w", pady=(8, 0))

    marker_box(side).pack(fill="x", pady=(12, 0))

    btns = ttk.Frame(side)
    btns.pack(fill="x", pady=(14, 0))

    foot = ttk.Frame(tab_curve)
    foot.grid(row=2, column=0, sticky="nsew")
    status = ttk.Label(foot, text="", style="Hint.TLabel",
                       padding=(12, 0, 12, 10))
    status.pack(fill="x", anchor="n")

    # ---- drawing --------------------------------------------------------
    def cur_points():
        """Full point list to store: the edited ones plus the fixed endpoint.

        The trailing point is the curve's end, always (100,100) in every stock
        and user preset, so it is pinned rather than carried through - that also
        repairs a preset where it was edited to something else.
        """
        ax = state["axis"]
        if not ax:
            return None
        out = []
        for x, yv in point_vars:
            try:
                out.append((x, float(yv.get())))
            except ValueError:
                return None
        out += [ENDPOINT] * (len(ax["points"]) - len(point_vars))
        return out

    # ---- shared plotting ------------------------------------------------
    hover = {"curve": None, "live": None}

    def plot_frame(c):
        """Clear c, draw the grid sized to its current extent.

        Returns (px, py, w, h) mapping percent -> pixels, or None while the
        canvas is still unmapped (winfo_* reports 1x1 before first layout).
        """
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 80 or h < 80:
            return None
        # Left and bottom margins carry the tick labels plus an axis title.
        pl, pb = int(54 * scale), int(46 * scale)
        pt, pr = int(16 * scale), int(16 * scale)
        x0, y0, x1, y1 = pl, pt, w - pr, h - pb
        px = lambda v: x0 + (x1 - x0) * v / 100.0
        py = lambda v: y1 - (y1 - y0) * v / 100.0
        for i in range(11):
            v = i * 10
            col = "#42424e" if v % 50 == 0 else GRID
            c.create_line(px(v), y0, px(v), y1, fill=col)
            c.create_line(x0, py(v), x1, py(v), fill=col)
            c.create_text(px(v), y1 + 12, text=str(v), fill="#6d6d78",
                          font=("Segoe UI", 7))
            c.create_text(x0 - 14, py(v), text=str(v), fill="#6d6d78",
                          font=("Segoe UI", 7))
        c.create_line(px(0), py(0), px(100), py(100), fill="#3a3a44", dash=(3, 3))
        # Drawn with the grid rather than per chart, so every plot that goes
        # through here gets the markers without knowing about them.
        for mv in marker_values():
            c.create_line(x0, py(mv), x1, py(mv), fill=MARKER, dash=(4, 4))
            c.create_text(x1 - 3, py(mv) - 3, text="%g" % mv, anchor="se",
                          fill=MARKER, font=("Segoe UI", 7))
        c.create_text((x0 + x1) / 2, y1 + int(30 * scale), text=AXIS_X,
                      fill="#8a8a95", font=("Segoe UI", 8))
        c.create_text(x0 - int(38 * scale), (y0 + y1) / 2, text=AXIS_Y,
                      fill="#8a8a95", font=("Segoe UI", 8), angle=90)
        c.hover_geom = (x0, y0, x1, y1)
        return px, py, w, h

    def keep_square(c):
        """Hold a chart square for the life of its tab.

        Both axes are percentages, so a stretched chart misleads: the dotted
        diagonal is the linear baseline only while the two axes share a scale,
        and a slope read off the shape by eye means nothing otherwise.

        The canvas is sized to the largest square its grid cell can hold
        rather than filled to the cell. Because that square is never wider
        than the cell already is, asking for it cannot push the cell wider in
        turn: the column carries weight, so it hands back as slack whatever
        the canvas stops requesting, and the size settles in one pass instead
        of oscillating.
        """
        body = c.master
        info = c.grid_info()
        col, row = int(info["column"]), int(info["row"])
        c.grid_configure(sticky="nw")    # fill would defeat the sizing below
        state = {"job": None, "left": 0}

        def apply():
            # Run from the idle queue, never straight off <Configure>: at the
            # moment that event fires the cell has not been re-laid out yet,
            # so measuring it there sizes the chart to the window's previous
            # shape and it trails a resize behind.
            state["job"] = None
            box = body.grid_bbox(col, row)
            if not box or box[2] < 80 or box[3] < 80:
                return
            side = min(box[2], box[3])
            # -width is the drawable area; the highlight border sits outside
            # it, so asking for the full cell would request two pixels more
            # than the cell holds and the wider axis would come back clipped.
            edge = c.winfo_reqwidth() - int(c.cget("width"))
            if abs(side - c.winfo_reqwidth()) > 1 or \
               abs(side - c.winfo_reqheight()) > 1:
                c.config(width=side - edge, height=side - edge)
                if state["left"] > 0:    # the cell may shift under us once
                    state["left"] -= 1
                    schedule(reset=False)

        def schedule(reset=True):
            if reset:
                state["left"] = 3        # a budget, so this can never spin
            if state["job"] is None:
                state["job"] = c.after_idle(apply)

        body.bind("<Configure>", lambda _e: schedule(), add="+")
        schedule()

    def hover_pct(c, ev):
        """Mouse x -> percent along the plot, or None if outside it."""
        g = getattr(c, "hover_geom", None)
        if not g:
            return None
        x0, _y0, x1, _y1 = g
        if not (x0 - 2 <= ev.x <= x1 + 2):
            return None
        return max(0.0, min(100.0, (ev.x - x0) * 100.0 / (x1 - x0)))

    def draw_tooltip(c, px, py, w, h, xv, ey, first, rows):
        """Crosshair, markers and a value box.

        rows = [(label, colour, marker_y|None, text)]; a row only gets a marker
        on the crosshair when marker_y is set, so slope rows are text-only.
        """
        x0, y0, x1, y1 = c.hover_geom
        c.create_line(px(xv), y0, px(xv), y1, fill="#5a5a66", dash=(2, 3))
        for _lab, col, yv, _txt in rows:
            if yv is not None:
                c.create_oval(px(xv) - 4, py(yv) - 4, px(xv) + 4, py(yv) + 4,
                              outline=col, width=2, fill="#111114")
        items = [(first, "%.2f %%" % xv, "#e6e6e6")]
        for lab, col, _yv, txt in rows:
            items.append((lab, txt, col))
        fh = int(15 * scale)
        bw = int(216 * scale)
        bh = fh * len(items) + int(10 * scale)
        bx = px(xv) + int(14 * scale)
        if bx + bw > w - 4:
            bx = px(xv) - int(14 * scale) - bw
        by = max(y0, min(ey - bh // 2, h - bh - 4))
        c.create_rectangle(bx, by, bx + bw, by + bh, fill="#1b1b22",
                           outline="#4a4a56")
        ty = by + int(5 * scale)
        for lab, txt, col in items:
            c.create_text(bx + int(9 * scale), ty, anchor="nw", text=lab,
                          fill="#8a8a95", font=("Segoe UI", 8))
            c.create_text(bx + bw - int(9 * scale), ty, anchor="ne", text=txt,
                          fill=col, font=("Consolas", 8))
            ty += fh

    def cur_axis_like():
        """Editor values shaped like a parsed axis, for the shared curve maths."""
        pts = cur_points()
        if pts is None:
            return None
        try:
            lo, hi = float(int(lo_var.get())), float(int(hi_var.get()))
        except ValueError:
            return None
        if hi <= lo:
            return None
        return {"lo": lo, "hi": hi, "points": pts}

    def refresh_segments():
        pts = cur_points()
        seg_label.config(text="" if pts is None else "\n".join(
            "%3d-%-3d  %5.2f   %s" % (a, b, m, txt)
            for a, b, m, txt in curve_segments(pts)))

    def redraw(*_):
        # Ahead of the canvas check so the table still updates while the tab
        # is unmapped (its canvas reports 1x1 until first shown).
        refresh_segments()
        fr = plot_frame(canvas)
        if not fr:
            return
        px, py, w, h = fr
        pts = cur_points()
        ax_like = cur_axis_like()
        if pts is None or ax_like is None:
            canvas.create_text(w / 2, h / 2, text="invalid number",
                               fill="#f0b429", font=("Segoe UI", 10))
            return

        # Plotted against real pedal travel, deadzone included, so this is the
        # exact same curve the Live tab predicts.
        measured_scatter(canvas, px, py)   # recorded sweep, if there is one
        draw_curve(canvas, px, py, pts)
        for i, (x, y) in enumerate(pts):
            if i < len(point_vars):          # draggable
                canvas.create_oval(px(x) - 5, py(y) - 5, px(x) + 5, py(y) + 5,
                                   fill="#ffffff", outline=ACCENT, width=2)
            else:                            # fixed endpoint
                canvas.create_oval(px(x) - 3, py(y) - 3, px(x) + 3, py(y) + 3,
                                   fill="#111114", outline="#7a7a86", width=1)
        draw_legend(canvas)
        if hover["curve"]:
            xv, ey = hover["curve"]
            draw_tooltip(canvas, px, py, w, h, xv, ey, "pedal travel",
                         tooltip_rows(pts, xv))

    def mark_dirty(*_):
        state["dirty"] = True
        if not sync["busy"]:
            sync["last"] = "points"
        redraw()

    # ---- population -----------------------------------------------------
    def build_point_rows(points):
        """One row per adjustable point. The final point is the fixed (100,100)
        endpoint and the X of the rest is fixed too, so only Y is editable."""
        for child in list(pts_frame.grid_slaves()):
            if int(child.grid_info()["row"]) > 0:
                child.destroy()
        point_vars.clear()
        for i, (x, y) in enumerate(points[:-1]):
            yv = tk.StringVar(value=fmt_num(y))
            ttk.Label(pts_frame, text="P%d" % (i + 1)).grid(row=i + 1, column=0,
                                                            sticky="w", pady=2)
            ttk.Label(pts_frame, text="%d" % round(x), anchor="e",
                      width=6).grid(row=i + 1, column=1, padx=4, pady=2)
            pct_spin_frac(pts_frame, yv).grid(row=i + 1, column=2, padx=4, pady=2)
            yv.trace_add("write", mark_dirty)
            point_vars.append((float(x), yv))

    def show_axis(*_):
        i = axis_cb.current()
        if i < 0 or i >= len(state["axes"]):
            return
        ax = state["axes"][i]
        state["axis"] = ax
        lo_var.set("%d" % round(ax["lo"]))
        hi_var.set("%d" % round(ax["hi"]))
        build_point_rows(ax["points"])
        state["dirty"] = False
        note = ("   deadzone is not whole - saving rounds it"
                if any(v != round(v) for v in (ax["lo"], ax["hi"])) else "")
        status.config(text="axis id %s   %d adjustable points%s"
                           % (ax["axis_id"], len(point_vars), note))
        redraw()

    def show_preset(*_):
        i = preset_cb.current()
        if i < 0:
            return
        p = state["presets"][i]
        state["preset"] = p
        state["axes"] = parse_axes(p["blob"])
        axis_cb["values"] = ["%s  (%d-%d%%)" % (a["name"], round(a["lo"]),
                                               round(a["hi"]))
                             for a in state["axes"]]
        want = state.pop("want_axis", None)
        pick = None
        if want is not None:
            pick = next((n for n, a in enumerate(state["axes"])
                         if a["axis_id"] == want), None)
        if pick is None:
            pick = next((n for n, a in enumerate(state["axes"])
                         if a["axis_id"] == 4), 0)
        axis_cb.current(pick)
        show_axis()
        if live_refresh["fn"]:
            live_refresh["fn"]()

    def reload_db(keep_preset_id=None, keep_axis_id=None):
        try:
            state["presets"] = load_presets()
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Cannot read the DB:\n\n%s" % exc)
            return
        preset_cb["values"] = preset_labels(state["presets"])
        if not state["presets"]:
            messagebox.showerror(
                APP_NAME,
                "No presets with pedal curve data found in the DB.\n\n"
                "Either none exist yet (create one in SimPro Manager first) "
                "or this pedal model stores its curves in a form this tool "
                "does not recognise.")
        if state["presets"]:
            # Keep the caller's selection (a save reloads and must not jump);
            # otherwise open on whatever SimPro has loaded on the pedals.
            pick = None
            if keep_preset_id is not None:
                pick = next((n for n, p in enumerate(state["presets"])
                             if p["id"] == keep_preset_id), None)
            if pick is None:
                pick = pick_selected_preset(state["presets"])
            if pick is None:
                pick = len(state["presets"]) - 1
            if keep_axis_id is not None:
                state["want_axis"] = keep_axis_id
            preset_cb.current(pick)
            show_preset()
        running = simpro_running()
        warn.config(text=("SimPro Manager is running (%s) - close it before saving, "
                          "or it will overwrite your edit."
                          % ", ".join(running)) if running else "")

    # ---- actions --------------------------------------------------------
    def make_linear():
        for x, yv in point_vars:
            yv.set(fmt_num(x))

    def ask_kill_and_store(names):
        """Modal for saving while SimPro Manager is open. True = kill and write.

        Not messagebox.askyesno: writing underneath a running SimPro simply
        does not take, so a plain "yes" would only ever produce a silent
        no-op. The only way forward is to close it first, which is what the
        one affirmative button does.
        """
        dlg = tk.Toplevel(root)
        dlg.title("SimPro Manager is running")
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.resizable(False, False)
        choice = {"go": False}
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left", wraplength=int(430 * scale),
                  text="SimPro Manager (%s) is open. It holds its own copy of "
                       "the preset and writes that back, so a save made now "
                       "does not stick - closing it first is the only way to "
                       "make the edit take." % ", ".join(names)
                  ).pack(anchor="w")
        ttk.Label(frm, style="Hint.TLabel", justify="left",
                  wraplength=int(430 * scale),
                  text="\"First kill and store\" force-closes SimPro Manager, "
                       "writes, then starts it again - re-select the preset "
                       "there to push it to the pedals. It runs as "
                       "administrator, so Windows will ask you to allow both "
                       "steps."
                  ).pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(16, 0))

        def go():
            choice["go"] = True
            dlg.destroy()

        ttk.Button(row, text="No", command=dlg.destroy).pack(side="right")
        ttk.Button(row, text="First kill and store",
                   command=go).pack(side="right", padx=(0, 8))
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (
            max(0, root.winfo_rootx()
                + (root.winfo_width() - dlg.winfo_width()) // 2),
            max(0, root.winfo_rooty()
                + (root.winfo_height() - dlg.winfo_height()) // 3)))
        dlg.grab_set()
        root.wait_window(dlg)
        return choice["go"]

    def commit_axis(p, ax, lo, hi, pts):
        """Validate, back up and write one axis. True if it was written.

        Shared by every editor tab so the write path can never diverge.
        """
        problems = []
        if not 0 <= lo < hi <= 100:
            problems.append("Deadzone must satisfy 0 <= low < high <= 100.")
        if any(not (0 <= x <= 100) or not (0 <= y <= 100) for x, y in pts):
            problems.append("Control points must be within 0-100.")
        if any(pts[i][0] >= pts[i + 1][0] for i in range(len(pts) - 1)):
            problems.append("Input % values must strictly increase.")
        if any(pts[i][1] > pts[i + 1][1] for i in range(len(pts) - 1)):
            problems.append("Output % values must not decrease "
                            "(a falling curve makes the pedal go backwards).")
        if problems:
            messagebox.showerror(APP_NAME, "\n\n".join(problems))
            return False

        running = simpro_running()
        restart_exe = None
        if running:
            if not ask_kill_and_store(running):
                return False
            # Located before the kill, purely so a missing install is reported
            # while SimPro is still up rather than after it has been closed.
            restart_exe = find_simpro_exe()
            root.config(cursor="watch")
            root.update_idletasks()
            try:
                killed, detail = kill_simpro()
            finally:
                root.config(cursor="")
            if not killed:
                messagebox.showerror(
                    APP_NAME,
                    "Could not close SimPro Manager, so nothing was written:"
                    "\n\n%s" % detail)
                return False

        try:
            new_blob = patch_axis(
                p["blob"], ax["field_index"],
                lo if (ax["lo_present"] or lo != 0.0) else None,
                hi, pts)
            check = parse_axes(new_blob)
            got = next(a for a in check if a["field_index"] == ax["field_index"])
            if got["points"] != pts or got["hi"] != hi:
                raise ValueError("verification of the re-encoded preset failed")
            dest = backup_db()
            save_preset(p["id"], new_blob, p["storage"])
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Write failed:\n\n%s" % exc)
            return False

        # Started again only once the new blob is safely on disk, so what it
        # reads on the way up is the edited preset.
        if running:
            if restart_exe:
                ok, why = start_simpro(restart_exe)
                next_step = ("SimPro Manager is starting again - re-select the "
                             "preset there to push it to the pedals."
                             if ok else
                             "Could not start SimPro Manager again (%s), so "
                             "start it yourself and re-select the preset."
                             % why)
            else:
                next_step = ("SimPro Manager was closed but its install could "
                             "not be found, so start it yourself and re-select "
                             "the preset to push it to the pedals.")
        else:
            next_step = ("Start SimPro Manager and re-select the preset to "
                         "push it to the pedals.")

        messagebox.showinfo(
            "Saved",
            "Preset %d \"%s\" - %s updated.\n\n"
            "%s\n\n"
            "--- Backup ---\n%s\n\n"
            "To undo: close SimPro Manager, then copy that file over\n%s\n"
            "(\"Open DB folder in Explorer\" takes you straight there.)"
            % (p["id"], p["name"], ax["name"], next_step, dest, DB_PATH))
        if live_refresh["fn"]:
            live_refresh["fn"]()
        return True

    def save():
        ax, p = state["axis"], state["preset"]
        if not ax or not p:
            return
        try:
            lo, hi = float(int(lo_var.get())), float(int(hi_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME,
                                 "Deadzone values must be whole percentages.")
            return
        pts = cur_points()
        if pts is None:
            messagebox.showerror(APP_NAME,
                                 "Control point outputs must be numbers.")
            return
        if commit_axis(p, ax, lo, hi, pts):
            sync["last"] = None          # saved state wins over any snapshot
            reload_db(p["id"], ax["axis_id"])
            sl_reload(p["id"], ax["axis_id"])
            pv_reload(p["id"], ax["axis_id"])

    def open_db_folder():
        """Open Explorer with user.db selected."""
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(DB_PATH)])
        except Exception as exc:
            messagebox.showerror(APP_NAME,
                                 "Could not open Explorer:\n\n%s" % exc)

    ttk.Button(btns, text="Reload", command=reload_db).pack(side="left")
    ttk.Button(btns, text="Make linear", command=make_linear).pack(side="left", padx=6)
    ttk.Button(btns, text="Save to DB", command=save).pack(side="right")
    ttk.Button(side, text="Open DB folder in Explorer",
               command=open_db_folder).pack(fill="x", pady=(10, 0))

    # Lives in the fixed-height footer, which has room to spare, rather than
    # lengthening the side panel past the chart.
    ttk.Label(foot, style="Hint.TLabel", justify="left", padding=(12, 0, 12, 0),
              text="Output arrows step a tenth of a percent and hundredths can "
                   "be typed; the deadzone is whole. Every save backs up "
                   "user.db first."
              ).pack(fill="x", anchor="n")

    lo_var.trace_add("write", mark_dirty)
    hi_var.trace_add("write", mark_dirty)
    def on_preset_change(*_):
        if state.get("axis"):            # stay on the same pedal across presets
            state["want_axis"] = state["axis"]["axis_id"]
        show_preset()

    preset_cb.bind("<<ComboboxSelected>>", on_preset_change)
    axis_cb.bind("<<ComboboxSelected>>", show_axis)

    drag = {"i": None}

    def point_near(ev, radius=14):
        """Index into point_vars of the control point under the cursor."""
        g = getattr(canvas, "hover_geom", None)
        if not g:
            return None
        x0, y0, x1, y1 = g
        for i, (x, yv) in enumerate(point_vars):
            try:
                y = float(yv.get())
            except ValueError:
                continue
            cx = x0 + (x1 - x0) * x / 100.0
            cy = y1 - (y1 - y0) * y / 100.0
            if abs(ev.x - cx) <= radius and abs(ev.y - cy) <= radius:
                return i
        return None

    def drag_y(ev, i):
        """Pixel y -> output % in tenths, kept between the neighbouring points."""
        x0, y0, x1, y1 = canvas.hover_geom
        v = to_tenth((y1 - ev.y) * 100.0 / (y1 - y0))
        pts = cur_points() or []
        lo_lim = pts[i - 1][1] if i > 0 else 0.0
        hi_lim = pts[i + 1][1] if i + 1 < len(pts) else 100.0
        return max(lo_lim, min(hi_lim, v))

    def curve_press(ev):
        drag["i"] = point_near(ev)

    def curve_drag(ev):
        i = drag["i"]
        if i is None:
            return
        point_vars[i][1].set(fmt_num(drag_y(ev, i)))

    def curve_motion(ev):
        v = hover_pct(canvas, ev)
        hover["curve"] = (v, ev.y) if v is not None else None
        canvas.config(cursor="sb_v_double_arrow" if point_near(ev) is not None
                      else "")
        redraw()

    canvas.bind("<Configure>", redraw)
    canvas.bind("<Motion>", curve_motion)
    canvas.bind("<Button-1>", curve_press)
    canvas.bind("<B1-Motion>", curve_drag)
    canvas.bind("<ButtonRelease-1>", lambda _e: drag.__setitem__("i", None))
    canvas.bind("<Leave>", lambda _e: (hover.__setitem__("curve", None), redraw()))

    reload_db()

    # ================= Slope curve tab =================
    # Same stored curve, different levers: the first point's output plus the
    # slope of each following segment. Reference lines are drawn at the linear
    # baseline and successive 10%-slower slopes.
    SLOPE_REFS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)
    sl = {"presets": [], "axes": [], "preset": None, "axis": None, "dirty": False}
    sl_hover = {"v": None}
    sl_drag = {"i": None}
    sl_slope_vars = []
    sl_out_labels = []
    sl_y1_var = tk.StringVar()
    sl_lock = tk.BooleanVar(value=False)
    sl_sync = {"busy": False}

    def only_slope(proposed):
        if proposed in ("", "."):
            return True
        try:
            return 0.0 <= float(proposed) <= 5.0
        except ValueError:
            return False

    vslope = (root.register(only_slope), "%P")

    def slope_spin(parent, var, width=7):
        """Slope field. Like the fractional percent fields it carries no
        -format, so the arrows step by 0.01 while a value set from code or
        typed in keeps every digit it has - the stored points are float64 and
        a slope's last decimals are still visible in them."""
        return ttk.Spinbox(parent, textvariable=var, from_=0.0, to=5.0,
                           increment=0.01, width=width,
                           wrap=False, validate="key", validatecommand=vslope)

    tab_slope.grid_rowconfigure(0, minsize=HEADER_H, weight=0)
    tab_slope.grid_rowconfigure(1, weight=1)
    tab_slope.grid_rowconfigure(2, minsize=FOOTER_H, weight=0)
    tab_slope.grid_columnconfigure(0, weight=1)

    sl_head = ttk.Frame(tab_slope)
    sl_head.grid(row=0, column=0, sticky="nsew")
    sl_top = ttk.Frame(sl_head, padding=(12, 10, 12, 4))
    sl_top.pack(fill="x")
    ttk.Label(sl_top, text="Preset").grid(row=0, column=0, sticky="w")
    sl_preset_cb = ttk.Combobox(sl_top, state="readonly", width=38)
    sl_preset_cb.grid(row=0, column=1, padx=(6, 18))
    ttk.Label(sl_top, text="Pedal").grid(row=0, column=2, sticky="w")
    sl_axis_cb = ttk.Combobox(sl_top, state="readonly", width=24)
    sl_axis_cb.grid(row=0, column=3, padx=6)

    sl_warn = ttk.Label(sl_head, text="", style="Warn.TLabel", padding=(12, 0))
    sl_warn.pack(fill="x")

    sl_body = ttk.Frame(tab_slope, padding=12)
    sl_body.grid(row=1, column=0, sticky="nsew")
    sl_canvas = tk.Canvas(sl_body, width=int(430 * scale), height=int(430 * scale),
                          bg="#111114", highlightthickness=1,
                          highlightbackground=GRID)
    sl_canvas.grid(row=0, column=0, sticky="nsew")
    sl_side = ttk.Frame(sl_body, padding=(18, 0, 0, 0))
    sl_side.grid(row=0, column=1, sticky="n")
    sl_body.grid_columnconfigure(0, weight=1)
    sl_body.grid_columnconfigure(1, weight=0, minsize=SIDE_W)
    sl_body.grid_rowconfigure(0, weight=1)

    sl_rng = ttk.Labelframe(sl_side, text=" Deadzone ", padding=10)
    sl_rng.pack(fill="x")
    sl_lo_var, sl_hi_var = tk.StringVar(), tk.StringVar()
    ttk.Label(sl_rng, text="Low %").grid(row=0, column=0, sticky="w")
    pct_spin(sl_rng, sl_lo_var).grid(row=0, column=1, padx=6, pady=3)
    ttk.Label(sl_rng, text="High %").grid(row=1, column=0, sticky="w")
    pct_spin(sl_rng, sl_hi_var).grid(row=1, column=1, padx=6, pady=3)

    sl_frame = ttk.Labelframe(sl_side, text=" Curve shape ", padding=10)
    sl_frame.pack(fill="x", pady=(12, 0))

    sl_pts_frame = ttk.Labelframe(sl_side, text=" Stored points ", padding=10)
    sl_pts_frame.pack(fill="x", pady=(12, 0))
    ttk.Label(sl_pts_frame, text="pedal travel %",
              style="Hint.TLabel").grid(row=0, column=1)
    ttk.Label(sl_pts_frame, text="output %",
              style="Hint.TLabel").grid(row=0, column=2)
    sl_pt_vars = []

    marker_box(sl_side).pack(fill="x", pady=(12, 0))

    sl_btns = ttk.Frame(sl_side)
    sl_btns.pack(fill="x", pady=(14, 0))

    sl_foot = ttk.Frame(tab_slope)
    sl_foot.grid(row=2, column=0, sticky="nsew")
    sl_status = ttk.Label(sl_foot, text="", style="Hint.TLabel",
                          padding=(12, 0, 12, 10))
    sl_status.pack(fill="x", anchor="n")

    def sl_points():
        """Current editor values -> full stored point list."""
        ax = sl["axis"]
        if not ax:
            return None
        try:
            y1 = float(sl_y1_var.get())
            slopes = [float(v.get()) for v in sl_slope_vars]
        except ValueError:
            return None
        return slopes_to_points(ax["points"], y1, slopes)

    def sl_axis_like():
        pts = sl_points()
        if pts is None:
            return None
        try:
            lo, hi = float(int(sl_lo_var.get())), float(int(sl_hi_var.get()))
        except ValueError:
            return None
        if hi <= lo:
            return None
        return {"lo": lo, "hi": hi, "points": pts}

    def sl_refresh_outputs():
        pts = sl_points()
        if pts is None:
            return
        for k, lab in enumerate(sl_out_labels):
            if k + 1 < len(pts):
                # Name both coordinates: "output 50%" alone is ambiguous when
                # the travel and the output happen to be the same number. Two
                # places here, where it only has to be read; the mirror below
                # is the one that has to be exact. The arrow the line used to
                # open with pays for those two places, so the widest reading
                # is no wider than before and the side panel does not grow.
                lab.config(text="%.2f%% out at %d%% travel"
                                % (pts[k + 1][1], round(pts[k + 1][0])))
        for k, var in enumerate(sl_pt_vars):
            if k < len(pts):
                var.set(fmt_num(pts[k][1]))

    def sl_mark_dirty(*_):
        sl["dirty"] = True
        if not sync["busy"]:
            sync["last"] = "slope"
        sl_refresh_outputs()
        sl_redraw()

    def sl_slope_edited(idx):
        """Trace for one slope field; mirrors to the others while locked.

        The busy flag stops the mirrored writes from re-entering this handler.
        """
        def handler(*_):
            if sl_lock.get() and not sl_sync["busy"]:
                sl_sync["busy"] = True
                try:
                    val = sl_slope_vars[idx].get()
                    for j, v in enumerate(sl_slope_vars):
                        if j != idx and v.get() != val:
                            v.set(val)
                finally:
                    sl_sync["busy"] = False
            sl_mark_dirty()
        return handler

    def sl_lock_toggled(*_):
        """Locking matches the rest to the first slope so they start in step."""
        if sl_lock.get() and len(sl_slope_vars) > 1:
            sl_sync["busy"] = True
            try:
                val = sl_slope_vars[0].get()
                for v in sl_slope_vars[1:]:
                    v.set(val)
            finally:
                sl_sync["busy"] = False
            sl_mark_dirty()

    def sl_build_rows(points):
        for child in list(sl_frame.grid_slaves()):
            child.destroy()
        for child in list(sl_pts_frame.grid_slaves()):
            if int(child.grid_info()["row"]) > 0:
                child.destroy()
        sl_slope_vars.clear()
        sl_out_labels.clear()
        sl_pt_vars.clear()
        edit = points[:-1]
        if not edit:
            return
        # Read-only mirror of what the slopes produce, in the same shape the
        # other tabs use; the slopes stay the single source of truth.
        for i, (x, y) in enumerate(edit):
            var = tk.StringVar(value=fmt_num(y))
            ttk.Label(sl_pts_frame, text="P%d" % (i + 1)).grid(
                row=i + 1, column=0, sticky="w", pady=2)
            ttk.Label(sl_pts_frame, text="%d" % round(x), anchor="e",
                      width=6).grid(row=i + 1, column=1, padx=4, pady=2)
            # Wide enough for anything the models produce; a stray long float
            # from elsewhere only clips the display, the value behind it is
            # what gets stored.
            ttk.Label(sl_pts_frame, textvariable=var, anchor="e",
                      width=11).grid(row=i + 1, column=2, padx=4, pady=2)
            sl_pt_vars.append(var)
        y1, slopes = points_to_slopes(points)
        ttk.Label(sl_frame, text="output at %d%%" % round(edit[0][0])
                  ).grid(row=0, column=0, sticky="w", pady=2)
        sl_y1_var.set(fmt_num(y1))
        pct_spin_frac(sl_frame, sl_y1_var).grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(sl_frame, text="", style="Hint.TLabel").grid(row=0, column=2)
        for k, s in enumerate(slopes):
            var = tk.StringVar(value=fmt_num(s))
            ttk.Label(sl_frame, text="slope %d%s%d%%"
                      % (round(edit[k][0]), "-", round(edit[k + 1][0]))
                      ).grid(row=k + 1, column=0, sticky="w", pady=2)
            slope_spin(sl_frame, var).grid(row=k + 1, column=1, padx=6, pady=2)
            lab = ttk.Label(sl_frame, text="", style="Hint.TLabel")
            lab.grid(row=k + 1, column=2, sticky="w", padx=(6, 0))
            sl_out_labels.append(lab)
            sl_slope_vars.append(var)
        sl_y1_var.trace_add("write", sl_mark_dirty)
        for k, v in enumerate(sl_slope_vars):
            v.trace_add("write", sl_slope_edited(k))
        if len(sl_slope_vars) > 1:
            ttk.Checkbutton(sl_frame, text="Lock slopes together",
                            variable=sl_lock, command=sl_lock_toggled
                            ).grid(row=len(slopes) + 1, column=0, columnspan=3,
                                   sticky="w", pady=(8, 0))
        ttk.Label(sl_frame, style="Hint.TLabel",
                  text="1.00 = linear. Lower is a slower response for the "
                       "same travel.", wraplength=int(250 * scale),
                  justify="left").grid(row=len(slopes) + 2, column=0,
                                       columnspan=3, sticky="w", pady=(8, 0))

    def sl_redraw(*_):
        fr = plot_frame(sl_canvas)
        if not fr:
            return
        px, py, w, h = fr
        for s in SLOPE_REFS:
            ex, ey = (100.0, 100.0 * s) if s <= 1.0 else (100.0 / s, 100.0)
            sl_canvas.create_line(px(0), py(0), px(ex), py(ey),
                                  fill="#3a4d55", dash=(1, 3))
            sl_canvas.create_text(px(ex) - 3, py(ey) - 7, text="%.1f" % s,
                                  anchor="e", fill="#5d7d8a",
                                  font=("Segoe UI", 7))
        pts = sl_points()
        ax_like = sl_axis_like()
        if pts is None or ax_like is None:
            sl_canvas.create_text(w / 2, h / 2, text="invalid number",
                                  fill="#f0b429", font=("Segoe UI", 10))
            return
        measured_scatter(sl_canvas, px, py)   # recorded sweep, if there is one
        draw_curve(sl_canvas, px, py, pts)
        for i, (x, y) in enumerate(pts):
            if i < len(sl_slope_vars) + 1:
                sl_canvas.create_oval(px(x) - 5, py(y) - 5, px(x) + 5, py(y) + 5,
                                      fill="#ffffff", outline=ACCENT, width=2)
            else:
                sl_canvas.create_oval(px(x) - 3, py(y) - 3, px(x) + 3, py(y) + 3,
                                      fill="#111114", outline="#7a7a86", width=1)
        draw_legend(sl_canvas)
        if sl_hover["v"]:
            xv, ey2 = sl_hover["v"]
            draw_tooltip(sl_canvas, px, py, w, h, xv, ey2, "pedal travel",
                         tooltip_rows(pts, xv))

    def sl_show_axis(*_):
        i = sl_axis_cb.current()
        if i < 0 or i >= len(sl["axes"]):
            return
        ax = sl["axes"][i]
        sl["axis"] = ax
        sl_lo_var.set("%d" % round(ax["lo"]))
        sl_hi_var.set("%d" % round(ax["hi"]))
        sl_build_rows(ax["points"])
        sl["dirty"] = False
        sl_refresh_outputs()
        sl_status.config(text="axis id %s   %d slopes"
                              % (ax["axis_id"], len(sl_slope_vars)))
        sl_redraw()

    def sl_show_preset(*_):
        i = sl_preset_cb.current()
        if i < 0:
            return
        p = sl["presets"][i]
        sl["preset"] = p
        sl["axes"] = parse_axes(p["blob"])
        sl_axis_cb["values"] = ["%s  (%d-%d%%)" % (a["name"], round(a["lo"]),
                                                  round(a["hi"]))
                                for a in sl["axes"]]
        want = sl.pop("want_axis", None)
        pick = None
        if want is not None:
            pick = next((n for n, a in enumerate(sl["axes"])
                         if a["axis_id"] == want), None)
        if pick is None:
            pick = next((n for n, a in enumerate(sl["axes"])
                         if a["axis_id"] == 4), 0)
        sl_axis_cb.current(pick)
        sl_show_axis()

    def sl_reload(keep_preset_id=None, keep_axis_id=None):
        try:
            sl["presets"] = load_presets()
        except Exception as exc:
            messagebox.showerror(APP_NAME,
                                 "Cannot read the DB:\n\n%s" % exc)
            return
        sl_preset_cb["values"] = preset_labels(sl["presets"])
        if sl["presets"]:
            pick = None
            if keep_preset_id is not None:
                pick = next((n for n, p in enumerate(sl["presets"])
                             if p["id"] == keep_preset_id), None)
            if pick is None:
                pick = pick_selected_preset(sl["presets"])
            if pick is None:
                pick = len(sl["presets"]) - 1
            if keep_axis_id is not None:
                sl["want_axis"] = keep_axis_id
            sl_preset_cb.current(pick)
            sl_show_preset()
        running = simpro_running()
        sl_warn.config(text=("SimPro Manager is running (%s) - close it before "
                             "saving, or it will overwrite your edit."
                             % ", ".join(running)) if running else "")

    def sl_make_linear():
        ax = sl["axis"]
        if ax and ax["points"]:
            sl_y1_var.set(fmt_num(ax["points"][0][0]))
        for v in sl_slope_vars:
            v.set("1")

    def sl_save():
        ax, p = sl["axis"], sl["preset"]
        if not ax or not p:
            return
        try:
            lo, hi = float(int(sl_lo_var.get())), float(int(sl_hi_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME,
                                 "Deadzone values must be whole percentages.")
            return
        pts = sl_points()
        if pts is None:
            messagebox.showerror(APP_NAME,
                                 "Slope values must be numbers.")
            return
        if commit_axis(p, ax, lo, hi, pts):
            sync["last"] = None          # saved state wins over any snapshot
            sl_reload(p["id"], ax["axis_id"])
            reload_db(p["id"], ax["axis_id"])
            pv_reload(p["id"], ax["axis_id"])

    ttk.Button(sl_btns, text="Reload", command=sl_reload).pack(side="left")
    ttk.Button(sl_btns, text="Make linear",
               command=sl_make_linear).pack(side="left", padx=6)
    ttk.Button(sl_btns, text="Save to DB", command=sl_save).pack(side="right")
    ttk.Button(sl_side, text="Open DB folder in Explorer",
               command=open_db_folder).pack(fill="x", pady=(10, 0))
    ttk.Label(sl_foot, style="Hint.TLabel", justify="left",
              padding=(12, 0, 12, 0),
              text="Slope is output % gained per pedal travel %. Dotted guides "
                   "run from the 1.00 baseline down in 10% steps to 0.30. The "
                   "arrows step 0.01; type for finer, it is all stored."
              ).pack(fill="x", anchor="n")

    sl_lo_var.trace_add("write", sl_mark_dirty)
    sl_hi_var.trace_add("write", sl_mark_dirty)

    def sl_on_preset_change(*_):
        if sl.get("axis"):
            sl["want_axis"] = sl["axis"]["axis_id"]
        sl_show_preset()

    sl_preset_cb.bind("<<ComboboxSelected>>", sl_on_preset_change)
    sl_axis_cb.bind("<<ComboboxSelected>>", sl_show_axis)

    def sl_point_near(ev, radius=14):
        g = getattr(sl_canvas, "hover_geom", None)
        pts = sl_points()
        if not g or pts is None:
            return None
        x0, y0, x1, y1 = g
        for i in range(min(len(sl_slope_vars) + 1, len(pts))):
            x, y = pts[i]
            cx = x0 + (x1 - x0) * x / 100.0
            cy = y1 - (y1 - y0) * y / 100.0
            if abs(ev.x - cx) <= radius and abs(ev.y - cy) <= radius:
                return i
        return None

    def sl_curve_drag(ev):
        """Dragging point i sets the slope of the segment that ends at it;
        later points keep their slopes and follow along."""
        i = sl_drag["i"]
        pts = sl_points()
        if i is None or pts is None:
            return
        x0, y0, x1, y1 = sl_canvas.hover_geom
        # Snapped before the slope is derived, so a drag lands the point on a
        # tenth rather than on whatever the pixel happened to be worth.
        v = to_tenth((y1 - ev.y) * 100.0 / (y1 - y0))
        if i == 0:
            sl_y1_var.set(fmt_num(v))
        else:
            prev_x, prev_y = pts[i - 1]
            dx = pts[i][0] - prev_x
            if dx:
                sl_slope_vars[i - 1].set(fmt_num(max(0.0, (v - prev_y) / dx)))

    def sl_motion(ev):
        v = hover_pct(sl_canvas, ev)
        sl_hover["v"] = (v, ev.y) if v is not None else None
        sl_canvas.config(cursor="sb_v_double_arrow"
                         if sl_point_near(ev) is not None else "")
        sl_redraw()

    sl_canvas.bind("<Configure>", sl_redraw)
    sl_canvas.bind("<Motion>", sl_motion)
    sl_canvas.bind("<Button-1>", lambda e: sl_drag.__setitem__("i",
                                                               sl_point_near(e)))
    sl_canvas.bind("<B1-Motion>", sl_curve_drag)
    sl_canvas.bind("<ButtonRelease-1>", lambda _e: sl_drag.__setitem__("i", None))
    sl_canvas.bind("<Leave>",
                   lambda _e: (sl_hover.__setitem__("v", None), sl_redraw()))

    sl_reload()

    # ================= Pivot curve tab =================
    # One movable point plus one slope. The curve is a cubic Hermite through
    # (0,0), the pivot and (100,100) with that slope prescribed at the pivot,
    # so it leaves the pivot equally steep in both directions. The three
    # storable points are then sampled straight off it at 25/50/75.
    pv = {"presets": [], "axes": [], "preset": None, "axis": None,
          "dirty": False}
    pv_hover = {"v": None}
    pv_drag = {"on": False}
    pv_sync = {"busy": False}
    pv_x_var, pv_y_var, pv_s_var = tk.StringVar(), tk.StringVar(), tk.StringVar()

    tab_pivot.grid_rowconfigure(0, minsize=HEADER_H, weight=0)
    tab_pivot.grid_rowconfigure(1, weight=1)
    tab_pivot.grid_rowconfigure(2, minsize=FOOTER_H, weight=0)
    tab_pivot.grid_columnconfigure(0, weight=1)

    pv_head = ttk.Frame(tab_pivot)
    pv_head.grid(row=0, column=0, sticky="nsew")
    pv_top = ttk.Frame(pv_head, padding=(12, 10, 12, 4))
    pv_top.pack(fill="x")
    ttk.Label(pv_top, text="Preset").grid(row=0, column=0, sticky="w")
    pv_preset_cb = ttk.Combobox(pv_top, state="readonly", width=38)
    pv_preset_cb.grid(row=0, column=1, padx=(6, 18))
    ttk.Label(pv_top, text="Pedal").grid(row=0, column=2, sticky="w")
    pv_axis_cb = ttk.Combobox(pv_top, state="readonly", width=24)
    pv_axis_cb.grid(row=0, column=3, padx=6)
    pv_warn = ttk.Label(pv_head, text="", style="Warn.TLabel", padding=(12, 0))
    pv_warn.pack(fill="x")

    pv_body = ttk.Frame(tab_pivot, padding=12)
    pv_body.grid(row=1, column=0, sticky="nsew")
    pv_canvas = tk.Canvas(pv_body, width=int(430 * scale), height=int(430 * scale),
                          bg="#111114", highlightthickness=1,
                          highlightbackground=GRID)
    pv_canvas.grid(row=0, column=0, sticky="nsew")
    pv_side = ttk.Frame(pv_body, padding=(18, 0, 0, 0))
    pv_side.grid(row=0, column=1, sticky="n")
    pv_body.grid_columnconfigure(0, weight=1)
    pv_body.grid_columnconfigure(1, weight=0, minsize=SIDE_W)
    pv_body.grid_rowconfigure(0, weight=1)

    pv_rng = ttk.Labelframe(pv_side, text=" Deadzone ", padding=10)
    pv_rng.pack(fill="x")
    pv_lo_var, pv_hi_var = tk.StringVar(), tk.StringVar()
    ttk.Label(pv_rng, text="Low %").grid(row=0, column=0, sticky="w")
    pct_spin(pv_rng, pv_lo_var).grid(row=0, column=1, padx=6, pady=3)
    ttk.Label(pv_rng, text="High %").grid(row=1, column=0, sticky="w")
    pct_spin(pv_rng, pv_hi_var).grid(row=1, column=1, padx=6, pady=3)

    pv_frame = ttk.Labelframe(pv_side, text=" Pivot ", padding=10)
    pv_frame.pack(fill="x", pady=(12, 0))
    ttk.Label(pv_frame, text="pedal travel %").grid(row=0, column=0, sticky="w",
                                                    pady=2)
    ttk.Spinbox(pv_frame, textvariable=pv_x_var, from_=5, to=95, increment=0.1,
                width=8, wrap=False, validate="key",
                validatecommand=vpctf).grid(row=0, column=1, padx=6, pady=2)
    ttk.Label(pv_frame, text="output %").grid(row=1, column=0, sticky="w", pady=2)
    pct_spin_frac(pv_frame, pv_y_var).grid(row=1, column=1, padx=6, pady=2)
    ttk.Label(pv_frame, text="slope at pivot").grid(row=2, column=0, sticky="w",
                                                    pady=2)
    slope_spin(pv_frame, pv_s_var).grid(row=2, column=1, padx=6, pady=2)
    pv_slope_vs = ttk.Label(pv_frame, text="", style="Hint.TLabel")
    pv_slope_vs.grid(row=2, column=2, sticky="w", padx=(6, 0))
    # Wraps a little wider than the hints on the other tabs: this panel has a
    # few pixels of width to spare and none of height, and the text has to
    # stay off the bottom button at the window's minimum size. Past ~315 the
    # label itself starts setting the panel width, which would nudge this
    # tab's chart out of line with the rest.
    ttk.Label(pv_frame, style="Hint.TLabel", justify="left",
              wraplength=int(310 * scale),
              text="You set the tangent at the pivot; beside it is what the "
                   "three stored points really do there."
              ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    pv_pts_frame = ttk.Labelframe(pv_side, text=" Stored points (from pivot) ",
                                  padding=10)
    pv_pts_frame.pack(fill="x", pady=(12, 0))
    ttk.Label(pv_pts_frame, text="pedal travel %",
              style="Hint.TLabel").grid(row=0, column=1)
    ttk.Label(pv_pts_frame, text="output %",
              style="Hint.TLabel").grid(row=0, column=2)
    pv_point_vars = []

    marker_box(pv_side).pack(fill="x", pady=(12, 0))

    pv_btns = ttk.Frame(pv_side)
    pv_btns.pack(fill="x", pady=(14, 0))

    pv_foot = ttk.Frame(tab_pivot)
    pv_foot.grid(row=2, column=0, sticky="nsew")
    pv_status = ttk.Label(pv_foot, text="", style="Hint.TLabel",
                          padding=(12, 0, 12, 10))
    pv_status.pack(fill="x", anchor="n")
    ttk.Label(pv_foot, style="Hint.TLabel", justify="left",
              padding=(12, 0, 12, 0),
              text="One point and one slope: the curve leaves the pivot equally "
                   "steep both ways, then bends to reach 0 and 100. The three "
                   "points are stored exactly as sampled, so every step of the "
                   "slope changes them and reads back as the number you set."
              ).pack(fill="x", anchor="n")

    def pv_params():
        try:
            return (float(pv_x_var.get()), float(pv_y_var.get()),
                    float(pv_s_var.get()))
        except ValueError:
            return None

    def pv_points():
        """The stored points come from the fields, so a direct edit sticks."""
        ax = pv["axis"]
        if not ax:
            return None
        out = []
        for x, yv in pv_point_vars:
            try:
                out.append((x, float(yv.get())))
            except ValueError:
                return None
        out += [ENDPOINT] * (len(ax["points"]) - len(out))
        return out

    def pv_build_rows(points):
        for child in list(pv_pts_frame.grid_slaves()):
            if int(child.grid_info()["row"]) > 0:
                child.destroy()
        pv_point_vars.clear()
        for i, (x, y) in enumerate(points[:-1]):
            yv = tk.StringVar(value=fmt_num(y))
            ttk.Label(pv_pts_frame, text="P%d" % (i + 1)).grid(
                row=i + 1, column=0, sticky="w", pady=2)
            ttk.Label(pv_pts_frame, text="%d" % round(x), anchor="e",
                      width=6).grid(row=i + 1, column=1, padx=4, pady=2)
            # Read-only: the pivot inputs are the single source of truth here.
            # Direct point editing lives on the 3-point tab, which syncs across.
            ttk.Label(pv_pts_frame, textvariable=yv, anchor="e", width=11
                      ).grid(row=i + 1, column=2, padx=4, pady=2)
            yv.trace_add("write", pv_mark_dirty)
            pv_point_vars.append((float(x), yv))

    def pv_apply_model(*_):
        """Pivot inputs regenerate the three points; a direct edit to a point
        stays put until the pivot is touched again."""
        if pv_sync["busy"]:
            return
        ax, prm = pv["axis"], pv_params()
        if not ax or prm is None:
            return
        pts = pivot_curve_points(ax["points"], *prm)
        pv_sync["busy"] = True
        try:
            for k, (_x, yv) in enumerate(pv_point_vars):
                if k < len(pts):
                    yv.set(fmt_num(pts[k][1]))
        finally:
            pv_sync["busy"] = False
        pv_mark_dirty()

    def pv_refresh_readouts():
        prm = pv_params()
        pts = pv_points()
        if prm is None or pts is None:
            return
        px, py, s = prm
        lim = pivot_slope_limit(px, py)
        # Read off the points that actually get stored rather than off the
        # model. The model's tangent is only ever an instruction for placing
        # three points; what the pedals then do between them is the curve
        # through those points, and at the pivot that is measurably less steep
        # than the tangent asked for - a 0.70 tangent lands nearer 0.84 there.
        # Taken with the same call the hover tooltip uses, so the two readings
        # of one curve can never disagree.
        here = curve_slope_at(pts, px)
        pv_slope_vs.config(text="%.2f  %s" % (here, slope_vs_linear(here)))
        ax = pv["axis"]
        pv_status.config(
            text="axis id %s   pivot model   %s"
                 % (ax["axis_id"] if ax else "-",
                    ("slope capped at %.2f - beyond that the curve would dip"
                     % lim) if s > lim + 1e-9 else
                    "max slope for this pivot: %.2f" % lim))

    # The preset has nowhere to keep the pivot itself - only the three points it
    # produced - so the parameters of the last save are remembered here, keyed
    # by preset and axis. The fit below can recover the slope exactly, but not
    # a pivot placed anywhere other than the middle control point: the model
    # has three degrees of freedom and only three points to read them from, so
    # different pivots describe the same curve equally well. Recall is only
    # trusted when the points it recorded still match the ones on disk, which
    # means an edit made anywhere else - this tool's other tabs, or SimPro -
    # falls through to the fit on its own.
    def pv_key(p, ax):
        return "%s|%s" % (p["uuid"] or p["id"], ax["axis_id"])

    def pv_remember(p, ax, pts):
        prm = pv_params()
        if prm is None:
            return
        if not isinstance(settings.get("pivot"), dict):
            settings["pivot"] = {}       # also repairs a hand-edited file
        settings["pivot"][pv_key(p, ax)] = {
            "x": prm[0], "y": prm[1], "slope": prm[2],
            "points": [y for _x, y in pts[:len(pv_point_vars)]],
        }
        save_settings(settings)

    def pv_recall(p, ax, pts, tol=1e-6):
        """The remembered pivot for this curve, or None if it has moved on."""
        store = settings.get("pivot")
        rec = store.get(pv_key(p, ax)) if isinstance(store, dict) else None
        if not isinstance(rec, dict):
            return None
        was = rec.get("points")
        now = [y for _x, y in pts[:-1]]
        if (not isinstance(was, list) or len(was) != len(now)
                or any(abs(float(a) - b) > tol for a, b in zip(was, now))):
            return None
        try:
            return float(rec["x"]), float(rec["y"]), float(rec["slope"])
        except (KeyError, TypeError, ValueError):
            return None

    def pv_describe(pts):
        """(px, py, slope) for a point list: remembered if it still fits, fitted
        otherwise. Either way the points themselves are left untouched."""
        p, ax = pv.get("preset"), pv.get("axis")
        if p and ax:
            got = pv_recall(p, ax, pts)
            if got:
                return got
        return fit_pivot_from_points(pts)

    def pv_mark_dirty(*_):
        pv["dirty"] = True
        if not sync["busy"] and not pv_sync["busy"]:
            sync["last"] = "pivot"
        pv_refresh_readouts()
        pv_redraw()

    def pv_redraw(*_):
        fr = plot_frame(pv_canvas)
        if not fr:
            return
        px_, py_, w, h = fr
        prm = pv_params()
        pts = pv_points()
        if prm is None or pts is None:
            pv_canvas.create_text(w / 2, h / 2, text="invalid number",
                                  fill="#f0b429", font=("Segoe UI", 10))
            return
        cx, cy, s = prm
        measured_scatter(pv_canvas, px_, py_)

        # what the three points can actually do
        draw_curve(pv_canvas, px_, py_, pts)

        for i, (x, y) in enumerate(pts):
            r = 5 if i < len(pts) - 1 else 3
            pv_canvas.create_oval(px_(x) - r, py_(y) - r, px_(x) + r, py_(y) + r,
                                  fill="#ffffff" if i < len(pts) - 1 else "#111114",
                                  outline=ACCENT if i < len(pts) - 1 else "#7a7a86",
                                  width=2 if i < len(pts) - 1 else 1)
        pv_canvas.create_oval(px_(cx) - 7, py_(cy) - 7, px_(cx) + 7, py_(cy) + 7,
                              outline=PIVOT_COL, width=3)

        draw_legend(pv_canvas)
        if pv_hover["v"]:
            xv, ey = pv_hover["v"]
            draw_tooltip(pv_canvas, px_, py_, w, h, xv, ey, "pedal travel",
                         tooltip_rows(pts, xv))

    def pv_show_axis(*_):
        i = pv_axis_cb.current()
        if i < 0 or i >= len(pv["axes"]):
            return
        ax = pv["axes"][i]
        pv["axis"] = ax
        # Load the stored points exactly, and set the pivot to the closest fit;
        # opening the tab must not silently reshape the curve.
        pv_sync["busy"] = True
        try:
            pv_lo_var.set("%d" % round(ax["lo"]))
            pv_hi_var.set("%d" % round(ax["hi"]))
            pv_build_rows(ax["points"])
            gx, gy, gs = pv_describe(ax["points"])
            pv_x_var.set(fmt_num(gx))
            pv_y_var.set(fmt_num(gy))
            pv_s_var.set(fmt_num(gs))
        finally:
            pv_sync["busy"] = False
        pv["dirty"] = False
        pv_refresh_readouts()
        pv_redraw()

    def pv_show_preset(*_):
        i = pv_preset_cb.current()
        if i < 0:
            return
        p = pv["presets"][i]
        pv["preset"] = p
        pv["axes"] = parse_axes(p["blob"])
        pv_axis_cb["values"] = ["%s  (%d-%d%%)" % (a["name"], round(a["lo"]),
                                                   round(a["hi"]))
                                for a in pv["axes"]]
        want = pv.pop("want_axis", None)
        pick = None
        if want is not None:
            pick = next((n for n, a in enumerate(pv["axes"])
                         if a["axis_id"] == want), None)
        if pick is None:
            pick = next((n for n, a in enumerate(pv["axes"])
                         if a["axis_id"] == 4), 0)
        pv_axis_cb.current(pick)
        pv_show_axis()

    def pv_reload(keep_preset_id=None, keep_axis_id=None):
        try:
            pv["presets"] = load_presets()
        except Exception as exc:
            messagebox.showerror(APP_NAME,
                                 "Cannot read the DB:\n\n%s" % exc)
            return
        pv_preset_cb["values"] = preset_labels(pv["presets"])
        if pv["presets"]:
            pick = None
            if keep_preset_id is not None:
                pick = next((n for n, p in enumerate(pv["presets"])
                             if p["id"] == keep_preset_id), None)
            if pick is None:
                pick = pick_selected_preset(pv["presets"])
            if pick is None:
                pick = len(pv["presets"]) - 1
            if keep_axis_id is not None:
                pv["want_axis"] = keep_axis_id
            pv_preset_cb.current(pick)
            pv_show_preset()
        running = simpro_running()
        pv_warn.config(text=("SimPro Manager is running (%s) - close it before "
                             "saving, or it will overwrite your edit."
                             % ", ".join(running)) if running else "")

    def pv_make_linear():
        pv_x_var.set("50")
        pv_y_var.set("50")
        pv_s_var.set("1")

    def pv_save():
        ax, p = pv["axis"], pv["preset"]
        if not ax or not p:
            return
        try:
            lo, hi = float(int(pv_lo_var.get())), float(int(pv_hi_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME,
                                 "Deadzone values must be whole percentages.")
            return
        pts = pv_points()
        if pts is None:
            messagebox.showerror(APP_NAME,
                                 "Pivot values must be numbers.")
            return
        if commit_axis(p, ax, lo, hi, pts):
            sync["last"] = None
            pv_remember(p, ax, pts)      # before the reload reads them back
            pv_reload(p["id"], ax["axis_id"])
            reload_db(p["id"], ax["axis_id"])
            sl_reload(p["id"], ax["axis_id"])

    ttk.Button(pv_btns, text="Reload", command=pv_reload).pack(side="left")
    ttk.Button(pv_btns, text="Make linear",
               command=pv_make_linear).pack(side="left", padx=6)
    ttk.Button(pv_btns, text="Save to DB", command=pv_save).pack(side="right")
    ttk.Button(pv_side, text="Open DB folder in Explorer",
               command=open_db_folder).pack(fill="x", pady=(10, 0))

    for _v in (pv_x_var, pv_y_var, pv_s_var):
        _v.trace_add("write", pv_apply_model)
    for _v in (pv_lo_var, pv_hi_var):
        _v.trace_add("write", pv_mark_dirty)

    def pv_on_preset_change(*_):
        if pv.get("axis"):
            pv["want_axis"] = pv["axis"]["axis_id"]
        pv_show_preset()

    pv_preset_cb.bind("<<ComboboxSelected>>", pv_on_preset_change)
    pv_axis_cb.bind("<<ComboboxSelected>>", pv_show_axis)

    def pv_near_pivot(ev, radius=16):
        g = getattr(pv_canvas, "hover_geom", None)
        prm = pv_params()
        if not g or prm is None:
            return False
        x0, y0, x1, y1 = g
        cx = x0 + (x1 - x0) * prm[0] / 100.0
        cy = y1 - (y1 - y0) * prm[1] / 100.0
        return abs(ev.x - cx) <= radius and abs(ev.y - cy) <= radius

    def pv_dragging(ev):
        """The pivot moves in both axes - X is a lever here, unlike elsewhere."""
        if not pv_drag["on"]:
            return
        x0, y0, x1, y1 = pv_canvas.hover_geom
        nx = to_tenth((ev.x - x0) * 100.0 / (x1 - x0))
        ny = to_tenth((y1 - ev.y) * 100.0 / (y1 - y0))
        pv_x_var.set(fmt_num(max(5.0, min(95.0, nx))))
        pv_y_var.set(fmt_num(ny))

    def pv_motion(ev):
        v = hover_pct(pv_canvas, ev)
        pv_hover["v"] = (v, ev.y) if v is not None else None
        pv_canvas.config(cursor="fleur" if pv_near_pivot(ev) else "")
        pv_redraw()

    pv_canvas.bind("<Configure>", pv_redraw)
    pv_canvas.bind("<Motion>", pv_motion)
    pv_canvas.bind("<Button-1>",
                   lambda e: pv_drag.__setitem__("on", pv_near_pivot(e)))
    pv_canvas.bind("<B1-Motion>", pv_dragging)
    pv_canvas.bind("<ButtonRelease-1>",
                   lambda _e: pv_drag.__setitem__("on", False))
    pv_canvas.bind("<Leave>",
                   lambda _e: (pv_hover.__setitem__("v", None), pv_redraw()))

    pv_reload()

    # ---- keep the two editors showing the same curve --------------------
    # They hold separate widgets, so the one being shown adopts whatever the
    # other was last edited to. Switching tabs is the only moment it matters,
    # since you can never see both at once.
    def editor_snapshot(which):
        if which == "points":
            st, lov, hiv, pts = state, lo_var, hi_var, cur_points()
        elif which == "pivot":
            st, lov, hiv, pts = pv, pv_lo_var, pv_hi_var, pv_points()
        else:
            st, lov, hiv, pts = sl, sl_lo_var, sl_hi_var, sl_points()
        if pts is None or not st.get("preset") or not st.get("axis"):
            return None
        try:
            lo, hi = int(lov.get()), int(hiv.get())
        except ValueError:
            return None
        return (st["preset"]["id"], st["axis"]["axis_id"], lo, hi, pts)

    def _select(cb_preset, cb_axis, st, show_p, show_a, pid, aid):
        idx = next((n for n, p in enumerate(st["presets"]) if p["id"] == pid),
                   None)
        if idx is None:
            return False
        if cb_preset.current() != idx:
            st["want_axis"] = aid
            cb_preset.current(idx)
            show_p()
        aidx = next((n for n, a in enumerate(st["axes"])
                     if a["axis_id"] == aid), None)
        if aidx is not None and cb_axis.current() != aidx:
            cb_axis.current(aidx)
            show_a()
        return True

    def apply_to_points(snap):
        pid, aid, lo, hi, pts = snap
        if not _select(preset_cb, axis_cb, state, show_preset, show_axis,
                       pid, aid):
            return
        lo_var.set("%d" % lo)
        hi_var.set("%d" % hi)
        for k, (_x, yv) in enumerate(point_vars):
            if k < len(pts):
                yv.set(fmt_num(pts[k][1]))

    def apply_to_slope(snap):
        pid, aid, lo, hi, pts = snap
        if not _select(sl_preset_cb, sl_axis_cb, sl, sl_show_preset,
                       sl_show_axis, pid, aid):
            return
        sl_lo_var.set("%d" % lo)
        sl_hi_var.set("%d" % hi)
        y1, slopes = points_to_slopes(pts)
        sl_y1_var.set(fmt_num(y1))
        for k, v in enumerate(sl_slope_vars):
            if k < len(slopes):
                v.set(fmt_num(slopes[k]))

    def apply_to_pivot(snap):
        """Points carry over exactly; the pivot is recalled or fitted, so it
        describes the incoming curve without reshaping it."""
        pid, aid, lo, hi, pts = snap
        if not _select(pv_preset_cb, pv_axis_cb, pv, pv_show_preset,
                       pv_show_axis, pid, aid):
            return
        pv_sync["busy"] = True
        try:
            pv_lo_var.set("%d" % lo)
            pv_hi_var.set("%d" % hi)
            for k, (_x, yv) in enumerate(pv_point_vars):
                if k < len(pts):
                    yv.set(fmt_num(pts[k][1]))
            gx, gy, gs = pv_describe(pts)
            pv_x_var.set(fmt_num(gx))
            pv_y_var.set(fmt_num(gy))
            pv_s_var.set(fmt_num(gs))
        finally:
            pv_sync["busy"] = False
        pv_refresh_readouts()
        pv_redraw()

    TABS = {str(tab_curve): ("points", apply_to_points),
            str(tab_pivot): ("pivot", apply_to_pivot),
            str(tab_slope): ("slope", apply_to_slope)}

    def on_tab_changed(*_):
        if sync["busy"]:
            return
        entry = TABS.get(nb.select())
        if not entry or sync["last"] in (None, entry[0]):
            return
        sync["busy"] = True
        try:
            snap = editor_snapshot(sync["last"])
            if snap:
                entry[1](snap)
        finally:
            sync["busy"] = False

    nb.bind("<<NotebookTabChanged>>", on_tab_changed)

    # ================= Live / Verify tab =================

    tab_live.grid_rowconfigure(0, minsize=HEADER_H, weight=0)
    tab_live.grid_rowconfigure(1, weight=1)
    tab_live.grid_rowconfigure(2, minsize=FOOTER_H, weight=0)
    tab_live.grid_columnconfigure(0, weight=1)

    lv_head = ttk.Frame(tab_live)
    lv_head.grid(row=0, column=0, sticky="nsew")
    lv_top = ttk.Frame(lv_head, padding=(12, 10, 12, 4))
    lv_top.pack(fill="x")
    ttk.Label(lv_top, text="Pedal").pack(side="left")
    lv_axis_cb = ttk.Combobox(lv_top, state="readonly", width=26)
    lv_axis_cb.pack(side="left", padx=(6, 16))
    lv_dev = ttk.Label(lv_top, text="", style="Hint.TLabel")
    lv_dev.pack(side="left")
    # Stands in for the editors' warning line so the header is the same height.
    ttk.Label(lv_head, text="", style="Hint.TLabel",
              padding=(12, 0)).pack(fill="x")

    # Packed further down, after the bottom inspector strip has claimed its
    # space - an expanding widget packed first leaves nothing for later ones.
    lv_body = ttk.Frame(tab_live, padding=12)
    lv_canvas = tk.Canvas(lv_body, width=int(430 * scale), height=int(430 * scale),
                          bg="#111114", highlightthickness=1,
                          highlightbackground=GRID)
    lv_canvas.grid(row=0, column=0, sticky="nsew")

    lv_side = ttk.Frame(lv_body, padding=(18, 0, 0, 0))
    lv_side.grid(row=0, column=1, sticky="n")
    lv_body.grid_columnconfigure(0, weight=1)
    lv_body.grid_columnconfigure(1, weight=0, minsize=SIDE_W)
    lv_body.grid_rowconfigure(0, weight=1)

    dev_f = ttk.Labelframe(lv_side, text=" Device ", padding=10)
    dev_f.pack(fill="x")
    lv_dev_cb = ttk.Combobox(dev_f, state="readonly", width=40)
    lv_dev_cb.pack(fill="x")
    lv_dev_btn = ttk.Button(dev_f, text="Rescan devices")
    lv_dev_btn.pack(fill="x", pady=(6, 0))

    pair_f = ttk.Labelframe(lv_side, text=" Pedal pairing ", padding=10)
    pair_f.pack(fill="x", pady=(12, 0))
    lv_pair = ttk.Label(pair_f, text="", style="Hint.TLabel", justify="left")
    lv_pair.pack(anchor="w")
    lv_ident_btn = ttk.Button(pair_f, text="Identify this pedal")
    lv_ident_btn.pack(fill="x", pady=(8, 0))

    read_f = ttk.Labelframe(lv_side, text=" Live ", padding=10)
    read_f.pack(fill="x", pady=(12, 0))
    lv_read = ttk.Label(read_f, text="-", justify="left",
                        font=("Consolas", int(10 * scale)))
    lv_read.pack(anchor="w")

    sweep_f = ttk.Labelframe(lv_side, text=" Measured curve ", padding=10)
    sweep_f.pack(fill="x", pady=(12, 0))
    lv_rec_btn = ttk.Button(sweep_f, text="Record sweep")
    lv_rec_btn.pack(fill="x")
    def clear_sweep():
        live["samples"].clear()
        live["stats"].clear()
        live["regress"] = None
        lv_regress.config(text="")
        lv_stats.config(text="Record a slow full press and release.")
        draw_live()
        redraw()
        sl_redraw()
        pv_redraw()

    ttk.Button(sweep_f, text="Clear",
               command=clear_sweep).pack(fill="x", pady=(6, 0))
    lv_stats = ttk.Label(sweep_f, text="Record a slow full press and release.",
                         style="Hint.TLabel", justify="left",
                         wraplength=int(330 * scale))
    lv_stats.pack(anchor="w", pady=(8, 0))
    lv_regress = ttk.Label(sweep_f, text="", style="Warn.TLabel", justify="left",
                           wraplength=int(330 * scale))
    lv_regress.pack(anchor="w", pady=(6, 0))

    marker_box(lv_side).pack(fill="x", pady=(12, 0))

    lv_foot = ttk.Frame(tab_live)
    lv_foot.grid(row=2, column=0, sticky="nsew")
    insp = ttk.Labelframe(lv_foot, text=" HID input report ", padding=(10, 6))
    insp.pack(fill="x", padx=12, pady=(0, 10))
    lv_body.grid(row=1, column=0, sticky="nsew")
    lv_hex = tk.Text(insp, height=2, bg="#111114", fg="#8a8a95",
                     insertbackground=FG, relief="flat", highlightthickness=0,
                     font=("Consolas", int(10 * scale)))
    lv_hex.pack(fill="x")
    lv_hex.tag_configure("chg", foreground="#f0b429")
    lv_hex.tag_configure("out", foreground=ACCENT)
    lv_hex.tag_configure("inp", foreground=MEAS)
    lv_hex.configure(state="disabled")

    # ---- helpers --------------------------------------------------------
    def lv_sel_axis():
        i = lv_axis_cb.current()
        return live["axes"][i] if 0 <= i < len(live["axes"]) else None

    def refresh_live_axes():
        live["axes"] = list(state["axes"])
        lv_axis_cb["values"] = ["%s  (%g-%g%%)" % (a["name"], a["lo"], a["hi"])
                                for a in live["axes"]]
        if live["axes"]:
            pick = next((n for n, a in enumerate(live["axes"])
                         if a["axis_id"] == 4), 0)
            lv_axis_cb.current(pick)
        live["samples"].clear()
        live["stats"].clear()
        update_pair_label()
        draw_live()

    live_refresh["fn"] = refresh_live_axes

    def update_pair_label():
        ax = lv_sel_axis()
        if not ax:
            lv_pair.config(text="no preset loaded")
            return
        m = live["map"].get(ax["axis_id"])
        if m:
            oname = GD_USAGE_NAMES.get(m["out_usage"],
                                       "0x%02x" % m["out_usage"])
            where = ("output %s%s" % (oname,
                     "  (byte %d)" % m["out_off"]
                     if m.get("out_off") is not None else ""))
            if m.get("in_off") is not None:
                lv_pair.config(text="%s  <-  input at byte %d%s"
                                    % (where, m["in_off"],
                                       "\n" + m["note"] if m.get("note") else ""))
            else:
                lv_pair.config(text="%s\nno separate pre-curve input found -\n"
                                    "sweeps are unavailable for this pedal"
                                    % where)
        else:
            lv_pair.config(text="not identified yet - click below, then press\n"
                                "this pedal through its full travel")

    def draw_live():
        c = lv_canvas
        fr = plot_frame(c)
        if not fr:
            return
        px, py, w, h = fr

        reg = live.get("regress")
        if reg:
            _x0, y0, _x1, y1 = c.hover_geom
            c.create_rectangle(px(reg[0]), y0, px(reg[1]), y1,
                               fill="#f0b429", outline="", stipple="gray12")

        measured_scatter(c, px, py)
        ax = lv_sel_axis()
        if ax:
            draw_curve(c, px, py, ax["points"])
            # The same control points the editor tabs show, on the same axis.
            for i, (x, y) in enumerate(ax["points"]):
                if i < len(ax["points"]) - 1:
                    c.create_oval(px(x) - 5, py(y) - 5, px(x) + 5, py(y) + 5,
                                  fill="#ffffff", outline=ACCENT, width=2)
                else:
                    c.create_oval(px(x) - 3, py(y) - 3, px(x) + 3, py(y) + 3,
                                  fill="#111114", outline="#7a7a86", width=1)

        cur = live_point()
        if cur:
            cx = curve_x_of(cur[0], ax["lo"], ax["hi"]) if ax else cur[0]
            c.create_oval(px(cx) - 5, py(cur[1]) - 5,
                          px(cx) + 5, py(cur[1]) + 5,
                          outline="#ffffff", width=2)

        # Both entries always: this tab plots live output whether or not a
        # sweep has been recorded.
        draw_legend(c, measured=True)
        if hover["live"] and ax:
            xv, ey = hover["live"]
            # curve_eval, not predicted_output: this chart's x is already the
            # curve domain, so remapping it through the deadzone a second time
            # put the marker off the drawn curve.
            draw_tooltip(c, px, py, w, h, xv, ey, "pedal travel",
                         tooltip_rows(ax["points"], xv))

    def live_point():
        """Current (input%, output%) for the selected pedal, if paired."""
        ax = lv_sel_axis()
        r = live["reader"]
        if not ax or not live["raw"] or not r:
            return None
        m = live["map"].get(ax["axis_id"])
        if not m or m.get("in_off") is None:
            return None
        d = r.decode(live["raw"])
        if not d or m["out_usage"] not in d:
            return None
        return (r.in_pct(r.u16(live["raw"], m["in_off"])),
                r.out_pct(m["out_usage"], d[m["out_usage"]]))

    # ---- identify -------------------------------------------------------
    # Movement-based pairing: the user presses one pedal and whatever moves
    # is that pedal. Nothing here assumes how many pedals there are or where
    # their bytes live, which is what lets the tool work on a set it has
    # never seen.
    def start_identify():
        ax = lv_sel_axis()
        if not ax:
            return
        if not (live["reader"] and live["reader"].running):
            messagebox.showerror("Live", "Not connected to the pedals.")
            return
        live["ident"] = {"t0": time.time(), "samples": [], "axis": ax}
        lv_ident_btn.config(state="disabled")

    def _pearson(xs, ys):
        n = len(xs)
        if n < 3:
            return 0.0
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        sx = sum((a - mx) ** 2 for a in xs) ** 0.5
        sy = sum((b - my) ** 2 for b in ys) ** 0.5
        return sxy / (sx * sy) if sx > 0 and sy > 0 else 0.0

    def finish_identify():
        d = live["ident"]
        live["ident"] = None
        lv_ident_btn.config(state="normal")
        r = live["reader"]
        samples = d["samples"]           # [(outs {usage: raw}, wins {off: u16})]
        if not r or len(samples) < 20:
            lv_pair.config(text="no data received - try again")
            return

        # 1. Which output axis is this pedal? The one with the most travel,
        #    provided it moved at all and nothing else moved comparably -
        #    two moving axes means two pedals were pressed, and guessing
        #    between them would silently mis-pair.
        usages = [u for u, _lm, _rid in r.out_axes]
        travel = {u: max(s[0].get(u, 0) for s in samples)
                     - min(s[0].get(u, 0) for s in samples) for u in usages}
        winner = max(travel, key=travel.get)
        others = [travel[u] for u in usages if u != winner]
        if travel[winner] < MIN_TRAVEL_FRAC * r.out_max(winner):
            lv_pair.config(text="no clear movement detected - try again and\n"
                                "press the pedal through its full travel")
            return
        if others and max(others) > 0.5 * travel[winner]:
            lv_pair.config(text="more than one axis moved - try again and\n"
                                "press only this pedal")
            return
        out_vals = [s[0].get(winner, 0) for s in samples]

        # 2. Where in the report is the matching pre-curve input? Consider
        #    every u16 window that travelled, split them into mirrors of the
        #    output (byte-for-byte equal: the output's own location, or an
        #    input while the curve is identity) and true candidates, then
        #    rank candidates by correlation with the output. A byte-swapped
        #    or misaligned window travels a lot but correlates poorly, which
        #    is what keeps this ranking honest.
        offs = sorted(samples[0][1])
        win_vals = {o: [s[1].get(o, 0) for s in samples] for o in offs}
        moved = [o for o in offs
                 if max(win_vals[o]) - min(win_vals[o])
                 >= MIN_TRAVEL_FRAC * r.in_full]
        dups, cands = [], []
        for o in moved:
            eq = sum(1 for w, v in zip(win_vals[o], out_vals) if w == v)
            (dups if eq >= 0.98 * len(samples) else cands).append(o)
        best, best_r = None, 0.0
        for o in cands:
            cr = _pearson(win_vals[o], out_vals)
            if cr > best_r:
                best, best_r = o, cr
        # The output's own bytes: the lowest mirror offset (vendor data
        # follows the standard axes in every layout seen so far).
        out_off = min(dups) if dups else None

        note = None
        if best is not None and best_r >= 0.9:
            in_off = best
        elif len(dups) > 1:
            # Input mirrors output exactly - a linear curve with no deadzone.
            # The later mirror is taken for the input on the same
            # vendor-data-follows-axes grounds as out_off above.
            in_off = max(dups)
            note = ("input mirrors output (linear curve?) - re-identify\n"
                    "with a curve loaded to pin it down for sure")
        else:
            in_off = None

        live["map"][d["axis"]["axis_id"]] = {"out_usage": winner,
                                             "out_off": out_off,
                                             "in_off": in_off, "note": note}
        if live["devkey"]:
            live["maps"][live["devkey"]] = live["map"]
            save_axis_map(live["maps"])
        update_pair_label()
        draw_live()

    lv_ident_btn.config(command=start_identify)

    # ---- sweep ----------------------------------------------------------
    def stop_record(timed_out=False):
        """End a recording and work up the stats. Shared by the button and the
        timeout, so a sweep can only ever be finished one way."""
        live["rec"] = False
        live["rec_left"] = None
        lv_rec_btn.config(text="Record sweep")
        compute_stats()
        if timed_out:
            # Appended rather than shown alone: compute_stats has just written
            # the numbers for whatever was captured, and those still stand.
            lv_stats.config(text="%s\n\nstopped automatically after %ds."
                                 % (lv_stats.cget("text"), SWEEP_TIMEOUT))

    def toggle_record():
        ax = lv_sel_axis()
        if not ax:
            return
        if live["rec"]:
            stop_record()
            return
        m = live["map"].get(ax["axis_id"])
        if not m:
            messagebox.showerror("Live", "Identify this pedal first.")
            return
        if m.get("in_off") is None:
            messagebox.showerror(
                "Live", "No pre-curve input was found for this pedal, so a "
                        "sweep cannot be recorded. Re-identify with a "
                        "non-linear curve loaded.")
            return
        live["samples"].clear()
        live["stats"].clear()
        live["regress"] = None
        # Pin the deadzone in force now, so later edits cannot slide the
        # recorded data along the x-axis when it is mapped for display.
        live["sweep_lohi"] = (ax["lo"], ax["hi"])
        lv_regress.config(text="")
        live["rec"] = True
        live["rec_t0"] = time.time()
        live["rec_left"] = None
        lv_rec_btn.config(text="Stop recording  (%ds)" % SWEEP_TIMEOUT)
        lv_stats.config(text="recording - press slowly through full travel,\n"
                             "then release (stops on its own after %ds)"
                             % SWEEP_TIMEOUT)

    lv_rec_btn.config(command=toggle_record)

    def compute_stats():
        ax = lv_sel_axis()
        s = sweep_in_curve_domain()          # same axis the charts use
        if not ax or len(s) < 20:
            lv_stats.config(text="not enough samples - record a slower sweep")
            return
        xs = [p[0] for p in s]
        ys = [p[1] for p in s]
        cov = max(xs) - min(xs)
        errs = [abs(y - curve_eval(ax["points"], x, "catmull")) for x, y in s]
        m = live["map"].get(ax["axis_id"])
        full = (live["reader"].out_max(m["out_usage"])
                if live["reader"] and m else 4095.0)
        steps = len({round(y * full / 100.0) for y in ys})
        lines = [
            "%d samples, travel covered %.1f%%" % (len(s), cov),
            "output reached %.1f%% .. %.1f%%" % (min(ys), max(ys)),
            "distinct output steps: %d" % steps,
            "",
            "error vs predicted (spline):",
            "  mean %.2f%%   max %.2f%%"
            % (sum(errs) / len(errs), max(errs)),
        ]
        if cov < 80:
            lines.append("")
            lines.append("WARNING: sweep covered only %.0f%% of travel;" % cov)
            lines.append("the rest of the curve is unverified.")
        lv_stats.config(text="\n".join(lines))
        live["stats"] = lines

        # A curve whose control points only ever rise can still dip between
        # them, because the spline undershoots ahead of a steep final segment.
        reg = find_regression(s)             # s is already curve-domain
        live["regress"] = reg
        if reg:
            lv_regress.config(
                text="THROTTLE REVERSES\nBetween %.0f%% and %.0f%% travel the "
                     "output falls %.1f%% - pressing harder there gives you "
                     "less, not more. Raise the middle slopes to remove the "
                     "dip." % (reg[0], reg[1], reg[2]))
        else:
            lv_regress.config(text="")
        redraw()        # the editor charts overlay the sweep as well
        sl_redraw()

    # ---- inspector ------------------------------------------------------
    def update_hex():
        raw, prev, r = live["raw"], live["prev"], live["reader"]
        if not raw or not r:
            return
        # Byte colouring follows the identified pairings: only Identify knows
        # where the axes live in the report, so un-identified pedals simply
        # show up through the change highlighting.
        outs_b, ins_b = set(), set()
        for m in live["map"].values():
            if m.get("out_off") is not None:
                outs_b.update((m["out_off"], m["out_off"] + 1))
            if m.get("in_off") is not None:
                ins_b.update((m["in_off"], m["in_off"] + 1))
        lv_hex.configure(state="normal")
        lv_hex.delete("1.0", "end")
        for i, b in enumerate(raw):
            tag = None
            if prev and i < len(prev) and prev[i] != b:
                tag = "chg"
            elif i in outs_b:
                tag = "out"
            elif i in ins_b:
                tag = "inp"
            lv_hex.insert("end", "%02x " % b, (tag,) if tag else ())
        d = r.decode(raw)
        if d:
            lv_hex.insert("end", "\nout %s    %.0f Hz"
                          % (" ".join("%s=%4d"
                                      % (GD_USAGE_NAMES.get(u, "0x%02x" % u), v)
                                      for u, v in sorted(d.items())),
                             live["rate"]))
        lv_hex.configure(state="disabled")

    # ---- pump -----------------------------------------------------------
    def pump():
        r = live["reader"]
        if r and r.running:
            batch = r.drain()
            if batch:
                live["n"] += len(batch)
                now = time.perf_counter()
                if live["t0"]:
                    dt = now - live["t0"]
                    if dt > 0.5:
                        live["rate"] = live["n"] / dt
                        live["n"] = 0
                        live["t0"] = now
                else:
                    live["t0"] = now
                live["prev"] = live["raw"]
                live["raw"] = batch[-1][1]

                ax = lv_sel_axis()
                m = live["map"].get(ax["axis_id"]) if ax else None
                rec_ok = m and m.get("in_off") is not None
                for _ts, raw in batch:
                    d = r.decode(raw)
                    if not d:
                        continue
                    ident = live["ident"]
                    if ident:
                        ident["samples"].append((d, r.windows(raw)))
                    if live["rec"] and rec_ok and m["out_usage"] in d:
                        live["samples"].append(
                            (r.in_pct(r.u16(raw, m["in_off"])),
                             r.out_pct(m["out_usage"], d[m["out_usage"]])))

                if rec_ok:
                    d = r.decode(live["raw"])
                    if d and m["out_usage"] in d:
                        in_raw = r.u16(live["raw"], m["in_off"])
                        out_raw = d[m["out_usage"]]
                        i_pct = r.in_pct(in_raw)
                        o_pct = r.out_pct(m["out_usage"], out_raw)
                        exp = predicted_output(ax, i_pct, "catmull")
                        lv_read.config(
                            text="travel           %6.2f %%  (raw %4d)\n"
                                 "output           %6.2f %%  (raw %4d)\n"
                                 "expected(spline) %6.2f %%  diff %+.2f"
                                 % (i_pct, in_raw, o_pct, out_raw,
                                    exp, o_pct - exp))
                elif m:
                    lv_read.config(text="no pre-curve input for this pedal")
                else:
                    lv_read.config(text="pedal not identified")

                now2 = time.time()
                if now2 - live["hexat"] > 0.1:
                    live["hexat"] = now2
                    update_hex()
                if now2 - live["drawat"] > 0.08:
                    live["drawat"] = now2
                    draw_live()

        # Both countdowns sit outside the batch guard so they still finish if
        # the device goes quiet mid-identify or mid-sweep - a recording left
        # running by a disconnected pedal is exactly the case the sweep
        # timeout exists for.
        if live["ident"]:
            left = 6.0 - (time.time() - live["ident"]["t0"])
            if left <= 0:
                finish_identify()
            else:
                lv_pair.config(text="press the pedal fully... %.1fs" % left)
        if live["rec"]:
            left = SWEEP_TIMEOUT - (time.time() - live["rec_t0"])
            if left <= 0:
                stop_record(timed_out=True)
            else:
                # Only relabel on a whole-second change; pump runs at ~33 Hz
                # and rewriting the button text that often is pure churn.
                secs = int(left) + 1
                if secs != live["rec_left"]:
                    live["rec_left"] = secs
                    lv_rec_btn.config(text="Stop recording  (%ds)" % secs)
        root.after(30, pump)

    def refresh_devices(keep=None):
        """Rescan HID devices, rebuild the dropdown. -> the entry to use.

        Preference order: the caller's key, then the remembered choice, then
        the best-scored candidate - so a saved override survives restarts but
        a missing device degrades to the auto-pick instead of an error.
        """
        live["devices"] = enum_pedal_candidates()
        lv_dev_cb["values"] = [device_label(d) for d in live["devices"]]
        if not live["devices"]:
            lv_dev_cb.set("")
            return None
        want = keep or settings.get("device")
        idx = next((n for n, d in enumerate(live["devices"])
                    if device_key(d) == want), 0)
        lv_dev_cb.current(idx)
        return live["devices"][idx]

    def connect(info=None):
        """(Re)connect the live reader; auto-picks a device when none given."""
        old = live["reader"]
        if old:
            if (info is not None and old.running and old.info
                    and device_key(old.info) == device_key(info)):
                return                   # already on that device
            old.stop()
            live["reader"] = None
        live["map"], live["devkey"] = {}, None
        # Abandoned rather than finished: the samples belong to the device
        # being left, so there are no stats worth working up.
        if live["rec"]:
            live["rec"] = False
            live["rec_left"] = None
            lv_rec_btn.config(text="Record sweep")
        live["samples"].clear()
        live["stats"].clear()
        live["regress"] = None
        r = HidReader(info)
        if r.start():
            live["reader"] = r
            key = device_key(r.info)
            live["devkey"] = key
            # First sight of the legacy P1000 since the cache went
            # per-device: adopt the old pairing instead of asking for a
            # re-identify it does not need.
            if (key not in live["maps"] and live["legacy"]
                    and all(t in r.path.lower() for t in LEGACY_VIDPID)):
                live["maps"][key] = migrate_legacy_map(live["legacy"])
                live["legacy"] = {}
                save_axis_map(live["maps"])
            live["map"] = live["maps"].get(key, {})
            lv_dev.config(text="connected  -  %s" % device_label(r.info))
        else:
            live["reader"] = None
            lv_dev.config(text="not connected: %s" % r.error)
        update_pair_label()
        draw_live()

    def on_device_selected(*_):
        i = lv_dev_cb.current()
        if 0 <= i < len(live["devices"]):
            info = live["devices"][i]
            settings["device"] = device_key(info)
            save_settings(settings)
            connect(info)

    def rescan_devices():
        info = refresh_devices(live["devkey"])
        if info and not (live["reader"] and live["reader"].running):
            connect(info)

    lv_dev_cb.bind("<<ComboboxSelected>>", on_device_selected)
    lv_dev_btn.config(command=rescan_devices)

    lv_axis_cb.bind("<<ComboboxSelected>>",
                    lambda *_: (live["samples"].clear(), live["stats"].clear(),
                                update_pair_label(), draw_live()))

    def live_motion(ev):
        v = hover_pct(lv_canvas, ev)
        hover["live"] = (v, ev.y) if v is not None else None
        draw_live()

    # Every tab's chart, kept square together now they all exist. Each canvas
    # already redraws on its own <Configure>, so the resize carries through.
    for _c in (canvas, sl_canvas, pv_canvas, lv_canvas):
        keep_square(_c)

    lv_canvas.bind("<Configure>", lambda _e: draw_live())
    lv_canvas.bind("<Motion>", live_motion)
    lv_canvas.bind("<Leave>",
                   lambda _e: (hover.__setitem__("live", None), draw_live()))

    # Bound here rather than at creation because these settings are on every
    # chart, so one edit has to repaint all four - including the tabs that are
    # hidden, which would otherwise show stale lines when you switch to them.
    # Debounced, because a trace fires on every keystroke while a value is
    # being typed and only the value you settle on is worth writing.
    prefs_save = {"job": None}

    def persist_prefs():
        prefs_save["job"] = None
        settings["markers"] = [v.get().strip() for v in marker_vars]
        settings["slope_colour"] = bool(slope_colour.get())
        save_settings(settings)

    def display_changed(*_):
        if prefs_save["job"] is not None:
            root.after_cancel(prefs_save["job"])
        prefs_save["job"] = root.after(400, persist_prefs)
        redraw()
        sl_redraw()
        pv_redraw()
        draw_live()

    for _mv in marker_vars:
        _mv.trace_add("write", display_changed)
    slope_colour.trace_add("write", display_changed)

    connect(refresh_devices())
    refresh_live_axes()
    pump()
    nb.select(tab_live if "--live" in sys.argv else
              tab_slope if "--slope" in sys.argv else
              tab_pivot if "--pivot" in sys.argv else tab_curve)





    def on_close():
        if prefs_save["job"] is not None:    # flush a debounced display edit
            root.after_cancel(prefs_save["job"])
            persist_prefs()
        if live["reader"]:
            live["reader"].stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # Tk's root size request under-reports the real content width here, so use a
    # fixed default with slack for the four float64 entry columns. Resizable.
    # The height has to clear the tallest side panel outright: it is packed, so
    # anything that does not fit is simply cut off (the last button losing its
    # label first). Text is scaled by scale * 1.3333 while these constants are
    # scaled by scale alone, so it needs headroom beyond the scale-1 layout.
    # 950 not 900: the Pivot tab carries one section more than the others, and
    # +25 for the slope-colour row added to the Chart display box.
    root.geometry("%dx%d" % (int(1120 * scale), int(975 * scale)))
    root.minsize(int(900 * scale), int(845 * scale))
    root.mainloop()


def start_report(kind):
    """Send the text modes somewhere they will actually be seen. -> path|None.

    The packaged build targets the GUI subsystem, so that double-clicking it
    does not raise a console window nobody asked for. That leaves it with no
    stdout at all - print() would fail on None - and borrowing the launching
    console only works when it was launched from one, which is exactly the
    case you cannot count on. Writing the report to a file in the program's
    own folder works however it was started, and leaves something to paste
    into a bug report, which is what these modes are for.

    From source, stdout is right there and nothing is redirected.
    """
    if not getattr(sys, "frozen", False):
        return None
    path = os.path.join(app_dir(), "%s-report.txt" % kind)
    sys.stdout = sys.stderr = open(path, "w", encoding="utf-8")
    return path


def finish_report(path):
    """Close the report and show it, so the run does not end in silence."""
    if not path:
        return
    try:
        sys.stdout.close()
    finally:
        sys.stdout = sys.stderr = None
    try:
        os.startfile(path)               # default text editor; best effort
    except Exception:
        pass


if __name__ == "__main__":
    if "--version" in sys.argv:
        line = "%s %s" % (APP_NAME, APP_VERSION)
        if getattr(sys, "frozen", False):
            ctypes.windll.user32.MessageBoxW(None, line, APP_NAME, 0x40)
        else:
            print(line)
        sys.exit(0)
    if "--hidtest" in sys.argv:
        secs = 15
        for a in sys.argv[1:]:
            if a.isdigit():
                secs = int(a)
        report = start_report("hidtest")
        try:
            rc = hidtest(secs)
        finally:
            finish_report(report)
        sys.exit(rc)
    if not os.path.exists(DB_PATH):
        sys.exit("SimPro database not found at:\n  %s" % DB_PATH)
    if "--selftest" in sys.argv:
        report = start_report("selftest")
        try:
            rc = selftest()
        finally:
            finish_report(report)
        sys.exit(rc)
    launch_gui()
