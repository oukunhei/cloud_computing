#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ALPHA="team-alpha"
BETA="team-beta"
USER_NS="lab-platform-users"
LABEL="validation-run=isolation"

for ns in "$ALPHA" "$BETA"; do
    kubectl delete ns "$ns" --ignore-not-found=true --wait=true
    sleep 2
    bash "$PROJECT_ROOT/onboard-team.sh" "$ns"
done

PASS() { echo -e "\033[32m[PASS]\033[0m $*"; }
FAIL() { echo -e "\033[31m[FAIL]\033[0m $*"; }
INFO() { echo -e "\033[34m[INFO]\033[0m $*"; }
STEP() { echo -e "\033[36m[STEP]\033[0m $*"; }

cleanup() {
    INFO "Cleaning up temporary resources..."
    kubectl delete pod,svc -n "$ALPHA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete pod -n "$BETA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    sleep 2
}
trap cleanup EXIT

if ! command -v kubectl &>/dev/null; then
    echo "kubectl not found" >&2
    exit 1
fi

for ns in "$ALPHA" "$BETA"; do
    if ! kubectl get ns "$ns" &>/dev/null; then
        INFO "Creating tenant $ns ..."
        bash "$PROJECT_ROOT/onboard-team.sh" "$ns"
    fi
done

FAILED=0

# ═══════════════════════════════════════════════════════════
# Experiment 1.1: RBAC Lateral Movement Boundary Testing
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "Experiment 1.1: RBAC Lateral Movement & Privilege Escalation"
INFO "═══════════════════════════════════════════════════════"

SA_DEV="system:serviceaccount:$USER_NS:${ALPHA}-dev"
SA_ADMIN="system:serviceaccount:$USER_NS:${ALPHA}-admin"
SA_VIEW="system:serviceaccount:$USER_NS:${ALPHA}-view"

