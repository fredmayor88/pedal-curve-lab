# Pedal Curve Lab

A precise curve editor for **Simagic pedals** — the bits missing from **SimPro
Manager**:

- SimPro only lets you **drag** control points around a small graph, so your
  curve ends up being whatever value the drag happened to land on.
- There's **no way to type an exact number**.
- There's **no way to check** what the pedals actually do afterwards.

This adds all three. It reads and writes SimPro Manager's own preset database,
so curves you make here show up there as normal presets.

<!-- screenshots / video go here -->

## What it does

- **Type exact curve values** instead of dragging. The preset stores float64;
  SimPro's editor snaps to whatever pixel you dropped the point on.
- **Three ways to shape the same curve**, whichever one matches how you think
  about it:
  - *3-point* — set the output at 25/50/75% travel directly.
  - *Pivot* — place one point and set how steep the curve runs through it.
    Best for fixing the twitchy throttle in Assetto Corsa Rally.
  - *Slope* — set the gradient of each segment (1.00 = linear, lower = slower).
- **Measure what the pedals actually do.** The Live / Verify tab reads the
  pedal's HID report, which carries both the raw pre-curve input and the
  post-curve output the game sees. Record a press-and-release sweep and the
  measured response is overlaid on the curve you designed.
- **Deadzone (low/high travel trim)** editing alongside the curve.
- **Backs up `user.db` before every write**, and only re-encodes the one axis
  you changed — every other byte of the preset is copied verbatim.

## Why you might want it

The obvious case is **Assetto Corsa Rally's twitchy throttle**. On gravel and
snow the default throttle model gives you almost no usable resolution in the
range that matters — RWD cars especially just light up the rears and step out.
Thanks to this tool you can flatten the throttle response around the point of
rapid power increase — that is, around 44% of output — so you get better
modulation both when on-power and when lifting. The *Pivot* mode was built for
exactly this use case.

It works just as well for brake curves, or anything else where you want a
specific response rather than a hand-drawn approximation of one.

## Running it

Grab the zip from [Releases](../../releases), unzip anywhere, run
`pedal-curve-lab.exe`. No installer, no Python, no dependencies. Everything it
writes — settings, DB backups — stays in that folder, so you can move it about
or delete it and be back to nothing.

Close SimPro Manager before saving. It holds its own copy of the preset and
writes that back over yours on exit — the tool warns you and offers to close it.
After saving, re-select the preset in SimPro Manager to push it to the pedals.

## From source

Needs Python 3.9+ and nothing else — standard library, tkinter, ctypes.

```
make run       # run it
make test      # round-trip checks on your own presets
make setup     # venv + PyInstaller, only needed for building
make build     # -> dist/pedal-curve-lab-vX.Y.Z-win64.zip
```

## Notes

- **Simagic presets only hold three points.** One output value each at 25%,
  50% and 75% travel — the travel positions are fixed, and the ends are always
  0 and 100%. That's their format, not a limit this tool imposes. Every mode
  here is just a different way of deciding those three values, and the pedals
  interpolate between them — which is why the Pivot and Slope tabs also show
  you what the stored points actually deliver, rather than only the figure you
  asked for.
- **Windows only.** It reads SimPro Manager's SQLite database and talks to the
  pedals over the Windows HID API.
- Built and tested against a **P1000**. Nothing is hardcoded to it — the device
  is auto-detected (and overridable), the report layout is read from the
  device's own descriptor, and the pre-curve input bytes are found by watching
  the pedals move. So other Simagic sets such as the **P2000** should work, but
  I have not had one to try. If yours misbehaves, run it with `--selftest`
  (does your preset database parse?) or `--hidtest` (what do the pedals
  actually report?) — each leaves a `-report.txt` next to the program worth
  attaching to an issue.
- Not affiliated with, endorsed by, or supported by Simagic. It writes to
  SimPro Manager's database; there is a backup before every write, but the risk
  is yours.

## License

[AGPL-3.0](LICENSE)
