#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$BUILDROOT"/proc
mkdir -p "$BUILDROOT"/sys
mkdir -p "$BUILDROOT"/dev
mkdir -p "$BUILDROOT"/run
mkdir -p "$BUILDROOT"/tmp
mkdir -p "$BUILDROOT"/var/tmp
chmod 1777 "$BUILDROOT"/tmp
chmod 1777 "$BUILDROOT"/var/tmp
mkdir -p "$BUILDROOT"/persistent/data/searcher
mkdir -p "$BUILDROOT"/persistent/data/input
mkdir -p "$BUILDROOT"/persistent/data/lighthouse-logs
mkdir -p "$BUILDROOT"/etc/ssh/tundravm/host
mkdir -p "$BUILDROOT"/etc/ssh/tundravm/keys
