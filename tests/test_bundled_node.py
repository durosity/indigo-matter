"""Step 4c: the bundled node, Auto, unbridged devices, the lock file, the leftover controller."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_agent
from export_store import ExportEntry, ExportStore
from fakes import FakeIndigoDevices, RelayDevice

REPO = Path(__file__).resolve().parent.parent


def test_the_bundled_node_is_the_repo_node_and_is_used_when_present():
    package = json.loads((REPO / "bridge-node" / "package.json").read_text(encoding="utf-8"))
    assert bridge_agent.BUNDLED_NODE_VERSION == package["version"], "rebuild the bundled node"
    assert bridge_agent.BUNDLED_NODE_TARBALL.endswith(f"indigo-matter-bridge-{package['version']}.tgz")
    expected = (bridge_agent.BUNDLED_NODE_TARBALL if os.path.isfile(bridge_agent.BUNDLED_NODE_TARBALL)
                else bridge_agent.REGISTRY_INSTALL_SPEC)
    assert bridge_agent.DEFAULT_INSTALL_SPEC == expected


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


def test_device_state_starts_on_auto(plugin_mod, mock_indigo_base):
    mock_indigo_base.devices = FakeIndigoDevices([RelayDevice(101, "Study Plug")])
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = "x"
    p.pluginPrefs = {}
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    p.bridge_runtimes = {}
    p._current_bridge_id = None
    assert p.exportDeviceChanged({"exportDevice": "101"})["exportStateKey"] == "_auto"
    assert p.exportDeviceChanged({"exportDevice": ""})["exportStateKey"] == "_auto"


def test_no_state_updates_for_a_device_the_bridge_is_not_serving(mock_indigo_base):
    import export_bridge  # pylint: disable=import-outside-toplevel
    eb = importlib.reload(export_bridge)
    store = ExportStore(lambda: {}, Mock())
    store.upsert(ExportEntry(101, "onOffPlugInUnit"))
    bridge = eb.ExportBridge.__new__(eb.ExportBridge)
    bridge._store = store
    bridge._skipped = {101: "the node is too old for it"}
    bridge._live_client = Mock()
    dev = RelayDevice(101, "Study Plug")
    bridge.device_updated(dev, dev)
    bridge._live_client.assert_not_called()


def test_the_storage_move_drops_the_old_lock_file(plugin_mod, tmp_path):
    src = tmp_path / "bridge-node" / "indigo-matter-bridge"
    src.mkdir(parents=True)
    (src / "matter.lock").write_text("63777")
    (src / "fabrics.json").write_text("paired")
    dst = tmp_path / "bridges" / "7000"
    plugin_mod.Plugin._move_bridge_storage(str(tmp_path / "bridge-node"), str(dst))
    assert (dst / "indigo-matter-bridge" / "fabrics.json").exists()
    assert not (dst / "indigo-matter-bridge" / "matter.lock").exists()


def test_the_export_only_build_removes_a_leftover_controller(plugin_mod, tmp_path, monkeypatch):
    import server_process  # pylint: disable=import-outside-toplevel
    plist = tmp_path / "controller.plist"
    plist.write_text("x")
    removed = []

    class _Server:
        def __init__(self, prefs, logger):
            self.plist_path = str(plist)

        def uninstall(self):
            removed.append(True)

    monkeypatch.setattr(server_process, "ServerProcess", _Server)
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginPrefs = {}
    p._retire_controller_agent()
    assert removed == [True]
    plist.unlink()
    p._retire_controller_agent()
    assert removed == [True], "nothing to do when no controller agent is installed"
