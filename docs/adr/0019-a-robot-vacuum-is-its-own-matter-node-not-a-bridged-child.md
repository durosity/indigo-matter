---
parent: Decisions
nav_order: 19
title: "ADR-0019: A robot vacuum is published as its own Matter node, not a bridged child"

status: "proposed"
date: 2026-11-20
decision-makers: Simon (proposed by a contributor PR)
consulted: none
informed: none
---
# ADR-0019: A robot vacuum is published as its own Matter node, not a bridged child

## Context and Problem Statement

Indigo users want to export a robot vacuum (a Tuya, Roborock, Roomba… driven
by its own Indigo plugin) so Apple Home and Siri can start it, pause it and
send it home. Matter has a device type for this — Robotic Vacuum Cleaner,
0x0074, with RVC Run Mode and RVC Operational State — and matter.js 0.17.8
ships it.

Every other export this plugin makes is a bridged child of one aggregator.
But a bridged vacuum does not work where it matters. Projects that have
shipped Matter vacuums report the same thing independently: matterbridge's
Home Assistant plugin says Apple Home crashes when the vacuum is inside the
bridge; Home Assistant Matter Hub says a bridged vacuum shows "Updating" in
Apple Home, Siri cannot drive it and room selection never appears, and that
Alexa does not discover it at all. Both ship a "server mode" that publishes
the vacuum as a standalone Matter device, one per node, with its own pairing
code.

How should this bridge publish a vacuum?

## Decision Drivers

* It has to work in Apple Home and with Siri — that is the reason anyone
  exports a vacuum.
* The bridge's hard-won invariants (stable endpoint numbers, drift detection,
  the commissioning witness, restore-before-online) must not be weakened for
  every other export to accommodate one device type.
* No new install, LaunchAgent or WebSocket for users to manage.
* An older bridge node must never be sent a role it does not know: an unknown
  role refuses the WHOLE `attach`, taking every export offline.

## Considered Options

* A bridged `roboticVacuumCleaner` role, like every other role.
* A second LaunchAgent and node process per vacuum.
* Standalone Matter nodes hosted inside the existing bridge process.

## Decision Outcome

Chosen option: "Standalone Matter nodes hosted inside the existing bridge
process", because it is the only option that works in Apple Home and Alexa
without a second process to install, supervise and recover.

`roboticVacuumCleaner` is a **standalone role** (BRIDGE_PROTOCOL §3.12,
§4.2). The bridge node hosts each such export as its own matter.js
`ServerNode` — own UDP port (persisted, above the bridge's), own storage
context, own fabrics, own codes — alongside the bridge's. matter.js supports
several `ServerNode`s in one environment. The device endpoint is built from
the same role table as everything else; the role simply carries no Bridged
Device Basic Information, and its name and `Reachable` live on the node's
root Basic Information. The plugin addresses it by `indigoDeviceId` through
the ordinary `attach`/`set_state`/`command` vocabulary; the only new wire
surface is §3.12's three pairing commands and §4.3's `standalone[]`.

Removal is two-tier: an `attach` that omits a standalone export makes it
dormant (node closed, pairings kept), because the plugin omits specs for
transient reasons; `remove_endpoint` erases it (ADR-0015/0016).

The plugin withholds standalone specs from a node older than 0.18.0, so an
upgrade-order mistake costs the vacuum, not every export.

### Consequences

* Good, because a vacuum appears in Apple Home as a native vacuum (start,
  pause, return to dock, battery, Siri) — the device type ecosystems actually
  support.
* Good, because the aggregator's invariants are untouched: standalone roles
  never reach the registry (it refuses them outright), and the endpoint map,
  drift detection and witness are exactly as before.
* Good, because users install nothing new.
* Bad, because a vacuum is paired separately from the bridge, with its own
  code — a new concept the UI has to explain ("Pair Matter Vacuum…", the role
  label "Robot vacuum (paired separately)").
* Bad, because a standalone node's lost fabric storage is not detected (no
  commissioning witness). With one endpoint, always number 1, there is
  nothing to renumber, so the harm the witness prevents for the aggregator
  cannot happen here; the vacuum simply advertises for pairing again.
* Neutral, because RVC Clean Mode and Service Area (room selection) are not
  published in this version: Indigo has no common vocabulary for either.
  Both are additive later.

### Confirmation

* `bridge-node/test/standalone.test.ts` drives real matter.js nodes: device
  type 0x0074 at endpoint 1 with no bridged info; `operationalState` landing
  on both RVC clusters; commands reported without auto-confirmation;
  dormant/erase/reset; restore-before-attach; `BridgeNode` partitioning,
  role moves and the whole-attach mass-removal guard.
* `tests/test_robotic_vacuum.py` pins the plugin's status mapping, battery,
  action-group commands, option validation and the version guard.
* Field validation on real hardware (Apple Home pairing a standalone vacuum
  and Siri driving it) before this ADR moves to accepted.

## Pros and Cons of the Options

### A bridged `roboticVacuumCleaner` role

* Good, because it is the smallest change and matches every other role.
* Bad, because it does not work in Apple Home or Alexa — the reason the
  feature exists.

### A second LaunchAgent and node process per vacuum

* Good, because each vacuum is fully isolated.
* Bad, because it duplicates the install, launchd, health and recovery
  machinery `bridge_agent.py`/`launch_agent.py` exist for, and gives users a
  second process to diagnose.

### Standalone Matter nodes hosted inside the existing bridge process

* Good, because it works in Apple Home and Alexa and needs no new process.
* Bad, because a fault in one node's matter.js stack shares a process with
  the bridge. Mitigated: a node that cannot start is reported (`online:
  false`, `warnings`) and never takes the bridge down.
