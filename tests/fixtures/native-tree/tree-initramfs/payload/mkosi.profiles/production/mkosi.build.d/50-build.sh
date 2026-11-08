#!/usr/bin/env bash
set -euo pipefail

# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"mode-switch","package":"./cmd/mode-switch","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/mode-switch","kind":"file","mode":"0755","path":"build/mode-switch"}],"name":"mode-switch","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build mode-switch is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
