#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

KUBECONFIG_HOST_PATH="${KUBECONFIG_HOST_PATH:-/etc/rancher/k3s/k3s.yaml}"
HOST_KUBECTL_PATH="${HOST_KUBECTL_PATH:-/usr/local/bin/kubectl}"
FLASK_PORT="${FLASK_PORT:-8080}"

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

echo -e "${BLUE}Checking host prerequisites for the lab portal...${NC}"

command -v docker >/dev/null 2>&1 || fail "docker is not installed."
ok "docker found: $(docker --version)"

if docker compose version >/dev/null 2>&1; then
    ok "Docker Compose plugin found: $(docker compose version --short 2>/dev/null || docker compose version)"
elif command -v docker-compose >/dev/null 2>&1; then
    ok "docker-compose found: $(docker-compose --version)"
else
    fail "Docker Compose is not installed. Install either the docker compose plugin or docker-compose."
fi

if ! docker info >/dev/null 2>&1; then
    fail "Docker daemon is not running or current user cannot access it."
fi
ok "Docker daemon is reachable."

if command -v k3s >/dev/null 2>&1; then
    ok "k3s found: $(k3s --version | head -n 1)"
else
    warn "k3s command not found. This is acceptable only if your kubeconfig points to another reachable Kubernetes cluster."
fi

if command -v kubectl >/dev/null 2>&1; then
    ok "kubectl found: $(kubectl version --client=true --short 2>/dev/null || kubectl version --client=true)"
else
    warn "kubectl is not installed on the host. The portal image includes kubectl, but host-side manual tests need it."
fi

if [ -f "$HOST_KUBECTL_PATH" ]; then
    ok "host kubectl mount source found: $HOST_KUBECTL_PATH"
else
    warn "HOST_KUBECTL_PATH does not exist: $HOST_KUBECTL_PATH. If image download also fails, set this to the real host kubectl path."
fi

if [ ! -f "$KUBECONFIG_HOST_PATH" ]; then
    fail "Kubeconfig not found at $KUBECONFIG_HOST_PATH. Set KUBECONFIG_HOST_PATH in .env if it lives elsewhere."
fi
ok "kubeconfig found: $KUBECONFIG_HOST_PATH"

if [ ! -r "$KUBECONFIG_HOST_PATH" ]; then
    fail "Kubeconfig exists but is not readable. Try: sudo chmod 644 $KUBECONFIG_HOST_PATH or run compose with appropriate permissions."
fi
ok "kubeconfig is readable."

if command -v kubectl >/dev/null 2>&1; then
    if KUBECONFIG="$KUBECONFIG_HOST_PATH" kubectl get nodes >/dev/null 2>&1; then
        ok "Kubernetes API is reachable from the host."
    else
        warn "kubectl cannot reach the cluster with $KUBECONFIG_HOST_PATH. Check K3s status and the server address in kubeconfig."
    fi
fi

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$FLASK_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    warn "Port $FLASK_PORT is already in use. Change FLASK_PORT or stop the conflicting process."
else
    ok "Port $FLASK_PORT appears available."
fi

echo -e "${GREEN}Preflight checks completed.${NC}"
