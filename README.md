# 多租户 K3s 实验平台搭建与验证指南

## 1. 环境准备（在远程服务器上）

### 1.1 服务器要求
- **操作系统**：Ubuntu 20.04/22.04（推荐）或 CentOS 7+
- **硬件**：2 CPU，4GB 内存（最低），10GB 可用磁盘
- **网络**：开放 6443（K8s API）、22（SSH）、80/443（可选 Ingress）

### 1.2 安装 K3s
```bash
# 使用官方脚本安装 K3s（自动配置 kubectl）
curl -sfL https://get.k3s.io | sh -

# 等待节点就绪
sudo k3s kubectl get node

# 设置 kubectl 别名（方便非 root 用户）
mkdir -p ~/.kube
sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config
chmod 600 ~/.kube/config
export KUBECONFIG=~/.kube/config
echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc

# 验证集群
kubectl get nodes
kubectl get pods -A
```

**调试提示**：
- 若 `kubectl` 报权限错误，检查 `~/.kube/config` 内容是否正确，或使用 `sudo k3s kubectl`。
- K3s 默认 CNI 为 Flannel，但它**不支持 NetworkPolicy**。我们需要安装支持 NetworkPolicy 的 CNI（Calico）。下一步操作。

### 1.3 安装 Calico 以支持 NetworkPolicy
```bash
# 卸载默认 Flannel（K3s 默认使用 Flannel）
kubectl delete -f /var/lib/rancher/k3s/server/manifests/flannel.yaml
# 等待 Flannel Pod 删除干净

# 安装 Calico（使用官方 Operator 方式）
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.26/manifests/tigera-operator.yaml
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.26/manifests/custom-resources.yaml

# 监控 Calico Pod 启动（约2分钟）
kubectl get pods -n calico-system --watch
```

**调试**：若 Calico Pod 启动失败，检查节点是否开放 179 (BGP) 端口，或重启 K3s：`sudo systemctl restart k3s`。

### 1.4 验证 NetworkPolicy 能力
```bash
# 检查 Calico 是否就绪
kubectl get pods -n calico-system
kubectl get networkpolicies -A
```

---

## 2. 多租户架构设计

- **租户模型**：每个团队一个命名空间（例如 `team-alpha`, `team-bravo`）
- **角色**：
  - `lab-admin`（集群管理员）—— 绑定 `cluster-admin` ClusterRole
  - `developer`（每个命名空间内）—— 可读写工作负载，不能修改 RBAC/配额/网络策略
  - `viewer`（每个命名空间内）—— 只读所有资源
- **隔离机制**：
  - RBAC（Role/RoleBinding）
  - ResourceQuota + LimitRange
  - NetworkPolicy（禁止跨命名空间入站）

---

## 3. 创建租户及所有组件的自动化脚本

我们将编写一个脚本 `onboard-team.sh`，输入团队名称（如 `team-alpha`），自动完成以下工作：

1. 创建命名空间
2. 创建 ServiceAccount：`dev-user`, `view-user`
3. 创建 Role 和 RoleBinding
4. 创建 ResourceQuota 和 LimitRange
5. 创建 NetworkPolicy
6. 生成 kubeconfig 文件供用户使用

### 3.1 准备角色模板文件

创建 `rbac/developer-role.yaml`：
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer
rules:
- apiGroups: ["", "apps", "networking.k8s.io", "extensions"]
  resources: ["pods", "pods/log", "pods/exec", "services", "deployments", "configmaps", "ingresses", "secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: [""]
  resources: ["events"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# 禁止操作 roles, rolebindings, resourcequotas, limitranges, networkpolicies
```

创建 `rbac/viewer-role.yaml`：
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: viewer
rules:
- apiGroups: ["", "apps", "networking.k8s.io", "extensions"]
  resources: ["pods", "pods/log", "services", "deployments", "configmaps", "ingresses", "events"]
  verbs: ["get", "list", "watch"]
# 禁止写操作，禁止 exec，禁止查看 secrets
```

创建 `rbac/rolebinding-template.yaml`：
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ROLE}}-binding
  namespace: {{NAMESPACE}}
subjects:
- kind: ServiceAccount
  name: {{USER}}
  namespace: {{NAMESPACE}}
roleRef:
  kind: Role
  name: {{ROLE}}
  apiGroup: rbac.authorization.k8s.io
```

### 3.2 准备 ResourceQuota 和 LimitRange 模板

创建 `resources/quota.yaml`：
```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-quota
spec:
  hard:
    requests.cpu: "2"
    requests.memory: "4Gi"
    limits.cpu: "4"
    limits.memory: "8Gi"
    persistentvolumeclaims: "5"
    pods: "20"
    services: "10"
    configmaps: "20"
```

创建 `resources/limitrange.yaml`：
```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: mem-cpu-limit
spec:
  limits:
  - default:
      cpu: "500m"
      memory: "1Gi"
    defaultRequest:
      cpu: "200m"
      memory: "256Mi"
    type: Container
```

### 3.3 准备 NetworkPolicy 模板

创建 `networkpolicies/default-deny-ingress.yaml`：
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  # 无 ingress 规则 → 拒绝所有入站
```

