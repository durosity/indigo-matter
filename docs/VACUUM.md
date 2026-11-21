# Exporting a robot vacuum

A robot vacuum controlled by Indigo can appear in Apple Home (and other Matter
ecosystems) as a native **Robotic Vacuum Cleaner**. You get start, stop, pause,
resume, return to dock, play sound to locate, battery level, optional cleaning
modes (vacuum / mop) and optional room selection, with Siri ("Hey Siri, start
the vacuum").

Requires plugin 2026.35.0 and bridge node 0.18.0 or later. The design is
recorded in [ADR-0019](adr/0019-a-robot-vacuum-is-its-own-matter-node-not-a-bridged-child.md);
the wire protocol in [BRIDGE_PROTOCOL §3.12 and §4.2](BRIDGE_PROTOCOL.md).

## Why the vacuum pairs separately

Apple Home and Alexa cannot drive a vacuum that sits behind a Matter bridge, so
the vacuum is **not** part of the bridge. It is published as its own Matter
accessory, with its own pairing code, from the same bridge process (no extra
install). Pairing the bridge does not add it; you pair it with
**Plugins ▸ Matter ▸ Pair Matter Vacuum…**.

## 1. Make the action groups

Indigo has no vacuum device class, so every command runs an Indigo **action
group** you choose. Make one per command, each calling your vacuum plugin's
matching action:

| Command | Typical action in the vacuum plugin |
|---|---|
| Start | Start / clean |
| Stop | Stop |
| Pause | Pause |
| Resume | Resume (optional — Start's group is used if none) |
| Return to dock | Dock / go home |
| Play sound | Locate / find (optional) |
| Each cleaning mode | Set vacuum / mop / vacuum-then-mop mode (optional) |

## 2. Export it

**Plugins ▸ Matter ▸ Manage Matter Exports…**, pick the vacuum. A device that
looks like a vacuum (its name, model or owning plugin say so, or it has states
such as `suction` or `mop`) defaults to **Robot vacuum (paired separately)**.

- **Status state** — the state carrying what the vacuum is doing. *Automatic*
  looks for `status`, `state`, `workStatus` and similar. Tuya words
  (`smart_clean`, `goto_charge`, `charge_done`…) and common Roomba, Roborock,
  Ecovacs and Dreame wordings are understood; anything unrecognised publishes
  nothing rather than a guess.
- **Battery state** — *Automatic* uses Indigo's battery level, else a state such
  as `battery` or `electricity_left`.
- **Start / Stop / Pause / Resume / Return to dock / Play sound runs** — the
  action groups from step 1.
- **Cleaning modes** (optional) — tick at least two of Vacuum, Mop and Vacuum
  then mop, and choose the action group each one runs. Tick none for a vacuum
  that cannot mop. A mode can also (or instead) be written to an Indigo variable.
- **Rooms** (optional) — `id=name` using the ids from the vacuum's own app map,
  e.g. `1=Kitchen, 7=Hallway`, plus the variable the selection is written to.
  When rooms are picked in Apple Home the ids are written there (`7,1`; empty
  means everywhere) and then Start runs — so your Start action group must read
  the variable and pass it to the vacuum plugin's room-clean action. Leave
  empty if the vacuum cannot clean by room.

Click **Add / update export**. After the bridge attaches, the Event Log says
whether the vacuum is online and paired.

## 3. Pair it

**Plugins ▸ Matter ▸ Pair Matter Vacuum…**, choose the vacuum, **Show pairing
code**. The code and a QR-page link go to the Event Log. In Apple Home, add
**the vacuum's name** (not "Indigo Matter Bridge") with that code; expect an
"uncertified accessory" prompt and choose *Add Anyway*.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The Pair Matter Vacuum menu is empty | No export is saved as a robot vacuum yet. |
| Log says the vacuum is withheld: bridge node too old | Install/update the Matter bridge (0.18.0 or later). |
| Log says "paired with 1 ecosystem" but it is gone from Home | Home forgot it without unpairing it. Pair Matter Vacuum… with **Unpair everywhere first** ticked, then add it again with the new code. |
| Modes, rooms or Play sound don't appear in Home | Home caches what it learned at first pairing. The bridge signals a change, but if nothing appears after a few minutes, re-pair with **Unpair everywhere first**. |
| A button in Home does nothing | No action group is chosen for that command — the Event Log names it. |
| Status in Home doesn't follow the vacuum | Set **Status state** explicitly, and check the plugin's status values are among those described above. |

## Limits

Room skipping, multi-floor maps and live room progress are not published.
Rooms depend on the vacuum (and its Indigo plugin) supporting room cleaning.
