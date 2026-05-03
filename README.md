# Multi-Tenant K3s Lab Platform

A lightweight, secure, multi-tenant Kubernetes lab platform built on K3s with RBAC, ResourceQuota, LimitRange, and NetworkPolicy isolation. Includes a web-based management portal for one-click tenant onboarding.

> **Project Goal**: Design and implement a simplified multi-tenant Kubernetes environment suitable for a teaching lab or student project platform.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│                     K3s Cluster                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Web Portal (Flask)                     │   │
│  │  Dashboard | Tenant Mgmt | Kubeconfig | Permissions │   │
│  └─────────────────────────────────────────────────────┘   │
│                           │                                │
│  ┌────────────────────────┼────────────────────────────┐   │
│  │                        ▼                            │   │
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
│  │                                                     │   │
│  │  Isolation: RBAC + ResourceQuota + NetworkPolicy    │   │
│  └─────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

### Security Layers (Defense in Depth)

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| **Access Control** | RBAC (Role/RoleBinding) | Restrict what each user can do |
| **Resource Guard** | ResourceQuota + LimitRange | Prevent resource exhaustion |
| **Network Wall** | NetworkPolicy | Block cross-namespace traffic |
| **Token Mgmt** | TokenRequest API (v1.24+) | Short-lived, revocable tokens |

---

## Engineering Focus: Multi-Tenancy, Isolation, RBAC & Platform Design

> This section highlights how the project addresses the core engineering requirements: **multi-tenancy, isolation, RBAC, and platform-style Kubernetes design**.

### 1. Multi-Tenancy Design

This platform implements **namespace-as-a-tenant** isolation, where each student team receives a fully provisioned, independent namespace:

- **Tenant Onboarding**: A single command (`./onboard-team.sh team-alpha`) or one click in the Web Portal creates an entire tenant stack.
- **Per-Tenant Resources**: Every tenant gets its own `ServiceAccounts`, `Roles`, `RoleBindings`, `ResourceQuota`, `LimitRange`, and `NetworkPolicies`.
- **Shared Cluster, Isolated Workloads**: Multiple teams share a single lightweight K3s node, but their workloads, credentials, and network traffic are fully segregated.

```
┌──────────────────────────────────────────────────┐
│           K3s Cluster (Shared)                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  │
│  │ team-α     │  │ team-β     │  │ team-γ     │  │
│  │ Namespace  │  │ Namespace  │  │ Namespace  │  │
│  │ • Quota    │  │ • Quota    │  │ • Quota    │  │
│  │ • NetPol   │  │ • NetPol   │  │ • NetPol   │  │
│  │ • dev-user │  │ • dev-user │  │ • dev-user │  │
│  │ • view-user│  │ • view-user│  │ • view-user│  │
│  └────────────┘  └────────────┘  └────────────┘  │
└──────────────────────────────────────────────────┘
```

### 2. Isolation Mechanisms

Isolation is enforced through **three complementary layers** (defense in depth):

| Isolation Layer | Implementation | What It Blocks |
|-----------------|----------------|----------------|
| **RBAC Isolation** | `Role` + `RoleBinding` per namespace | Cross-namespace API access; unauthorized actions within a namespace |
| **Resource Isolation** | `ResourceQuota` + `LimitRange` | Resource exhaustion by a single tenant; runaway containers |
| **Network Isolation** | `NetworkPolicy` (deny ingress + allow intra-NS + allow DNS) | Cross-namespace pod-to-pod traffic; unauthorized inbound connections |

**Key Design Decision**: We explicitly use `deny` rules in RBAC (`verbs: ["*"]` on sensitive resources) so that even if a cluster-level binding is misconfigured, tenant-level restrictions still hold.

### 3. RBAC Design

The platform implements a **three-tier RBAC model** using Kubernetes native `Role` and `RoleBinding` resources:

| Role | Read | Write | Exec/PortForward | Explicitly Denied |
|------|------|-------|------------------|-------------------|
| **Admin** | All resources | All resources | Yes | — (cluster-admin scope) |
| **Developer** | Pods, Deployments, Services, ConfigMaps, Ingresses, Events, HPA | Pods, Deployments, Services, ConfigMaps, Ingresses, Jobs | Yes | `secrets`, `roles`, `rolebindings`, `resourcequotas`, `limitranges`, `networkpolicies` |
| **Viewer** | Pods, Deployments, Services, ConfigMaps, Ingresses, Events, HPA | — | No | `pods/exec`, `pods/portforward`, `pods/attach`, `secrets`, `roles`, `rolebindings` |

**Why this matters**: Developers cannot read `secrets` (mitigates credential leakage if kubeconfig is lost) and cannot modify platform-level controls (prevents privilege escalation). Viewers are strictly read-only and cannot exec into pods.

