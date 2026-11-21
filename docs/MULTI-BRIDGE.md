# Multi-bridge prototype — design (private fork)

Goal: replace "Manage Matter Exports…" (one bridge, one menu) with
HomeKitLink-Siri-style **bridge devices**: create an Indigo device of type
"Matter Bridge", put the devices you want on it in its settings, pair it.
Several bridges side by side, each its own Matter accessory with its own code.

Status: **phase 1 done** (this fork runs beside upstream, export-only — see
`fork_identity.py`). Phase 2: **steps 1 and 2a done**, 2b next — see
"Progress and hand-off" at the end.

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
2. **ExportStore** — one store per bridge device. *Revised during step 2a:*
   held in the plugin prefs under `bridge.<device id>.*` through
   `bridges.NamespacedPrefs`, not in the device's pluginProps — writing
   pluginProps re-saves the device (and can restart its comm), while the prefs
   path already has the store's atomic save. Same store code, different view.
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

## Progress and hand-off

Done (each commit tested in full before landing; suite about 4650 passing):

- `7765ab4` phase 1 — fork identity, export-only (`fork_identity.py`).
- `e730c65` step 1 — `bridge_agent.BridgeInstance` / `bridge_instance(device_id, slot)`;
  `BridgeProcess(..., instance=)`. Tests: `tests/test_bridge_instance.py`.
- `20ee33e` step 2a — `bridges.py`: `NamespacedPrefs`, `allocate_slot`, `slot_of`,
  `BridgeRuntime`, `BRIDGE_DEVICE_TYPE = "matterBridge"`, `PROP_SLOT = "bridgeSlot"`.
  Tests: `tests/test_multi_bridge.py`. (NB `tests/test_bridges.py` is an
  unrelated upstream file — do not overwrite it.)

### Step 2b — the plan worked out so far

UI decision: keep the existing dialogs (Manage Matter Exports, pairing,
recovery menus) and add a **Bridge** popup at the top of each, rather than
moving the export picker into the device's ConfigUI. About 60 of the ~90
singleton references live in those dialogs and keep working unchanged:

- `Plugin.exports`, `Plugin.export_bridge`, `Plugin.bridge_process` become
  **properties** resolving to the bridge chosen in the open dialog
  (`self._current_bridge_id`, set by the popup's callback), falling back to the
  legacy single bridge when no bridge devices exist, so nothing breaks midway.
  Setters write to the same target (`bridge_agent_menu_mixin` and
  `pairing_menu_mixin._bridge_restore_control` assign `bridge_process`).
- Background paths in `plugin.py` must LOOP over every bridge, not resolve one:
  `deviceUpdated` (forward to all), `deviceDeleted` (remove from whichever
  store owns it), plugin stop (stop all), `runConcurrentThread` -> `health_tick`
  (all), `_exports_changed` / `_exported_ids` (union), the status body (sum).
- `_start_bridge_agent` / `_stop_bridge_agent` / `_bridge_agent_diagnosis`
  (bridge_agent_menu_mixin) take an optional runtime and use
  `runtime.process` + `BridgeProcess(..., instance=runtime.instance)` instead
  of `self.bridge_process`; each bridge's `ExportBridge` gets closures bound to
  its own runtime (no shared "current" state on the runtime thread).
- Each `ExportBridge` gets `prefs_getter=lambda: runtime.prefs` where
  `runtime.prefs = NamespacedPrefs(lambda: self.pluginPrefs, prefs_prefix(id),
  {bridge_protocol.PREF_WS_PORT: instance.ws_port})` — the engine dials the
  bridge's own port via the existing `PREF_WS_PORT` lookup (export_bridge.py
  around L2262). Check `PREF_EXPORT_ENABLED`'s default when absent (~L647).
- Device type in `Devices.xml`: custom `matterBridge`, states `status`
  (UiDisplayStateId as a child element), `online`, `paired`, `ecosystems`,
  `endpoints`, `bridgeVersion`. Slot allocated in `validateDeviceConfigUi`
  (lowest free among existing bridge devices) and saved as `bridgeSlot`;
  `deviceStartComm` refuses to start a bridge with no valid slot.
  `deviceStartComm` builds the runtime; `deviceStopComm` stops it; deleting
  the device stops the agent and calls `runtime.prefs.clear_namespace()` —
  storage (fabrics) is left on disk, as upstream's uninstall does.

### Working method used (sandbox and Mac)

Code is written ONCE, on the Mac, as an idempotent edit script in
`~/Public/claude-xfer/stepN.py` (asserting each anchor occurs exactly once),
copied to the sandbox with the Filesystem tool, run against the same commit
there with the full suite, then run on the Mac; the two `git write-tree`
hashes must match before committing and pushing. Desktop Commander refuses
command lines containing some words (e.g. the plugin-stop method's name), so
run edits from a script file rather than inline.
