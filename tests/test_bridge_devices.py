"""Step 2b: Matter Bridge devices — each its own ExportBridge, node and export list.

(``test_multi_bridge.py`` covers ``bridges.py`` on its own; ``test_bridges.py``
is an unrelated upstream file.)
"""
from __future__ import annotations

import importlib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_protocol
import bridges
import export_catalog
from export_store import ExportEntry, ExportStore
from fakes import FakeIndigoDevices, RelayDevice

OURS = export_catalog.DEFAULT_PLUGIN_ID
SERVER_PLUGIN = Path(__file__).resolve().parent.parent / "Matter Prototype.indigoPlugin" / "Contents" / "Server Plugin"


class _FakeRuntime:
    is_running = True

    def submit(self, coro):
        if hasattr(coro, "close"):
            coro.close()
        return Mock()


def _bridge_dev(dev_id, name, slot):
    props = {} if slot is None else {bridges.PROP_SLOT: str(slot)}
    return SimpleNamespace(id=dev_id, name=name, deviceTypeId=bridges.BRIDGE_DEVICE_TYPE,
                           pluginProps=props, stateListOrDisplayStateIdChanged=lambda: None)


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([RelayDevice(101, "Study Plug", onState=False),
                                    RelayDevice(102, "Porch Light", onState=False)])
    mock_indigo_base.devices = collection
    return collection


