# Pedal Curve Lab

**A precise curve editor for Simagic pedals — the parts missing from SimPro Manager.**

SimPro Manager only lets you *drag* control points around a small graph.

You can't type a number. You can't write down what you set. You can't check what the
pedals actually do afterwards. You can't hand your curve to anyone else.

You can save it, of course. You just can't say what's in it — or repeat it on purpose, or
change it by a known amount.

So tuning is nudge, drive, guess.

Pedal Curve Lab adds all three. It reads and writes SimPro Manager's own preset database,
so curves you build here show up there as normal presets.

![Pivot curve tab: the stored curve in red and the measured pedal response in cyan,
overlaid on one graph, with the three stored points, deadzone trim and per-point slope
readouts alongside](docs/pivot-curve.png)

*Pivot mode, mid-edit. Red is the curve being edited. Cyan is a sweep recorded from the
pedals beforehand — the response I was already driving — left on the chart as a reference,
so I can see exactly what I'm changing and by how much. Hover anywhere and you get both:
at 60.56% travel the new curve gives 42.87% output, where the old one measured 48.97%.*

**▶ Quick start video (7 min):** https://www.youtube.com/watch?v=5uPeqN9SkiE

---

## What you get

**Exact numbers.** Type the value you want. No dragging, no eyeballing, no "close enough".
The preset stores a float64 — SimPro's editor snaps to whatever pixel you dropped the
point on.

**Settings you can rebuild — or send to someone.** A curve is three numbers. Write them
down and you can recreate it any time: months later, on a fresh install, after a firmware
update. Paste them to someone else and they get your curve exactly, not an approximation
of it.

**Modes that match the problem.** Sometimes you're calming a twitchy centre. Sometimes you
want the whole curve lower without changing its shape. Those are different jobs. So there
are three ways in, and you pick whichever matches how you're thinking about it.

**Proof it worked.** Press the pedal. The Live / Verify tab reads the pedal's own HID
report and draws what your pedals really did, on top of the curve you designed. No guessing
whether the save landed. Record a sweep *before* you edit and that trace becomes your
reference — you're changing a known response, not a blank graph.

**A safety net.** Every save backs up `user.db` first. Only the axis you changed gets
re-encoded — every other byte of the preset is copied across untouched.

## Why you might want it

The obvious case is **Assetto Corsa Rally's twitchy throttle**.

On gravel and snow the default throttle model gives you almost no usable resolution in the
range that matters. RWD cars especially just light up the rears and step out.

This lets you flatten the throttle response around the point of rapid power increase —
around 44% of output — so you get better modulation both on-power and when lifting. *Pivot*
mode was built for exactly this.

It works just as well for brake curves. Or anything else where you want a specific
response, rather than a hand-drawn approximation of one.

## Get it

Grab the zip from [Releases](../../releases), unzip anywhere, run `pedal-curve-lab.exe`.

No installer, no Python, no dependencies. Everything it writes — settings, DB backups —
stays in that folder. Move it about or delete it and you're back to nothing.

Two habits worth keeping:

- **Close SimPro Manager before saving.** It holds its own copy of the preset and writes
  that back over yours on exit. The tool warns you and offers to close it.
- **Re-select the preset in SimPro Manager after saving.** That's what pushes it to the
  pedals.

## Before you rely on it

Built and tested against a **P1000**. Nothing is hardcoded to it, so other Simagic sets
such as the **P2000** should work — but I haven't had one to try. See
[hardware support](#hardware-support) for what "not hardcoded" actually means here.

**Windows only.** It reads SimPro Manager's SQLite database and talks to the pedals over
the Windows HID API.

Not affiliated with, endorsed by, or supported by Simagic. It writes to SimPro Manager's
database. There's a backup before every write, but the risk is yours.

---
---

# Technical documentation

## The three edit modes

All three decide the same three numbers. They differ in what you hold still while you
work.

- **3-point** — set the output at 25/50/75% travel directly.
- **Pivot** — place one point and set how steep the curve runs through it. The curve leaves
  the pivot equally steep both ways, then bends to reach 0 and 100. Best for fixing the
  twitchy throttle in Assetto Corsa Rally.
- **Slope** — set the gradient of each segment (1.00 = linear, lower = slower).

**Deadzone (low/high travel trim)** is edited alongside the curve in every mode.

### Why the tabs show what the points actually deliver

Simagic presets only hold three points: one output value each at 25%, 50% and 75% travel.
The travel positions are fixed, and the ends are always 0 and 100%.

That's their format, not a limit this tool imposes. Every mode here is just a different way
of deciding those three values, and the pedals interpolate between them.

Which is why the Pivot and Slope tabs show you what the stored points *actually deliver*,
next to the figure you asked for. In the screenshot above, a requested pivot slope of 0.59
is delivered as 0.79 by the three points that can actually be stored.

## Measuring what the pedals do

The Live / Verify tab reads the pedal's HID report. That report carries both the raw
pre-curve input and the post-curve output the game sees.

Record a press-and-release sweep and the measured response is overlaid on the curve you
designed.

You have to identify the pedal first — click *identify this pedal*, then press it, so the
tool knows which bytes to watch.

That overlay is good for two different jobs:

- **Verify.** Save a curve, push it to the pedals, sweep the pedal, confirm the pedals are really doing what you
  asked for.
- **Reference.** Sweep *before* you change anything, then leave that trace on the chart
  while you edit. Now you're not editing against a blank graph — you're editing against
  the response you've actually been driving, and you can read the difference at any point
  on the travel. This is the screenshot above.

*Clear* removes the measured trace when it stops being useful and starts blocking the view.

## Hardware support

The device is auto-detected (and overridable). The report layout is read from the device's
own descriptor. The pre-curve input bytes are found by watching the pedals move.

So nothing is tied to a specific model. But detection working on hardware I've never
touched is a reasonable expectation, not a tested fact — if it finds your pedals, or if it
doesn't, [open an issue](../../issues).

## From source

Needs Python 3.8+ and nothing else — standard library, tkinter, ctypes. To run it without
any of the below: `python pedal-curve-lab.py`.

The `make` targets need two things on Windows: **Git for Windows** (whose shell runs the
recipes — the Makefile finds it for you, so any terminal will do) and **`make`** itself,
which is *not* part of Git. GnuWin32, `choco install make`, Scoop or MSYS2 all work.

```
make run       # run it
make test      # round-trip checks on your own presets
make setup     # venv + PyInstaller, only needed for building
make build     # -> dist/pedal-curve-lab-vX.Y.Z-win64.zip
```

## Something misbehaving?

There's a `pedal-curve-lab.log` beside the program recording what it did — every save with
the values written and where the backup went, the device it found, and anything that
failed.

*Open log file* on the Live / Verify tab brings it up. Attaching it to an issue says far
more than a description can.

For deeper digging, two flags each leave a `-report.txt` beside the program:

- `--selftest` — does your preset database parse?
- `--hidtest` — what do the pedals actually report?

## License

[AGPL-3.0](LICENSE)
