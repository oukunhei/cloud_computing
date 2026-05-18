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
        if grep -Eq '^[[:space:]]*flannel-backend:[[:space:]]*' "$path"; then
            sed -i.bak -E 's/^[[:space:]]*flannel-backend:[[:space:]]*.*/flannel-backend: vxlan/' "$path"
        else
            printf '\nflannel-backend: vxlan\n' >> "$path"
        fi
    else
        cat > "$path" <<'EOF'
flannel-backend: vxlan
EOF
    fi

    ok "Ensured $path enables flannel with vxlan"
}

rewrite_k3s_arg_file() {
    local path="$1"
    local tmp_path
    if [ ! -f "$path" ]; then
        return 0
    fi

    if ! grep -Eq -- '--flannel-backend([=[:space:]]+|[[:space:]]*["'\'']?[[:space:]]*)none|--flannel-backend' "$path"; then
        return 0
    fi

    backup_file "$path"
    sed -i.bak \
        -E \
        -e 's/--flannel-backend[=[:space:]]+["'\'']?none["'\'']?/--flannel-backend=vxlan/g' \
        -e 's/--flannel-backend=["'\'']?none["'\'']?/--flannel-backend=vxlan/g' \
        "$path"

    tmp_path="$(mktemp)"
    awk '
        /--flannel-backend["'\'']?[[:space:]]*\\?$/ {
            print
            if (getline next_line) {
                sub(/none/, "vxlan", next_line)
                print next_line
            }
            next
        }
        { print }
    ' "$path" > "$tmp_path"
    cat "$tmp_path" > "$path"
    rm -f "$tmp_path"

    ok "Rewrote flannel setting in $path if it was present"
}

rewrite_systemd_sources() {
    local path

    for path in \
        /etc/systemd/system/k3s.service \
        /etc/systemd/system/k3s.service.env \
        /lib/systemd/system/k3s.service \
        /usr/lib/systemd/system/k3s.service; do
        rewrite_k3s_arg_file "$path"
    done

    if [ -d /etc/systemd/system/k3s.service.d ]; then
        for path in /etc/systemd/system/k3s.service.d/*.conf; do
            [ -e "$path" ] || continue
            rewrite_k3s_arg_file "$path"
        done
    fi
}

ensure_containerd_registry_mirrors() {
    local path="/etc/rancher/k3s/registries.yaml"

    if [ -f "$path" ]; then
        backup_file "$path"
    fi

    cat > "$path" <<'EOF'
mirrors:
  docker.io:
    endpoint:
      - "https://docker.m.daocloud.io"
      - "https://docker.nju.edu.cn"
      - "https://docker.mirrors.sjtug.sjtu.edu.cn"
EOF

    ok "Configured K3s containerd registry mirrors in $path"
}

verify_no_disabled_flannel_args() {
    if grep -Eq '^[[:space:]]*flannel-backend:[[:space:]]*none[[:space:]]*$' /etc/rancher/k3s/config.yaml 2>/dev/null; then
        fail "flannel-backend: none is still present in /etc/rancher/k3s/config.yaml"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if systemctl cat k3s 2>/dev/null | grep -Eq -- '--flannel-backend([=[:space:]]+)none'; then
            systemctl cat k3s 2>/dev/null | grep -n -- '--flannel-backend' || true
            fail "--flannel-backend none is still present in the k3s systemd unit. Please paste the output above."
        fi
        if systemctl cat k3s 2>/dev/null | awk '
            /--flannel-backend["'\'']?[[:space:]]*\\?$/ {
                if (getline next_line && next_line ~ /none/) {
                    found=1
                }
            }
            END { exit found ? 0 : 1 }
        '; then
            systemctl cat k3s 2>/dev/null | grep -n -A2 -- '--flannel-backend' || true
            fail "--flannel-backend followed by none is still present in the k3s systemd unit. Please paste the output above."
        fi
    fi
}

main() {
    require_root

    log "Fixing K3s network configuration to use built-in flannel"
    rewrite_config_yaml
    rewrite_systemd_sources
    ensure_containerd_registry_mirrors
    verify_no_disabled_flannel_args

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
