"""Step 4: the built-in bridge moves into a Matter Bridge device."""
from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridges
from export_store import PREF_KEY, ExportEntry, ExportStore
from fakes import FakeIndigoDevices, RelayDevice


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def world(plugin_mod, mock_indigo_base, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    devices = FakeIndigoDevices([RelayDevice(101, "Study Plug")])
    mock_indigo_base.devices = devices
    created = []

    def create(**kwargs):
        dev = SimpleNamespace(id=7000 + len(created), name=kwargs["name"], deviceTypeId=kwargs["deviceTypeId"],
                              pluginProps=dict(kwargs.get("props") or {}), enabled=True,
                              stateListOrDisplayStateIdChanged=lambda: None)
        created.append(dev)
        devices.add(dev)
        return dev

    mock_indigo_base.device = Mock()
    mock_indigo_base.device.create.side_effect = create
    agents = []

    class _Agent:
        def __init__(self, prefs, logger, instance=None):
            self.storage_path = str(tmp_path / "Library" / "bridge-node") if instance is None \
                else instance.storage_path
            agents.append(self)

        def uninstall(self):
            agents.append("uninstall")

    monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _Agent)
    import bridge_device_mixin
    monkeypatch.setattr(bridge_device_mixin.bridge_agent, "BridgeProcess", _Agent)
    health = SimpleNamespace(id=6000, name="Matter Bridge Health")
    monkeypatch.setattr(bridge_device_mixin.bridge_health, "_find_health_device_only",
                        lambda logger=None: health)
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginPrefs = {}
    p.bridge_runtimes = {}
    p._current_bridge_id = None
    p._save_plugin_prefs = Mock()
    return SimpleNamespace(plug=p, created=created, agents=agents, indigo=mock_indigo_base,
                           legacy=tmp_path / "Library" / "bridge-node", health=health, devices=devices)


def _legacy_in_use(world):
    ExportStore(lambda: world.plug.pluginPrefs, Mock()).upsert(ExportEntry(101, "onOffPlugInUnit"))
    world.plug.pluginPrefs["exportParked"] = "{}"
    world.plug.pluginPrefs["verboseLogging"] = True
    world.legacy.mkdir(parents=True)
    (world.legacy / "fabrics.json").write_text("paired")


def test_the_built_in_bridge_moves_into_a_device_on_slot_0(world):
    _legacy_in_use(world)
    world.plug._migrate_legacy_bridge()
    (dev,) = world.created
    assert dev.pluginProps == {bridges.PROP_SLOT: "0"} and dev.deviceTypeId == bridges.BRIDGE_DEVICE_TYPE
    assert "uninstall" in world.agents, "the built-in node is stopped before its storage moves"
    import bridge_agent  # pylint: disable=import-outside-toplevel
    moved = os.path.join(bridge_agent.bridge_instance(dev.id, 0).storage_path, "fabrics.json")
    assert open(moved, encoding="utf-8").read() == "paired" and not world.legacy.exists()
    assert (world.legacy.parent / "bridge-node.pre-device-backup" / "fabrics.json").exists()
    prefix = bridges.prefs_prefix(dev.id)
    assert PREF_KEY not in world.plug.pluginPrefs and prefix + PREF_KEY in world.plug.pluginPrefs
    assert prefix + "exportParked" in world.plug.pluginPrefs
    assert world.plug.pluginPrefs["verboseLogging"] is True, "global settings stay global"
    world.indigo.device.delete.assert_called_once_with(world.health)
    assert json.loads(world.plug.pluginPrefs["legacyBridgeMigration"])["stage"] == "done"


def test_it_runs_once(world):
    _legacy_in_use(world)
    world.plug._migrate_legacy_bridge()
    world.plug._migrate_legacy_bridge()
    assert len(world.created) == 1


def test_an_interrupted_move_finishes_without_a_second_device(world):
    _legacy_in_use(world)
    world.plug._write_migration_marker = Mock(side_effect=[None, RuntimeError("power cut")])
    world.plug._migrate_legacy_bridge()
    marker = {"deviceId": world.created[0].id, "stage": "created"}
    world.plug.pluginPrefs["legacyBridgeMigration"] = json.dumps(marker)
    del world.plug._write_migration_marker
    world.plug._migrate_legacy_bridge()
    assert len(world.created) == 1
    assert json.loads(world.plug.pluginPrefs["legacyBridgeMigration"])["stage"] == "done"


def test_nothing_to_move_creates_nothing(world):
    world.plug._migrate_legacy_bridge()
    assert world.created == [] and "legacyBridgeMigration" not in world.plug.pluginPrefs


def test_the_device_waits_until_the_move_is_done(world):
    _legacy_in_use(world)
    world.plug.pluginPrefs["legacyBridgeMigration"] = json.dumps({"deviceId": 7000, "stage": "stopped"})
    dev = SimpleNamespace(id=7000, name="Matter Bridge", deviceTypeId=bridges.BRIDGE_DEVICE_TYPE,
                          pluginProps={bridges.PROP_SLOT: "0"})
    world.plug.runtime = Mock()
    world.plug._start_bridge_device(dev)
    assert 7000 not in world.plug.bridge_runtimes
