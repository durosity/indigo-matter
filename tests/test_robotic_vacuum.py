"""ADR-0019 — the plugin half of the robot vacuum (``roboticVacuumCleaner``).

The node half lives in ``bridge-node/test/standalone.test.ts``. This file pins
what only the plugin decides: how a vacuum plugin's status text becomes §4.2
``operationalState``, where the battery comes from, which action group each
command runs, what the store will accept, how §4.3 ``standalone[]`` parses,
and — the one that protects everyone else's exports — that a vacuum is never
sent to a bridge node too old to know the role.
"""
from __future__ import annotations

import importlib

import pytest

import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore
from fakes import (
    CustomDevice,
    FakeBridgeClient,
    FakeIndigoDevices,
    InlineExecutor,
    RecordingRuntime,
    RelayDevice,
)

from conftest import load_bridge_frames

OURS = export_catalog.DEFAULT_PLUGIN_ID
VACUUM = 900021
ROLE = "roboticVacuumCleaner"
FRAMES = load_bridge_frames()


@pytest.fixture
def handlers(mock_indigo_base):
    import export_handlers as module
    importlib.reload(module)
    return module


def tuya_vacuum(status="charge_done", **states):
    return CustomDevice(VACUUM, "Hallway Vacuum", plugin_id="com.racarter.indigoplugin.tuya",
                        deviceTypeId="tuyaDevice",
                        states={"status": status, "suction": "strong", "electricity_left": 87, **states})


# ---------------------------------------------------------------------------
# Status text → operationalState
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    # Tuya's own vocabulary, the plugin this was written for.
    ("standby", "stopped"), ("smart_clean", "running"), ("zone_clean", "running"),
    ("paused", "paused"), ("goto_charge", "seekingCharger"), ("charging", "charging"),
    ("charge_done", "docked"), ("sleep", "stopped"), ("fault", "error"),
    # Other common wordings, and the ordering traps the table exists for.
    ("Returning to dock", "seekingCharger"), ("Docking", "seekingCharger"),
    ("Docked", "docked"), ("Fully charged", "docked"), ("Cleaning", "running"),
    ("Error: stuck", "error"), ("Idle", "stopped"),
    # §4.2's own spellings pass straight through.
    ("seekingCharger", "seekingCharger"), ("docked", "docked"),
    # Booleans: a vacuum modelled as on/off.
    (True, "running"), (False, "stopped"),
])
def test_status_text_maps_to_operational_state(handlers, text, expected):
    assert handlers.rvc_operational_state_of(text) == expected


@pytest.mark.parametrize("text", ["", "?!", "banana", None, 3, 4.5])
def test_unrecognised_status_publishes_nothing(handlers, text):
    """A guessed state about a physical machine is worse than none (§3.4 is partial)."""
    assert handlers.rvc_operational_state_of(text) is None


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
class TestStates:
    def test_reads_a_common_status_state_without_any_declaration(self, handlers):
        handler = handlers.handler_for(ROLE)
        assert handler.states_for(tuya_vacuum("smart_clean")) == {"operationalState": "running"}

    def test_a_declared_state_key_wins(self, handlers):
        handler = handlers.handler_for(ROLE)
        dev = tuya_vacuum("standby", robotPhase="goto_charge")
        options = {export_catalog.OPTION_RVC_STATE_KEY: "robotPhase"}
        assert handler.states_for(dev, options) == {"operationalState": "seekingCharger"}

    def test_a_declared_state_that_is_missing_publishes_nothing(self, handlers):
        """Not a fall back to the guess list: the user said where it lives."""
        handler = handlers.handler_for(ROLE)
        options = {export_catalog.OPTION_RVC_STATE_KEY: "gone"}
        assert handler.states_for(tuya_vacuum(), options) == {}

    def test_a_relay_shaped_vacuum_reads_on_as_cleaning(self, handlers):
        handler = handlers.handler_for(ROLE)
        assert handler.states_for(RelayDevice(1, "Vac", onState=True)) == {"operationalState": "running"}
        assert handler.states_for(RelayDevice(1, "Vac", onState=False)) == {"operationalState": "stopped"}

    def test_battery_comes_from_a_state_when_indigo_has_none(self, handlers):
        handler = handlers.handler_for(ROLE)
        dev = tuya_vacuum()
        assert handler.published_states(dev) == {"operationalState": "docked", "batteryLevel": 87}
        assert handler.has_battery(dev) is True
        assert "batteryLevel" not in handler.states_for(dev), "battery rides the wrapper, never states_for"

    def test_indigos_own_battery_level_wins_over_a_state(self, handlers):
        handler = handlers.handler_for(ROLE)
        dev = tuya_vacuum()
        dev.batteryLevel = 42
        assert handler.published_states(dev)["batteryLevel"] == 42

    def test_a_declared_battery_state_is_used_and_judged_like_batterylevel(self, handlers):
        handler = handlers.handler_for(ROLE)
        options = {export_catalog.OPTION_RVC_BATTERY_KEY: "juice"}
        assert handler.published_states(tuya_vacuum(juice=55.4), options)["batteryLevel"] == 55
        # 0 is "never polled", not a reading — suppressed, as for batteryLevel.
        assert "batteryLevel" not in handler.published_states(tuya_vacuum(juice=0), options)
        assert handler.has_battery(tuya_vacuum(juice=0), options) is True

    def test_no_battery_anywhere(self, handlers):
        handler = handlers.handler_for(ROLE)
        dev = CustomDevice(1, "Robot", states={"status": "idle"})
        assert handler.has_battery(dev) is False
        assert handler.published_states(dev) == {"operationalState": "stopped"}


