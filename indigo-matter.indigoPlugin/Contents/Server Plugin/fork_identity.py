"""Identity of the PRIVATE ``indigo-matter-bridges`` prototype fork.

Everything that would otherwise collide with the upstream ``indigo-matter``
plugin when both are installed on the same Mac lives here, so a merge from
upstream touches as few lines as possible. Each value was checked against the
mechanism it feeds:

* ``PLUGIN_ID`` — Indigo plugin id; also the export loop guard.
* ``CONTROLLER_LABEL`` — matter-server's LaunchAgent label, and the ROOT of the
  bridge node's storage path (``~/Library/Application Support/<label>/bridge-node``).
  Sharing it would have pointed this fork's bridge at upstream's fabrics.
* ``BRIDGE_LABEL`` — the bridge node's LaunchAgent label.
* ``PROJECT_DIRNAME`` / ``LOG_DIRNAME`` — npm install root and log folder.
  The stray-process reaper matches on the npm root AND storage path, so with
  both distinct it can never touch upstream's processes.
* ``MATTER_PORT`` / ``WS_PORT`` — upstream uses 5540 / 5581 (+5541… for its
  standalone vacuums); 5640 / 5681 keep well clear.
* ``EXPORT_ONLY`` — never run or dial a Matter *controller*: upstream's
  matter-server already does that, and dialling it would mirror every Matter
  device into Indigo a second time.
* ``FOREIGN_MATTER_PLUGIN_IDS`` — devices those plugins create came FROM
  Matter; exporting them back out is the loop the guard exists for.
"""

import os

#: Set ONLY by tests/conftest.py. The upstream suite pins upstream's labels,
#: paths and ports byte-for-byte (plist goldens, URIs, log paths); running it
#: under upstream identity keeps every one of those checks meaningful instead of
#: rewriting ~40 of them, and ``tests/test_fork_identity.py`` pins the fork's own
#: values in a subprocess without this variable.
UPSTREAM_IDENTITY = os.environ.get("INDIGO_MATTER_UPSTREAM_IDENTITY") == "1"

PLUGIN_ID = "com.durosity.indigo-matter-bridges"
PLUGIN_NAME = "indigo-matter-bridges"
CONTROLLER_LABEL = "com.durosity.indigo-matter-bridges"
BRIDGE_LABEL = "com.durosity.indigo-matter-bridges.bridge"
PROJECT_DIRNAME = "indigo-matter-bridges"
LOG_DIRNAME = "indigo-matter-bridges"
MATTER_PORT = "5640"
WS_PORT = "5681"
EXPORT_ONLY = True
FOREIGN_MATTER_PLUGIN_IDS = frozenset({"com.simons-plugins.indigo-matter"})

if UPSTREAM_IDENTITY:  # tests only — see above
    PLUGIN_ID = "com.simons-plugins.indigo-matter"
    PLUGIN_NAME = "indigo-matter"
    CONTROLLER_LABEL = "com.simons-plugins.indigo-matter"
    BRIDGE_LABEL = "com.simons-plugins.indigo-matter.bridge"
    PROJECT_DIRNAME = "indigo-matter"
    LOG_DIRNAME = "indigo-matter"
    MATTER_PORT = "5540"
    WS_PORT = "5581"
    EXPORT_ONLY = False
    FOREIGN_MATTER_PLUGIN_IDS = frozenset()