@pytest.fixture
def plug(plugin_mod, devices, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("HOME", str(tmp_path))
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = OURS
    p._version = "2026.7.28"
    p.pluginPrefs = {}
    p.device_sync = Mock()
    p.runtime = _FakeRuntime()
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    p.export_bridge = Mock(active=False)
    p._exported_ids = frozenset()
    p._subscribed_to_devices = True
    p._device_updates_seen = False
    p._export_callback_failed = set()
    p._install_thread = None
    p._stopping = False
    p.bridge_runtimes = {}       # Plugin.__init__ sets these; __new__ skips it
    p._current_bridge_id = None
    return p


def _start(plug, devices, dev_id=500, name="Upstairs", slot=1):
    dev = _bridge_dev(dev_id, name, slot)
    devices.add(dev)
    plug.deviceStartComm(dev)
    return dev


# ---------------------------------------------------------------------------
# which bridge
# ---------------------------------------------------------------------------
def test_with_no_bridge_devices_the_properties_are_the_legacy_bridge(plug):
    legacy = plug._legacy_runtime()
    assert plug.exports is legacy.store and plug.export_bridge is legacy.bridge
    plug.bridge_process = "agent"
    assert legacy.process == "agent"


def test_the_dialog_choice_retargets_the_properties(plug, devices):
    _start(plug, devices)
    record = plug.bridge_runtimes[500]
    plug.bridgeTargetChanged({"bridgeTarget": "500"})
    assert plug.exports is record.store and plug.export_bridge is record.bridge
    plug.bridgeTargetChanged({"bridgeTarget": "0"})
    assert plug.exports is plug._legacy_runtime().store
    plug.bridgeTargetChanged({"bridgeTarget": "999"})   # not a running bridge: legacy, not a crash
    assert plug.export_bridge is plug._legacy_runtime().bridge


def test_a_thread_pin_wins_over_the_dialog_and_is_restored(plug, devices):
    _start(plug, devices)
    plug.bridgeTargetChanged({"bridgeTarget": "500"})
    legacy = plug._legacy_runtime()
    with plug._targeting(legacy):
        assert plug.exports is legacy.store
    assert plug.exports is plug.bridge_runtimes[500].store


def test_opening_a_dialog_resets_the_choice_to_the_first_entry(plug, devices):
    _start(plug, devices, 500, "Upstairs", 1)
    _start(plug, devices, 600, "Garden", 2)
    plug._current_bridge_id = 500
    options = plug.getBridgeTargets()
    assert options == [("600", "Garden"), ("500", "Upstairs"), ("0", "Legacy bridge (no bridge device)")]
    assert plug._current_bridge_id == 600, "the dialog shows the first entry selected"


def test_a_reload_keeps_the_choice_the_popup_shows(plug, devices):
    _start(plug, devices, 500, "Upstairs", 1)
    _start(plug, devices, 600, "Garden", 2)
    plug.getBridgeTargets(valuesDict={"bridgeTarget": "500"})
    assert plug._current_bridge_id == 500
    plug.getBridgeTargets(valuesDict={"bridgeTarget": "0"})
    assert plug._current_bridge_id is None


def test_with_no_bridge_devices_the_popup_offers_the_legacy_bridge(plug):
    plug._current_bridge_id = 42
    assert plug.getBridgeTargets() == [("0", "Legacy bridge (no bridge device)")]
    assert plug._current_bridge_id is None


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------
def test_a_new_bridge_device_gets_the_lowest_free_slot_never_the_legacy_one(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", 1))
    ok, values = plug.validateDeviceConfigUi({}, bridges.BRIDGE_DEVICE_TYPE, 0)
    assert ok and values[bridges.PROP_SLOT] == "2"


def test_a_slot_is_kept_once_allocated(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", 1))
    ok, values = plug.validateDeviceConfigUi({bridges.PROP_SLOT: "7"}, bridges.BRIDGE_DEVICE_TYPE, 600)
    assert ok and values[bridges.PROP_SLOT] == "7"


def test_a_duplicated_device_gets_its_own_slot(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", 1))
    ok, values = plug.validateDeviceConfigUi({bridges.PROP_SLOT: "1"}, bridges.BRIDGE_DEVICE_TYPE, 600)
    assert ok and values[bridges.PROP_SLOT] == "2"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_starting_a_bridge_device_builds_its_own_engine_store_and_ports(plug, devices, plugin_mod):
    _start(plug, devices, slot=3)
    record = plug.bridge_runtimes[500]
    assert record.instance.matter_port == str(int(plugin_mod.bridge_agent.DEFAULT_MATTER_PORT) + 30)
    assert record.prefs[bridge_protocol.PREF_WS_PORT] == record.instance.ws_port
    assert record.bridge is not plug._legacy_runtime().bridge
    record.store.upsert(ExportEntry(101, "onOffPlugInUnit"))
    assert any(k.startswith("bridge.500.") for k in plug.pluginPrefs)
    assert len(plug._legacy_runtime().store) == 0, "the legacy list is untouched"
    states = dict((kv["key"], kv["value"]) for kv in plug.device_sync.apply_states.call_args[0][1])
    assert states["status"] == "idle" and states["online"] is False


@pytest.mark.parametrize("slot", [None, bridges.LEGACY_SLOT])
def test_a_bridge_device_without_a_usable_slot_is_refused(plug, devices, slot):
    _start(plug, devices, slot=slot)
    assert 500 not in plug.bridge_runtimes
    plug.logger.error.assert_called()


def test_two_bridge_devices_on_one_slot_start_only_once(plug, devices):
    _start(plug, devices, 500, "Upstairs", 1)
    _start(plug, devices, 600, "Copy of Upstairs", 1)
    assert 600 not in plug.bridge_runtimes and 500 in plug.bridge_runtimes


def test_stopping_a_bridge_device_drops_it_and_its_dialog_choice(plug, devices):
    dev = _start(plug, devices)
    record = plug.bridge_runtimes[500]
    record.bridge = Mock()
    bridge = record.bridge
    plug.bridgeTargetChanged({"bridgeTarget": "500"})
    plug.deviceStopComm(dev)
    bridge.stop.assert_called_once()
    assert 500 not in plug.bridge_runtimes and plug._current_bridge_id is None


def test_deleting_a_bridge_device_stops_its_node_and_clears_its_list(plug, devices, plugin_mod, monkeypatch):
    built = []

    class _Agent:
        def __init__(self, prefs, logger, instance=None):
            built.append(instance)
            self.uninstalled = False

        def uninstall(self):
            built.append("uninstall")

    monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _Agent)
    plug._save_plugin_prefs = Mock()
    dev = _start(plug, devices)
    plug.bridge_runtimes[500].store.upsert(ExportEntry(101, "onOffPlugInUnit"))
    plug.pluginPrefs["bridge.5000.keep"] = "another bridge"
    plug.deviceDeleted(dev)
    assert built[0].label.endswith(".500") and built[1] == "uninstall"
    assert not any(k.startswith("bridge.500.") for k in plug.pluginPrefs)
    assert plug.pluginPrefs["bridge.5000.keep"] == "another bridge"
    assert 500 not in plug.bridge_runtimes


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------
def _with_two_bridges(plug, devices):
    _start(plug, devices)
    record = plug.bridge_runtimes[500]
    record.store.upsert(ExportEntry(102, "onOffPlugInUnit"))
    record.bridge = Mock()
    plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    plug._exports_changed([])
    return plug._legacy_runtime().bridge, record.bridge


def test_a_change_goes_only_to_the_bridge_exporting_the_device(plug, devices):
    legacy, upstairs = _with_two_bridges(plug, devices)
    plug.deviceUpdated(devices[102], devices[102])
    upstairs.device_updated.assert_called_once()
    legacy.device_updated.assert_not_called()
    plug.deviceUpdated(devices[101], devices[101])
    legacy.device_updated.assert_called_once()
    assert upstairs.device_updated.call_count == 1


def test_one_failing_bridge_does_not_starve_another(plug, devices):
    legacy, upstairs = _with_two_bridges(plug, devices)
    plug._legacy_runtime().store.upsert(ExportEntry(102, "onOffPlugInUnit"))
    plug._exports_changed([])
    legacy.device_updated.side_effect = RuntimeError("boom")
    plug.deviceUpdated(devices[102], devices[102])
    upstairs.device_updated.assert_called_once()
    plug.logger.error.assert_called()


def test_a_deleted_device_leaves_the_list_of_the_bridge_that_had_it(plug, devices):
    legacy, upstairs = _with_two_bridges(plug, devices)
    plug.deviceDeleted(devices[102])
    assert 102 not in plug.bridge_runtimes[500].store.ids()
    upstairs.remove.assert_called_once_with(102)
    legacy.remove.assert_not_called()
    assert 102 not in plug._exported_ids and 101 in plug._exported_ids


def test_the_watchdog_ticks_every_bridge_and_refreshes_bridge_devices(plug, devices):
    legacy, upstairs = _with_two_bridges(plug, devices)
    upstairs.client = Mock(connected=True, attached=True, hello=Mock(bridge_version="0.17.5"))
    upstairs.enabled = True
    upstairs.fabrics = []
    plug.runtime = None
    plug.matter = None
    plug._resubscribe_tick = Mock()
    plug._port_conflict_tick = Mock()
    plug._health_tick()
    legacy.health_tick.assert_called_once()
    upstairs.health_tick.assert_called_once()
    states = dict((kv["key"], kv["value"]) for kv in plug.device_sync.apply_states.call_args[0][1])
    assert states == {"status": "online", "online": True, "paired": False, "ecosystems": "none",
                      "endpoints": 1, "bridgeVersion": "0.17.5"}
    calls = plug.device_sync.apply_states.call_count
    plug._health_tick()
    assert plug.device_sync.apply_states.call_count == calls, "unchanged states are not rewritten"


def test_a_list_edit_nudges_only_the_bridge_the_dialog_is_on(plug, devices):
    legacy, upstairs = _with_two_bridges(plug, devices)
    legacy.reset_mock()
    plug.bridgeTargetChanged({"bridgeTarget": "500"})
    plug._exports_changed()
    upstairs.exports_changed.assert_called_once()
    legacy.exports_changed.assert_not_called()


# ---------------------------------------------------------------------------
# the agent seams act on their own bridge
# ---------------------------------------------------------------------------
def test_a_bridge_devices_agent_is_built_with_its_instance_and_kept_on_its_record(plug, devices,
                                                                                   plugin_mod, monkeypatch):
    built = []

    class _Agent:
        RUNNING = "running"

        def __init__(self, prefs, logger, instance=None):
            built.append(instance)
            self.ws_port, self.matter_port = "x", "y"

        def ensure_installed(self):
            return True

        def run_state(self):
            return self.RUNNING

    monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _Agent)
    _start(plug, devices)
    record = plug.bridge_runtimes[500]
    plug._start_bridge_agent(record)
    assert built[-1] is record.instance and isinstance(record.process, _Agent)
    assert plug._legacy_runtime().process is None, "the legacy bridge's agent slot is untouched"


def test_the_pairing_page_url_names_a_device_bridge(plug, devices):
    plug._iws_page_url = lambda action: "http://mac:8176/pairing"
    _start(plug, devices)
    assert plug._pairing_page_url() == "http://mac:8176/pairing"
    plug.bridgeTargetChanged({"bridgeTarget": "500"})
    assert plug._pairing_page_url() == "http://mac:8176/pairing?bridge=500"


def test_the_pairing_page_serves_the_bridge_it_is_asked_for(plug, devices):
    _start(plug, devices)
    plug.bridgeTargetChanged({"bridgeTarget": "500"})   # a dialog choice must not leak into the page
    assert plug._pairing_page_bridge({}) is plug._legacy_runtime().bridge
    assert plug._pairing_page_bridge({"bridge": "500"}) is plug.bridge_runtimes[500].bridge
    assert plug._pairing_page_bridge({"bridge": "77"}) is None


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------
def test_the_bridge_device_type_is_declared_with_its_states():
    root = ET.parse(SERVER_PLUGIN / "Devices.xml").getroot()
    dev = root.find("./Device[@id='matterBridge']")
    assert dev is not None
    assert dev.find("UiDisplayStateId").text == "status"
    assert {s.get("id") for s in dev.find("States")} == {
        "status", "online", "paired", "ecosystems", "endpoints", "bridgeVersion"}
    assert dev.find("ConfigUI/Field[@id='bridgeSlot']").get("hidden") == "true"


def test_the_bridge_device_dialog_is_the_whole_export_panel_plus_pairing():
    dev_ui = ET.parse(SERVER_PLUGIN / "Devices.xml").getroot().find("./Device[@id='matterBridge']/ConfigUI")
    menu_ui = ET.parse(SERVER_PLUGIN / "MenuItems.xml").getroot().find(
        "./MenuItem[@id='manageMatterExports']/ConfigUI")
    panel = {f.get("id") for f in menu_ui} - {"bridgeTarget"}
    fields = {f.get("id") for f in dev_ui}
    assert panel <= fields, sorted(panel - fields)
    assert "bridgeTarget" not in fields, "the device dialog IS its bridge: no popup"
    assert {"exportRvcStart", "exportRvcModeActionMop", "exportList", "bridgePair"} <= fields
    assert dev_ui.find("Field[@id='bridgePair']/CallbackMethod").text == "bridgeDevicePair"


def test_opening_a_new_bridge_devices_dialog_starts_its_bridge_and_targets_it(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", None))
    values, _ = plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 500)
    assert values[bridges.PROP_SLOT] == "1"
    record = plug.bridge_runtimes[500]
    assert record.slot == 1 and plug.exports is record.store
    plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    assert len(plug._legacy_runtime().store) == 0, "the legacy list is untouched"
    plug.closedDeviceConfigUi(values, False, bridges.BRIDGE_DEVICE_TYPE, 500)
    assert plug.exports is plug._legacy_runtime().store


def test_a_second_new_bridge_does_not_take_an_unsaved_bridges_slot(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", None))
    devices.add(_bridge_dev(600, "Garden", None))
    plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 500)
    values, _ = plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 600)
    assert values[bridges.PROP_SLOT] == "2"


def test_a_bridge_dialog_whose_bridge_cannot_run_acts_on_nothing(plug, devices):
    plug.runtime = None          # cannot start a bridge
    devices.add(_bridge_dev(500, "Upstairs", None))
    plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 500)
    assert plug.exports is None and plug.export_bridge is None, "never the legacy bridge"


