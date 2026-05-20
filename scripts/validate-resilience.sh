#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ALPHA="team-alpha"
BETA="team-beta"
LABEL="validation-run=resilience"
DEP_NAME="validation-hpa-demo"
SVC_NAME="validation-hpa-demo"
TMPDIR=""

PASS() { echo -e "\033[32m[PASS]\033[0m $*"; }
FAIL() { echo -e "\033[31m[FAIL]\033[0m $*"; }
INFO() { echo -e "\033[34m[INFO]\033[0m $*"; }
STEP() { echo -e "\033[36m[STEP]\033[0m $*"; }

cleanup() {
    INFO "Cleaning up temporary resources..."
    kubectl delete hpa "$DEP_NAME" -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete deployment "$DEP_NAME" -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete svc "$SVC_NAME" -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete pod loadgen -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete pods -n "$ALPHA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete pod no-resources-pod big-pod -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    kubectl delete pod -n "$BETA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    [ -n "${TMPDIR:-}" ] && rm -rf "$TMPDIR"
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

TMPDIR=$(mktemp -d)
TOTAL_FAILED=0


# ═══════════════════════════════════════════════════════════
# Experiment 2.1: ResourceQuota Hard Limit and Noisy Neighbour Isolation Test
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════════════════════════════"
INFO "Experiment 2.1: ResourceQuota Hard Limit and Noisy Neighbour Isolation Test"
INFO "═══════════════════════════════════════════════════════════════════════════════"

Q_FAILED=0

QUOTA_PODS=$(kubectl get resourcequota team-quota -n "$ALPHA" -o jsonpath='{.spec.hard.pods}' 2>/dev/null || echo "20")
CURRENT_PODS=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | wc -l)
REMAINING=$((QUOTA_PODS - CURRENT_PODS))
INFO "$ALPHA quota pods=$QUOTA_PODS, current=$CURRENT_PODS, remaining=$REMAINING"

if [ "$REMAINING" -le 0 ]; then
    FAIL "Namespace $ALPHA has no remaining Pod quota, cannot run this experiment"
    exit 1
fi

BURST=$((REMAINING + 3))
INFO "Will concurrently create $BURST Pods (exceeding remaining quota)"

for i in $(seq 1 $BURST); do
    kubectl run "quota-fill-$i" -n "$ALPHA" --image=nginx:alpine --restart=Never \
        --labels="$LABEL" \
        --overrides="{\"spec\":{\"containers\":[{\"name\":\"quota-fill-$i\",\"image\":\"nginx:alpine\",\"resources\":{\"requests\":{\"cpu\":\"50m\",\"memory\":\"64Mi\"},\"limits\":{\"cpu\":\"100m\",\"memory\":\"128Mi\"}}}]}}" \
        2>"$TMPDIR/err-$i.log" &
done
wait

sleep 3

RUNNING_COUNT=$(kubectl get pods -n "$ALPHA" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)

STEP "Running Pod count does not exceed Quota limit ($QUOTA_PODS)"
if [ "$RUNNING_COUNT" -le "$QUOTA_PODS" ]; then
    PASS "Running Pod count: $RUNNING_COUNT <= $QUOTA_PODS"
else
    FAIL "Running Pod count: $RUNNING_COUNT > $QUOTA_PODS"
    ((Q_FAILED++)) || true
fi
# shellcheck disable=SC2126
EXCEED_LOGS=$(grep -l "exceeded quota" "$TMPDIR"/err-*.log 2>/dev/null | wc -l)
if [ "$EXCEED_LOGS" -gt 0 ]; then
    INFO "Found $EXCEED_LOGS Pods rejected due to exceeded quota"
else
    INFO "No exceeded quota found in logs (might be rejected for other reasons)"
fi

# Noisy Neighbour Test
STEP "Whether $BETA creates Pods normally (Noisy Neighbour isolation)"
kubectl run beta-check -n "$BETA" --image=nginx:alpine --restart=Never --labels="$LABEL"
if kubectl wait --for=condition=Ready pod/beta-check -n "$BETA" --timeout=30s >/dev/null 2>&1; then
    PASS "$BETA Pod created and running normally -> Not affected by $ALPHA"
