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

# Validate namespace name
if [[ ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo -e "${RED}Error: Invalid namespace name. Must be DNS-compatible (lowercase alphanumeric and hyphens).${NC}"
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

# 2. Create ServiceAccounts
echo -e "${YELLOW}  → Creating ServiceAccounts...${NC}"
kubectl create sa dev-user -n "$NAMESPACE"
kubectl create sa view-user -n "$NAMESPACE"

# 3. Apply Roles
echo -e "${YELLOW}  → Applying Roles...${NC}"
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/rbac/developer-role.yaml" | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" "$SCRIPT_DIR/rbac/viewer-role.yaml" | kubectl apply -f -

# 4. Create RoleBindings
echo -e "${YELLOW}  → Creating RoleBindings...${NC}"
sed "s/{{ROLE}}/developer/g; s/{{USER}}/dev-user/g; s/{{NAMESPACE}}/$NAMESPACE/g" \
    "$SCRIPT_DIR/rbac/rolebinding-template.yaml" | kubectl apply -f -
sed "s/{{ROLE}}/viewer/g; s/{{USER}}/view-user/g; s/{{NAMESPACE}}/$NAMESPACE/g" \
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
        CA_DATA=$(base64 -w 0 "$CA_FILE")
    fi
fi

# Function to generate kubeconfig
generate_kubeconfig() {
    local role=$1
    local sa_name=$2
    local out_file=$3

    # K3s v1.24+: Use TokenRequest API instead of reading static secret
    local token
    token=$(kubectl create token "$sa_name" -n "$NAMESPACE" --duration=8760h)

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
- name: $role-context
  context:
    cluster: $CLUSTER_NAME
    user: $sa_name
    namespace: $NAMESPACE
current-context: $role-context
EOF

    chmod 600 "$out_file"
}

generate_kubeconfig "developer" "dev-user" "${NAMESPACE}-dev-kubeconfig"
generate_kubeconfig "viewer" "view-user" "${NAMESPACE}-view-kubeconfig"

echo ""
echo -e "${GREEN}✅ Tenant '$NAMESPACE' created successfully!${NC}"
echo ""
echo -e "${BLUE}📁 Generated Kubeconfig Files:${NC}"
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
