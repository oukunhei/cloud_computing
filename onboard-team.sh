#!/bin/bash
# Usage: ./onboard-team.sh <team-namespace>
# Multi-tenant Kubernetes Lab Platform - Tenant Onboarding Script
# Compatible with K3s v1.24+ (ServiceAccount token via TokenRequest API)

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Prerequisites check
command -v kubectl >/dev/null 2>&1 || { echo -e "${RED}Error: kubectl is required but not installed.${NC}" >&2; exit 1; }

if [ -z "${1:-}" ]; then
    echo -e "${RED}Usage: $0 <team-namespace>${NC}"
    echo -e "Example: $0 team-alpha"
    exit 1
fi

NAMESPACE=$1
USER_NAMESPACE="${USER_NAMESPACE:-lab-platform-users}"

# Validate namespace name
if [[ ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo -e "${RED}Error: Invalid namespace name. Must be DNS-compatible (lowercase alphanumeric and hyphens).${NC}"
    exit 1
fi
if [ "${#NAMESPACE}" -gt 63 ]; then
    echo -e "${RED}Error: Namespace name must be 63 characters or fewer.${NC}"
    exit 1
fi

# Check if namespace already exists
if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Error: Namespace '$NAMESPACE' already exists.${NC}"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}🚀 Creating tenant: $NAMESPACE${NC}"

# 1. Create Namespace
echo -e "${YELLOW}  → Creating namespace...${NC}"
kubectl create namespace "$NAMESPACE"
kubectl label namespace "$NAMESPACE" \
    tenant.lab/platform=enabled \
    pod-security.kubernetes.io/enforce=baseline \
    pod-security.kubernetes.io/audit=restricted \
    pod-security.kubernetes.io/warn=restricted \
    --overwrite

# 2. Create ServiceAccounts
echo -e "${YELLOW}  → Creating isolated user ServiceAccounts...${NC}"
kubectl create namespace "$USER_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$USER_NAMESPACE" tenant.lab/system=users --overwrite
kubectl create sa "${NAMESPACE}-admin" -n "$USER_NAMESPACE"
kubectl create sa "${NAMESPACE}-dev" -n "$USER_NAMESPACE"
kubectl create sa "${NAMESPACE}-view" -n "$USER_NAMESPACE"

# 3. Apply Roles
echo -e "${YELLOW}  → Applying Roles...${NC}"
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/rbac/admin-role.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/rbac/developer-role.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/rbac/viewer-role.yaml" | kubectl apply -f -

# 4. Create RoleBindings
echo -e "${YELLOW}  → Creating RoleBindings...${NC}"
sed "s/{{ROLE}}/admin/g; s/{{USER}}/${NAMESPACE}-admin/g; s/{{NAMESPACE}}/$NAMESPACE/g; s/{{USER_NAMESPACE}}/$USER_NAMESPACE/g" \
    "$SCRIPT_DIR/rbac/rolebinding-template.yaml" | kubectl apply -f -
sed "s/{{ROLE}}/developer/g; s/{{USER}}/${NAMESPACE}-dev/g; s/{{NAMESPACE}}/$NAMESPACE/g; s/{{USER_NAMESPACE}}/$USER_NAMESPACE/g" \
    "$SCRIPT_DIR/rbac/rolebinding-template.yaml" | kubectl apply -f -
sed "s/{{ROLE}}/viewer/g; s/{{USER}}/${NAMESPACE}-view/g; s/{{NAMESPACE}}/$NAMESPACE/g; s/{{USER_NAMESPACE}}/$USER_NAMESPACE/g" \
    "$SCRIPT_DIR/rbac/rolebinding-template.yaml" | kubectl apply -f -

# 5. Apply ResourceQuota and LimitRange
echo -e "${YELLOW}  → Applying ResourceQuota and LimitRange...${NC}"
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/resources/quota.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/resources/limitrange.yaml" | kubectl apply -f -

# 6. Apply NetworkPolicies
echo -e "${YELLOW}  → Applying NetworkPolicies...${NC}"
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/networkpolicies/default-deny-ingress.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/networkpolicies/allow-same-namespace.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/networkpolicies/allow-dns.yaml" | kubectl apply -f -

# 7. Generate kubeconfig files (K3s v1.24+ compatible)
echo -e "${YELLOW}  → Generating kubeconfig files...${NC}"

# Get cluster information
CLUSTER_NAME=$(kubectl config current-context)
SERVER=$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.server}")
CA_DATA=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.certificate-authority-data}")

if [ -z "$CA_DATA" ]; then
    CA_FILE=$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.certificate-authority}")
    if [ -n "$CA_FILE" ] && [ -f "$CA_FILE" ]; then
        CA_DATA=$(base64 < "$CA_FILE" | tr -d '\n')
    fi
fi

# Function to generate kubeconfig
generate_kubeconfig() {
    local role=$1
    local sa_name=$2
    local out_file=$3

    # K3s v1.24+: Use TokenRequest API instead of reading static secret
    local token
    token=$(kubectl create token "$sa_name" -n "$USER_NAMESPACE" --duration=8760h)
    local context_name="${NAMESPACE}-${role}"

    cat > "$out_file" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: $CLUSTER_NAME
  cluster:
    server: $SERVER
    certificate-authority-data: $CA_DATA
users:
- name: $sa_name
  user:
    token: $token
contexts:
- name: $context_name
  context:
    cluster: $CLUSTER_NAME
    user: $sa_name
    namespace: $NAMESPACE
current-context: $context_name
EOF

    chmod 600 "$out_file"
}

generate_kubeconfig "admin" "${NAMESPACE}-admin" "${NAMESPACE}-admin-kubeconfig"
generate_kubeconfig "developer" "${NAMESPACE}-dev" "${NAMESPACE}-dev-kubeconfig"
generate_kubeconfig "viewer" "${NAMESPACE}-view" "${NAMESPACE}-view-kubeconfig"

echo ""
echo -e "${GREEN}✅ Tenant '$NAMESPACE' created successfully!${NC}"
echo ""
echo -e "${BLUE}📁 Generated Kubeconfig Files:${NC}"
echo -e "   ${GREEN}Admin:${NC}     ${NAMESPACE}-admin-kubeconfig"
echo -e "   ${GREEN}Developer:${NC} ${NAMESPACE}-dev-kubeconfig"
echo -e "   ${GREEN}Viewer:${NC}    ${NAMESPACE}-view-kubeconfig"
echo ""
echo -e "${BLUE}🔐 Quick Start:${NC}"
echo -e "   export KUBECONFIG=./${NAMESPACE}-dev-kubeconfig"
echo -e "   kubectl get pods"
echo ""
echo -e "${BLUE}🧪 Test Network Isolation:${NC}"
echo -e "   kubectl run test-nginx --image=nginx --namespace $NAMESPACE"
echo ""
