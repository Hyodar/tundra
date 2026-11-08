#!/usr/bin/env bash
set -euo pipefail

printf "%s\n" bob:x:60706:60706::/persistent/data/searcher:/bin/sh >>"$BUILDROOT"/etc/passwd
printf "%s\n" bob:x:60706: >>"$BUILDROOT"/etc/group
rm -f "$BUILDROOT"/etc/ssh/ssh_host_*_key "$BUILDROOT"/etc/ssh/ssh_host_*_key.pub
