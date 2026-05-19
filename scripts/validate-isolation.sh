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
    INFO "清理临时资源..."
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
        INFO "创建租户 $ns ..."
        bash "$PROJECT_ROOT/onboard-team.sh" "$ns"
    fi
done

FAILED=0

# ═══════════════════════════════════════════════════════════
# 实验 1.1: RBAC 横向越权边界测试
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "实验 1.1: RBAC 横向越权边界测试"
INFO "═══════════════════════════════════════════════════════"

SA_DEV="system:serviceaccount:$USER_NS:${ALPHA}-dev"
SA_ADMIN="system:serviceaccount:$USER_NS:${ALPHA}-admin"
SA_VIEW="system:serviceaccount:$USER_NS:${ALPHA}-view"

# Developer 读跨租户 Pods → Forbidden
STEP "Developer 读取 $BETA Pods"
OUTPUT=$(kubectl auth can-i get pods -n "$BETA" --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Developer 读取跨租户 Pods → Forbidden"
else
    FAIL "Developer 读取跨租户 Pods → $OUTPUT"
    ((FAILED++)) || true
fi

# Developer 删 Namespace → Forbidden
STEP "Developer 删除 Namespace"
OUTPUT=$(kubectl auth can-i delete namespaces --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Developer 删除 Namespace → Forbidden"
else
    FAIL "Developer 删除 Namespace → $OUTPUT"
    ((FAILED++)) || true
fi

# Developer 在自家创建 Deployment → Allowed
STEP "Developer 在 $ALPHA 创建 Deployment"
OUTPUT=$(kubectl auth can-i create deployments -n "$ALPHA" --as "$SA_DEV" 2>&1)
if echo "$OUTPUT" | grep -q "^yes$"; then
    PASS "Developer 创建 Deployment → Allowed"
else
    FAIL "Developer 创建 Deployment → $OUTPUT"
    ((FAILED++)) || true
fi

# Viewer 删 Pod → Forbidden
STEP "Viewer 删除 $ALPHA Pod"
OUTPUT=$(kubectl auth can-i delete pods -n "$ALPHA" --as "$SA_VIEW" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Viewer 删除 Pod → Forbidden"
else
    FAIL "Viewer 删除 Pod → $OUTPUT"
    ((FAILED++)) || true
fi

# Admin 删 Namespace → Forbidden (tenant admin 不能删 ns)
STEP "Admin 删除 Namespace"
OUTPUT=$(kubectl auth can-i delete namespaces --as "$SA_ADMIN" 2>&1)
if echo "$OUTPUT" | grep -q "^no$"; then
    PASS "Admin 删除 Namespace → Forbidden"
else
    FAIL "Admin 删除 Namespace → $OUTPUT"
    ((FAILED++)) || true
fi

if [ "$FAILED" -eq 0 ]; then
    PASS "实验 1.1 结论: 全部通过"
else
    FAIL "实验 1.1 结论: $FAILED 项未通过"
fi


# ═══════════════════════════════════════════════════════════
# 实验 1.2: 网络微分段与跨 Namespace 隔离测试
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "实验 1.2: 网络微分段与跨 Namespace 隔离测试"
INFO "═══════════════════════════════════════════════════════"

NET_FAILED=0
NGINX_IP=""

# 部署 nginx（带 app label 避免被误选）
STEP "在 $ALPHA 部署 nginx"
kubectl run nginx-alpha -n "$ALPHA" --image=nginx:alpine --restart=Never \
    --labels="app=validation-nginx,$LABEL" --port=80
kubectl expose pod nginx-alpha -n "$ALPHA" --name=nginx-alpha --port=80 \
    --selector="app=validation-nginx"

if ! OUTPUT=$(kubectl wait --for=condition=Ready pod/nginx-alpha -n "$ALPHA" --timeout=60s 2>&1); then
    FAIL "nginx-alpha 未在 60s 内就绪: $OUTPUT"
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

    # 1) 跨租户 Pod IP 直连 → 应失败
    STEP "跨租户 Pod IP 直连 ($BETA -> $NGINX_IP)"
    if OUTPUT=$(kubectl exec curl-beta -n "$BETA" -- wget -q -O- -T 5 "http://$NGINX_IP" 2>&1); then
        FAIL "跨租户 Pod IP 直连 → 成功 (应被阻断). Output: $OUTPUT"
        ((NET_FAILED++)) || true
    else
        PASS "跨租户 Pod IP 直连 → 被阻断"
    fi

    # 2) 跨租户 Service DNS → 应被阻断 (NetworkPolicy 仅允许同 Namespace 访问)
    STEP "跨租户 Service DNS (nginx-alpha.$ALPHA.svc)"
    if OUTPUT=$(kubectl exec curl-beta -n "$BETA" -- wget -q -O- -T 5 "http://nginx-alpha.$ALPHA.svc.cluster.local" 2>&1); then
        FAIL "跨租户 Service DNS → 成功 (应被阻断). Output: $OUTPUT"
        ((NET_FAILED++)) || true
    else
        PASS "跨租户 Service DNS → 被阻断"
    fi

    # 3) 同 Namespace → 应成功
    STEP "同 Namespace 访问 ($ALPHA -> nginx-alpha)"
    if OUTPUT=$(kubectl exec curl-alpha -n "$ALPHA" -- wget -q -O- -T 5 "http://nginx-alpha" 2>&1); then
        PASS "同 Namespace 访问 → 成功"
    else
        FAIL "同 Namespace 访问 → 失败 (应成功): $OUTPUT"
        ((NET_FAILED++)) || true
    fi

    # DNS 解析参考（仅输出）
    STEP "DNS 解析测试 (参考)"
    DNS_OUT=$(kubectl exec curl-beta -n "$BETA" -- nslookup "nginx-alpha.$ALPHA.svc.cluster.local" 2>&1 || true)
    if echo "$DNS_OUT" | grep -qi "address"; then
        PASS "DNS 可解析 (allow-dns 策略生效)"
    else
        FAIL "DNS 解析失败 (应成功). Output: $DNS_OUT"
        ((NET_FAILED++)) || true
    fi
fi

if [ "$NET_FAILED" -eq 0 ]; then
    PASS "实验 1.2 结论: 全部通过"
else
    FAIL "实验 1.2 结论: $NET_FAILED 项未通过"
fi

INFO "═══════════════════════════════════════════════════════"
INFO "隔离实验执行完毕"
INFO "═══════════════════════════════════════════════════════"
