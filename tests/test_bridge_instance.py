"""Phase 2 step 1 — per-bridge resources (docs/MULTI-BRIDGE.md).

Runs under the suite's upstream identity (conftest), so the expected values are
upstream's constants plus the per-instance derivation; what is pinned is the
DERIVATION: distinct, stable, and never colliding with the legacy bridge.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def agent_mod(mock_indigo_base):
    import bridge_agent
    return importlib.reload(bridge_agent)


def test_resources_are_derived_from_device_and_slot(agent_mod):
    inst = agent_mod.bridge_instance(1234, 2, home="/Users/x")
    assert inst.label == f"{agent_mod.LABEL}.1234"
    assert inst.storage_path.endswith("/bridges/1234")
    assert inst.matter_port == str(int(agent_mod.DEFAULT_MATTER_PORT) + 20)
    assert int(inst.ws_port) == int(agent_mod.bridge_protocol_default_ws_port()) + 2
    assert (inst.out_log, inst.err_log) == ("bridge-1234.log", "bridge-1234.err.log")


def test_two_bridges_share_nothing(agent_mod):
    a = agent_mod.bridge_instance(10, 0, home="/Users/x")
    b = agent_mod.bridge_instance(11, 1, home="/Users/x")
    for field in ("label", "storage_path", "matter_port", "ws_port", "out_log", "err_log"):
        assert getattr(a, field) != getattr(b, field), field


def test_a_bridge_never_reuses_the_legacy_bridges_storage_or_label(agent_mod):
    inst = agent_mod.bridge_instance(10, 0, home="/Users/x")
    legacy = agent_mod.bridge_storage_path(agent_mod.default_controller_storage("/Users/x"))
    assert inst.storage_path != legacy
    assert inst.label != agent_mod.LABEL


def test_vacuum_ports_cannot_reach_the_next_bridge(agent_mod):
    """Each node puts its standalone accessories on the ports just above its own."""
    a = agent_mod.bridge_instance(10, 0, home="/Users/x")
    b = agent_mod.bridge_instance(11, 1, home="/Users/x")
    assert int(b.matter_port) - int(a.matter_port) == agent_mod.MATTER_PORT_STRIDE >= 10


@pytest.mark.parametrize("device_id, slot", [(0, 0), (-1, 0), (True, 0), ("5", 0), (5, -1), (5, True)])
def test_garbage_inputs_are_refused(agent_mod, device_id, slot):
    with pytest.raises(ValueError):
        agent_mod.bridge_instance(device_id, slot, home="/Users/x")


def test_process_uses_every_instance_resource_and_ignores_the_port_prefs(agent_mod, mock_logger):
    inst = agent_mod.bridge_instance(77, 3, home="/Users/x")
    proc = agent_mod.BridgeProcess({"bridgeMatterPort": "9999", "bridgeWsPort": "9998"}, mock_logger,
                                   home="/Users/x", instance=inst)
    assert proc.spec.label == inst.label
    assert proc.storage_path == inst.storage_path
    assert (proc.matter_port, proc.ws_port) == (inst.matter_port, inst.ws_port)
    assert proc.spec.port == int(inst.ws_port), "the reaper polices THIS bridge's port"
    assert (proc.spec.out_log, proc.spec.err_log) == (inst.out_log, inst.err_log)
    assert proc.instance is inst


def test_without_an_instance_nothing_changes(agent_mod, mock_logger):
    proc = agent_mod.BridgeProcess({}, mock_logger, home="/Users/x")
    assert proc.spec.label == agent_mod.LABEL
    assert proc.matter_port == agent_mod.DEFAULT_MATTER_PORT
    assert proc.instance is None
