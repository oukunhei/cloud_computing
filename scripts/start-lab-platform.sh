#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

KUBECONFIG_HOST_PATH="${KUBECONFIG_HOST_PATH:-/etc/rancher/k3s/k3s.yaml}"
HOST_KUBECTL_PATH="${HOST_KUBECTL_PATH:-/usr/local/bin/kubectl}"
FLASK_PORT="${FLASK_PORT:-8080}"
DEMO_TENANT="${DEMO_TENANT:-team-alpha}"
GRAFANA_PUBLIC_BASE_URL="${GRAFANA_PUBLIC_BASE_URL:-http://127.0.0.1:3000}"
GRAFANA_INTERNAL_BASE_URL="${GRAFANA_INTERNAL_BASE_URL:-http://127.0.0.1:3000}"
PROMETHEUS_INTERNAL_BASE_URL="${PROMETHEUS_INTERNAL_BASE_URL:-http://127.0.0.1:9090}"
PORTAL_METRICS_BASE_URL="${PORTAL_METRICS_BASE_URL:-http://127.0.0.1:${FLASK_PORT}}"
K3S_INSTALL_EXEC="${K3S_INSTALL_EXEC:-server --flannel-backend=vxlan}"

log() {
    echo -e "${BLUE}==>${NC} $1"
}

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

inspect_k3s_network_config() {
    local found_issue=0

    if [ -f /etc/rancher/k3s/config.yaml ] && grep -Eq '(^|\s)flannel-backend:\s*none(\s|$)' /etc/rancher/k3s/config.yaml; then
        warn "Detected disabled K3s CNI in /etc/rancher/k3s/config.yaml: flannel-backend: none"
        found_issue=1
    fi

    if [ -f /etc/systemd/system/k3s.service.env ] && grep -Fq -- '--flannel-backend none' /etc/systemd/system/k3s.service.env; then
        warn "Detected disabled K3s CNI in /etc/systemd/system/k3s.service.env: --flannel-backend none"
        found_issue=1
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if systemctl cat k3s 2>/dev/null | grep -Fq -- '--flannel-backend none'; then
            warn "Detected disabled K3s CNI in systemd unit output: --flannel-backend none"
            found_issue=1
        fi
    fi

    if [ "$found_issue" -eq 1 ]; then
        fail "K3s is configured with flannel disabled, so the node will stay NotReady with 'cni plugin not initialized'. Run ./scripts/fix-k3s-flannel.sh on the host, then restart k3s."
    fi
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        fail "Docker Compose is not installed."
    fi
}

log "Preparing environment file"
if [ ! -f .env ]; then
    cp .env.example .env
    ok "Created .env from .env.example"
else
    ok ".env already exists"
fi

for env_key in HOST_KUBECTL_PATH KUBECTL_VERSION KUBECTL_BASE_URL RUNTIME_KUBECTL_INSTALL SECRET_KEY GRAFANA_PUBLIC_BASE_URL GRAFANA_INTERNAL_BASE_URL PROMETHEUS_INTERNAL_BASE_URL PORTAL_METRICS_BASE_URL; do
    if ! grep -q "^${env_key}=" .env; then
        grep "^${env_key}=" .env.example >> .env
        ok "Added missing ${env_key} to .env"
    fi
done

log "Checking Docker"
command -v docker >/dev/null 2>&1 || fail "Docker is required. Install Docker Engine first."
docker info >/dev/null 2>&1 || fail "Docker daemon is not running or current user cannot access it."
ok "Docker daemon is reachable"

log "Checking K3s"
if ! command -v k3s >/dev/null 2>&1; then
    if [ "${INSTALL_K3S:-false}" = "true" ]; then
        warn "K3s not found. Installing K3s because INSTALL_K3S=true."
        curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="$K3S_INSTALL_EXEC" sh -
    else
        fail "K3s is not installed. Install it first, or run: INSTALL_K3S=true ./scripts/start-lab-platform.sh"
    fi
fi

inspect_k3s_network_config

if command -v systemctl >/dev/null 2>&1; then
    if ! systemctl is-active --quiet k3s; then
        log "Starting k3s service"
        sudo systemctl start k3s
    fi
    ok "K3s service is active"
else
    warn "systemctl not available; skipping K3s service check"
fi

log "Preparing kubeconfig"
if [ ! -f "$KUBECONFIG_HOST_PATH" ]; then
    fail "Kubeconfig not found at $KUBECONFIG_HOST_PATH. Set KUBECONFIG_HOST_PATH in .env if needed."
fi

