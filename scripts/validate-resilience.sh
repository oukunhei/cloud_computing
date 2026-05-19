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
    INFO "清理临时资源..."
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
        INFO "创建租户 $ns ..."
        bash "$PROJECT_ROOT/onboard-team.sh" "$ns"
    fi
done

TMPDIR=$(mktemp -d)
TOTAL_FAILED=0


# ═══════════════════════════════════════════════════════════
# 实验 2.1: ResourceQuota 硬顶与嘈杂邻居隔离测试
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "实验 2.1: ResourceQuota 硬顶与嘈杂邻居隔离测试"
INFO "═══════════════════════════════════════════════════════"

Q_FAILED=0

QUOTA_PODS=$(kubectl get resourcequota team-quota -n "$ALPHA" -o jsonpath='{.spec.hard.pods}' 2>/dev/null || echo "20")
CURRENT_PODS=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | wc -l)
REMAINING=$((QUOTA_PODS - CURRENT_PODS))
INFO "$ALPHA quota pods=$QUOTA_PODS, current=$CURRENT_PODS, remaining=$REMAINING"

if [ "$REMAINING" -le 0 ]; then
    FAIL "Namespace $ALPHA 已无剩余 Pod 配额，无法执行本实验"
    exit 1
fi

BURST=$((REMAINING + 3))
INFO "将并发创建 $BURST 个 Pod（超出剩余额度）"

for i in $(seq 1 $BURST); do
    kubectl run "quota-fill-$i" -n "$ALPHA" --image=nginx:alpine --restart=Never \
        --labels="$LABEL" \
        --overrides="{\"spec\":{\"containers\":[{\"name\":\"quota-fill-$i\",\"image\":\"nginx:alpine\",\"resources\":{\"requests\":{\"cpu\":\"50m\",\"memory\":\"64Mi\"},\"limits\":{\"cpu\":\"100m\",\"memory\":\"128Mi\"}}}]}}" \
        2>"$TMPDIR/err-$i.log" &
done
wait

sleep 3

RUNNING_COUNT=$(kubectl get pods -n "$ALPHA" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)

STEP "Running Pod 数量不超过 Quota 上限 ($QUOTA_PODS)"
if [ "$RUNNING_COUNT" -le "$QUOTA_PODS" ]; then
    PASS "Running Pod 数量: $RUNNING_COUNT <= $QUOTA_PODS"
else
    FAIL "Running Pod 数量: $RUNNING_COUNT > $QUOTA_PODS"
    ((Q_FAILED++)) || true
fi
# shellcheck disable=SC2126
EXCEED_LOGS=$(grep -l "exceeded quota" "$TMPDIR"/err-*.log 2>/dev/null | wc -l)
if [ "$EXCEED_LOGS" -gt 0 ]; then
    INFO "发现 $EXCEED_LOGS 个 Pod 因 exceeded quota 被拒绝"
else
    INFO "未在日志中发现 exceeded quota（可能因其他原因被拒绝）"
fi

# 嘈杂邻居测试
STEP "$BETA 是否正常创建 Pod（嘈杂邻居隔离）"
kubectl run beta-check -n "$BETA" --image=nginx:alpine --restart=Never --labels="$LABEL"
if kubectl wait --for=condition=Ready pod/beta-check -n "$BETA" --timeout=30s >/dev/null 2>&1; then
    PASS "$BETA Pod 正常创建并运行 → 未受 $ALPHA 影响"
else
    FAIL "$BETA Pod 创建失败 → 可能受 $ALPHA 资源耗尽影响"
    ((Q_FAILED++)) || true
fi

if [ "$Q_FAILED" -eq 0 ]; then
    PASS "实验 2.1 结论: 全部通过"
else
    FAIL "实验 2.1 结论: $Q_FAILED 项未通过"
fi
TOTAL_FAILED=$((TOTAL_FAILED + Q_FAILED))

# 清理 2.1 的 pod，给后续实验腾空间
kubectl delete pods -n "$ALPHA" -l "$LABEL" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
sleep 3


