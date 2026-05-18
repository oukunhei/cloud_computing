#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok() {
    echo -e "${GREEN}OK${NC} $1"
}

warn() {
    echo -e "${YELLOW}WARN${NC} $1"
}

fail() {
    echo -e "${RED}FAIL${NC} $1"
    exit 1
}

log() {
    echo -e "${BLUE}==>${NC} $1"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fail "Please run this script as root: sudo ./scripts/fix-k3s-flannel.sh"
    fi
}

backup_file() {
    local path="$1"
    if [ -f "$path" ]; then
        cp "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
        ok "Backed up $path"
    fi
}

rewrite_config_yaml() {
    local path="/etc/rancher/k3s/config.yaml"
    mkdir -p /etc/rancher/k3s

    if [ -f "$path" ]; then
        backup_file "$path"
        sed -i.bak '/^[[:space:]]*flannel-backend:[[:space:]]*none[[:space:]]*$/d' "$path"
        if grep -Eq '^[[:space:]]*disable-network-policy:[[:space:]]*(true|false)[[:space:]]*$' "$path"; then
            :
        fi
        if ! grep -Eq '^[[:space:]]*flannel-backend:[[:space:]]*' "$path"; then
            printf '\nflannel-backend: vxlan\n' >> "$path"
        fi
    else
        cat > "$path" <<'EOF'
flannel-backend: vxlan
EOF
    fi

    ok "Ensured $path enables flannel with vxlan"
}

rewrite_service_env() {
    local path="/etc/systemd/system/k3s.service.env"
    if [ ! -f "$path" ]; then
        return 0
    fi

    backup_file "$path"
    sed -i.bak \
        -e 's/--flannel-backend none/--flannel-backend=vxlan/g' \
        -e 's/--flannel-backend=none/--flannel-backend=vxlan/g' \
        "$path"
    ok "Rewrote flannel setting in $path"
}

main() {
    require_root

    log "Fixing K3s network configuration to use built-in flannel"
    rewrite_config_yaml
    rewrite_service_env

    log "Reloading systemd and restarting k3s"
    systemctl daemon-reload
    systemctl restart k3s

    log "Recommended verification"
    cat <<'EOF'
1. ip link show flannel.1
2. KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes
3. KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n kube-system
EOF
}

main "$@"
