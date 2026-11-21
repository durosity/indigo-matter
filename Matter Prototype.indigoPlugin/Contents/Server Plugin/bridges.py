"""Multi-bridge plumbing for the private fork (docs/MULTI-BRIDGE.md).

Each Indigo device of type ``matterBridge`` is one Matter bridge: its own node
process (``bridge_agent.BridgeInstance``), its own export list and its own
``ExportBridge``. This module holds the pieces that make that possible without
rewriting the store or the export engine, both of which take a *prefs mapping*:

* :class:`NamespacedPrefs` gives each bridge a private slice of the plugin's
  prefs (keys stored as ``bridge.<device id>.<key>``), so the existing
  ``ExportStore``/``ExportBridge`` persistence works per bridge unchanged, and
  overlays read-only values (the bridge's protocol port) the engine looks up.
* :func:`allocate_slot` hands out the port slot, once, for a new bridge device.
* :class:`BridgeRuntime` is the bundle the plugin keeps per bridge device.

Pure Python, no ``indigo`` import, so it is unit-testable on its own.
"""
from __future__ import annotations

import functools
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

#: Indigo device type id of a bridge device (Devices.xml).
BRIDGE_DEVICE_TYPE = "matterBridge"
#: pluginProps key holding the bridge's port slot (allocated once, never moved).
PROP_SLOT = "bridgeSlot"
#: How many bridges one Mac may run. Ports: Matter 5640+10*slot, WS 5681+slot.
MAX_SLOTS = 32


def prefs_prefix(device_id: int) -> str:
    """The prefs-key prefix for bridge device ``device_id``."""
    return f"bridge.{int(device_id)}."


class NamespacedPrefs(MutableMapping):
    """A bridge's private view of the plugin prefs.

    Reads and writes go to ``<prefix><key>`` in the mapping ``getter()`` returns
    (read through the getter every time: Indigo can rebind ``pluginPrefs``).
    ``overrides`` are answered without touching the base and cannot be written
    or deleted — they are facts about the bridge (its port), not settings.
    """

    def __init__(self, getter: Callable[[], Any], prefix: str,
                 overrides: Optional[dict] = None) -> None:
        if not prefix:
            raise ValueError("a namespace prefix is required")
        self._getter = getter
        self._prefix = prefix
        self._overrides = dict(overrides or {})

    @property
    def prefix(self) -> str:
        """The key prefix this view reads and writes under."""
        return self._prefix

    def __getitem__(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return self._getter()[self._prefix + key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._overrides:
            raise KeyError(f"{key!r} is fixed for this bridge and cannot be set")
        self._getter()[self._prefix + key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._overrides:
            raise KeyError(f"{key!r} is fixed for this bridge and cannot be removed")
        del self._getter()[self._prefix + key]

    def __iter__(self) -> Iterator[str]:
        seen = set(self._overrides)
        yield from self._overrides
        for key in list(self._getter().keys()):
            if isinstance(key, str) and key.startswith(self._prefix):
                short = key[len(self._prefix):]
                if short not in seen:
                    seen.add(short)
                    yield short

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, key: object) -> bool:
        if key in self._overrides:
            return True
        return isinstance(key, str) and (self._prefix + key) in self._getter()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def clear_namespace(self) -> int:
        """Delete every stored key of this bridge (not the overrides). Returns the count."""
        base = self._getter()
        doomed = [k for k in list(base.keys()) if isinstance(k, str) and k.startswith(self._prefix)]
        for key in doomed:
            del base[key]
        return len(doomed)


def allocate_slot(used: set) -> int:
    """The lowest free slot. Raises ``ValueError`` when all :data:`MAX_SLOTS` are taken."""
    for slot in range(MAX_SLOTS):
        if slot not in used:
            return slot
    raise ValueError(f"all {MAX_SLOTS} bridge slots are in use")


def slot_of(props: Any) -> Optional[int]:
    """A bridge device's persisted slot, or ``None`` if it has none (or a bad one)."""
    try:
        raw = (props or {}).get(PROP_SLOT)
    except AttributeError:
        return None
    if isinstance(raw, bool):
        return None
    try:
        slot = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return slot if 0 <= slot < MAX_SLOTS else None


@dataclass
class BridgeRuntime:
    """Everything the plugin keeps for one running bridge device."""

    device_id: int
    name: str
    instance: Any            # bridge_agent.BridgeInstance
    prefs: NamespacedPrefs
    store: Any = None        # export_store.ExportStore
    bridge: Any = None       # export_bridge.ExportBridge
    process: Any = None      # bridge_agent.BridgeProcess (built on demand)
    exported_ids: frozenset = field(default_factory=frozenset)
    slot: Any = None         # int once running; a new device's slot lives here before its first Save


#: The slot the legacy single bridge ("Manage Matter Exports…" with no bridge
#: device chosen) effectively occupies: its default ports ARE slot 0's (Matter
#: 5640, protocol 5681), so no bridge DEVICE may take slot 0 until step 5
#: retires the legacy bridge — two nodes on one port is the §7 bind failure.
LEGACY_SLOT = 0


def port_check_attrs(device_id: int) -> tuple:
    """The plugin attributes holding one bridge's port-conflict check state.

    Bridge 0 (the legacy bridge) keeps the upstream names, so every existing
    test and code path that names them still means the same thing.
    """
    if not device_id:
        return ("_next_bridge_port_check", "_last_bridge_port_conflict")
    return (f"_next_bridge_port_check_{int(device_id)}", f"_last_bridge_port_conflict_{int(device_id)}")


def bridge_states(*, export_count: int, enabled: bool, connected: bool, attached: bool,
                  fabrics: Optional[list], version: Any,
                  describe_fabric: Callable[[Any], str] = str) -> list:
    """The ``matterBridge`` device's states, as an ``updateStatesOnServer`` list.

    ``fabrics`` is ``None`` while nothing has been reported (shown as
    "unknown", never as "none" — not knowing is not the same as unpaired).
    """
    if not export_count:
        status = "idle"
    elif not enabled:
        status = "switched off"
    elif not connected:
        status = "not connected"
    elif not attached:
        status = "not serving"
    else:
        status = "online"
    names = None if fabrics is None else [describe_fabric(f) for f in fabrics]
    if names is None:
        ecosystems = "unknown"
    else:
        ecosystems = ", ".join(names) or "none"
    return [
        {"key": "status", "value": status},
        {"key": "online", "value": status == "online"},
        {"key": "paired", "value": bool(names)},
        {"key": "ecosystems", "value": ecosystems},
        {"key": "endpoints", "value": int(export_count)},
        {"key": "bridgeVersion", "value": str(version or "")},
    ]


def legacy_bridge_only(method: Callable) -> Callable:
    """Run a plugin callback against the LEGACY bridge, whatever a dialog chose.

    For callbacks that have no Bridge popup yet (fabric backup/restore, the
    colour-temperature calibration): without the pin they would act on
    whichever bridge the last export dialog left selected.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        targeting = getattr(self, "_targeting", None)
        legacy = getattr(self, "_legacy_runtime", None)
        if targeting is None or legacy is None:
            return method(self, *args, **kwargs)
        with targeting(legacy()):
            return method(self, *args, **kwargs)
    return wrapper