def test_saving_a_bridge_device_keeps_only_its_slot(plug, devices):
    ok, values = plug.validateDeviceConfigUi(
        {bridges.PROP_SLOT: "3", "exportDevice": "101", "exportName": "x", "duration": "900"},
        bridges.BRIDGE_DEVICE_TYPE, 500)
    assert ok and dict(values) == {bridges.PROP_SLOT: "3"}


def test_the_dialog_pairing_button_pairs_its_own_bridge(plug, devices):
    devices.add(_bridge_dev(500, "Upstairs", None))
    plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 500)
    seen = []
    plug.menuPairMatterBridge = lambda values: seen.append(plug.export_bridge) or (True, values)
    plug._current_bridge_id = None   # even with nothing targeted, the button knows its bridge
    assert plug.bridgeDevicePair({"duration": "900"}, bridges.BRIDGE_DEVICE_TYPE, 500) == {"duration": "900"}
    assert seen == [plug.bridge_runtimes[500].bridge]
    _, errors = plug.bridgeDevicePair({}, bridges.BRIDGE_DEVICE_TYPE, 999)
    assert "duration" in errors


def test_deleting_an_unsaved_bridge_device_still_stops_its_node(plug, devices, plugin_mod, monkeypatch):
    built = []

    class _Agent:
        def __init__(self, prefs, logger, instance=None):
            built.append(instance)

        def uninstall(self):
            built.append("uninstall")

    monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _Agent)
    dev = _bridge_dev(500, "Upstairs", None)
    devices.add(dev)
    plug.getDeviceConfigUiValues({}, bridges.BRIDGE_DEVICE_TYPE, 500)
    plug.deviceDeleted(dev)
    assert built and built[-1] == "uninstall"


