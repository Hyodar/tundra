#!/usr/bin/env bash
set -euo pipefail

printf "%s\n" searcher-container:x:60632:60632::/persistent/data/searcher:/bin/sh >>"$BUILDROOT"/etc/passwd
printf "%s\n" searcher-container:x:60632: >>"$BUILDROOT"/etc/group
rm -f "$BUILDROOT"/etc/ssh/ssh_host_*_key "$BUILDROOT"/etc/ssh/ssh_host_*_key.pub
