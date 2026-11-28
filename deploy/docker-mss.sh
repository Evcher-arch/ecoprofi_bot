#!/usr/bin/env bash
# Match TCP segment sizes to the VPS path MTU without restarting containers.
# The OpenStack NIC may use MTU 1450 while Docker bridges default to 1500.
# Only forwarded IPv4 TCP traffic between this project's subnet and uplink
# is affected; firewall allow/deny rules are not changed.
# Usage: bash deploy/docker-mss.sh [apply|remove]
# Install persistence:
#   install -m 644 deploy/systemd/ecoprofi-docker-mss.service /etc/systemd/system/
#   systemctl daemon-reload
#   systemctl enable --now ecoprofi-docker-mss.service
# Roll back: systemctl disable --now ecoprofi-docker-mss.service
#            bash deploy/docker-mss.sh remove
# Reapply after recreating the Docker network or resetting firewall rules:
#   systemctl restart ecoprofi-docker-mss.service
# Reference: https://www.iptables.org/documentation/HOWTO/netfilter-extensions-HOWTO-4.html
set -euo pipefail

action="${1:-apply}"
case "$action" in apply|remove) ;; *) echo 'usage: docker-mss.sh [apply|remove]' >&2; exit 2 ;; esac
network="${DOCKER_NETWORK:-ecoprofi_internal}"
subnet=$(docker network inspect "$network" --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' | awk '/^[0-9]+\./ {print; exit}')
uplink=$(ip -4 route show default | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
[ -n "$subnet" ] && [ -n "$uplink" ] || { echo 'Cannot determine Docker subnet or VPS uplink' >&2; exit 1; }

rule() {
    local -a args=("$@" -p tcp --tcp-flags SYN,RST SYN -m comment --comment ecoprofi-path-mtu -j TCPMSS --clamp-mss-to-pmtu)
    if [ "$action" = apply ]; then
        if ! iptables -w 10 -t mangle -C FORWARD "${args[@]}" 2>/dev/null; then
            iptables -w 10 -t mangle -A FORWARD "${args[@]}"
        fi
    else
        while iptables -w 10 -t mangle -C FORWARD "${args[@]}" 2>/dev/null; do
            iptables -w 10 -t mangle -D FORWARD "${args[@]}"
        done
    fi
}

rule -s "$subnet" -o "$uplink"
rule -d "$subnet" -i "$uplink"
echo "TCP MSS $action: network=$network subnet=$subnet uplink=$uplink"