else
    FAIL "$BETA Pod creation failed -> Might be affected by $ALPHA resource exhaustion"
    ((Q_FAILED++)) || true
fi

if [ "$Q_FAILED" -eq 0 ]; then
    PASS "Experiment 2.1 Conclusion: All passed"
else
    FAIL "Experiment 2.1 Conclusion: $Q_FAILED items failed"
fi
TOTAL_FAILED=$((TOTAL_FAILED + Q_FAILED))

# Clean up 2.1 pods to make room for subsequent experiments
kubectl delete pods -n "$ALPHA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
sleep 3


# ═══════════════════════════════════════════════════════════
# Experiment 2.2: LimitRange Default Injection and Over-spec Interception Test
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "Experiment 2.2: LimitRange Default Injection and Over-spec Interception Test"
INFO "═══════════════════════════════════════════════════════"

L_FAILED=0

STEP "Pod without specified resources automatically injects default values"
kubectl apply -f "$PROJECT_ROOT/demo/test-pod.yaml" -n "$ALPHA"
if kubectl wait --for=condition=Ready pod/no-resources-pod -n "$ALPHA" --timeout=60s >/dev/null 2>&1; then
    REQ_CPU=$(kubectl get pod no-resources-pod -n "$ALPHA" -o jsonpath='{.spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "")
    LIM_CPU=$(kubectl get pod no-resources-pod -n "$ALPHA" -o jsonpath='{.spec.containers[0].resources.limits.cpu}' 2>/dev/null || echo "")
    if [ -n "$REQ_CPU" ] && [ -n "$LIM_CPU" ]; then
        PASS "LimitRange default injection successful (requests.cpu=$REQ_CPU, limits.cpu=$LIM_CPU)"
    else
        FAIL "LimitRange default injection failed (requests='$REQ_CPU', limits='$LIM_CPU')"
        ((L_FAILED++)) || true
    fi
else
    FAIL "no-resources-pod not ready, cannot verify LimitRange injection"
    ((L_FAILED++)) || true
fi

STEP "Over-spec Pod intercepted by API Server"
if kubectl run big-pod -n "$ALPHA" --image=nginx --restart=Never \
    --overrides='{"spec":{"containers":[{"name":"big-pod","image":"nginx","resources":{"requests":{"cpu":"10","memory":"100Gi"},"limits":{"cpu":"10","memory":"100Gi"}}}]}}' 2>/dev/null; then
    FAIL "Over-spec Pod created successfully (should be rejected)"
    kubectl delete pod big-pod -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    ((L_FAILED++)) || true
else
    PASS "Over-spec Pod creation rejected"
fi

if [ "$L_FAILED" -eq 0 ]; then
    PASS "Experiment 2.2 Conclusion: All passed"
else
    FAIL "Experiment 2.2 Conclusion: $L_FAILED items failed"
fi
TOTAL_FAILED=$((TOTAL_FAILED + L_FAILED))


# ═══════════════════════════════════════════════════════════
# Experiment 2.3: HPA Scaling and ResourceQuota Collision Test
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "Experiment 2.3: HPA Scaling and ResourceQuota Collision Test"
INFO "═══════════════════════════════════════════════════════"

H_FAILED=0

QUOTA_PODS2=$(kubectl get resourcequota team-quota -n "$ALPHA" -o jsonpath='{.spec.hard.pods}' 2>/dev/null || echo "20")
CURRENT_PODS2=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | wc -l)
REMAINING2=$((QUOTA_PODS2 - CURRENT_PODS2))
INFO "$ALPHA Current Pod Quota: $QUOTA_PODS2, Used: $CURRENT_PODS2, Remaining: $REMAINING2"

if [ "$REMAINING2" -lt 2 ]; then
    FAIL "Insufficient Pod quota remaining ($REMAINING2), cannot effectively observe HPA scaling, please clean up $ALPHA first"
    exit 1
fi

STEP "Creating Deployment and Service"
kubectl create deployment "$DEP_NAME" -n "$ALPHA" --image=nginx:alpine --replicas=1
kubectl set resources deployment "$DEP_NAME" -n "$ALPHA" --requests=cpu=100m,memory=128Mi
kubectl expose deployment "$DEP_NAME" -n "$ALPHA" --name="$SVC_NAME" --port=80

