# Multi-bridge prototype — design (private fork)

Goal: replace "Manage Matter Exports…" (one bridge, one menu) with
HomeKitLink-Siri-style **bridge devices**: create an Indigo device of type
"Matter Bridge", put the devices you want on it in its settings, pair it.
Several bridges side by side, each its own Matter accessory with its own code.

Status: **phase 1 done** (this fork runs beside upstream, export-only — see
`fork_identity.py`). Phase 2 below is the plan; nothing in it is built yet.

## What Matter does and does not give you

- Each bridge is paired separately in each ecosystem (one code, one
  "uncertified accessory" prompt each).
- Real wins: different device sets for different ecosystems (a single bridge
  shows everything to everyone it is paired with), and fault isolation (one
  wedged bridge takes down only its own devices).
- Not a win in Apple Home: rooms. Matter carries no rooms; you assign each
  accessory to a room in Home whichever bridge it came from. Bridges per room
  organise *Indigo*, not Home. Matter also has no equivalent of HomeKit's
  149-accessories-per-bridge limit.

## Architecture decision: one node process per bridge device

The bridge node (`bridge-node/`) is single-bridge by construction, and its
safeguards are per node: the endpoint-number map, drift detection, the
commissioning witness, session hygiene. Running **one unchanged node process
per bridge device** reuses all of that as-is. Cost: ~60-100 MB RAM per bridge.
The alternative (N bridges in one process, every protocol command carrying a
bridge id) is cleaner but re-engineers exactly the code that prevents
accessory duplication — wrong trade for a prototype.

## Per-bridge resources (derived from the Indigo device id)

| Resource | Value for bridge device `D` |
|---|---|
| LaunchAgent label | `com.durosity.indigo-matter-bridges.bridge.D` |
| Storage | `~/Library/Application Support/com.durosity.indigo-matter-bridges/bridges/D` |
| Matter port | 5640 + 10·n (n = slot, persisted in the device's props) |
| WS port | 5681 + n |
| Standalone (vacuum) ports | Matter port + 1…+9 — the node takes the ports just above its own, hence the step of 10 |
| Logs | `~/Library/Logs/indigo-matter-bridges/bridge-D.*.log` |

Ports and slot are allocated once and persisted in the device's pluginProps,
never recomputed (a moving port is harmless to Matter but breaks firewall
rules and the stray-process reaper's port check).

## Plugin changes

1. **Devices.xml** — new device type `matterBridge`:
   - ConfigUI: the existing export picker/role/options fields (moved from
     `MenuItems.xml` `manageMatterExports`), list of this bridge's exports,
     Add/Remove buttons. A device already on another bridge is shown as
     excluded ("on bridge X") — one bridge per device in v1 of the prototype
     (two bridges paired to the same Home would duplicate it).
   - States: `online`, `paired`, `ecosystems`, `endpoints`, `bridgeVersion`,
     `status` (UiDisplayStateId). Sensor-style so it is triggerable.
   - Actions: Show pairing code, Open pairing window, Unpair everywhere
     (factory reset), Restart bridge node. Vacuum pairing stays an action too.
2. **ExportStore** — one store per bridge device, held in that device's
   `pluginProps` (`exports` key) instead of `pluginPrefs`. The store already
   takes a prefs getter, so this is a different getter, not a rewrite.
3. **BridgeProcess / bridge_agent** — label, storage, ports and log names
   become constructor inputs (today they are module constants).
4. **ExportBridge** — one instance per bridge device, created in
   `deviceStartComm`, stopped in `deviceStopComm`, keyed by device id in
   `self.export_bridges`. Its Indigo-device subscription is shared: a device
   change is routed to whichever bridge owns that device.
5. **Menus → device actions** — every menu that today does
   `self.export_bridge.<x>` (pairing, unpair ecosystem, factory reset,
   endpoint-map recovery, fabric backup, restart) takes the bridge device from
   the action instead. Plugin-wide menus left: Install/update bridge node.
6. **Install** — one shared npm install (`~/indigo-matter-bridges`) serves
   every bridge process; installing restarts all bridges.

## Build order (each step leaves the fork working)

1. Parameterise `bridge_agent` (label/storage/ports/logs as inputs; defaults =
   today's) — no behaviour change, tests prove it.
2. `matterBridge` device type with states + pairing actions, driving ONE
   ExportBridge built from the device (upstream's single bridge, now a device).
3. Per-device ExportStore + the device-list ConfigUI.
4. N devices → N ExportBridges/agents; ownership routing; "on bridge X" exclusion.
5. Retire `manageMatterExports` and the plugin-wide pairing menus.

## Testing notes

- The upstream suite runs under upstream identity (`conftest.py` sets
  `INDIGO_MATTER_UPSTREAM_IDENTITY=1`); add multi-bridge tests alongside.
- Bridge-node tests are unaffected (the node does not change).
- On the Mac: two bridges paired to Apple Home must survive an Indigo restart
  with no duplicates — the acceptance test.
