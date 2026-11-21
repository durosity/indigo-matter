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
