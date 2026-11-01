#!/bin/sh
# tundravm boot probe (plan 5.2 item 7): what this profile's declarations promise, checked
# from inside the guest. One "tundravm-probe: <check> ok|fail|skip <detail>" line per check.
set -u
passed=0
failed=0
check=
say() { echo "tundravm-probe: $*"; }
ok() { passed=$((passed + 1)); say "$check ok $*"; }
fail() { failed=$((failed + 1)); say "$check fail $*"; }
skip() { say "$check skip $*"; }
probe_service() {
    state=$(systemctl show -P ActiveState "$1")
    waited=0
    while [ "$state" = activating ] && [ "$waited" -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
        state=$(systemctl show -P ActiveState "$1")
    done
    result=$(systemctl show -P Result "$1")
    if [ "$state" = active ]; then
        ok active
    elif [ "$state" = inactive ] && [ "$result" = success ] &&
        [ "$(systemctl show -P ExecMainExitTimestampMonotonic "$1")" != 0 ]; then
        ok "exited 0"
    elif [ "$state" = inactive ] && [ "$result" = success ]; then
        fail "never started: a unit it requires failed; systemctl list-dependencies $1"
    else
        fail "$state ($result): journalctl -u $1 says why"
    fi
}
probe_active() {
    state=$(systemctl show -P ActiveState "$1")
    if [ "$state" = active ]; then ok active; else fail "$state: $2"; fi
}
probe_port() {
    hex=$(printf '%04X' "$2")
    want=0A
    [ "$1" = udp ] && want=07
    waited=0
    while :; do
        for table in /proc/net/"$1" /proc/net/"$1"6; do
            [ -r "$table" ] || continue
            while read -r _ local _ state _; do
                if [ "$state" = "$want" ] && [ "${local##*:}" = "$hex" ]; then
                    ok listening
                    return
                fi
            done <"$table"
        done
        [ "$waited" -ge 30 ] && break
        sleep 1
        waited=$((waited + 1))
    done
    fail "nothing listens on $2/$1 after 30s: the service must bind every port expose= names, or drop it from expose="
}
probe_volume() {
    if ! command -v findmnt >/dev/null 2>&1; then
        skip "no findmnt in the image (util-linux)"
        return
    fi
    mount=$1
    encrypted=$2
    set -- $(findmnt -n -o FSTYPE,SOURCE --mountpoint "$mount" 2>/dev/null)
    if [ $# -lt 2 ]; then
        fail "nothing is mounted at $mount: unlock it (tundravm secrets unlock INSTANCE VOLUME) or read journalctl -u runtime-init.service"
    elif [ "$1" != ext4 ]; then
        fail "$mount is $1 on $2, not ext4: disk-setup formats ext4, so this is not the volume"
    elif [ "$encrypted" = no ]; then
        ok "ext4 on $2"
    else
        device=$(readlink -f "$2")
        uuid=$(cat "/sys/class/block/${device##*/}/dm/uuid" 2>/dev/null || true)
        case $uuid in
            CRYPT-*) ok "ext4 on $2, dm-crypt" ;;
            *) fail "$mount is on $2, not a dm-crypt device: an encrypted volume mounts from its LUKS mapping; journalctl -u runtime-init.service" ;;
        esac
    fi
}
probe_egress() {
    if ! command -v bash >/dev/null 2>&1; then
        skip "no bash to send a datagram with"
        return
    fi
    if said=$(bash -c 'printf x >"/dev/udp/$0/$1"' "$1" "$2" 2>&1); then
        fail "a datagram to $1:$2/udp left the guest: the output chain does not drop it; systemctl status nftables.service, and check Network(egress=Egress(...))"
        return
    fi
    case $said in
        *"not permitted"*) ok "refused by the output chain (Operation not permitted)" ;;
        *) skip "a datagram to $1:$2/udp failed before the firewall ($(echo "$said" | tail -n 1)): no route, so the deny is untested" ;;
    esac
}
probe_hardening() {
    if ! command -v systemd-analyze >/dev/null 2>&1; then
        skip "no systemd-analyze in the image"
        return
    fi
    exposure=$(systemd-analyze security --no-pager "$1" 2>/dev/null |
        sed -n 's/.*Overall exposure level for [^:]*: \([0-9][0-9]*\.[0-9]\).*/\1/p')
    if [ -z "$exposure" ]; then
        fail "systemd-analyze security $1 printed no exposure level: run it in the guest for its error"
    elif [ "$(echo "$exposure" | tr -d .)" -le "$(echo "$2" | tr -d .)" ]; then
        ok "exposure $exposure, ceiling $2 ($3)"
    else
        fail "exposure $exposure over the $3 ceiling $2: a directive the preset sets does not hold; systemd-analyze security $1 lists each"
    fi
}
probe_symbols() {
    if [ ! -r /proc/kallsyms ]; then
        skip "no /proc/kallsyms (CONFIG_KALLSYMS=n): no symbols to read"
        return
    fi
    present=
    for symbol in "$@"; do
        if grep -Eq " $symbol([[:space:]]|\$)" /proc/kallsyms; then present="$present $symbol"; fi
    done
    if [ -z "$present" ]; then
        ok "$# symbols absent"
    else
        fail "present:$present: the running kernel is not the one built with those options off; rebuild, and check Kernel.build(config=...)"
    fi
}
probe_modules() {
    if [ ! -e /proc/modules ]; then
        ok "no loadable modules"
        return
    fi
    allowed=" $* "
    grew=yes
    while [ "$grew" = yes ]; do
        grew=no
        while read -r name _; do
            case $allowed in *" $name "*) continue ;; esac
            for holder in /sys/module/"$name"/holders/*; do
                case $allowed in *" ${holder##*/} "*) allowed="$allowed$name " grew=yes; break ;; esac
            done
        done </proc/modules
    done
    loaded=0
    outside=
    while read -r name _; do
        loaded=$((loaded + 1))
        case $allowed in *" $name "*) ;; *) outside="$outside $name" ;; esac
    done </proc/modules
    count=0
    shown=
    for name in $outside; do
        count=$((count + 1))
        [ "$count" -le 8 ] && shown="$shown $name"
    done
    [ "$count" -gt 8 ] && shown="$shown (and $((count - 8)) more)"
    if [ "$count" -eq 0 ]; then
        ok "$loaded loaded, each curated or held by one"
    else
        fail "$count loaded beyond the curated set:$shown: add each the image needs to Debian(kernel_modules=[...]), or find what loads it (udev, modprobe.d)"
    fi
}
probe_keys() {
    directory=${1%/*}
    if [ ! -e "$1" ]; then
        fail "no $1: ssh-host-keys.service did not make it; journalctl -u ssh-host-keys.service"
        return
    fi
    set -- "$1" $(stat -c '%U:%G %a' "$directory") $(stat -c '%U:%G %a' "$1")
    if [ "$2" != root:root ] || [ $((0$3 & 022)) -ne 0 ]; then
        fail "$directory is $2 $3: it must be root:root and writable by root only (chown root:root, chmod go-w)"
    elif [ "$4" != root:root ] || [ "$5" != 600 ]; then
        fail "$1 is $4 $5: a host key must be root:root 0600 (chown root:root, chmod 600)"
    else
        ok "$directory root:root $3, ${1##*/} root:root 600"
    fi
}
check=mode
mode=$(cat /run/tundravm/mode 2>/dev/null || true)
case $mode in
    simulated|tdx) ok "$mode" ;;
    *) fail "no mode in /run/tundravm/mode: tdx-evidence.service did not record one; journalctl -u tdx-evidence.service" ;;
esac
check=rtmr
if [ "$mode" = tdx ]; then
    skip "the guest reads RTMR0..2 only from a quote, which tdxs (tundra-tools) makes; tundravm attest INSTANCE --policy FILE compares them with measurements.json"
else
    skip "no TDX: a simulated guest has no registers to compare with measurements.json"
fi
check=socket:tdxs.socket
probe_active tdxs.socket 'the socket must listen; systemctl status names why it does not'
check=service:app.service
probe_service app.service
check=timer:tick.timer
probe_active tick.timer 'the timer must be enabled; systemctl status names why it is not'
check=port:8080/tcp
probe_port tcp 8080
check=port:5353/udp
probe_port udp 5353
check=volume:/persistent
probe_volume /persistent yes
check=egress:198.51.100.1:7
probe_egress 198.51.100.1 7
check=hardening:app.service
probe_hardening app.service 2.6 jit
check=kernel:symbols
probe_symbols handle_sysrq
check=kernel:modules
probe_modules
check=host-keys
probe_keys /etc/ssh/ssh_host_ed25519_key
say "done $passed ok $failed fail"
