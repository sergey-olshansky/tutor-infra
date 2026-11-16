#!/usr/bin/env bash
# Run on the owner's LAN workstation, never on a public CI runner.
set -Eeuo pipefail
[[ "${GITHUB_ACTIONS:-false}" != true ]] || { echo 'Control recovery requires the home LAN.' >&2; exit 1; }
component="${1:-gateway}"
case "$component" in gateway|release|all) ;; *) echo 'Usage: recover-control-local.sh [gateway|release|all]' >&2; exit 2 ;; esac
ssh -o BatchMode=yes -o ConnectTimeout=10 "claw@${CLAW_HOST:-clawpi.local}" bash -s -- "$component" <<'REMOTE'
set -Eeuo pipefail
[[ $(hostname) == clawpi && $(uname -m) == aarch64 ]]
if [[ "$1" == gateway || "$1" == all ]]; then
    sudo -n -u openclaw XDG_RUNTIME_DIR=/run/user/1500 systemctl --user restart openclaw-gateway.service
    sudo -n -u openclaw XDG_RUNTIME_DIR=/run/user/1500 systemctl --user is-active openclaw-gateway.service
fi
if [[ "$1" == release || "$1" == all ]]; then
    sudo -n systemctl restart release-ops.service
    sudo -n systemctl is-active release-ops.service
fi
REMOTE
