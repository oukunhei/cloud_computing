# Multi-Tenant K3s Lab Platform

A lightweight, secure, multi-tenant Kubernetes lab platform built on K3s with RBAC, ResourceQuota, LimitRange, and NetworkPolicy isolation. Includes a web-based management portal for one-click tenant onboarding.

> **Project Goal**: Design and implement a simplified multi-tenant Kubernetes environment suitable for a teaching lab or student project platform.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     K3s Cluster                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Web Portal (Flask)                      │   │
│  │  Dashboard | Tenant Mgmt | Kubeconfig | Permissions  │   │
│  └─────────────────────────────────────────────────────┘   │
│                           │                                  │
│  ┌────────────────────────┼─────────────────────────────┐   │
│  │                        ▼                              │   │
│  │  ┌──────────┐   ┌──────────┐   ┌──────────┐         │   │
│  │  │ team-α   │   │ team-β   │   │ team-γ   │  ...    │   │
│  │  │ Namespace│   │ Namespace│   │ Namespace│         │   │
│  │  ├──────────┤   ├──────────┤   ├──────────┤         │   │
│  │  │ dev-user │   │ dev-user │   │ dev-user │         │   │
│  │  │ view-user│   │ view-user│   │ view-user│         │   │
│  │  ├──────────┤   ├──────────┤   ├──────────┤         │   │
│  │  │ Role     │   │ Role     │   │ Role     │         │   │
│  │  │ Quota    │   │ Quota    │   │ Quota    │         │   │
│  │  │ NetPol   │   │ NetPol   │   │ NetPol   │         │   │
│  │  └──────────┘   └──────────┘   └──────────┘         │   │
│  │                                                       │   │
│  │  Isolation: RBAC + ResourceQuota + NetworkPolicy     │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Security Layers (Defense in Depth)

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| **Access Control** | RBAC (Role/RoleBinding) | Restrict what each user can do |
| **Resource Guard** | ResourceQuota + LimitRange | Prevent resource exhaustion |
| **Network Wall** | NetworkPolicy | Block cross-namespace traffic |
| **Token Mgmt** | TokenRequest API (v1.24+) | Short-lived, revocable tokens |

---

## Directory Structure

```
.
├── docker-compose.yml              # One-click deploy
├── onboard-team.sh                 # CLI tenant onboarding script
├── rbac/
│   ├── developer-role.yaml         # Developer role permissions
│   ├── viewer-role.yaml            # Viewer role permissions
│   └── rolebinding-template.yaml   # RoleBinding template
├── resources/
│   ├── quota.yaml                  # ResourceQuota per tenant
│   └── limitrange.yaml             # Default container limits
├── networkpolicies/
│   ├── default-deny-ingress.yaml   # Block all inbound by default
│   ├── allow-same-namespace.yaml   # Allow intra-namespace traffic
│   └── allow-dns.yaml              # Allow DNS resolution
├── demo/
│   ├── test-pod.yaml               # Pod without resource specs
│   ├── test-quota-pod.yaml         # Pod exceeding quota
│   └── network-test.yaml           # Network isolation test pods
└── web-portal/                     # Flask Web UI
    ├── Dockerfile
    ├── entrypoint.sh
    ├── requirements.txt
    ├── app.py
    ├── k8s_client.py
    ├── config.py
    ├── static/
    │   ├── css/style.css
    │   └── js/main.js
    └── templates/
        ├── base.html
        ├── dashboard.html
        ├── tenants.html
        ├── resources.html
        ├── kubeconfig.html
        └── permissions.html
```

---

## Prerequisites

- **OS**: Ubuntu 20.04/22.04 or CentOS 7+
- **K3s**: v1.24+ installed and running
- **Docker**: 20.10+ with Docker Compose
- **Hardware**: 2 CPU, 4GB RAM (minimum)

### Verify K3s

```bash
k3s --version
kubectl get nodes
```

---

## Quick Start (Clone & Run)

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd Kubernetes_Lab_Platform
```

### 2. Start the Web Portal

```bash
docker-compose up -d --build
```

### 3. Access the Platform

Open your browser:
```
http://<your-server-ip>:8080
```

### 4. Create Your First Tenant

1. Go to **Tenants** page
2. Click **Create Tenant**
3. Enter a name (e.g. `team-alpha`)
4. Click **Create** — done!

---

## Manual Setup (Without Docker)

### 1. Create a Tenant via CLI

```bash
./onboard-team.sh team-alpha
```

This automatically creates:
- Namespace `team-alpha`
- ServiceAccounts: `dev-user`, `view-user`
- Roles & RoleBindings
- ResourceQuota & LimitRange
- NetworkPolicies
- Kubeconfig files: `team-alpha-dev-kubeconfig`, `team-alpha-view-kubeconfig`

### 2. Run the Web Portal Manually

```bash
cd web-portal
pip install -r requirements.txt
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
python app.py
```

---

## RBAC Role Design

### Three-Tier Permission Model

| Capability | **Admin** | **Developer** | **Viewer** |
|-----------|:---------:|:-------------:|:----------:|
| Read pods, deployments, services | ✅ | ✅ | ✅ |
| Create/Update/Delete workloads | ✅ | ✅ | ❌ |
| Exec into pods / port-forward | ✅ | ✅ | ❌ |
| Read events, HPA | ✅ | ✅ | ✅ |
| Read secrets | ✅ | ❌ | ❌ |
| Manage RBAC (roles/bindings) | ✅ | ❌ | ❌ |
| Manage ResourceQuota | ✅ | ❌ | ❌ |
| Manage LimitRange | ✅ | ❌ | ❌ |
| Manage NetworkPolicy | ✅ | ❌ | ❌ |
| Delete namespace | ✅ | ❌ | ❌ |

> **Design Rationale**: Developers are denied access to `secrets` to prevent credential leakage if their kubeconfig is compromised. They cannot modify platform-level controls (RBAC, quotas, network policies) to prevent privilege escalation or accidental breaking of isolation.

---

## Resource Isolation Design

### ResourceQuota per Tenant

| Resource | Hard Limit |
|----------|-----------|
| requests.cpu | 2 |
| requests.memory | 4Gi |
| limits.cpu | 4 |
| limits.memory | 8Gi |
| pods | 20 |
| services | 10 |
| persistentvolumeclaims | 5 |

### LimitRange Defaults

| Field | Value | Purpose |
|-------|-------|---------|
| default.cpu | 500m | Prevents unlimited CPU usage |
| default.memory | 1Gi | Prevents unlimited memory usage |
| defaultRequest.cpu | 200m | Sensible baseline for scheduling |
| defaultRequest.memory | 256Mi | Sensible baseline for scheduling |
| max.cpu | 2 | Prevents single pod from dominating |
| max.memory | 4Gi | Prevents single pod from dominating |

---

## Network Isolation Design

Every tenant namespace gets three NetworkPolicies:

1. **default-deny-ingress**: Blocks all inbound traffic by default
2. **allow-same-namespace**: Allows pods within the same namespace to communicate
3. **allow-dns**: Allows DNS queries to CoreDNS (required for service discovery)

### Verification

```bash
# In team-alpha namespace
kubectl run test-nginx --image=nginx --namespace team-alpha

