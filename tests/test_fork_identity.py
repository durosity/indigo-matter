"""Pins the PRIVATE fork's identity — everything that must differ from upstream.

Runs in a subprocess WITHOUT ``INDIGO_MATTER_UPSTREAM_IDENTITY`` (which
conftest sets for the rest of the suite), so these are the values the installed
fork actually uses.
"""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "Matter Prototype.indigoPlugin" / "Contents"
SERVER = PLUGIN / "Server Plugin"

PROBE = """
import json, fork_identity as f, server_process, bridge_agent, bridge_protocol, launch_agent, export_catalog
print(json.dumps({
  "plugin_id": f.PLUGIN_ID, "controller": server_process.LABEL, "bridge": bridge_agent.LABEL,
  "matter_port": bridge_agent.DEFAULT_MATTER_PORT, "ws_port": bridge_protocol.DEFAULT_WS_PORT,
  "project": launch_agent.DEFAULT_PROJECT_DIRNAME, "catalog_id": export_catalog.DEFAULT_PLUGIN_ID,
  "storage": bridge_agent.bridge_storage_path(bridge_agent.default_controller_storage("/Users/x")),
  "export_only": f.EXPORT_ONLY, "foreign": sorted(f.FOREIGN_MATTER_PLUGIN_IDS)}))
"""


def _fork_values() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "INDIGO_MATTER_UPSTREAM_IDENTITY"}
    stub = "import sys, types; sys.modules['indigo'] = types.ModuleType('indigo');"
    out = subprocess.run([sys.executable, "-c", stub + PROBE], cwd=SERVER, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_nothing_the_fork_owns_collides_with_upstream():
    v = _fork_values()
    assert v["plugin_id"] == v["catalog_id"] == "com.durosity.indigo-matter-bridges"
    assert v["controller"] != "com.simons-plugins.indigo-matter"
    assert v["bridge"] != "com.simons-plugins.indigo-matter.bridge"
    assert (v["matter_port"], v["ws_port"]) == ("5640", "5681")
    assert v["project"] == "indigo-matter-bridges"
    # The one that would have been catastrophic: upstream's fabrics live here.
    assert "com.simons-plugins.indigo-matter" not in v["storage"]
    assert v["storage"].endswith("/com.durosity.indigo-matter-bridges/bridge-node")


def test_the_controller_is_enabled_and_upstreams_matter_devices_are_never_reexported():
    v = _fork_values()
    assert v["export_only"] is False
    assert v["foreign"] == ["com.simons-plugins.indigo-matter"]


def test_info_plist_carries_the_fork_identity():
    info = plistlib.loads((PLUGIN / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.durosity.indigo-matter-bridges"
    assert info["CFBundleDisplayName"] == "Matter Bridge"