class TestCommands:
    OPTIONS = {
        "rvcActionStart": 11, "rvcActionStop": 12, "rvcActionPause": 13, "rvcActionGoHome": 15,
        "rvcActionLocate": 16,
    }

    @pytest.mark.parametrize("command, group", [
        ("start", 11), ("stop", 12), ("pause", 13), ("goHome", 15), ("locate", 16),
        # resume has no group of its own here, so it reuses start's.
        ("resume", 11),
    ])
    def test_each_command_runs_its_action_group(self, handlers, mock_indigo_base, command, group):
        handler = handlers.handler_for(ROLE)
        assert handler.dispatch(command, {}, tuya_vacuum(), self.OPTIONS) is True
        mock_indigo_base.actionGroup.execute.assert_called_once_with(group)

    def test_resume_prefers_its_own_group(self, handlers, mock_indigo_base):
        handler = handlers.handler_for(ROLE)
        handler.dispatch("resume", {}, tuya_vacuum(), {**self.OPTIONS, "rvcActionResume": 14})
        mock_indigo_base.actionGroup.execute.assert_called_once_with(14)

    def test_a_command_with_no_group_is_a_reasoned_no_op(self, handlers, mock_indigo_base):
        handler = handlers.handler_for(ROLE)
        outcome = handler.dispatch("goHome", {}, tuya_vacuum(), {})
        assert isinstance(outcome, str) and "goHome" in outcome
        mock_indigo_base.actionGroup.execute.assert_not_called()

    def test_a_relay_vacuum_falls_back_to_on_off_for_start_and_stop_only(self, handlers, mock_indigo_base):
        handler = handlers.handler_for(ROLE)
        dev = RelayDevice(7, "Vac")
        assert handler.dispatch("start", {}, dev, {}) is True
        mock_indigo_base.device.turnOn.assert_called_once_with(dev)
        assert handler.dispatch("stop", {}, dev, {}) is True
        mock_indigo_base.device.turnOff.assert_called_once_with(dev)
        assert isinstance(handler.dispatch("pause", {}, dev, {}), str)
        assert isinstance(handler.dispatch("locate", {}, dev, {}), str), "no on/off fallback for a sound"
        mock_indigo_base.device.turnOn.assert_called_once()

    def test_nothing_is_auto_confirmed(self, handlers):
        """PRD §7 — a command never claims the vacuum moved."""
        handler = handlers.handler_for(ROLE)
        for command in bridge_protocol.ROLE_COMMANDS[ROLE]:
            assert handler.commanded_states(command, {}) == {}

    def test_a_garbage_group_id_is_ignored_not_executed(self, handlers, mock_indigo_base):
        handler = handlers.handler_for(ROLE)
        for bad in ("", "abc", 0, -3, True, None):
            outcome = handler.dispatch("stop", {}, tuya_vacuum(), {"rvcActionStop": bad})
            assert isinstance(outcome, str)
        mock_indigo_base.actionGroup.execute.assert_not_called()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class TestStore:
    def _entry(self, role=ROLE, **options):
        return {"indigoDeviceId": VACUUM, "role": role, "options": options}

    def test_accepts_a_full_vacuum_declaration(self):
        entry = ExportEntry.from_dict(self._entry(
            rvcStateKey="status", rvcBatteryKey="electricity_left",
            rvcActionStart=1, rvcActionStop=2, rvcActionPause=3, rvcActionResume=4, rvcActionGoHome=5,
            rvcActionLocate=6))
        assert entry.options["rvcActionGoHome"] == 5
        assert entry.options["rvcActionLocate"] == 6

    @pytest.mark.parametrize("options", [
        {"rvcActionStart": "1"}, {"rvcActionStart": 0}, {"rvcActionStart": True},
        {"rvcStateKey": ""}, {"rvcBatteryKey": 3},
    ])
    def test_rejects_a_malformed_vacuum_option(self, options):
        with pytest.raises(ValueError):
            ExportEntry.from_dict(self._entry(**options))

    def test_rejects_a_vacuum_option_on_any_other_role(self):
        """An action group on a plug would run on "turn on" — silent cross-wiring."""
        with pytest.raises(ValueError, match="not a robot vacuum"):
            ExportEntry.from_dict(self._entry(role="onOffPlugInUnit", rvcActionStart=1))


