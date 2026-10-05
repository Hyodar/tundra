"""The Azure integration's files: what lowering adds to an ``azure`` target variant.

``tundravm.declarative.state`` installs ``dmidecode``, ships the provisioning
script and its unit, enables the unit, and sets the ``azure`` target, whose
VHD postoutput script the compiler generates when
``MkosiOptions.generate_cloud_postoutput`` is true (the default).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Azure provisioning completion script
# ---------------------------------------------------------------------------

AZURE_PROVISIONING_SCRIPT = """\
#!/bin/bash
set -euo pipefail

# Azure provisioning completion script
# Reports VM health to the Azure fabric controller (wireserver)

WIRESERVER="168.63.129.16"
MAX_RETRIES=5

# Only run on Azure (check DMI for Microsoft Corporation)
MANUFACTURER=$(dmidecode -s system-manufacturer 2>/dev/null || true)
if [[ "$MANUFACTURER" != *"Microsoft Corporation"* ]]; then
    echo "Not running on Azure, skipping provisioning completion"
    exit 0
fi

get_goal_state() {
    curl -s -H "x-ms-version: 2012-11-30" \
        "http://${WIRESERVER}/machine/?comp=goalstate" 2>/dev/null
}

send_health() {
    local container_id="$1"
    local instance_id="$2"

    local health_xml="<?xml version=\\"1.0\\" encoding=\\"utf-8\\"?>
<Health>
  <GoalStateIncarnation>1</GoalStateIncarnation>
  <Container>
    <ContainerId>${container_id}</ContainerId>
    <RoleInstanceList>
      <Role>
        <InstanceId>${instance_id}</InstanceId>
        <Health>
          <State>Ready</State>
        </Health>
      </Role>
    </RoleInstanceList>
  </Container>
</Health>"

    curl -s -X POST \
        -H "x-ms-version: 2012-11-30" \
        -H "Content-Type: text/xml; charset=utf-8" \
        -d "$health_xml" \
        "http://${WIRESERVER}/machine/?comp=health" 2>/dev/null
}

for i in $(seq 1 "$MAX_RETRIES"); do
    echo "Attempt $i/$MAX_RETRIES: Retrieving goal state..."

    GOAL_STATE=$(get_goal_state) || true

    if [ -z "$GOAL_STATE" ]; then
        echo "Failed to retrieve goal state"
        if [ "$i" -lt "$MAX_RETRIES" ]; then
            sleep 5
            continue
        fi
        echo "Exhausted retries, exiting"
        exit 1
    fi

    CONTAINER_ID=$(echo "$GOAL_STATE" | \\
        sed -n 's/.*<ContainerId>\\(.*\\)<\\/ContainerId>.*/\\1/p' | head -1)
    INSTANCE_ID=$(echo "$GOAL_STATE" | \\
        sed -n 's/.*<InstanceId>\\(.*\\)<\\/InstanceId>.*/\\1/p' | head -1)

    if [ -z "$CONTAINER_ID" ] || [ -z "$INSTANCE_ID" ]; then
        echo "Failed to parse goal state"
        if [ "$i" -lt "$MAX_RETRIES" ]; then
            sleep 5
            continue
        fi
        echo "Exhausted retries, exiting"
        exit 1
    fi

    echo "Sending health report (container=$CONTAINER_ID, instance=$INSTANCE_ID)..."
    if send_health "$CONTAINER_ID" "$INSTANCE_ID"; then
        echo "Successfully reported Health/Ready to Azure fabric controller"
        exit 0
    fi

    echo "Failed to send health report"
    if [ "$i" -lt "$MAX_RETRIES" ]; then
        sleep 5
    fi
done

echo "Exhausted retries, exiting"
exit 1
"""

# ---------------------------------------------------------------------------
# Systemd service unit for azure-complete-provisioning
# ---------------------------------------------------------------------------

AZURE_PROVISIONING_SERVICE = """\
[Unit]
Description=Azure Provisioning Completion
After=network.target network-setup.service
Requires=network-setup.service

[Service]
Type=oneshot
ExecStart=/usr/bin/azure-complete-provisioning
RemainAfterExit=yes

[Install]
WantedBy=minimal.target
"""
"""The unit for a profile that ships ``network-setup.service`` (always under ``nethermind-v1``)."""

AZURE_PROVISIONING_SERVICE_ONLINE = AZURE_PROVISIONING_SERVICE.replace(
    "After=network.target network-setup.service\nRequires=network-setup.service",
    "After=network-online.target\nWants=network-online.target",
)
"""The unit for any other profile: it waits for ``network-online.target``, as runtime-init does."""
