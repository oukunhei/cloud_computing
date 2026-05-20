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
│  │  │ Role     │   │ Role     │   │ Role     │         │   │
│  │  │ Binding  │   │ Binding  │   │ Binding  │         │   │
│  │  ├──────────┤   ├──────────┤   ├──────────┤         │   │
│  │  │ Quota    │   │ Quota    │   │ Quota    │         │   │
│  │  │ NetPol   │   │ NetPol   │   │ NetPol   │         │   │
│  │  └──────────┘   └──────────┘   └──────────┘         │   │
│  │                                                     │   │
│  │  User SAs live in lab-platform-users namespace      │   │
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
| **Pod Guardrails** | Pod Security Admission labels | Block privileged/host-level pods |
| **Token Mgmt** | TokenRequest API (v1.24+) | Short-lived, revocable tokens |

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **OS** | Ubuntu 20.04/22.04/24.04 (or CentOS/RHEL-compatible Linux) |
| **Docker** | Docker Engine + Docker Compose (v1 or plugin) |
| **K3s** | v1.24+ with default flannel (`--flannel-backend=vxlan`) |
| **Hardware** | 2 CPU, 4 GB RAM minimum |

Quick install on Ubuntu:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --flannel-backend=vxlan" sh -
sudo chmod 644 /etc/rancher/k3s/k3s.yaml
```

Verify:

```bash
k3s --version
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes
./scripts/check-prereqs.sh
```

> If a node stays `NotReady` with `NetworkPluginNotReady`, check `cat /etc/rancher/k3s/config.yaml` for `--flannel-backend none` and run `sudo ./scripts/fix-k3s-flannel.sh` if needed.

---

## Quick Start (Clone & Run)

### 1. Clone the Repository

```bash
git clone https://github.com/SHENZhouan/cloud_computing.git
cd cloud_computing
```

### 2. Start the Web Portal

One-command startup for the whole lab platform:

```bash
./scripts/start-lab-platform.sh
```

To also create a demo tenant after the portal starts:

```bash
CREATE_DEMO_TENANT=true ./scripts/start-lab-platform.sh
```

Manual startup:

```bash
cp .env.example .env
./scripts/check-prereqs.sh
docker-compose up -d --build
```

If your machine only has the newer Compose plugin, use:

```bash
docker compose up -d --build
```

### 3. Access the Platform

Open your browser:
```
http://<server-ip>:8080
```

For local testing on the same machine:

```bash
http://localhost:8080
```

The first page is the **Role Login** screen. Choose one simulated role:

- `admin`: can create/delete tenants and generate all role kubeconfigs.
- `developer`: can view tenants/resources and represents workload-management access inside a tenant.
- `viewer`: read-only web view for demonstration and audit-style inspection.

### 4. Create Your First Tenant

1. Log in as `admin`
2. Go to **Tenants** page
3. Click **Create Tenant**
4. Enter a name (e.g. `team-alpha`)
5. Click **Create** — done!


## Manual Setup (Without Docker)

If you prefer to run without Docker, or need to debug the platform step by step:

### 1. Create a Tenant via CLI

```bash
./onboard-team.sh team-alpha
```

This automatically creates:
- Namespace `team-alpha`
- Isolated ServiceAccounts in `lab-platform-users`: `team-alpha-admin`, `team-alpha-dev`, `team-alpha-view`
- Roles & RoleBindings
- ResourceQuota & LimitRange
- NetworkPolicies
- Kubeconfig files: `team-alpha-admin-kubeconfig`, `team-alpha-dev-kubeconfig`, `team-alpha-view-kubeconfig`

### 2. Run the Web Portal Manually

```bash
cd web-portal
pip install -r requirements.txt
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
python app.py
```

---

## Verification: Isolation Tests

> For course instructors and evaluators — the following tests verify the three core isolation layers: **RBAC**, **Network**, and **Resource**.

### 1. RBAC Isolation

Verify that each role has the correct (and only the correct) permissions.

**Developer** — can manage workloads, but cannot access secrets or cluster controls:

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig

# Should SUCCEED
kubectl get pods
kubectl create deployment nginx --image=nginx
kubectl get resourcequota
kubectl get limitranges

# Should FAIL (forbidden)
kubectl get secrets
kubectl get networkpolicy
kubectl get roles
```

**Viewer** — read-only, cannot create or exec into pods:

```bash
export KUBECONFIG=./team-alpha-view-kubeconfig

# Should SUCCEED
kubectl get pods,deployments,services

# Should FAIL (forbidden)
kubectl create deployment nginx --image=nginx
kubectl exec -it <pod> -- /bin/sh
```

**Tenant Admin** — full control inside the namespace, but cannot delete the namespace itself:

```bash
export KUBECONFIG=./team-alpha-admin-kubeconfig

# Should SUCCEED inside the tenant namespace
kubectl get secrets
kubectl get resourcequota
kubectl get roles

# Should FAIL (namespace lifecycle is reserved for cluster admin)
kubectl delete namespace team-alpha
```