# ---------------------------------------------------------------------------
# The protocol mirror
# ---------------------------------------------------------------------------
class TestProtocol:
    def test_the_vacuum_is_the_one_standalone_role(self):
        assert [role for role in bridge_protocol.ROLES if bridge_protocol.is_standalone_role(role)] == [ROLE]

    def test_parses_standalone_from_the_golden_status(self):
        status = bridge_protocol.parse_status(FRAMES["get_status_standalone"]["response"]["result"])
        (vacuum,) = status.standalone
        assert (vacuum.indigo_device_id, vacuum.role, vacuum.port, vacuum.online, vacuum.commissioned) == \
            (VACUUM, ROLE, 5541, True, True)
        assert [fabric.label for fabric in vacuum.fabrics] == ["Apple Home"]
        assert status.endpoint_count == len(status.endpoints), "the vacuum is not an aggregator endpoint"

    def test_an_older_nodes_status_has_no_standalone(self):
        status = bridge_protocol.parse_status(FRAMES["get_status"]["response"]["result"])
        result = dict(FRAMES["get_status"]["response"]["result"])
        result.pop("standalone", None)
        assert bridge_protocol.parse_status(result).standalone == []
        assert status.standalone == []

    def test_one_malformed_standalone_entry_does_not_cost_the_status(self):
        result = dict(FRAMES["get_status_standalone"]["response"]["result"])
        result["standalone"] = [{"role": ROLE}, *result["standalone"]]
        assert len(bridge_protocol.parse_status(result).standalone) == 1

    @pytest.mark.parametrize("version, supported", [
        ("0.18.0", True), ("0.18.3", True), ("1.0.0", True), ("0.18.0-rc.1", True),
        ("0.17.5", False), ("0.9.99", False), ("unknown", False), (None, False), ("0.18", False),
    ])
    def test_standalone_needs_bridge_0_18(self, version, supported):
        assert bridge_protocol.supports_standalone(version) is supported


# ---------------------------------------------------------------------------
# The version guard in ExportBridge — protects everyone else's exports
# ---------------------------------------------------------------------------
@pytest.fixture
def bridge_mod(mock_indigo_base):
    import export_handlers
    import export_bridge as module
    importlib.reload(export_handlers)
    importlib.reload(module)
    return module


def _bridge(module, mock_logger, devices, entries, bridge_version):
    prefs: dict = {}
    store = ExportStore(lambda: prefs, mock_logger)
    for entry in entries:
        store.upsert(entry)
    bridge = module.ExportBridge(
        store, RecordingRuntime(), mock_logger, lambda: prefs,
        plugin_version="2026.35.0", plugin_id=OURS,
        device_getter=lambda device_id: next((d for d in devices if d.id == device_id), None),
        client_factory=lambda **_kwargs: FakeBridgeClient(),
        save_prefs=lambda: None,
        executor_factory=InlineExecutor,
    )
    client = FakeBridgeClient()
    if bridge_version is not None:
        client.hello = bridge_protocol.Hello(2, bridge_version, "0.17.8")
    bridge.client = client
    return bridge