# Developer read cross namespace Pods → Forbidden
STEP "Developer read $BETA Pods"
OUTPUT=$(kubectl auth can-i get pods -n "$BETA" --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Developer read cross Namespace Pods → Forbidden"
else
    FAIL "Developer read cross Namespace Pods → $OUTPUT"
    ((FAILED++)) || true
fi

# Developer delete Namespace → Forbidden
STEP "Developer delete Namespace"
OUTPUT=$(kubectl auth can-i delete namespaces --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Developer delete Namespace → Forbidden"
else
    FAIL "Developer delete Namespace → $OUTPUT"
    ((FAILED++)) || true
fi

# Developer create Deployment → Allowed
STEP "Developer create Deployment in $ALPHA"
OUTPUT=$(kubectl auth can-i create deployments -n "$ALPHA" --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^yes$"; then
    PASS "Developer create Deployment → Allowed"
else
    FAIL "Developer create Deployment → $OUTPUT"
    ((FAILED++)) || true
fi

# Viewer delete Pod → Forbidden
STEP "Viewer delete $ALPHA Pod"
OUTPUT=$(kubectl auth can-i delete pods -n "$ALPHA" --as "$SA_VIEW" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Viewer delete Pod → Forbidden"
else
    FAIL "Viewer delete Pod → $OUTPUT"
    ((FAILED++)) || true
fi

# Admin delete Namespace → Forbidden (tenant admin cannot delete ns)
STEP "Admin delete Namespace"
OUTPUT=$(kubectl auth can-i delete namespaces --as "$SA_ADMIN" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Admin delete Namespace → Forbidden"
else
    FAIL "Admin delete Namespace → $OUTPUT"
    ((FAILED++)) || true
fi

if [ "$FAILED" -eq 0 ]; then
    PASS "Experiment 1.1 result: ALL Pass"
else
    FAIL "Experiment 1.1 result: $FAILED items failed"
fi


# ═══════════════════════════════════════════════════════════
# Experiment 1.2: Network Micro-segmentation & Cross-Namespace Isolation
# ═══════════════════════════════════════════════════════════
INFO "════════════════════════════════════════════════════════════════════════"
INFO "Experiment 1.2: Network Micro-segmentation & Cross-Namespace Isolation"
INFO "════════════════════════════════════════════════════════════════════════"

NET_FAILED=0
NGINX_IP=""

# deploy nginx
STEP " Deploy nginx on $ALPHA"
kubectl run nginx-alpha -n "$ALPHA" --image=nginx:alpine --restart=Never \
    --labels="app=validation-nginx,$LABEL" --port=80
kubectl expose pod nginx-alpha -n "$ALPHA" --name=nginx-alpha --port=80 \
    --selector="app=validation-nginx"

if ! OUTPUT=$(kubectl wait --for=condition=Ready pod/nginx-alpha -n "$ALPHA" --timeout=60s 2>&1); then
    FAIL "nginx-alpha failed to be ready within 60s: $OUTPUT"
    NET_FAILED=1
else
    NGINX_IP=$(kubectl get pod nginx-alpha -n "$ALPHA" -o jsonpath='{.status.podIP}')
    INFO "nginx-alpha Pod IP: $NGINX_IP"

    # 探测 Pod
    kubectl run curl-beta -n "$BETA" --image=busybox:stable --restart=Never \
        --labels="$LABEL" -- sleep 300
    kubectl run curl-alpha -n "$ALPHA" --image=busybox:stable --restart=Never \
        --labels="$LABEL" -- sleep 300

    kubectl wait --for=condition=Ready pod/curl-beta -n "$BETA" --timeout=60s >/dev/null 2>&1 || true
    kubectl wait --for=condition=Ready pod/curl-alpha -n "$ALPHA" --timeout=60s >/dev/null 2>&1 || true

    # 1) cross tenant Pod IP connect → should fail
    STEP "Cross-tenant Pod IP connectivity ($BETA -> $NGINX_IP)"
    if OUTPUT=$(kubectl exec curl-beta -n "$BETA" -- wget -q -O- -T 5 "http://$NGINX_IP" 2>&1); then
        FAIL "Cross-tenant Pod IP connectivity → Success (expected to be blocked). Output: $OUTPUT"
        ((NET_FAILED++)) || true
    else
        PASS "Cross-tenant Pod IP connectivity → Blocked"
    fi

    # 2) cross tenant Service DNS → should be blocked (NetworkPolicy allow same Namespace visit)
    STEP "Cross-tenant Service DNS (nginx-alpha.$ALPHA.svc)"
    if OUTPUT=$(kubectl exec curl-beta -n "$BETA" -- wget -q -O- -T 5 "http://nginx-alpha.$ALPHA.svc.cluster.local" 2>&1); then
        FAIL "Cross-tenant Service DNS → Success (expected to be blocked). Output: $OUTPUT"
        ((NET_FAILED++)) || true
    else
        PASS "Cross-tenant Service DNS → Blocked"
    fi

    # 3) same Namespace → should success
    STEP "Same-namespace connectivity ($ALPHA -> nginx-alpha)"
    if OUTPUT=$(kubectl exec curl-alpha -n "$ALPHA" -- wget -q -O- -T 5 "http://nginx-alpha" 2>&1); then
        PASS "Same-namespace connectivity → Success"
    else
        FAIL "Same-namespace connectivity → Failed (expected success): $OUTPUT"
        ((NET_FAILED++)) || true
    fi

    # DNS 
    STEP "DNS resolution test (Reference)"
    DNS_OUT=$(kubectl exec curl-beta -n "$BETA" -- nslookup "nginx-alpha.$ALPHA.svc.cluster.local" 2>&1 || true)
    if echo "$DNS_OUT" | grep -qi "address"; then
        PASS "DNS resolvable (allow-dns policy active)"
    else
        FAIL "DNS resolution failed (expected success). Output: $DNS_OUT"
        ((NET_FAILED++)) || true
    fi
fi

if [ "$NET_FAILED" -eq 0 ]; then
    PASS "Experiment 1.2 result: ALL Pass"
else
    FAIL "Experiment 1.2 result: $NET_FAILED items failed"
fi

INFO "═══════════════════════════════════════════════════════"
INFO "Isolation Experiment execution finished"
INFO "═══════════════════════════════════════════════════════"
