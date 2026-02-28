#!/usr/bin/env bash
set -euo pipefail

MIRROR=$(jq -r .Mirror "$BUILDDIR/config.json" 2>/dev/null || echo "")
if [ -z "$MIRROR" ] || [ "$MIRROR" = "null" ]; then
    MIRROR="http://deb.debian.org/debian"
fi
cat > "$BUILDDIR/debian-backports.sources" <<EOF
Types: deb deb-src
URIs: $MIRROR
Suites: ${RELEASE}-backports
Components: main
Enabled: yes
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb deb-src
URIs: $MIRROR
Suites: sid
Components: main
Enabled: yes
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
