"""Step 4b: names Matter accepts, and refused accessories kept out of the bridge."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_protocol
from export_store import ExportEntry, ExportStore


@pytest.fixture
def eb(mock_indigo_base):  # noqa: ARG001
    import export_bridge  # pylint: disable=import-outside-toplevel
    return importlib.reload(export_bridge)


@pytest.mark.parametrize("name, expected", [
    ("Utility Light", "Utility Light"),
    ("Kitchen Diner Ambient Lights (Table)", "Kitchen Diner Ambient Lights (Ta"),
    ("Küche Küche Küche Küche Küche Kü", "Küche Küche Küche Küche Küc"),
    ("🙂" * 9, "🙂" * 8),
])
def test_names_are_cut_to_32_bytes_without_splitting_a_character(eb, name, expected):
    clipped = eb.matter_label(name)
    assert clipped == expected and len(clipped.encode("utf-8")) <= 32


def _bridge(eb, prefs):
    store = ExportStore(lambda: prefs, Mock())
    store.upsert(ExportEntry(101, "onOffPlugInUnit"))
    store.upsert(ExportEntry(102, "onOffPlugInUnit"))
    bridge = eb.ExportBridge.__new__(eb.ExportBridge)
    bridge._store = store
    bridge._prefs_getter = lambda: prefs
    bridge._save_prefs = Mock()
    bridge._logger = Mock()
    bridge._pushed = {}
    bridge._skipped = {}
    return bridge


def test_a_refused_new_accessory_is_kept_out_of_every_attach(eb):
    prefs = {}
    bridge = _bridge(eb, prefs)
    refusal = bridge_protocol.BridgeProtocolError(bridge_protocol.ERR_INTERNAL, "Behaviors have errors")
    bridge._note_upsert_failure(101, False, refusal)
    assert bridge.quarantined() == {101: "Behaviors have errors"}
    bridge._spec_for = lambda entry: SimpleNamespace(indigo_device_id=entry.indigo_device_id)
    bridge._warn_if_wholly_unbridgeable = Mock()
    assert [spec.indigo_device_id for spec in bridge.endpoint_specs()] == [102]
    bridge._logger.error.assert_called_once()


@pytest.mark.parametrize("was_live, code", [(True, bridge_protocol.ERR_INTERNAL), (False, "timeout")])
def test_live_accessories_and_transient_failures_are_never_quarantined(eb, was_live, code):
    bridge = _bridge(eb, {})
    bridge._note_upsert_failure(101, was_live, bridge_protocol.BridgeProtocolError(code, "x"))
    assert bridge.quarantined() == {}, "leaving a live accessory out of an attach would remove it"


def test_saving_or_removing_it_again_lifts_the_quarantine(eb):
    prefs = {}
    bridge = _bridge(eb, prefs)
    bridge._note_upsert_failure(101, False, bridge_protocol.BridgeProtocolError("internal", "x"))
    bridge._unquarantine(101)
    assert bridge.quarantined() == {} and eb.PREF_QUARANTINE not in prefs


def test_the_quarantine_moves_with_the_built_in_bridge():
    import bridge_device_mixin  # pylint: disable=import-outside-toplevel
    import export_bridge  # pylint: disable=import-outside-toplevel
    assert export_bridge.PREF_QUARANTINE in bridge_device_mixin.LEGACY_BRIDGE_KEYS