class TestVersionGuard:
    def _devices(self):
        return FakeIndigoDevices([tuya_vacuum("smart_clean"), RelayDevice(101, "Study Plug", onState=True)])

    def _entries(self):
        return [ExportEntry(VACUUM, ROLE), ExportEntry(101, "onOffPlugInUnit")]

    def test_a_new_node_gets_the_vacuum_with_its_battery(self, bridge_mod, mock_logger):
        bridge = _bridge(bridge_mod, mock_logger, self._devices(), self._entries(), "0.18.0")
        specs = {spec.indigo_device_id: spec for spec in bridge.endpoint_specs()}
        vacuum = specs[VACUUM]
        assert vacuum.role == ROLE
        assert vacuum.states == {"operationalState": "running", "batteryLevel": 87}
        assert vacuum.battery is True, "the battery lives in a state, and still counts as evidence"

    @pytest.mark.parametrize("version", ["0.17.5", None])
    def test_an_old_or_unknown_node_never_sees_the_vacuum(self, bridge_mod, mock_logger, version):
        """An older node refuses the WHOLE attach over an unknown role. Withhold
        the vacuum instead, and keep everything else flowing."""
        bridge = _bridge(bridge_mod, mock_logger, self._devices(), self._entries(), version)
        assert [spec.indigo_device_id for spec in bridge.endpoint_specs()] == [101]
        warned = " ".join(str(call) for call in mock_logger.warning.call_args_list)
        assert "too old to publish a robot vacuum" in warned


class TestAttachReport:
    """Field report 2026-11-20: with only a vacuum exported the attach line read
    "0 endpoint(s) live, not yet paired" and said nothing about the vacuum."""

    def _status(self, **summary):
        base = {"indigoDeviceId": VACUUM, "role": ROLE, "publishedAs": f"indigo-{VACUUM}", "port": 5541,
                "online": True, "commissioned": False, "fabrics": []}
        result = dict(FRAMES["get_status"]["response"]["result"])
        result["standalone"] = [{**base, **summary}]
        return bridge_protocol.parse_status(result)

    def _logged(self, logger):
        calls = logger.info.call_args_list + logger.warning.call_args_list
        return " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0]) for c in calls)

    def _bridge(self, bridge_mod, mock_logger):
        entry = ExportEntry(VACUUM, ROLE, name_override="Hallway Roomba")
        return _bridge(bridge_mod, mock_logger, FakeIndigoDevices([tuya_vacuum()]), [entry], "0.18.0")

    def test_an_unpaired_vacuum_points_at_the_vacuum_pairing_menu(self, bridge_mod, mock_logger):
        self._bridge(bridge_mod, mock_logger)._report_standalone(self._status())
        logged = self._logged(mock_logger)
        assert '"Hallway Roomba" is online' in logged and "port 5541" in logged
        assert "Pair Matter Vacuum" in logged

    def test_a_paired_vacuum_says_how_many_ecosystems(self, bridge_mod, mock_logger):
        status = self._status(commissioned=True, fabrics=[{"fabricIndex": 1, "label": "", "vendorId": 4937}])
        self._bridge(bridge_mod, mock_logger)._report_standalone(status)
        assert "paired with 1 ecosystem" in self._logged(mock_logger)

    def test_a_vacuum_whose_node_failed_is_a_warning(self, bridge_mod, mock_logger):
        self._bridge(bridge_mod, mock_logger)._report_standalone(self._status(online=False))
        assert "could NOT start" in str(mock_logger.warning.call_args_list)

    def test_an_older_node_reports_nothing_extra(self, bridge_mod, mock_logger):
        status = bridge_protocol.parse_status(
            {k: v for k, v in FRAMES["get_status"]["response"]["result"].items() if k != "standalone"})
        self._bridge(bridge_mod, mock_logger)._report_standalone(status)
        assert self._logged(mock_logger) == ""


# ---------------------------------------------------------------------------
# Rooms (Service Area) and cleaning modes (RVC Clean Mode)
# ---------------------------------------------------------------------------
ROOMS_TEXT = "1=Kitchen, 7=Hallway"
ROOMS_OPTIONS = {"rvcRooms": ROOMS_TEXT, "rvcRoomsVariable": 501,
                 "rvcCleanModes": ["vacuum", "mop"], "rvcModeVariable": 502}


