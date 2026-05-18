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
DEMO_TENANT="${DEMO_TENANT:-team-alpha}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
GRAFANA_PUBLIC_BASE_URL="${GRAFANA_PUBLIC_BASE_URL:-/grafana}"
GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-}"
GRAFANA_INTERNAL_BASE_URL="${GRAFANA_INTERNAL_BASE_URL:-http://127.0.0.1:3000}"
PROMETHEUS_INTERNAL_BASE_URL="${PROMETHEUS_INTERNAL_BASE_URL:-http://127.0.0.1:9090}"
PORTAL_METRICS_BASE_URL="${PORTAL_METRICS_BASE_URL:-http://127.0.0.1:${FLASK_PORT}}"
K3S_INSTALL_EXEC="${K3S_INSTALL_EXEC:-server --flannel-backend=vxlan}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_PUBLIC_BASE_URL="$PUBLIC_BASE_URL"

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

set_env_value() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" .env; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

detect_public_base_url() {
    local detected_ip=""

    detected_ip="$(curl -fsS --connect-timeout 2 https://api.ipify.org 2>/dev/null || true)"
    if [ -z "$detected_ip" ]; then
        detected_ip="$(curl -fsS --connect-timeout 2 https://ifconfig.me/ip 2>/dev/null || true)"
    fi
    if [ -n "$detected_ip" ]; then
        printf 'http://%s:%s' "$detected_ip" "$FLASK_PORT"
    else
        printf 'http://127.0.0.1:%s' "$FLASK_PORT"
    fi
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
        warn "K3s is configured with flannel disabled, so the node will stay NotReady with 'cni plugin not initialized'. Run sudo ./scripts/fix-k3s-flannel.sh on the host."
        warn "Continuing to start the portal so the UI and diagnostics remain available."
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

ensure_k3s_ready() {
    if [ -x ./scripts/fix-k3s-flannel.sh ]; then
        log "Ensuring K3s networking and registry mirrors"
        if [ "$(id -u)" -eq 0 ]; then
            ./scripts/fix-k3s-flannel.sh
        elif command -v sudo >/dev/null 2>&1; then
            sudo ./scripts/fix-k3s-flannel.sh
        else
            warn "sudo is not available; skipped automatic K3s repair."
        fi
    else
        warn "K3s repair helper not found or not executable: ./scripts/fix-k3s-flannel.sh"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl is-active --quiet k3s; then
            log "Starting k3s service"
            sudo systemctl start k3s
        fi
        ok "K3s service is active"
    else
        warn "systemctl not available; skipping K3s service check"
    fi
}

wait_for_cluster_ready() {
    if ! command -v kubectl >/dev/null 2>&1; then
        warn "kubectl is not installed on the host; skipping node readiness wait."
        return
    fi

    log "Waiting for Kubernetes node readiness"
    for i in {1..60}; do
        if KUBECONFIG="$KUBECONFIG_HOST_PATH" kubectl wait --for=condition=Ready node --all --timeout=5s >/dev/null 2>&1; then
            ok "Kubernetes nodes are Ready"
            return
        fi
        sleep 2
    done

    KUBECONFIG="$KUBECONFIG_HOST_PATH" kubectl get nodes || true
    fail "Kubernetes nodes did not become Ready. Check: journalctl -u k3s -b --no-pager | tail -n 120"
}

prepull_runtime_images() {
    if ! command -v crictl >/dev/null 2>&1; then
        warn "crictl not found; skipping K3s pause image pre-pull."
        return
    fi

    log "Pre-pulling K3s pod sandbox image"
    if crictl pull docker.io/rancher/mirrored-pause:3.6 >/dev/null 2>&1; then
        ok "K3s pod sandbox image is available"
    else
        warn "Could not pre-pull docker.io/rancher/mirrored-pause:3.6. Pod creation may wait on registry access."
    fi
}

log "Preparing environment file"
if [ ! -f .env ]; then
    cp .env.example .env
    ok "Created .env from .env.example"
else
    ok ".env already exists"
fi

for env_key in HOST_KUBECTL_PATH KUBECTL_VERSION KUBECTL_BASE_URL RUNTIME_KUBECTL_INSTALL SECRET_KEY PUBLIC_BASE_URL GRAFANA_PUBLIC_BASE_URL GRAFANA_ROOT_URL GRAFANA_INTERNAL_BASE_URL PROMETHEUS_INTERNAL_BASE_URL PORTAL_METRICS_BASE_URL; do
    if ! grep -q "^${env_key}=" .env; then
        grep "^${env_key}=" .env.example >> .env
        ok "Added missing ${env_key} to .env"
    fi
done

set -a
. ./.env
set +a

PUBLIC_BASE_URL="${ENV_PUBLIC_BASE_URL:-${PUBLIC_BASE_URL:-}}"
GRAFANA_PUBLIC_BASE_URL="${GRAFANA_PUBLIC_BASE_URL:-/grafana}"
GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-}"
if [ -z "$PUBLIC_BASE_URL" ]; then
    PUBLIC_BASE_URL="$(detect_public_base_url)"
    set_env_value PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
    ok "Detected PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"
fi
if [ "$GRAFANA_PUBLIC_BASE_URL" != "/grafana" ]; then
    GRAFANA_PUBLIC_BASE_URL="/grafana"
    set_env_value GRAFANA_PUBLIC_BASE_URL "$GRAFANA_PUBLIC_BASE_URL"
    ok "Configured Grafana to be served through the portal at /grafana"
fi
GRAFANA_ROOT_URL="${PUBLIC_BASE_URL%/}/grafana/"
set_env_value GRAFANA_ROOT_URL "$GRAFANA_ROOT_URL"
ok "Configured GRAFANA_ROOT_URL=${GRAFANA_ROOT_URL}"

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
ensure_k3s_ready

log "Preparing kubeconfig"
if [ ! -f "$KUBECONFIG_HOST_PATH" ]; then
    fail "Kubeconfig not found at $KUBECONFIG_HOST_PATH. Set KUBECONFIG_HOST_PATH in .env if needed."
fi

if [ ! -r "$KUBECONFIG_HOST_PATH" ]; then
    warn "Kubeconfig is not readable by current user. Trying sudo chmod 644."
    sudo chmod 644 "$KUBECONFIG_HOST_PATH"
fi
ok "Kubeconfig is readable: $KUBECONFIG_HOST_PATH"
wait_for_cluster_ready
prepull_runtime_images

log "Running preflight checks"
KUBECONFIG_HOST_PATH="$KUBECONFIG_HOST_PATH" HOST_KUBECTL_PATH="$HOST_KUBECTL_PATH" FLASK_PORT="$FLASK_PORT" ./scripts/check-prereqs.sh

log "Using monitoring endpoints"
ok "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"
ok "GRAFANA_PUBLIC_BASE_URL=${GRAFANA_PUBLIC_BASE_URL}"
ok "GRAFANA_ROOT_URL=${GRAFANA_ROOT_URL}"
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
  Portal:   ${PUBLIC_BASE_URL%/}/login
  Grafana:  ${PUBLIC_BASE_URL%/}/grafana/  (embedded in the Resource page)
  Prometheus: http://127.0.0.1:9090

Useful commands:
  docker compose logs -f web
  docker compose ps
  docker compose down

Optional demo tenant:
  CREATE_DEMO_TENANT=true ./scripts/start-lab-platform.sh

EOF
