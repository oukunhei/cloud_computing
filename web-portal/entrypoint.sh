#!/bin/bash
set -e

# kubectl is installed in the Docker image. Keep a runtime fallback for
# manually rebuilt images or unusual base-image changes.
if ! command -v kubectl >/dev/null 2>&1; then
    echo "⏳ kubectl not found in container, installing matching version..."
    KUBE_VERSION="${KUBECTL_VERSION:-v1.30.6}"
    KUBECTL_BASE_URL="${KUBECTL_BASE_URL:-https://dl.k8s.io/release}"
    KUBECTL_ARCH="$(dpkg --print-architecture)"
    KUBECTL_URL="${KUBECTL_BASE_URL}/${KUBE_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl"
    if curl -fL --retry 3 --max-time 120 --connect-timeout 10 -o /tmp/kubectl "$KUBECTL_URL" 2>/dev/null; then
        install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
        rm -f /tmp/kubectl
        echo "✅ kubectl ${KUBE_VERSION} installed."
    else
        echo "⚠️  WARNING: Failed to download kubectl from ${KUBECTL_URL}. Tenant onboarding and kubeconfig generation may fail."
    fi
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
