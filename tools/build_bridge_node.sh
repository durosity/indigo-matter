#!/bin/sh
# Build the fork's own bridge node and pack it into the plugin bundle.
#
# Runs the node's full test suite first (it needs a normal user and working
# mDNS, so run it on the Mac, not in a container), then builds and packs
# bridge-node/ into "Matter Prototype.indigoPlugin/Contents/Server Plugin/
# bridge_node/", where bridge_agent.BUNDLED_NODE_TARBALL expects it. After a
# plugin install, "Install/update Matter bridge node" puts it on every bridge.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/Matter Prototype.indigoPlugin/Contents/Server Plugin/bridge_node"
cd "$here/bridge-node"
npm ci --no-audit --no-fund
npm test
npm run clean
npm run build
mkdir -p "$dest"
rm -f "$dest"/indigo-matter-bridge-*.tgz
npm pack --pack-destination "$dest"
ls -l "$dest"