### 4. Platform-Style Kubernetes Design

Rather than a collection of manual `kubectl` commands, this project is designed as a **mini PaaS (Platform as a Service)**:

- **Automation Layer**: `onboard-team.sh` encapsulates all provisioning logic (namespace, RBAC, quota, netpol, kubeconfig generation) into an idempotent-like workflow.
- **Management Portal**: A Flask-based web UI provides **Dashboard** (cluster telemetry), **Tenant Management** (CRUD), **Resource Monitor** (quota usage, pod list), **Kubeconfig Generator** (TokenRequest API), and **Permissions Viewer** (RBAC matrix).
- **Self-Service Onboarding**: A student team can be onboarded in ~30 seconds without the platform administrator running individual `kubectl` commands.
- **Token Lifecycle Management**: Uses the `TokenRequest` API (`kubectl create token`) instead of static ServiceAccount secrets, generating time-bound (1-year), revocable tokens per tenant role.

---

## Directory Structure (For Evaluation)

The repository is organized so that each directory directly maps to a specific project requirement, making it easy for evaluators to locate the relevant artifacts.

```
.
├── docker-compose.yml              # [Infra] One-click portal deployment
├── onboard-team.sh                 # [Automation] CLI tenant onboarding script
│                                   #   → Creates NS, SA, RBAC, Quota, NetPol, kubeconfig
├── rbac/                           # [Basic/Advanced] Role & RoleBinding definitions
│   ├── developer-role.yaml         #   → Developer permissions + explicit deny rules
│   ├── viewer-role.yaml            #   → Viewer read-only permissions + deny rules
│   └── rolebinding-template.yaml   #   → Binds Roles to per-tenant ServiceAccounts
├── resources/                      # [Standard] Resource controls per tenant
│   ├── quota.yaml                  #   → ResourceQuota (CPU, Mem, Pods, PVCs, Services)
│   └── limitrange.yaml             #   → LimitRange (defaults, min, max per container)
├── networkpolicies/                # [Advanced] Network isolation policies
│   ├── default-deny-ingress.yaml   #   → Deny all inbound by default
│   ├── allow-same-namespace.yaml   #   → Allow intra-namespace traffic
│   └── allow-dns.yaml              #   → Allow CoreDNS egress (UDP:53)
├── demo/                           # [Verification] Manual test manifests
│   ├── test-pod.yaml               #   → Verifies LimitRange default injection
│   ├── test-quota-pod.yaml         #   → Verifies ResourceQuota enforcement
│   └── network-test.yaml           #   → Verifies NetworkPolicy isolation
└── web-portal/                     # [Advanced] Flask-based management portal
    ├── Dockerfile                  #   → Container build instructions
    ├── entrypoint.sh               #   → Bootstraps kubectl & waits for K8s API
    ├── requirements.txt            #   → Python dependencies (Flask, kubernetes, PyYAML)
    ├── app.py                      #   → Flask routes (pages + REST API)
    ├── k8s_client.py               #   → K8s Python client wrapper (cluster ops, kubeconfig gen)
    ├── config.py                   #   → System namespaces, role matrix, constants
    ├── static/                     #   → Frontend assets (CSS, JS)
    └── templates/                  #   → Jinja2 HTML pages (Dashboard, Tenants, etc.)
```

**Quick Requirement Mapping for Reviewers**

| Project Requirement | Where to Look |
|---------------------|---------------|
| 3 Roles (admin/dev/viewer) | `rbac/developer-role.yaml`, `rbac/viewer-role.yaml`, `config.py` |
| Namespace + Role/RoleBinding | `onboard-team.sh` (lines 42–61), `rbac/` |
| Different access permissions | `rbac/*.yaml`, `demo/`, README "Demo & Verification" section |
| ResourceQuota / LimitRange | `resources/quota.yaml`, `resources/limitrange.yaml` |
| Per-team namespace design | `onboard-team.sh`, `web-portal/k8s_client.py` |
| Onboarding / usage guide | README "Quick Start" & "Manual Setup", `onboard-team.sh` |
| Security & isolation explanation | README "Engineering Focus" & "Security Layers" sections |
| Prevent misuse / accidental damage | `rbac/*.yaml` (explicit deny rules), `resources/quota.yaml` |
| NetworkPolicy / fine-grained permissions | `networkpolicies/*.yaml`, `rbac/developer-role.yaml` |
| Automation / lightweight portal | `onboard-team.sh`, `web-portal/` |
| Scalability & limitations discussion | README "Scalability & Limitations" section |

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
git clone https://github.com/oukunhei/Kubernetes_Lab_Platform.git
cd Kubernetes_Lab_Platform
```

### 2. Start the Web Portal

```bash
docker-compose up -d --build
```

### 3. Access the Platform

Open your browser:
```
http://8.160.177.198:8080
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
