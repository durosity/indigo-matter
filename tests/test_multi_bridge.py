"""bridges.py — the per-bridge prefs view, slots and runtime record."""
from __future__ import annotations

import pytest

import bridges


def _view(base, prefix="bridge.7.", **overrides):
    return bridges.NamespacedPrefs(lambda: base, prefix, overrides or None)


def test_reads_and_writes_land_in_the_bridges_own_namespace():
    base = {"other": 1, "bridge.8.matterExports": "theirs"}
    view = _view(base)
    view["matterExports"] = "ours"
    assert base["bridge.7.matterExports"] == "ours"
    assert base["bridge.8.matterExports"] == "theirs", "another bridge is untouched"
    assert view["matterExports"] == "ours" and view.get("missing") is None
    assert "matterExports" in view and "other" not in view
    del view["matterExports"]
    assert "bridge.7.matterExports" not in base


def test_iteration_and_len_see_only_this_bridge_plus_overrides():
    base = {"bridge.7.a": 1, "bridge.7.b": 2, "bridge.70.c": 3, "x": 4}
    view = _view(base, bridgeWsPort="5682")
    assert sorted(view) == ["a", "b", "bridgeWsPort"]
    assert len(view) == 3
    assert dict(view)["bridgeWsPort"] == "5682"


def test_a_prefix_never_matches_a_longer_id():
    """bridge.7. must not see bridge.70.* — the trailing dot is load-bearing."""
    assert bridges.prefs_prefix(7) == "bridge.7."
    assert "c" not in _view({"bridge.70.c": 3}, bridges.prefs_prefix(7))


def test_overrides_are_read_only_and_shadow_the_base():
    base = {"bridge.7.bridgeWsPort": "9999"}
    view = _view(base, bridgeWsPort="5682")
    assert view["bridgeWsPort"] == "5682"
    with pytest.raises(KeyError):
        view["bridgeWsPort"] = "1"
    with pytest.raises(KeyError):
        del view["bridgeWsPort"]


def test_the_getter_is_read_every_time():
    """Indigo can rebind pluginPrefs; a captured dict would go stale."""
    holder = {"prefs": {}}
    view = bridges.NamespacedPrefs(lambda: holder["prefs"], "bridge.1.")
    holder["prefs"] = {"bridge.1.k": "new"}
    assert view["k"] == "new"


def test_clear_namespace_removes_only_this_bridge():
    base = {"bridge.7.a": 1, "bridge.7.b": 2, "bridge.8.a": 3}
    assert _view(base).clear_namespace() == 2
    assert base == {"bridge.8.a": 3}


def test_an_empty_prefix_is_refused():
    with pytest.raises(ValueError):
        bridges.NamespacedPrefs(dict, "")


def test_allocate_slot_takes_the_lowest_free_and_has_a_ceiling():
    assert bridges.allocate_slot(set()) == 0
    assert bridges.allocate_slot({0, 1, 3}) == 2
    with pytest.raises(ValueError):
        bridges.allocate_slot(set(range(bridges.MAX_SLOTS)))


@pytest.mark.parametrize("props, slot", [
    ({"bridgeSlot": "3"}, 3), ({"bridgeSlot": 0}, 0), ({}, None), (None, None),
    ({"bridgeSlot": "x"}, None), ({"bridgeSlot": -1}, None), ({"bridgeSlot": True}, None),
    ({"bridgeSlot": str(bridges.MAX_SLOTS)}, None),
])
def test_slot_of(props, slot):
    assert bridges.slot_of(props) == slot


def test_the_export_store_persists_per_bridge_through_the_view(mock_logger):
    """The point of the view: ExportStore works per bridge with no change."""
    from export_store import ExportEntry, ExportStore
    base: dict = {}
    one = ExportStore(lambda: bridges.NamespacedPrefs(lambda: base, "bridge.1."), mock_logger)
    two = ExportStore(lambda: bridges.NamespacedPrefs(lambda: base, "bridge.2."), mock_logger)
    one.upsert(ExportEntry(100, "onOffPlugInUnit"))
    assert len(one) == 1 and len(two) == 0
    assert any(k.startswith("bridge.1.") for k in base)
    assert not any(k.startswith("bridge.2.") for k in base)
