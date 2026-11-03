#!/usr/bin/env bash
set -euo pipefail

# The kernel build's configs and drivers report, for the build directory
cp "$ARTIFACTDIR/io.tundravm.kernel/kernel-config.before" "$OUTPUTDIR/kernel-config.before"
cp "$ARTIFACTDIR/io.tundravm.kernel/kernel-config.after" "$OUTPUTDIR/kernel-config.after"
cp "$ARTIFACTDIR/io.tundravm.kernel/kernel-drivers.txt" "$OUTPUTDIR/kernel-drivers.txt"