if [ ! -r "$KUBECONFIG_HOST_PATH" ]; then
    warn "Kubeconfig is not readable by current user. Trying sudo chmod 644."
    sudo chmod 644 "$KUBECONFIG_HOST_PATH"
fi
ok "Kubeconfig is readable: $KUBECONFIG_HOST_PATH"

log "Running preflight checks"
KUBECONFIG_HOST_PATH="$KUBECONFIG_HOST_PATH" HOST_KUBECTL_PATH="$HOST_KUBECTL_PATH" FLASK_PORT="$FLASK_PORT" ./scripts/check-prereqs.sh

log "Using monitoring endpoints"
ok "GRAFANA_PUBLIC_BASE_URL=${GRAFANA_PUBLIC_BASE_URL}"
ok "GRAFANA_INTERNAL_BASE_URL=${GRAFANA_INTERNAL_BASE_URL}"
ok "PROMETHEUS_INTERNAL_BASE_URL=${PROMETHEUS_INTERNAL_BASE_URL}"
ok "PORTAL_METRICS_BASE_URL=${PORTAL_METRICS_BASE_URL}"

log "Starting all services (web, prometheus, grafana)"
if [ "${FULL_RESET:-false}" = "true" ]; then
    warn "FULL_RESET=true: removing existing containers, networks, and named volumes."
    compose down --volumes --remove-orphans
fi
compose up -d --build --force-recreate web
compose up -d prometheus grafana

log "Waiting for portal HTTP endpoint (port ${FLASK_PORT})"
for i in {1..60}; do
    if curl -fsS "http://127.0.0.1:${FLASK_PORT}/login" >/dev/null 2>&1; then
        ok "Portal is reachable at http://127.0.0.1:${FLASK_PORT}"
        break
    fi
    if [ "$i" -eq 60 ]; then
        compose logs --tail=80 web || true
        fail "Portal did not become reachable on port $FLASK_PORT."
    fi
    sleep 2
done

log "Checking portal backend build"
PORTAL_VERSION_JSON="$(curl -fsS "http://127.0.0.1:${FLASK_PORT}/api/portal/version" || true)"
if echo "$PORTAL_VERSION_JSON" | grep -q "custom-pod-create-v2"; then
    ok "Portal backend is running the custom Pod creation build: ${PORTAL_VERSION_JSON}"
else
    compose logs --tail=80 web || true
    fail "Portal backend version check did not return custom-pod-create-v2. Response was: ${PORTAL_VERSION_JSON:-<empty>}. Stop old portal processes/containers or rerun with FULL_RESET=true."
fi

log "Waiting for Prometheus (port 9090)"
for i in {1..30}; do
    if curl -fsS "http://127.0.0.1:9090/-/healthy" >/dev/null 2>&1; then
        ok "Prometheus is ready at http://127.0.0.1:9090"
        break
    fi
    [ "$i" -eq 30 ] && warn "Prometheus did not become reachable (non-fatal)"
    sleep 2
done

log "Waiting for Grafana (port 3000)"
for i in {1..30}; do
    if curl -fsS "http://127.0.0.1:3000/api/health" >/dev/null 2>&1; then
        ok "Grafana is ready at http://127.0.0.1:3000"
        break
    fi
    [ "$i" -eq 30 ] && warn "Grafana did not become reachable (non-fatal)"
    sleep 2
done

if [ "${CREATE_DEMO_TENANT:-false}" = "true" ]; then
    log "Creating demo tenant: $DEMO_TENANT"
    if command -v kubectl >/dev/null 2>&1; then
        if KUBECONFIG="$KUBECONFIG_HOST_PATH" kubectl get namespace "$DEMO_TENANT" >/dev/null 2>&1; then
            ok "Demo tenant already exists: $DEMO_TENANT"
        else
            KUBECONFIG="$KUBECONFIG_HOST_PATH" ./onboard-team.sh "$DEMO_TENANT"
            ok "Demo tenant created: $DEMO_TENANT"
        fi
    else
        warn "kubectl is not installed on host; skipped demo tenant creation."
    fi
fi

cat <<EOF

${GREEN}Lab platform is running.${NC}

Open:
  Portal:   http://127.0.0.1:${FLASK_PORT}/login
  Grafana:  http://127.0.0.1:3000  (admin / admin)
  Prometheus: http://127.0.0.1:9090

Useful commands:
  docker compose logs -f web
  docker compose ps
  docker compose down

Optional demo tenant:
  CREATE_DEMO_TENANT=true ./scripts/start-lab-platform.sh

EOF
