"""Step 3b: any device as any single-value type ("mapped" exports)."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import export_catalog
from export_store import ExportEntry, ExportStore
from fakes import CustomDevice, FakeIndigoDevices, RelayDevice, SensorDevice

CROSS = export_catalog.OPTION_CROSS


@pytest.fixture
def h(mock_indigo_base):  # noqa: ARG001
    """export_handlers, reloaded against the mocked indigo (it binds indigo at import)."""
    import export_handlers  # pylint: disable=import-outside-toplevel
    return importlib.reload(export_handlers)


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([
        RelayDevice(101, "Utility Relay", onState=True, states={"onOffState": True}),
        SensorDevice(102, "Hall PIR", supportsOnState=True, onState=False, states={"onOffState": False}),
        CustomDevice(103, "Garden Probe", states={"temp": "21.5", "door": "open"}),
    ])
    mock_indigo_base.devices = collection
    return collection


@pytest.fixture
def plug(plugin_mod, devices, mock_indigo_base):  # noqa: ARG001
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = export_catalog.DEFAULT_PLUGIN_ID
    p.pluginPrefs = {}
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    p.export_bridge = Mock(active=False)
    p._exported_ids = frozenset()
    p._export_callback_failed = set()
    p.bridge_runtimes = {}
    p._current_bridge_id = None
    p._exports_changed = Mock()
    return p


def _save(plug, device_id, role, **fields):
    values = plug.exportDeviceChanged({"exportDevice": str(device_id), "exportFilter": ""})
    values.update({"exportRole": role, **fields})
    return plug.exportAddOrUpdate(values)


def test_a_relay_offers_its_own_types_first_then_every_mapped_type(plug):
    rows = plug.getExportRoles(valuesDict={"exportDevice": "101"})
    roles = [role for role, _ in rows]
    assert roles[:3] == ["onOffPlugInUnit", "onOffLight", "doorLock"]
    assert "contactSensor" in roles and "temperatureSensor" in roles
    assert dict(rows)["contactSensor"].endswith("(mapped)")
    assert "thermostat" not in roles[3:], "multi-value types are not offered as mappings"


def test_a_relay_as_a_contact_sensor_follows_its_on_state_and_inversion(plug, devices, h):
    _save(plug, 101, "contactSensor", exportStateKey="", exportStateInvert=False)
    entry = plug.exports.get(101)
    assert entry.options[CROSS] is True
    handler = h.handler_for_entry(entry)
    assert handler.states_for(devices[101], entry.options) == {"contact": True}
    _save(plug, 101, "contactSensor", exportStateKey="", exportStateInvert=True)
    entry = plug.exports.get(101)
    assert h.handler_for_entry(entry).states_for(devices[101], entry.options) == {"contact": False}


def test_a_text_state_maps_onto_a_sensor_and_a_number_onto_temperature(plug, devices, h):
    _save(plug, 103, "contactSensor", exportStateKey="door")
    entry = plug.exports.get(103)
    assert h.handler_for_entry(entry).states_for(devices[103], entry.options) == {"contact": True}
    _save(plug, 103, "temperatureSensor", exportStateKey="temp")
    entry = plug.exports.get(103)
    assert h.handler_for_entry(entry).states_for(devices[103], entry.options) == {"temperatureC": 21.5}


def test_a_sensor_as_a_lock_runs_its_action_groups(plug, devices, h, mock_indigo_base):
    mock_indigo_base.actionGroup = Mock()
    _save(plug, 102, "doorLock", exportCmdOn="9001", exportCmdOff="9002")
    entry = plug.exports.get(102)
    assert entry.options[export_catalog.OPTION_CMD_ON_AG] == 9001
    handler = h.handler_for_entry(entry)
    assert handler.dispatch("lock", {}, devices[102], entry.options) is True
    assert handler.dispatch("unlock", {}, devices[102], entry.options) is True
    assert [c.args[0] for c in mock_indigo_base.actionGroup.execute.call_args_list] == [9001, 9002]


def test_a_sensor_as_a_lock_without_action_groups_says_why_nothing_happened(plug, devices, h):
    _save(plug, 102, "doorLock")
    entry = plug.exports.get(102)
    reason = h.handler_for_entry(entry).dispatch("lock", {}, devices[102], entry.options)
    assert isinstance(reason, str) and "action group" in reason


def test_an_inverted_mapped_relay_flips_its_own_commands(plug, devices, h, mock_indigo_base):
    mock_indigo_base.device = Mock()
    _save(plug, 101, "onOffLight", exportStateKey="", exportStateInvert=True)
    entry = plug.exports.get(101)
    assert entry.options[CROSS] is True
    h.handler_for_entry(entry).dispatch("onOff", {"value": True}, devices[101], entry.options)
    mock_indigo_base.device.turnOff.assert_called_once()


def test_a_plain_export_is_not_mapped(plug, h):
    _save(plug, 101, "onOffPlugInUnit", exportStateKey="", exportStateInvert=False)
    entry = plug.exports.get(101)
    assert CROSS not in entry.options
    assert h.handler_for_entry(entry) is h.handler_for("onOffPlugInUnit")


def test_excluded_and_unmapped_devices_stay_excluded(plug, devices):
    from fakes import SprinklerDevice
    devices.add(SprinklerDevice(104, "Irrigation"))
    plug.exportAddOrUpdate({"exportDevice": "x-104", "exportRole": "onOffPlugInUnit"})
    assert plug.exports.get(104) is None


def test_the_state_picker_starts_with_auto(plug):
    rows = plug.getExportStateKeys(valuesDict={"exportDevice": "101"})
    assert rows[0] == ("", "Auto (on/off state)") and ("onOffState", "onOffState") in rows


@pytest.mark.parametrize("role, options", [
    ("thermostat", {CROSS: True}),
    ("contactSensor", {CROSS: True, export_catalog.OPTION_CMD_ON_AG: 5}),
    ("onOffPlugInUnit", {export_catalog.OPTION_CMD_ON_AG: 5}),
    ("onOffPlugInUnit", {export_catalog.OPTION_STATE_KEY: "x"}),
])
def test_the_store_refuses_mapped_options_where_they_mean_nothing(role, options):
    with pytest.raises(ValueError):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": role, "options": options})
