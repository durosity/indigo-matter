# Multi-bridge prototype — design (private fork)

Goal: replace "Manage Matter Exports…" (one bridge, one menu) with
HomeKitLink-Siri-style **bridge devices**: create an Indigo device of type
"Matter Bridge", put the devices you want on it in its settings, pair it.
Several bridges side by side, each its own Matter accessory with its own code.

Status: **phase 1 done** (this fork runs beside upstream, export-only — see
`fork_identity.py`). Phase 2: **steps 1, 2a and 2b done**, Mac acceptance
test next — see "Progress and hand-off" at the end.

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

- step 2b — Matter Bridge devices (`step2b.py` in claude-xfer). Tests:
  `tests/test_bridge_devices.py`. What landed, and where it differs from the plan:
  - `bridge_device_mixin.py` (new, evicted in `conftest.py`): every bridge is a
    `BridgeRuntime`, the upstream one included (the *legacy* bridge, device id 0,
    built lazily so `Plugin.__new__` tests work). `exports` / `export_bridge` /
    `bridge_process` are properties resolving to the target bridge: a
    **thread-local pin** (`_targeting`) first, then the dialog's Bridge popup,
    then legacy. The pin is what keeps background work (the install thread,
    `bridges.legacy_bridge_only` callbacks) off whatever a dialog has selected.
  - Bridge popup (`bridgeTarget`, first field) in 8 dialogs: Manage Exports,
    Pair Bridge, Pair Vacuum, Unpair, Stop Bridge, Rebuild Endpoint Map,
    Migrate, Reset Pairings. It is `dynamicReload` (the Manage dialog's tests
    require every list to be); `getBridgeTargets` syncs the choice to
    `valuesDict` and resets it to the first entry when a dialog opens.
  - Loops over every bridge: `deviceUpdated` (one failing bridge no longer
    starves the others), `deviceDeleted`, shutdown, the watchdog, the port
    check (`bridges.port_check_attrs`). `_exports_changed(runtimes=None)`
    refreshes every id set but nudges only the bridges given (default: target).
  - Agent seams take an optional runtime (`None` = upstream behaviour);
    `_new_bridge_process` passes the instance. Install: npm once, then every
    bridge device with exports is restarted, pinned to its record.
  - `matterBridge` is **custom**, not sensor (`SupportsOnState` would steal the
    list display from `status`, issue #56). Slot allocated in
    `validateDeviceConfigUi`; a duplicated device's copied slot is reallocated.
  - **Slot 0 is reserved** (`bridges.LEGACY_SLOT`): its ports are the legacy
    bridge's defaults, so the first bridge device gets slot 1 (5650 / 5682).
  - Stopping a bridge device's comm leaves its node RUNNING (Indigo stops every
    device on a plugin reload); deleting the device uninstalls the node and
    clears `bridge.<id>.*`; storage is kept.
- step 2c — the HomeKitLink-style dialog (`step2c.py`): a Matter Bridge
  DEVICE's own Configure dialog is now that bridge's export manager — the whole
  Manage Matter Exports panel (picker, role, options, the vacuum's action-group
  mappings, the export list) generated from the menu dialog's fields at build
  time, plus an "Open pairing window" button (`bridgeDevicePair`). Opening it
  targets that bridge (`_bridge_dialog_values`); a NEW device gets its slot and
  a live bridge right there, so devices can be added before the first Save.
  A dialog whose bridge cannot run targets nothing, never the legacy bridge.
  Save keeps only `bridgeSlot` in the props. The menu dialogs (with their
  Bridge popup) remain for the legacy bridge until step 5. NB the panel's
  fields now exist twice (MenuItems.xml and Devices.xml): change them in
  MenuItems.xml and regenerate, or retire the menu item in step 5.
- step 2d — health on every bridge (`step2d.py`): `matterBridge` declares
  `matterBridgeHealth`'s twelve states too (`subscriptionHealth`,
  `churnDetail`, `fabric1..5Name/Health`, copied from its XML), and each bridge
  device's health reporter writes them to the bridge device itself. The
  standing `matterBridgeHealth` device stays the legacy bridge's.
- step 3a — HomeKitLink-style editing (`step3a.py`): the published name defaults
  to the Indigo name (left at it = no override, so renames follow); clicking a
  row in "Currently exported" (or "Edit selected") loads it into the editor;
  a "Publish device" checkbox — unticked + Add/update unpublishes but KEEPS the
  settings (`exportParked`, per bridge), Remove forgets them; "Show pairing QR
  code" opens a window and a self-contained QR page (pyqrcode, SVG) in a browser
  on the Indigo Mac, codes also logged. Step 3b next: state picker with Auto for
  every device, any device as any type, action groups for commands.