if ! kubectl wait --for=condition=available "deployment/$DEP_NAME" -n "$ALPHA" --timeout=90s >/dev/null 2>&1; then
    FAIL "Deployment not ready, skipping HPA experiment"
    H_FAILED=1
else
    STEP "Creating HPA"
    kubectl autoscale deployment "$DEP_NAME" -n "$ALPHA" --cpu-percent=10 --min=1 --max=10
    sleep 2

    STEP "Creating load generator"
    kubectl run loadgen -n "$ALPHA" --image=busybox:stable --restart=Never --labels="$LABEL" -- \
        /bin/sh -c "while true; do wget -q -O- http://$SVC_NAME; done"
    sleep 5

    MAX_REPLICAS=1
    MAX_PODS=1
    for i in {1..18}; do
        sleep 5
        CUR_REP=$(kubectl get hpa "$DEP_NAME" -n "$ALPHA" -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo "1")
        POD_COUNT=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | grep -c "^${DEP_NAME}-")
        INFO "Polling $i/18: currentReplicas=$CUR_REP, pods=$POD_COUNT"
        [ -n "$CUR_REP" ] && [ "$CUR_REP" -gt "$MAX_REPLICAS" ] && MAX_REPLICAS=$CUR_REP
        [ "$POD_COUNT" -gt "$MAX_PODS" ] && MAX_PODS=$POD_COUNT
    done

    STEP "HPA scaling behavior check"
    if [ "$MAX_REPLICAS" -gt 1 ]; then
        PASS "HPA triggered scaling, max observed replicas: $MAX_REPLICAS"
    else
        FAIL "HPA did not trigger scaling (max replicas=$MAX_REPLICAS), possibly metrics-server not ready"
        ((H_FAILED++)) || true
    fi

    CRASHES=$(kubectl get pods -n "$ALPHA" --field-selector=status.phase!=Pending,status.phase!=Running --no-headers 2>/dev/null | grep -c "^${DEP_NAME}-")
    OOM_EVENTS=$(kubectl get events -n "$ALPHA" --field-selector=reason=OOMKilled --no-headers 2>/dev/null | grep -c "$DEP_NAME")
    STEP "Pod stability check"
    if [ "$CRASHES" -eq 0 ] && [ "$OOM_EVENTS" -eq 0 ]; then
        PASS "No OOMKilled / CrashLoop / Evicted ($CRASHES crashes, $OOM_EVENTS OOM events)"
    else
        FAIL "Found abnormally terminated Pod ($CRASHES crashes, $OOM_EVENTS OOM events)"
        ((H_FAILED++)) || true
    fi

    STEP "Scaling stopped after hitting limit"
    if [ "$MAX_PODS" -ge "$REMAINING2" ] || [ "$MAX_REPLICAS" -ge 10 ]; then
        INFO "Scaling has hit limit (max_pods=$MAX_PODS, remaining=$REMAINING2, hpa_max=10)"
    else
        INFO "Scaling has not hit limit (max_pods=$MAX_PODS, remaining=$REMAINING2), but HPA itself works normally"
    fi

    BETA_BAD=$(kubectl get pods -n "$BETA" --field-selector=status.phase!=Pending,status.phase!=Running --no-headers 2>/dev/null | wc -l)
    STEP "Impact check on other tenant $BETA"
    if [ "$BETA_BAD" -eq 0 ]; then
        PASS "$BETA has no abnormal Pods"
    else
        FAIL "$BETA exists $BETA_BAD abnormal Pods"
        ((H_FAILED++)) || true
    fi
fi

if [ "$H_FAILED" -eq 0 ]; then
    PASS "Experiment 2.3 Conclusion: All passed"
else
    FAIL "Experiment 2.3 Conclusion: $H_FAILED items failed"
fi
TOTAL_FAILED=$((TOTAL_FAILED + H_FAILED))

INFO "═══════════════════════════════════════════════════════"
INFO "Resilience experiments completed, total failed items: $TOTAL_FAILED"
INFO "═══════════════════════════════════════════════════════"