# In team-bravo namespace (should FAIL)
kubectl run busybox --image=busybox -it --rm --restart=Never --namespace team-bravo -- wget -O- http://<team-alpha-pod-ip>

# In team-alpha namespace (should SUCCEED)
kubectl run test-client --image=busybox -it --rm --restart=Never --namespace team-alpha -- wget -O- http://test-nginx
```

---

## Web Portal Features

| Feature | Description |
|---------|-------------|
| **Dashboard** | Cluster overview: nodes, namespaces, pods, tenant count |
| **Tenant Management** | One-click create/delete tenants with full isolation stack |
| **Resource Monitoring** | Per-namespace ResourceQuota usage bars, LimitRange rules, Pod list |
| **Kubeconfig Generator** | Web UI to download dev/view kubeconfig files |
| **Permissions Viewer** | Visual matrix showing what each role can/cannot do |

---

## Demo & Verification

### 1. Verify RBAC (Developer)

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig

# Should SUCCEED
kubectl get pods
kubectl create deployment nginx --image=nginx

# Should FAIL (forbidden)
kubectl get secrets
kubectl get resourcequota
kubectl get networkpolicy
kubectl get roles
```

### 2. Verify RBAC (Viewer)

```bash
export KUBECONFIG=./team-alpha-view-kubeconfig

# Should SUCCEED
kubectl get pods,deployments,services

# Should FAIL (forbidden)
kubectl create deployment nginx --image=nginx
kubectl exec -it <pod> -- /bin/sh
```

### 3. Verify ResourceQuota

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig
kubectl apply -f demo/test-quota-pod.yaml
# Expected: Error from server (Forbidden): exceeded quota
```

### 4. Verify LimitRange Defaults

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig
kubectl apply -f demo/test-pod.yaml
kubectl describe pod no-resources-pod | grep -A5 "Requests"
# Should show: cpu=200m, memory=256Mi (defaults)
```

---

## Scalability & Limitations

### Current Design Strengths
- **Lightweight**: Single K3s node can host 10+ teaching teams
- **Fast Onboarding**: 30 seconds per tenant via automation
- **Layered Isolation**: RBAC + Quota + NetworkPolicy provides defense in depth
- **Token Lifecycle**: Uses TokenRequest API (1-year expiry), no static secrets

### Known Limitations

| Limitation | Impact | Future Improvement |
|-----------|--------|-------------------|
| Single-cluster | No node-level fault tolerance | Multi-node K3s or K3s HA |
| No persistent identity | Tokens expire, no SSO | Integrate OIDC/Keycloak |
| L3/L4 network only | Cannot filter by HTTP path | Add Istio/Linkerd service mesh |
| No audit logging | Cannot trace who did what | Enable K8s Audit Policy |
| No storage isolation | Tenants share StorageClass | Add Rook/Ceph per tenant |
| No Pod Security Standards | Privileged containers possible | Enable PodSecurity admission |

### When to Upgrade to Multi-Cluster

If tenants require:
- Privileged containers or hostPath volumes
- Custom admission webhooks
- Different K8s versions per team

Then **namespace-level isolation is insufficient** — migrate to dedicated clusters per tenant with a fleet management layer (Rancher, Fleet, or ArgoCD).

---

## Cleanup

### Remove a Single Tenant

```bash
# Via Web UI: Tenants page → Delete button
# Via CLI:
kubectl delete namespace team-alpha
rm -f team-alpha-*-kubeconfig
```

### Stop the Web Portal

```bash
docker-compose down
```

### Uninstall K3s

```bash
/usr/local/bin/k3s-uninstall.sh
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `kubectl` permission denied | Ensure `~/.kube/config` has correct content, or use `sudo k3s kubectl` |
| NetworkPolicy not working | Verify Calico is running: `kubectl get pods -n calico-system` |
| Web portal shows "Disconnected" | Check K3s status: `sudo systemctl status k3s`. Ensure `/etc/rancher/k3s/k3s.yaml` exists |
| Token creation fails | Ensure K3s API is reachable from container (host network mode should handle this) |
| Cannot delete namespace | Namespace may be stuck in Terminating. Force: `kubectl delete namespace <name> --force` |

---

## License

MIT — Educational use encouraged.