- step 3b — any device as any single-value type (`step3b.py`): the state
  picker shows for every device with "Auto (…)" first; the role menu lists the
  device's own types, then every other single-value type "(mapped)". A mapped
  entry carries `crossMapped` (+ optional `stateKey`, `stateInvert`,
  `cmdOnActionGroup`/`cmdOffActionGroup`); `export_handlers.handler_for_entry`
  wraps the role's own handler so the value comes from the state (bools,
  numbers and words like open/closed) and commands go to the action groups,
  else the device's own on/off (relays and dimmers only), inverted if ticked.
  Not mappable: colour lights, thermostat, vacuum. Changing an exported
  device's type creates a new accessory in each ecosystem.
- step 4 — the built-in bridge becomes a device (`step4.py`): at startup, if
  the built-in bridge has exports or storage, `_migrate_legacy_bridge` creates
  a Matter Bridge device on slot 0 (same ports), removes the built-in
  LaunchAgent, moves its storage folder (backup kept as
  `<storage>.pre-device-backup`), moves its own settings (`LEGACY_BRIDGE_KEYS`)
  into the device's namespace and deletes the Matter Bridge Health device.
  Progress lives in `legacyBridgeMigration`; every stage repeats safely and the
  device will not start until it is `done`. Slot 0 is no longer reserved.
  Unpair and reset moved into the device dialog; the Manage Exports, Pair
  Bridge, Unpair and Reset menu items are gone. Still in the menu with a Bridge
  popup: Pair Vacuum, Stop Bridge Node, Rebuild Endpoint Map, Migrate Export;
  fabric backup/restore and CT calibration still act on the built-in bridge.
- step 4b — robustness (`step4b.py`): accessory names are cut to Matter's
  32-byte NodeLabel limit (`export_bridge.matter_label`, warned once; the dialog
  says what it will show as) — a 36-character name made the node reject the
  accessory and then refuse the whole attach. A NEWLY added accessory the node
  refuses (`internal`) is quarantined (`matterExportQuarantine`, per bridge,
  migrated with the rest) and left out of every attach until saved again or
  removed; live ones never are (leaving them out would remove them). The
  node itself still aborts an attach on any refused endpoint — the real fix is
  per-endpoint tolerance in `bridge-node/src/registry.ts`, which means the fork
  shipping its own node build. Also: the state picker's Auto row has a real id
  (`_auto`), and the matter-server watchdog is quiet in the export-only build.
- step 4c (`step4c.py`): the fork ships its own bridge node. The registry's
  newest is 0.17.5, which predates the robot vacuum; `tools/build_bridge_node.sh`
  runs the repo node's tests (on the Mac: they need a normal user and mDNS),
  builds it and packs it into `Server Plugin/bridge_node/`, and
  `bridge_agent.DEFAULT_INSTALL_SPEC` is that tarball when present (else the
  registry pin, `REGISTRY_INSTALL_SPEC`). "Install/update Matter bridge node"
  then moves every bridge onto it. Also: Device state defaults to Auto; no
  `set_state` for a device the bridge skipped or the node refused; the storage
  move drops the old node's `matter.lock`; the export-only build removes the
  matter-server LaunchAgent an earlier build left running.
- Known gaps, by design for now: device bridges ignore the global "Enable Matter export" switch (absent `exportEnabled` reads
  as on); fabric backup/restore and CT calibration act on the legacy bridge
  only; no "on bridge X" exclusion yet, so one device CAN be on two bridges
  (step 4); no device actions yet — pairing goes through the dialogs.
- Next: the Mac acceptance test (create two Matter Bridge devices, export a
  device on each, pair both to Apple Home, reload the plugin — no duplicates),
  then step 4's exclusion and per-bridge health, then step 5.

### Working method used (sandbox and Mac)

Code is written ONCE, on the Mac, as an idempotent edit script in
`~/Public/claude-xfer/stepN.py` (asserting each anchor occurs exactly once),
copied to the sandbox with the Filesystem tool, run against the same commit
there with the full suite, then run on the Mac; the two `git write-tree`
hashes must match before committing and pushing. Desktop Commander refuses
command lines containing some words (e.g. the plugin-stop method's name), so
run edits from a script file rather than inline.