# ═══════════════════════════════════════════════════════════
# 实验 2.2: LimitRange 默认注入与超规格拦截测试
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "实验 2.2: LimitRange 默认注入与超规格拦截测试"
INFO "═══════════════════════════════════════════════════════"

L_FAILED=0

STEP "未指定 resources 的 Pod 自动注入默认值"
kubectl apply -f "$PROJECT_ROOT/demo/test-pod.yaml" -n "$ALPHA"
if kubectl wait --for=condition=Ready pod/no-resources-pod -n "$ALPHA" --timeout=60s >/dev/null 2>&1; then
    REQ_CPU=$(kubectl get pod no-resources-pod -n "$ALPHA" -o jsonpath='{.spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "")
    LIM_CPU=$(kubectl get pod no-resources-pod -n "$ALPHA" -o jsonpath='{.spec.containers[0].resources.limits.cpu}' 2>/dev/null || echo "")
    if [ -n "$REQ_CPU" ] && [ -n "$LIM_CPU" ]; then
        PASS "LimitRange 默认注入生效 (requests.cpu=$REQ_CPU, limits.cpu=$LIM_CPU)"
    else
        FAIL "LimitRange 默认注入未生效 (requests='$REQ_CPU', limits='$LIM_CPU')"
        ((L_FAILED++)) || true
    fi
else
    FAIL "no-resources-pod 未就绪，无法验证 LimitRange 注入"
    ((L_FAILED++)) || true
fi

STEP "超规格 Pod 被 API Server 拦截"
if kubectl run big-pod -n "$ALPHA" --image=nginx --restart=Never \
    --overrides='{"spec":{"containers":[{"name":"big-pod","image":"nginx","resources":{"requests":{"cpu":"10","memory":"100Gi"},"limits":{"cpu":"10","memory":"100Gi"}}}]}}' 2>/dev/null; then
    FAIL "超规格 Pod 创建成功 (应被拒绝)"
    kubectl delete pod big-pod -n "$ALPHA" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    ((L_FAILED++)) || true
else
    PASS "超规格 Pod 创建被拒绝"
fi

if [ "$L_FAILED" -eq 0 ]; then
    PASS "实验 2.2 结论: 全部通过"
else
    FAIL "实验 2.2 结论: $L_FAILED 项未通过"
fi
TOTAL_FAILED=$((TOTAL_FAILED + L_FAILED))


# ═══════════════════════════════════════════════════════════
# 实验 2.3: HPA 扩容与 ResourceQuota 碰撞测试
# ═══════════════════════════════════════════════════════════
INFO "═══════════════════════════════════════════════════════"
INFO "实验 2.3: HPA 扩容与 ResourceQuota 碰撞测试"
INFO "═══════════════════════════════════════════════════════"

H_FAILED=0

QUOTA_PODS2=$(kubectl get resourcequota team-quota -n "$ALPHA" -o jsonpath='{.spec.hard.pods}' 2>/dev/null || echo "20")
CURRENT_PODS2=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | wc -l)
REMAINING2=$((QUOTA_PODS2 - CURRENT_PODS2))
INFO "$ALPHA 当前 Pod Quota: $QUOTA_PODS2, 已用: $CURRENT_PODS2, 剩余: $REMAINING2"

if [ "$REMAINING2" -lt 2 ]; then
    FAIL "剩余 Pod 额度不足 ($REMAINING2)，无法有效观察 HPA 扩容，请先清理 $ALPHA"
    exit 1
fi

STEP "创建 Deployment 和 Service"
kubectl create deployment "$DEP_NAME" -n "$ALPHA" --image=nginx:alpine --replicas=1
kubectl set resources deployment "$DEP_NAME" -n "$ALPHA" --requests=cpu=100m,memory=128Mi
kubectl expose deployment "$DEP_NAME" -n "$ALPHA" --name="$SVC_NAME" --port=80

if ! kubectl wait --for=condition=available "deployment/$DEP_NAME" -n "$ALPHA" --timeout=90s >/dev/null 2>&1; then
    FAIL "Deployment 未就绪，跳过 HPA 实验"
    H_FAILED=1
