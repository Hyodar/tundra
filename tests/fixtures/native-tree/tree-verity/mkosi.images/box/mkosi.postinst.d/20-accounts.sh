#!/usr/bin/env bash
set -euo pipefail

printf "%s\n" box:x:60852:60852::/:/bin/sh >>"$BUILDROOT"/etc/passwd
printf "%s\n" box:x:60852: >>"$BUILDROOT"/etc/group
rm -f "$BUILDROOT"/etc/ssh/ssh_host_*_key "$BUILDROOT"/etc/ssh/ssh_host_*_key.pub
