#!/usr/bin/env bash
set -euo pipefail

# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"image-metrics","package":"./cmd/image-metrics","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/image-metrics","kind":"file","mode":"0755","path":"build/image-metrics"}],"name":"image-metrics","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build image-metrics is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"tdxs","package":"./cmd/tdxs","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/tdxs","kind":"file","mode":"0755","path":"build/tdxs"}],"name":"tdxs","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build tdxs is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"attested-endpoint","package":"./cmd/attested-endpoint","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/attested-endpoint","kind":"artifact","mode":"0755"}],"name":"attested-endpoint","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build attested-endpoint is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"mode-switch","package":"./cmd/mode-switch","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/mode-switch","kind":"file","mode":"0755","path":"build/mode-switch"}],"name":"mode-switch","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build mode-switch is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