### 2. Network Isolation

Every tenant namespace gets three NetworkPolicies:

1. **default-deny-ingress**: Blocks all inbound traffic by default
2. **allow-same-namespace**: Allows pods within the same namespace to communicate
3. **allow-dns**: Allows DNS queries to CoreDNS over UDP/TCP 53

Test cross-namespace blocking and intra-namespace allowance:

```bash
# In team-alpha namespace
kubectl run test-nginx --image=nginx --namespace team-alpha

# In team-bravo namespace (should FAIL)
kubectl run busybox --image=busybox -it --rm --restart=Never --namespace team-bravo -- wget -O- http://<team-alpha-pod-ip>

# In team-alpha namespace (should SUCCEED)
kubectl run test-client --image=busybox -it --rm --restart=Never --namespace team-alpha -- wget -O- http://test-nginx
```

### 3. Resource Isolation

**ResourceQuota** — prevents a single tenant from exhausting cluster capacity:

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig
kubectl apply -f demo/test-quota-pod.yaml
# Expected: the first pods may be accepted, then a later pod is denied with "exceeded quota"
```

**LimitRange Defaults** — ensures every pod gets default resource requests/limits even if not specified:

```bash
export KUBECONFIG=./team-alpha-dev-kubeconfig
kubectl apply -f demo/test-pod.yaml
kubectl describe pod no-resources-pod | grep -A5 "Requests"
# Should show: cpu=200m, memory=256Mi (defaults)
```


---

## Directory Structure (For Evaluation)

The repository is organized so that each directory directly maps to a specific project requirement, making it easy for evaluators to locate the relevant artifacts.

```
.
├── .env.example                    # [Config] Host kubeconfig path and portal env vars
├── .dockerignore                   # [Build] Prevents local kubeconfigs/cache from entering image
├── docker-compose.yml              # [Infra] One-click portal deployment
├── onboard-team.sh                 # [Automation] CLI tenant onboarding script
│                                   #   → Creates NS, isolated user SAs, RBAC, Quota, NetPol, kubeconfig
├── scripts/
│   ├── check-prereqs.sh            # [Ops] Host prerequisite checker before docker-compose
│   └── start-lab-platform.sh       # [Ops] Full cluster + portal startup workflow
├── rbac/                           # [Basic/Advanced] Role & RoleBinding definitions
│   ├── admin-role.yaml             #   → Tenant admin permissions inside one namespace
│   ├── developer-role.yaml         #   → Least-privilege workload-management permissions
│   ├── viewer-role.yaml            #   → Read-only namespace permissions
│   └── rolebinding-template.yaml   #   → Binds tenant Roles to isolated user ServiceAccounts
├── resources/                      # [Standard] Resource controls per tenant
│   ├── quota.yaml                  #   → ResourceQuota (CPU, Mem, Pods, PVCs, Services)
│   └── limitrange.yaml             #   → LimitRange (defaults, min, max per container)
├── networkpolicies/                # [Advanced] Network isolation policies
│   ├── default-deny-ingress.yaml   #   → Deny all inbound by default
│   ├── allow-same-namespace.yaml   #   → Allow intra-namespace traffic
│   └── allow-dns.yaml              #   → Allow CoreDNS egress (UDP/TCP:53)
├── demo/                           # [Verification] Manual test manifests
│   ├── test-pod.yaml               #   → Verifies LimitRange default injection
│   ├── test-quota-pod.yaml         #   → Verifies ResourceQuota enforcement
│   └── network-test.yaml           #   → Verifies NetworkPolicy isolation
└── web-portal/                     # [Advanced] Flask-based management portal
    ├── Dockerfile                  #   → Container build instructions
    ├── entrypoint.sh               #   → Loads kubeconfig & checks K8s API reachability
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
| 3 Roles (admin/dev/viewer) | `rbac/admin-role.yaml`, `rbac/developer-role.yaml`, `rbac/viewer-role.yaml`, `config.py` |
| Namespace + Role/RoleBinding | `onboard-team.sh`, `rbac/` |
| Different access permissions | `rbac/*.yaml`, `demo/`, README "Verification: Isolation Tests" section |
| ResourceQuota / LimitRange | `resources/quota.yaml`, `resources/limitrange.yaml` |
| Per-team namespace design | `onboard-team.sh`, `web-portal/k8s_client.py` |
| Onboarding / usage guide | README "Quick Start" & "Manual Setup", `onboard-team.sh` |
| Security & isolation explanation | README "Engineering Focus" & "Security Layers" sections |
| Prevent misuse / accidental damage | least-privilege `rbac/*.yaml`, Pod Security labels in `onboard-team.sh`, `resources/quota.yaml` |
| NetworkPolicy / fine-grained permissions | `networkpolicies/*.yaml`, `rbac/developer-role.yaml` |
| Automation / lightweight portal | `onboard-team.sh`, `web-portal/` |
| Scalability & limitations discussion | README "Scalability & Limitations" section |

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


## License

MIT — Educational use encouraged.
