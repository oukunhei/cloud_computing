#!/bin/bash
set -e

install_host_kubectl() {
    if [ -x /host/kubectl ]; then
        echo "✅ Using kubectl mounted from host."
        cp /host/kubectl /usr/local/bin/kubectl
        chmod 0755 /usr/local/bin/kubectl
        return 0
    fi
    return 1
}

install_runtime_kubectl() {
    echo "⏳ kubectl not found in container, trying short runtime install..."
    KUBE_VERSION="${KUBECTL_VERSION:-v1.30.6}"
    KUBECTL_BASE_URL="${KUBECTL_BASE_URL:-https://dl.k8s.io/release}"
    KUBECTL_ARCH="$(dpkg --print-architecture)"
    KUBECTL_URL="${KUBECTL_BASE_URL}/${KUBE_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl"
    if curl -fL --retry 1 --max-time 20 --connect-timeout 5 -o /tmp/kubectl "$KUBECTL_URL" 2>/dev/null; then
        install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
        rm -f /tmp/kubectl
        echo "✅ kubectl ${KUBE_VERSION} installed."
        return 0
    fi
    echo "⚠️  WARNING: Failed to download kubectl from ${KUBECTL_URL}."
    return 1
}

# kubectl is required for tenant onboarding and kubeconfig generation.
# Resolution order: image-bundled kubectl -> host-mounted kubectl -> short
# runtime download (when enabled).
if ! command -v kubectl >/dev/null 2>&1; then
    if ! install_host_kubectl; then
        case "${RUNTIME_KUBECTL_INSTALL:-auto}" in
            auto|always)
                install_runtime_kubectl || true
                ;;
            never)
                echo "⚠️  kubectl not found and runtime install is disabled."
                ;;
            *)
                echo "⚠️  Unknown RUNTIME_KUBECTL_INSTALL=${RUNTIME_KUBECTL_INSTALL}; expected auto, always, or never."
                ;;
        esac
    fi
fi

if command -v kubectl >/dev/null 2>&1; then
    kubectl version --client=true || true
else
    echo "⚠️  kubectl is unavailable. Portal will start, but tenant onboarding and kubeconfig generation will fail until kubectl is provided."
fi

# Copy kubeconfig from host mount and fix permissions
if [ -f /host/kubeconfig ]; then
    cp /host/kubeconfig /app/kubeconfig
    chmod 600 /app/kubeconfig
    echo "✅ Kubeconfig loaded from host."
else
    echo "❌ /host/kubeconfig not found."
    echo "   Set KUBECONFIG_HOST_PATH in .env or ensure K3s created /etc/rancher/k3s/k3s.yaml."
    exit 1
fi

# Wait for Kubernetes API to be reachable
if [ -f /app/kubeconfig ]; then
    echo "⏳ Testing Kubernetes connectivity..."
    for i in {1..30}; do
        if python -c "
from kubernetes import config, client
config.load_kube_config()
v1 = client.CoreV1Api()
v1.get_api_resources()
print('OK')
" 2>/dev/null; then
            echo "✅ Kubernetes API connected."
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "⚠️  Could not connect to Kubernetes API. Starting portal in disconnected mode."
            echo "   Check K3s status and the server address in /app/kubeconfig."
            break
        fi
        sleep 1
    done
fi

cd /app/web-portal
exec python app.py