else
    STEP "创建 HPA"
    kubectl autoscale deployment "$DEP_NAME" -n "$ALPHA" --cpu-percent=10 --min=1 --max=10
    sleep 2

    STEP "创建负载生成器"
    kubectl run loadgen -n "$ALPHA" --image=busybox:stable --restart=Never --labels="$LABEL" -- \
        /bin/sh -c "while true; do wget -q -O- http://$SVC_NAME; done"
    sleep 5

    MAX_REPLICAS=1
    MAX_PODS=1
    for i in {1..18}; do
        sleep 5
        CUR_REP=$(kubectl get hpa "$DEP_NAME" -n "$ALPHA" -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo "1")
        POD_COUNT=$(kubectl get pods -n "$ALPHA" --no-headers 2>/dev/null | grep -c "^${DEP_NAME}-")
        INFO "轮询 $i/18: currentReplicas=$CUR_REP, pods=$POD_COUNT"
        [ -n "$CUR_REP" ] && [ "$CUR_REP" -gt "$MAX_REPLICAS" ] && MAX_REPLICAS=$CUR_REP
        [ "$POD_COUNT" -gt "$MAX_PODS" ] && MAX_PODS=$POD_COUNT
    done

    STEP "HPA 扩容行为检查"
    if [ "$MAX_REPLICAS" -gt 1 ]; then
        PASS "HPA 触发了扩容，最大 observed replicas: $MAX_REPLICAS"
    else
        FAIL "HPA 未触发扩容 (max replicas=$MAX_REPLICAS)，可能 metrics-server 未就绪"
        ((H_FAILED++)) || true
    fi

    CRASHES=$(kubectl get pods -n "$ALPHA" --field-selector=status.phase!=Pending,status.phase!=Running --no-headers 2>/dev/null | grep -c "^${DEP_NAME}-")
    OOM_EVENTS=$(kubectl get events -n "$ALPHA" --field-selector=reason=OOMKilled --no-headers 2>/dev/null | grep -c "$DEP_NAME")
    STEP "Pod 稳定性检查"
    if [ "$CRASHES" -eq 0 ] && [ "$OOM_EVENTS" -eq 0 ]; then
        PASS "无 OOMKilled / CrashLoop / Evicted ($CRASHES crashes, $OOM_EVENTS OOM events)"
    else
        FAIL "发现异常终止 Pod ($CRASHES crashes, $OOM_EVENTS OOM events)"
        ((H_FAILED++)) || true
    fi

    STEP "扩容触及上限后停止"
    if [ "$MAX_PODS" -ge "$REMAINING2" ] || [ "$MAX_REPLICAS" -ge 10 ]; then
        INFO "扩容已触及上限 (max_pods=$MAX_PODS, remaining=$REMAINING2, hpa_max=10)"
    else
        INFO "扩容未触及上限 (max_pods=$MAX_PODS, remaining=$REMAINING2)，但 HPA 本身工作正常"
    fi

    BETA_BAD=$(kubectl get pods -n "$BETA" --field-selector=status.phase!=Pending,status.phase!=Running --no-headers 2>/dev/null | wc -l)
    STEP "其他租户 $BETA 影响检查"
    if [ "$BETA_BAD" -eq 0 ]; then
        PASS "$BETA 无异常 Pod"
    else
        FAIL "$BETA 存在 $BETA_BAD 个异常 Pod"
        ((H_FAILED++)) || true
    fi
fi

if [ "$H_FAILED" -eq 0 ]; then
    PASS "实验 2.3 结论: 全部通过"
else
    FAIL "实验 2.3 结论: $H_FAILED 项未通过"
fi
TOTAL_FAILED=$((TOTAL_FAILED + H_FAILED))

INFO "═══════════════════════════════════════════════════════"
INFO "韧性实验执行完毕，总计失败项: $TOTAL_FAILED"
INFO "═══════════════════════════════════════════════════════"
