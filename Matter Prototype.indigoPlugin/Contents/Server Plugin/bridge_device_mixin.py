"""Matter Bridge DEVICES — the private fork's multi-bridge plugin side.

See docs/MULTI-BRIDGE.md. Each Indigo device of type ``matterBridge`` is one
Matter bridge with its own node process, port slot, export list and
``ExportBridge``, kept as a :class:`bridges.BridgeRuntime` in
``self.bridge_runtimes``. The upstream single bridge lives on as the *legacy*
runtime (device id 0) until step 5 retires it.

**How ~60 upstream call sites keep working unchanged.** ``exports``,
``export_bridge`` and ``bridge_process`` are properties here, resolving to
the *target* runtime: a per-thread pin (:meth:`_targeting`, used by
background work and by :func:`bridges.legacy_bridge_only`) first, then the
bridge chosen in the open dialog's Bridge popup, then the legacy bridge.
Paths that must cover every bridge (device updates and deletions, the
watchdog, the port check, shutdown) loop over :meth:`_all_runtimes`
explicitly instead of going through the properties.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any

import indigo  # provided by the Indigo runtime

import bridge_agent
import bridge_health
import bridge_protocol
import bridges
from export_bridge import ExportBridge
from export_store import ExportStore


def _no_health_device():
    """Health-device factory for bridge DEVICES: there is no per-bridge one yet.

    The standing ``matterBridgeHealth`` device belongs to the legacy bridge;
    two bridges writing it would overwrite each other's verdicts. A bridge
    device's own states carry its status instead (step 4 may add more).
    """
    raise RuntimeError("bridge devices do not have a health device yet — the Matter Bridge "
                       "device's own states show this bridge's status")


class BridgeDeviceMixin:
    """Bridge-device lifecycle, the Bridge popup, and the target-bridge properties."""

    pluginPrefs: Any
    logger: Any
    runtime: Any
    device_sync: Any
    _version: Any

    # ------------------------------------------------------------------
    # Which bridge
    # ------------------------------------------------------------------
    def _legacy_runtime(self) -> bridges.BridgeRuntime:
        """The upstream single bridge's record — built on first use."""
        record = getattr(self, "_legacy_rt", None)
        if record is None:
            record = bridges.BridgeRuntime(device_id=0, name="Legacy bridge", instance=None, prefs=None)
            self._legacy_rt = record
        return record

    def _device_runtimes(self) -> list:
        """Every running bridge DEVICE's record (a copy: the watchdog thread iterates it)."""
        return list((getattr(self, "bridge_runtimes", None) or {}).values())

    def _all_runtimes(self) -> list:
        """The legacy bridge first, then every bridge device."""
        return [self._legacy_runtime(), *self._device_runtimes()]

    def _target_runtime(self) -> bridges.BridgeRuntime:
        """The bridge the singleton-style code (dialogs, menus) is acting on."""
        local = getattr(self, "_bridge_target", None)
        pinned = getattr(local, "runtime", None) if local is not None else None
        if pinned is not None:
            return pinned
        chosen_id = getattr(self, "_current_bridge_id", None)
        if chosen_id is None:
            return self._legacy_runtime()
        chosen = (getattr(self, "bridge_runtimes", None) or {}).get(chosen_id)
        if chosen is not None:
            return chosen
        # A bridge device's dialog is open but its bridge is not running: act on
        # NOTHING (no store, no engine) rather than silently on the legacy bridge.
        return bridges.BridgeRuntime(device_id=chosen_id, name="(not running)", instance=None, prefs=None)

    @contextlib.contextmanager
    def _targeting(self, runtime: bridges.BridgeRuntime):
        """Pin the target bridge for THIS thread only, then restore the previous pin."""
        local = getattr(self, "_bridge_target", None)
        if local is None:
            local = self._bridge_target = threading.local()
        previous = getattr(local, "runtime", None)
        local.runtime = runtime
        try:
            yield runtime
        finally:
            local.runtime = previous

    @property
    def exports(self):
        """The target bridge's export store (upstream's ``self.exports``)."""
        return self._target_runtime().store

    @exports.setter
    def exports(self, value) -> None:
        self._target_runtime().store = value

    @property
    def export_bridge(self):
        """The target bridge's export engine (upstream's ``self.export_bridge``)."""
        return self._target_runtime().bridge

    @export_bridge.setter
    def export_bridge(self, value) -> None:
        self._target_runtime().bridge = value

    @property
    def bridge_process(self):
        """The target bridge's LaunchAgent control (upstream's ``self.bridge_process``)."""
        return self._target_runtime().process

    @bridge_process.setter
    def bridge_process(self, value) -> None:
        self._target_runtime().process = value

    def _runtimes_exporting(self, device_id: int) -> list:
        """The bridges whose export list holds ``device_id``.

        Bridge devices answer from their cached id sets. The legacy bridge
        answers yes for its own ids AND for any exported id no bridge device
        claims — the union gate in ``deviceUpdated`` already proved the id is
        exported somewhere, and a caller that set ``exports`` directly (the
        legacy/test path) need not have refreshed the legacy id set.
        """
        found = [rt for rt in self._device_runtimes() if device_id in rt.exported_ids]
        legacy = self._legacy_runtime()
        if legacy.store is not None and (
                device_id in legacy.exported_ids
                or device_id not in getattr(self, "_device_exported_ids", frozenset())):
            found.insert(0, legacy)
        return found

    @staticmethod
    def _forward_device_update(targets, orig_dev, new_dev) -> None:
        """Hand one Indigo change to every bridge exporting it; re-raise the first failure."""
        failure = None
        for bridge in targets:
            try:
                bridge.device_updated(orig_dev, new_dev)
            except Exception as exc:  # pylint: disable=broad-except
                # one bridge's failure must not starve the others; reported by the caller
                failure = failure or exc
        if failure is not None:
            raise failure

    # ------------------------------------------------------------------
    # The Bridge popup (top of each export / pairing / recovery dialog)
    # ------------------------------------------------------------------
    # pylint: disable-next=redefined-builtin,unused-argument
    def getBridgeTargets(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback for every dialog's "Bridge:" popup — and the dialog's choice.

        dynamicReload, like every list in these dialogs, so Indigo asks again
        after each callback; ``valuesDict`` then carries the selection, and the
        choice is synced to it. As the dialog OPENS there is no selection yet
        and Indigo shows the first entry, so the choice is reset to that one —
        without the reset a dialog would silently act on the bridge the
        PREVIOUS dialog left chosen.
        """
        devices = sorted(self._device_runtimes(), key=lambda rt: str(rt.name).lower())
        options = [(str(rt.device_id), str(rt.name)) for rt in devices]
        options.append(("0", "Legacy bridge (no bridge device)"))
        raw = str((valuesDict or {}).get("bridgeTarget", "") or "").strip()
        if raw and raw in {value for value, _ in options}:
            self._current_bridge_id = int(raw) if raw != "0" else None
        else:
            self._current_bridge_id = devices[0].device_id if devices else None
        return options

    # pylint: disable-next=unused-argument
    def bridgeTargetChanged(self, valuesDict, typeId="", devId=0):  # noqa: N802, ARG002
        """The popup's callback: retarget the dialog. Its lists reload after this."""
        raw = str(valuesDict.get("bridgeTarget", "") or "").strip()
        chosen = int(raw) if raw.isdigit() else 0
        runtimes = getattr(self, "bridge_runtimes", None) or {}
        self._current_bridge_id = chosen if chosen in runtimes else None
        return valuesDict

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------
    def _validate_bridge_device(self, valuesDict, devId):
        """Give a new (or duplicated) bridge device its port slot, once.

        A slot that is valid and not held by another bridge device is kept —
        it never moves (MULTI-BRIDGE.md: a moving port breaks firewall rules
        and the reaper's port check). Indigo's "Duplicate Device" copies props,
        so a held slot is what a duplicate looks like; that one is reallocated.
        """
        used = self._used_bridge_slots(devId)
        slot = bridges.slot_of(valuesDict)
        if slot is None or slot in used:
            try:
                slot = bridges.allocate_slot(used)
            except ValueError as exc:
                errors = indigo.Dict()
                errors["showAlertText"] = f"No more Matter bridges can be created on this Mac: {exc}."
                return (False, valuesDict, errors)
            valuesDict[bridges.PROP_SLOT] = str(slot)
        # Everything else in the dialog is the export panel's working state (the
        # export list itself lives with the bridge): keep it out of the props, so
        # a Save does not change them and Indigo does not restart the bridge.
        for key in [k for k in valuesDict.keys() if k != bridges.PROP_SLOT]:
            del valuesDict[key]
        return (True, valuesDict)

    def _used_bridge_slots(self, devId) -> set:
        """Slots taken by OTHER bridge devices — saved, or running but not yet saved."""
        used = {bridges.LEGACY_SLOT}
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == bridges.BRIDGE_DEVICE_TYPE and dev.id != devId:
                slot = bridges.slot_of(dev.pluginProps)
                if slot is not None:
                    used.add(slot)
        for record in self._device_runtimes():
            if record.device_id != devId and record.slot is not None:
                used.add(record.slot)
        return used

    def _bridge_dialog_values(self, pluginProps, devId):
        """Open a bridge device's dialog: make sure its bridge runs, then target it.

        A brand-new device gets its slot and a live bridge right here, so devices
        can be added before the first Save (as in HomeKitLink). The slot rides in
        the dialog's values and is saved with the device on Save.
        """
        values = dict(pluginProps)
        if devId and devId not in (getattr(self, "bridge_runtimes", None) or {}):
            used = self._used_bridge_slots(devId)
            slot = bridges.slot_of(values)
            if slot is None or slot in used:
                try:
                    slot = bridges.allocate_slot(used)
                except ValueError as exc:
                    self.logger.error("No more Matter bridges can be created on this Mac: %s.", exc)
                    slot = None
            if slot is not None:
                values[bridges.PROP_SLOT] = str(slot)
                try:
                    self._start_bridge_device(indigo.devices[devId], slot)
                except Exception as exc:  # pylint: disable=broad-except
                    # the dialog must still open; it then acts on nothing
                    self.logger.exception(exc)
        self._current_bridge_id = devId or None
        return values

    def _bridge_dialog_closed(self, devId) -> None:
        if getattr(self, "_current_bridge_id", None) == devId:
            self._current_bridge_id = None

    # pylint: disable-next=unused-argument
    def bridgeDevicePair(self, valuesDict, typeId="", devId=0):  # noqa: N802, ARG002
        """The device dialog's "Open pairing window": Pair Matter Bridge, for THIS bridge."""
        record = (getattr(self, "bridge_runtimes", None) or {}).get(devId)
        if record is None:
            errors = indigo.Dict()
            errors["duration"] = "This bridge is not running — see the event log."
            return (valuesDict, errors)
        with self._targeting(record):
            result = self.menuPairMatterBridge(valuesDict)  # pylint: disable=no-member  # PairingMenuMixin
        if result[0]:
            return valuesDict
        return (valuesDict, result[2])

    def _refuse_bridge_device(self, dev, why: str) -> None:
        self.logger.error('Matter bridge "%s" was not started: %s.', dev.name, why)
        try:
            self.device_sync.apply_states(dev.id, [{"key": "status", "value": "not started"},
                                                   {"key": "online", "value": False}])
        except Exception as exc:  # pylint: disable=broad-except
            # cosmetic; the error line above is the report
            self.logger.debug("could not write bridge device %s's status (%s)", dev.id, exc)

    def _build_bridge_runtime(self, dev, slot: int) -> bridges.BridgeRuntime:
        """A bridge device's record: its node's resources, prefs slice, store and engine."""
        instance = bridge_agent.bridge_instance(dev.id, slot)
        prefs = bridges.NamespacedPrefs(
            lambda: self.pluginPrefs, bridges.prefs_prefix(dev.id),
            {bridge_protocol.PREF_WS_PORT: instance.ws_port})
        record = bridges.BridgeRuntime(device_id=dev.id, name=str(dev.name), instance=instance, prefs=prefs,
                                       slot=slot)
        record.store = ExportStore(
            lambda: record.prefs, self.logger,
            save_prefs=self._save_plugin_prefs,  # pylint: disable=no-member  # ExportDialogMixin
            entry_validator=self._reject_unexportable_entry,  # pylint: disable=no-member  # ExportDialogMixin
        )
        # Closures bound to THIS record: the engine calls its seams from
        # whichever thread changed the list, and must never reach the shared
        # dialog target from there.
        record.bridge = ExportBridge(
            record.store, self.runtime, self.logger, lambda: record.prefs,
            plugin_version=self._version,
            plugin_id=self._export_plugin_id(),  # pylint: disable=no-member  # ExportDialogMixin
            save_prefs=self._save_plugin_prefs,  # pylint: disable=no-member  # ExportDialogMixin
            agent_start=lambda: self._start_bridge_agent(record),  # pylint: disable=no-member
            agent_stop=lambda: self._stop_bridge_agent(record),  # pylint: disable=no-member
            agent_diagnose=lambda: self._bridge_agent_diagnosis(record),  # pylint: disable=no-member
            health_device_finder=lambda: None,
            health_device_factory=_no_health_device,
        )
        return record

    def _start_bridge_device(self, dev, slot=None) -> None:
        """``deviceStartComm`` for a bridge device: build its runtime and let it start itself.

        ``slot`` is given only by a new device's first dialog, before it is saved.
        """
        slot = bridges.slot_of(dev.pluginProps) if slot is None else slot
        if slot is None or slot == bridges.LEGACY_SLOT:
            self._refuse_bridge_device(dev, "it has no port slot yet — open its settings and click Save")
            return
        wanted = bridge_agent.bridge_instance(dev.id, slot)
        for other in self._device_runtimes():
            if other.device_id != dev.id and other.instance is not None \
                    and other.instance.ws_port == wanted.ws_port:
                self._refuse_bridge_device(
                    dev, f'it has the same port slot as "{other.name}" (a duplicated device?) — '
                         "open its settings and click Save to give it its own")
                return
        if self.runtime is None:
            self._refuse_bridge_device(dev, "the plugin has not finished starting")
            return
        if dev.id in (getattr(self, "bridge_runtimes", None) or {}):
            self._stop_bridge_device(dev)
        if getattr(self, "bridge_runtimes", None) is None:
            self.bridge_runtimes = {}
        record = self._build_bridge_runtime(dev, slot)
        self.bridge_runtimes[dev.id] = record
        with self._targeting(record):
            self._reconcile_exports()  # pylint: disable=no-member  # ExportDialogMixin
        self._exports_changed([record])  # pylint: disable=no-member  # plugin.py
        self._refresh_bridge_device(record, force=True)
        self.logger.info('Matter bridge "%s" started: Matter port %s, protocol port %s, %d device(s) '
                         "exported.", record.name, record.instance.matter_port,
                         record.instance.ws_port, len(record.store))

    def _stop_bridge_device(self, dev) -> None:
        """``deviceStopComm`` for a bridge device: close its socket, drop its record.

        The node process is deliberately LEFT RUNNING, exactly as upstream's
        plugin stop leaves the single bridge running: Indigo stops every
        device's comm on a plugin reload, and a reload must not take every
        paired accessory offline. Deleting the device is what stops the node.
        """
        runtimes = getattr(self, "bridge_runtimes", None) or {}
        record = runtimes.pop(dev.id, None)
        if getattr(self, "_current_bridge_id", None) == dev.id:
            self._current_bridge_id = None
        cache = getattr(self, "_bridge_state_cache", None)
        if cache is not None:
            cache.pop(dev.id, None)
        if record is None:
            return
        try:
            if record.bridge is not None and self.runtime is not None and self.runtime.is_running:
                record.bridge.stop()
        except Exception as exc:  # pylint: disable=broad-except
            # a stop must always complete; the socket dies with the plugin anyway
            self.logger.exception(exc)
        record.bridge = None
        self._exports_changed([])  # pylint: disable=no-member  # refresh the id sets only

    def _forget_bridge_device(self, dev) -> None:
        """``deviceDeleted`` for a bridge device: stop its node, drop its export list.

        Storage (fabrics + endpoint-number witness) stays on disk, as upstream's
        uninstall leaves it: deleting files nobody can recreate is not this
        callback's call to make.
        """
        running = (getattr(self, "bridge_runtimes", None) or {}).get(dev.id)
        # A device deleted before its first Save has its slot only on the record.
        slot = running.slot if running is not None and running.slot is not None \
            else bridges.slot_of(getattr(dev, "pluginProps", None))
        if running is not None:
            self._stop_bridge_device(dev)
        storage = "(unknown)"
        if slot is not None:
            try:
                instance = bridge_agent.bridge_instance(dev.id, slot)
                storage = instance.storage_path
                agent = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger, instance=instance)
                agent.uninstall()
            except Exception as exc:  # pylint: disable=broad-except
                # a deletion must always complete
                self.logger.error('Matter bridge "%s": its bridge node could not be stopped (%s) — '
                                  "check launchctl for it by hand.", dev.name, exc)
        removed = bridges.NamespacedPrefs(lambda: self.pluginPrefs, bridges.prefs_prefix(dev.id)).clear_namespace()
        if removed:
            self._save_plugin_prefs()  # pylint: disable=no-member  # ExportDialogMixin
        self.logger.info(
            'Matter bridge "%s" deleted: its bridge node has been stopped and its export list '
            "removed. Its pairings are kept on disk (%s); remove the bridge from each ecosystem "
            "it was paired with, where its accessories will now show as not responding.",
            dev.name, storage)

    # ------------------------------------------------------------------
    # Device states
    # ------------------------------------------------------------------
    def _refresh_bridge_devices(self) -> None:
        """Watchdog tick: bring every bridge device's states up to date."""
        for record in self._device_runtimes():
            self._refresh_bridge_device(record)

    def _refresh_bridge_device(self, record, force: bool = False) -> None:
        """Write one bridge device's states — only when they changed."""
        bridge = record.bridge
        client = getattr(bridge, "client", None)
        hello = getattr(client, "hello", None)
        states = bridges.bridge_states(
            export_count=len(record.store) if record.store is not None else 0,
            enabled=bool(getattr(bridge, "enabled", False)),
            connected=bool(getattr(client, "connected", False)),
            attached=bool(getattr(client, "attached", False)),
            fabrics=getattr(bridge, "fabrics", None),
            version=getattr(hello, "bridge_version", None),
            describe_fabric=bridge_health.describe_fabric,
        )
        key = tuple((state["key"], state["value"]) for state in states)
        cache = getattr(self, "_bridge_state_cache", None)
        if cache is None:
            cache = self._bridge_state_cache = {}
        if not force and cache.get(record.device_id) == key:
            return
        cache[record.device_id] = key
        try:
            self.device_sync.apply_states(record.device_id, states)
        except Exception as exc:  # pylint: disable=broad-except
            # cosmetic: the next tick retries (the cache entry is dropped)
            cache.pop(record.device_id, None)
            self.logger.debug("could not update Matter bridge device %s (%s)", record.device_id, exc)
