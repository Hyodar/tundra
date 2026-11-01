"""The Azure integration's files: what lowering adds to an ``azure`` target variant.

``tundravm.declarative.state`` installs :data:`AZURE_PACKAGES`, ships the provisioning
script and its unit and enables the unit in every payload of a recipe that targets
``azure`` (the unit is inert elsewhere); the ``azure`` wrapper image converts the disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm.declarative.model import Task

AZURE_PACKAGES = ("curl", "dmidecode")
"""What the provisioning script runs: ``dmidecode`` to tell Azure from elsewhere, ``curl``
for the wireserver. Without ``curl`` the VM never reports ready and Azure fails it."""

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


def provisioning_task(network_setup: bool) -> Task:
    """The provisioning unit as a typed ``Task``: a root boot oneshot
    (no ``User=``: it reads DMI and posts to the wireserver) under
    ``Hardening.relaxed()``, after ``network-setup.service`` when the profile ships it,
    else ``network-online.target``; it does not wait for runtime-init. It is gated by
    ``ConditionVirtualization=microsoft``, so the same UKI boots cleanly on QEMU and GCP."""
    from tundravm.declarative.model import Task
    from tundravm.declarative.systemd import Condition, Hardening, Root

    return Task(
        "azure-complete-provisioning",
        "/usr/bin/azure-complete-provisioning",
        description="Azure Provisioning Completion",
        user=Root(),
        after=("network.target",) if network_setup else (),
        requires=("network-setup.service",) if network_setup else (),
        wants=() if network_setup else ("network-online.target",),
        needs=(),
        hardening=Hardening.relaxed(),
        conditions=(Condition.virtualization("microsoft"),),
    )