创建 `networkpolicies/allow-same-namespace.yaml`：
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
spec:
  podSelector: {}
  ingress:
  - from:
    - podSelector: {}
  policyTypes:
  - Ingress
```

### 3.4 完整自动化脚本 `onboard-team.sh`

```bash
#!/bin/bash
# Usage: ./onboard-team.sh <team-namespace>

set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <team-namespace>"
  exit 1
fi

NAMESPACE=$1

echo "🚀 Creating tenant: $NAMESPACE"

# 1. 创建命名空间
kubectl create namespace $NAMESPACE

# 2. 创建 ServiceAccounts
kubectl create sa dev-user -n $NAMESPACE
kubectl create sa view-user -n $NAMESPACE

# 3. 应用 Role（需要先替换 namespace 字段）
sed "s/namespace: .*/namespace: $NAMESPACE/g" rbac/developer-role.yaml | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" rbac/viewer-role.yaml | kubectl apply -f -

# 4. 创建 RoleBindings
# developer binding
sed "s/{{ROLE}}/developer/g; s/{{USER}}/dev-user/g; s/{{NAMESPACE}}/$NAMESPACE/g" rbac/rolebinding-template.yaml | kubectl apply -f -
# viewer binding
sed "s/{{ROLE}}/viewer/g; s/{{USER}}/view-user/g; s/{{NAMESPACE}}/$NAMESPACE/g" rbac/rolebinding-template.yaml | kubectl apply -f -

# 5. 应用 ResourceQuota 和 LimitRange
sed "s/namespace: .*/namespace: $NAMESPACE/g" resources/quota.yaml | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" resources/limitrange.yaml | kubectl apply -f -

# 6. 应用 NetworkPolicy
sed "s/namespace: .*/namespace: $NAMESPACE/g" networkpolicies/default-deny-ingress.yaml | kubectl apply -f -
sed "s/namespace: .*/namespace: $NAMESPACE/g" networkpolicies/allow-same-namespace.yaml | kubectl apply -f -

# 7. 为 dev-user 和 view-user 生成 kubeconfig
# 获取集群信息
CLUSTER_NAME=$(kubectl config current-context)
SERVER=$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.server}")
CA_DATA=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.certificate-authority-data}")

# 生成 dev-user kubeconfig
DEV_SECRET=$(kubectl get sa dev-user -n $NAMESPACE -o jsonpath="{.secrets[0].name}")
DEV_TOKEN=$(kubectl get secret $DEV_SECRET -n $NAMESPACE -o jsonpath="{.data.token}" | base64 -d)
kubectl config set-cluster $CLUSTER_NAME --server=$SERVER --certificate-authority=<(echo $CA_DATA | base64 -d) --embed-certs=true --kubeconfig=${NAMESPACE}-dev-kubeconfig
kubectl config set-credentials dev-user --token=$DEV_TOKEN --kubeconfig=${NAMESPACE}-dev-kubeconfig
kubectl config set-context dev-context --cluster=$CLUSTER_NAME --user=dev-user --namespace=$NAMESPACE --kubeconfig=${NAMESPACE}-dev-kubeconfig
kubectl config use-context dev-context --kubeconfig=${NAMESPACE}-dev-kubeconfig

# 生成 view-user kubeconfig
VIEW_SECRET=$(kubectl get sa view-user -n $NAMESPACE -o jsonpath="{.secrets[0].name}")
VIEW_TOKEN=$(kubectl get secret $VIEW_SECRET -n $NAMESPACE -o jsonpath="{.data.token}" | base64 -d)
kubectl config set-cluster $CLUSTER_NAME --server=$SERVER --certificate-authority=<(echo $CA_DATA | base64 -d) --embed-certs=true --kubeconfig=${NAMESPACE}-view-kubeconfig
kubectl config set-credentials view-user --token=$VIEW_TOKEN --kubeconfig=${NAMESPACE}-view-kubeconfig
kubectl config set-context view-context --cluster=$CLUSTER_NAME --user=view-user --namespace=$NAMESPACE --kubeconfig=${NAMESPACE}-view-kubeconfig
kubectl config use-context view-context --kubeconfig=${NAMESPACE}-view-kubeconfig

echo "✅ Tenant $NAMESPACE created successfully!"
echo "📁 Kubeconfig files:"
echo "   Developer: ${NAMESPACE}-dev-kubeconfig"
echo "   Viewer:    ${NAMESPACE}-view-kubeconfig"
echo "🔐 To use developer access: export KUBECONFIG=./${NAMESPACE}-dev-kubeconfig"
```

### 3.5 创建集群管理员 ServiceAccount（可选）

```bash
kubectl create sa lab-admin -n kube-system
kubectl create clusterrolebinding lab-admin-binding --clusterrole=cluster-admin --serviceaccount=kube-system:lab-admin
# 获取 admin token
ADMIN_SECRET=$(kubectl get sa lab-admin -n kube-system -o jsonpath="{.secrets[0].name}")
kubectl get secret $ADMIN_SECRET -n kube-system -o jsonpath="{.data.token}" | base64 -d
```

使用该 token 可配置管理员 kubeconfig。

---

## 4. 验证与实验步骤

### 4.1 验证 RBAC 权限

**以 developer 身份操作**：
```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig

# 应该允许
kubectl get pods
kubectl create deployment nginx --image=nginx
kubectl get deployments
kubectl logs deployment/nginx
kubectl delete deployment nginx

# 应该被拒绝（无权限）
kubectl get role,rolebinding -n team-alpha
kubectl get resourcequota
kubectl get networkpolicy
```

**以 viewer 身份操作**：
```bash
export KUBECONFIG=./team-alpha-view-kubeconfig

# 允许
kubectl get pods,deployments,services

# 拒绝（写操作）
kubectl create deployment nginx --image=nginx  # error: forbidden
kubectl exec -it <pod> -- /bin/sh               # error
```

### 4.2 验证资源配额

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig

# 尝试创建超出配额的 Pod（例如申请 5Gi 内存）
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: big-pod
spec:
  containers:
  - name: app
    image: nginx
    resources:
      requests:
        memory: "5Gi"
EOF
# 期望输出: Error from server (Forbidden): error when creating ... exceeded quota
```

### 4.3 验证 LimitRange 默认值

创建一个不指定资源的 Pod：
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: no-resources-pod
spec:
  containers:
  - name: app
    image: nginx
```
```bash
kubectl apply -f no-resources-pod.yaml
kubectl describe pod no-resources-pod | grep -A5 Requests
# 应该显示默认的 200m CPU 和 256Mi 内存
```

### 4.4 验证 NetworkPolicy 隔离

在 `team-alpha` 和 `team-bravo` 各部署一个 nginx Pod：
```bash
# 创建第二个团队
./onboard-team.sh team-bravo

# 在 team-alpha 中启动 nginx
kubectl run test-nginx --image=nginx --namespace team-alpha

# 在 team-bravo 中启动一个 busybox 并尝试访问 team-alpha 的 nginx IP
kubectl run busybox --image=busybox -it --rm --restart=Never --namespace team-bravo -- sh
# 在容器内：
wget -O- http://<team-alpha-nginx-pod-ip>   # 应该超时或拒绝
# 退出容器
exit
```

**验证同命名空间内通信正常**：
```bash
# 在 team-alpha 中启动另一个临时容器
kubectl run test-client --image=busybox -it --rm --restart=Never --namespace team-alpha -- sh
# 在容器内：
wget -O- http://test-nginx.team-alpha   # 应该成功返回 nginx 欢迎页
```

### 4.5 模拟“误操作”防护

尝试使用 developer 账户修改 quota：
```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig
kubectl edit resourcequota team-quota -n team-alpha
# 应该得到: Error from server (Forbidden): resourcequotas is forbidden
```

尝试删除命名空间（需要集群权限，developer 无）：
```bash
kubectl delete namespace team-alpha
# 禁止
```

---

## 5. 项目文件结构

```
multi-tenant-lab/
├── onboard-team.sh
├── rbac/
│   ├── developer-role.yaml
│   ├── viewer-role.yaml
│   └── rolebinding-template.yaml
├── resources/
│   ├── quota.yaml
│   └── limitrange.yaml
├── networkpolicies/
│   ├── default-deny-ingress.yaml
│   └── allow-same-namespace.yaml
├── demo/
│   ├── test-pod.yaml
│   └── test-quota-pod.yaml
└── README.md
```

---

## 6. 清理与卸载

```bash
# 删除整个租户
kubectl delete namespace team-alpha
# 删除所有生成的 kubeconfig
rm -f *-kubeconfig
```

卸载 K3s：
```bash
/usr/local/bin/k3s-uninstall.sh
```

---

## 7. 常见问题及调试

| 问题 | 解决办法 |
|------|----------|
| `kubectl` 连接被拒绝 | 检查 K3s 服务状态：`sudo systemctl status k3s`；检查防火墙开放 6443 端口 |
| NetworkPolicy 不生效 | 确认 Calico 运行：`kubectl get pods -n calico-system`；检查节点 CNI 配置：`kubectl get nodes -o wide` |
| ServiceAccount token 为空 | K8s 1.24+ 默认不自动创建 secret，需手动创建：<br> `kubectl create token dev-user -n team-alpha`（短期）或创建 secret 对象 |
| 生成 kubeconfig 时 token 错误 | 使用 `kubectl create token` 动态获取（推荐）而非 secret 中的静态 token |
| 资源配额冲突 | 检查现有使用量：`kubectl describe resourcequota -n team-alpha` |

---

## 8. 扩展建议（高级目标）

- **轻量级 Web 门户**：使用 Flask + Kubernetes Python client 提供自助命名空间申请。
- **Pod 安全标准**：添加 `PodSecurityPolicy` 或使用 `PodSecurity` admission 限制特权容器。
- **审计日志**：启用 K3s 审计日志记录所有 API 请求。
- **动态存储隔离**：使用 CSI 驱动（如 Rook/Ceph）为每个租户创建独立的 StorageClass。

---