class TestRoomsAndModes:
    @pytest.mark.parametrize("text, rooms", [
        ("1=Kitchen, 7=Hallway", [(1, "Kitchen"), (7, "Hallway")]),
        ("1:Kitchen; 7 : Hallway\n12=Lounge", [(1, "Kitchen"), (7, "Hallway"), (12, "Lounge")]),
        ("", []), ("  ,  ", []),
    ])
    def test_parse_rooms(self, handlers, text, rooms):
        assert handlers.parse_rooms(text) == rooms

    @pytest.mark.parametrize("text", ["Kitchen", "x=Kitchen", "1=", "1=A, 1=B", "1=Kitchen, 2=kitchen",
                                      "-1=Kitchen", 7])
    def test_parse_rooms_refuses_with_a_reason(self, handlers, text):
        with pytest.raises(ValueError):
            handlers.parse_rooms(text)

    def test_the_wire_carries_rooms_and_modes_in_the_nodes_shape(self, handlers):
        assert handlers.handler_for(ROLE).wire_options(ROOMS_OPTIONS) == {
            "areas": [{"areaId": 1, "name": "Kitchen"}, {"areaId": 7, "name": "Hallway"}],
            "cleanModes": ["vacuum", "mop"],
        }

    def test_nothing_declared_sends_nothing(self, handlers):
        assert handlers.handler_for(ROLE).wire_options({}) == {}
        assert handlers.handler_for(ROLE).wire_options({"rvcCleanModes": ["vacuum"]}) == {}, \
            "one mode is nothing to choose between"

    def test_no_other_role_sends_wire_options(self, handlers):
        for role, handler in handlers.HANDLERS.items():
            if role != ROLE:
                assert handler.wire_options(ROOMS_OPTIONS) == {}, role

    def test_a_room_selection_is_written_to_the_variable(self, handlers, mock_indigo_base):
        handler = handlers.handler_for(ROLE)
        assert handler.dispatch("selectAreas", {"areaIds": [7, 1, 99]}, tuya_vacuum(), ROOMS_OPTIONS) is True
        mock_indigo_base.variable.updateValue.assert_called_once_with(501, value="7,1")
        mock_indigo_base.actionGroup.execute.assert_not_called()  # start does that

    def test_everywhere_is_an_empty_value(self, handlers, mock_indigo_base):
        handlers.handler_for(ROLE).dispatch("selectAreas", {"areaIds": []}, tuya_vacuum(), ROOMS_OPTIONS)
        mock_indigo_base.variable.updateValue.assert_called_once_with(501, value="")

    def test_a_mode_is_written_to_the_variable(self, handlers, mock_indigo_base):
        assert handlers.handler_for(ROLE).dispatch("setCleanMode", {"mode": "mop"}, tuya_vacuum(),
                                                   ROOMS_OPTIONS) is True
        mock_indigo_base.variable.updateValue.assert_called_once_with(502, value="mop")

    def test_an_undeclared_mode_writes_nothing(self, handlers, mock_indigo_base):
        outcome = handlers.handler_for(ROLE).dispatch("setCleanMode", {"mode": "vacuumThenMop"},
                                                      tuya_vacuum(), ROOMS_OPTIONS)
        assert isinstance(outcome, str)
        mock_indigo_base.variable.updateValue.assert_not_called()

    def test_no_variable_chosen_is_a_reasoned_no_op(self, handlers, mock_indigo_base):
        options = {"rvcRooms": ROOMS_TEXT}
        outcome = handlers.handler_for(ROLE).dispatch("selectAreas", {"areaIds": [1]}, tuya_vacuum(), options)
        assert isinstance(outcome, str) and "variable" in outcome
        mock_indigo_base.variable.updateValue.assert_not_called()

    def test_the_store_accepts_the_declaration(self):
        entry = ExportEntry.from_dict({"indigoDeviceId": VACUUM, "role": ROLE, "options": dict(ROOMS_OPTIONS)})
        assert entry.options["rvcRooms"] == ROOMS_TEXT

    @pytest.mark.parametrize("options", [
        {"rvcRooms": "Kitchen"}, {"rvcRooms": 7}, {"rvcCleanModes": ["steam"]},
        {"rvcCleanModes": "vacuum"}, {"rvcCleanModes": ["mop", "mop"]}, {"rvcRoomsVariable": "501"},
        {"rvcModeVariable": 0},
    ])
    def test_the_store_refuses_a_malformed_declaration(self, options):
        with pytest.raises(ValueError):
            ExportEntry.from_dict({"indigoDeviceId": VACUUM, "role": ROLE, "options": options})

    def test_the_spec_carries_rooms_on_the_wire(self, bridge_mod, mock_logger):
        entry = ExportEntry(VACUUM, ROLE, options=dict(ROOMS_OPTIONS))
        bridge = _bridge(bridge_mod, mock_logger, FakeIndigoDevices([tuya_vacuum()]), [entry], "0.18.0")
        (spec,) = bridge.endpoint_specs()
        assert spec.options == {
            "areas": [{"areaId": 1, "name": "Kitchen"}, {"areaId": 7, "name": "Hallway"}],
            "cleanModes": ["vacuum", "mop"],
        }
        assert "rvcRooms" not in spec.options, "the stored text never leaves the plugin"