def test_every_bridge_dialog_opens_with_the_bridge_popup(plugin_mod):
    root = ET.parse(SERVER_PLUGIN / "MenuItems.xml").getroot()
    for menu_id in ("manageMatterExports", "pairMatterBridge", "pairMatterVacuum", "unpairEcosystem",
                    "stopBridgeNode", "rebuildEndpointMap", "migrateExport", "resetBridgePairings"):
        first = root.find(f"./MenuItem[@id='{menu_id}']/ConfigUI/Field")
        assert first.get("id") == "bridgeTarget", menu_id
        assert first.find("List").get("method") == "getBridgeTargets"
        assert first.find("List").get("dynamicReload") == "true"
    assert callable(plugin_mod.Plugin.getBridgeTargets) and callable(plugin_mod.Plugin.bridgeTargetChanged)


# ---------------------------------------------------------------------------
# bridges.py additions
# ---------------------------------------------------------------------------
def test_port_check_attrs_keep_the_upstream_names_for_the_legacy_bridge():
    assert bridges.port_check_attrs(0) == ("_next_bridge_port_check", "_last_bridge_port_conflict")
    assert bridges.port_check_attrs(500) == ("_next_bridge_port_check_500", "_last_bridge_port_conflict_500")


@pytest.mark.parametrize("kwargs, status", [
    ({"export_count": 0}, "idle"),
    ({"enabled": False}, "switched off"),
    ({"connected": False}, "not connected"),
    ({"attached": False}, "not serving"),
    ({}, "online"),
])
def test_bridge_status(kwargs, status):
    base = {"export_count": 2, "enabled": True, "connected": True, "attached": True,
            "fabrics": None, "version": None}
    base.update(kwargs)
    states = {kv["key"]: kv["value"] for kv in bridges.bridge_states(**base)}
    assert states["status"] == status and states["online"] is (status == "online")
    assert states["ecosystems"] == "unknown", "not knowing is not the same as unpaired"
