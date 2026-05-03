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

## Engineering Focus: Multi-Tenancy, Isolation, RBAC & Platform Design

> This section highlights how the project addresses the core engineering requirements: **multi-tenancy, isolation, RBAC, and platform-style Kubernetes design**.

### 1. Multi-Tenancy Design

This platform implements **namespace-as-a-tenant** isolation, where each student team receives a fully provisioned, independent namespace:

- **Tenant Onboarding**: A single command (`./onboard-team.sh team-alpha`) or one click in the Web Portal creates an entire tenant stack.
- **Per-Tenant Resources**: Every tenant gets its own `Roles`, `RoleBindings`, `ResourceQuota`, `LimitRange`, and `NetworkPolicies`. User `ServiceAccounts` live in a separate `lab-platform-users` namespace to prevent tenant pods from mounting higher-privilege credentials.
- **Shared Cluster, Isolated Workloads**: Multiple teams share a single lightweight K3s node, but their workloads, credentials, and network traffic are fully segregated.

```
┌──────────────────────────────────────────────────┐
│           K3s Cluster (Shared)                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  │
│  │ team-α     │  │ team-β     │  │ team-γ     │  │
│  │ Namespace  │  │ Namespace  │  │ Namespace  │  │
│  │ • Quota    │  │ • Quota    │  │ • Quota    │  │
│  │ • NetPol   │  │ • NetPol   │  │ • NetPol   │  │
│  │ • RoleBind │  │ • RoleBind │  │ • RoleBind │  │
│  │ • PSS Label│  │ • PSS Label│  │ • PSS Label│  │
│  └────────────┘  └────────────┘  └────────────┘  │
└──────────────────────────────────────────────────┘
```

### 2. Isolation Mechanisms

Isolation is enforced through **three complementary layers** (defense in depth):

| Isolation Layer | Implementation | What It Blocks |
|-----------------|----------------|----------------|
| **RBAC Isolation** | `Role` + `RoleBinding` per namespace | Cross-namespace API access; unauthorized actions within a namespace |
| **Resource Isolation** | `ResourceQuota` + `LimitRange` | Resource exhaustion by a single tenant; runaway containers |
| **Network Isolation** | `NetworkPolicy` (deny ingress + scoped egress + allow intra-NS + allow DNS) | Cross-namespace pod-to-pod traffic; unauthorized inbound connections |
| **Pod Isolation** | Namespace Pod Security labels (`baseline` enforce, `restricted` warn/audit) | Privileged pods, host networking, hostPath-style misuse |

**Key Design Decision**: Kubernetes RBAC is additive and has no explicit deny rule. This platform therefore follows least privilege: sensitive resources such as `secrets`, `roles`, `rolebindings`, `resourcequotas`, `limitranges`, and `networkpolicies` are simply not granted to developer/viewer roles. User ServiceAccounts are stored outside tenant namespaces, so a developer cannot create a pod that mounts the tenant-admin token.

### 3. RBAC Design

The platform implements a **three-tier RBAC model** using Kubernetes native `Role` and `RoleBinding` resources:

| Role | Read | Write | Exec/PortForward | Not Granted / Restricted |
|------|------|-------|------------------|-------------------|
| **Admin** | All namespaced resources | All namespaced resources | Yes | Cannot delete namespaces unless using platform admin kubeconfig |
| **Developer** | Pods, logs, Deployments, Services, ConfigMaps, Ingresses, Events, HPA, Jobs | Workloads and app-facing resources | Yes | `secrets`, RBAC, quotas, limit ranges, network policies |
| **Viewer** | Pods, logs, Deployments, Services, ConfigMaps, Ingresses, Events, HPA, Jobs | — | No | write verbs, exec, port-forward, secrets, RBAC |

**Why this matters**: Developers cannot read `secrets` (mitigates credential leakage if kubeconfig is lost) and cannot modify platform-level controls (prevents privilege escalation). Viewers are strictly read-only and cannot exec into pods.

### 4. Platform-Style Kubernetes Design

Rather than a collection of manual `kubectl` commands, this project is designed as a **mini PaaS (Platform as a Service)**:

- **Automation Layer**: `onboard-team.sh` encapsulates all provisioning logic (namespace, RBAC, quota, netpol, kubeconfig generation) into an idempotent-like workflow.
- **Management Portal**: A Flask-based web UI provides **Dashboard** (cluster telemetry), **Tenant Management** (CRUD), **Resource Monitor** (quota usage, pod list), **Kubeconfig Generator** (TokenRequest API), and **Permissions Viewer** (RBAC matrix).
- **Role Login Demo**: The portal starts with a simulated `admin` / `developer` / `viewer` login screen so evaluators can see how different roles experience the platform.
- **Self-Service Onboarding**: A student team can be onboarded in ~30 seconds without the platform administrator running individual `kubectl` commands.
- **Token Lifecycle Management**: Uses the `TokenRequest` API (`kubectl create token`) instead of static ServiceAccount secrets, generating time-bound (1-year), revocable tokens per tenant role.

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
| Different access permissions | `rbac/*.yaml`, `demo/`, README "Demo & Verification" section |
| ResourceQuota / LimitRange | `resources/quota.yaml`, `resources/limitrange.yaml` |
| Per-team namespace design | `onboard-team.sh`, `web-portal/k8s_client.py` |
| Onboarding / usage guide | README "Quick Start" & "Manual Setup", `onboard-team.sh` |
| Security & isolation explanation | README "Engineering Focus" & "Security Layers" sections |
| Prevent misuse / accidental damage | least-privilege `rbac/*.yaml`, Pod Security labels in `onboard-team.sh`, `resources/quota.yaml` |
| NetworkPolicy / fine-grained permissions | `networkpolicies/*.yaml`, `rbac/developer-role.yaml` |
| Automation / lightweight portal | `onboard-team.sh`, `web-portal/` |
| Scalability & limitations discussion | README "Scalability & Limitations" section |

---

## Prerequisites

### Host Requirements

These must be installed on the machine where you run `docker-compose up -d --build`:

| Package | Required | Why |
|---------|----------|-----|
| Linux OS | Yes | Recommended: Ubuntu 20.04/22.04/24.04 or CentOS/RHEL-compatible Linux |
| Docker Engine | Yes | Builds and runs the Flask portal container |
| Docker Compose | Yes | Runs `docker-compose.yml`; either `docker-compose` v1 or Docker Compose plugin is OK |
| K3s | Yes for full demo | Provides the Kubernetes cluster and default kubeconfig |
| kubectl | Recommended | Needed for host-side verification and manual demos; the portal image also includes kubectl |
| curl / ca-certificates | Recommended | Useful for installing K3s/Docker and checking API access |

Minimum hardware: **2 CPU, 4 GB RAM**.

### Container Packages

The Docker image installs these automatically:

- Debian slim base packages: `bash`, `curl`, `ca-certificates`
- Kubernetes CLI: `kubectl`
- Python packages from `web-portal/requirements.txt`: `flask`, `kubernetes`, `pyyaml`, `Werkzeug`

### Kubeconfig Path

By default, the portal mounts the K3s admin kubeconfig from:

```bash
/etc/rancher/k3s/k3s.yaml
```

If your kubeconfig is elsewhere, create `.env` from the example and edit `KUBECONFIG_HOST_PATH`:

```bash
cp .env.example .env
# edit .env if needed
```

### Install K3s On Ubuntu

```bash
curl -sfL https://get.k3s.io | sh -
sudo chmod 644 /etc/rancher/k3s/k3s.yaml
```

### Verify Host Setup

```bash
k3s --version
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes
./scripts/check-prereqs.sh
```

---

## Quick Start (Clone & Run)

### 1. Clone the Repository

```bash
git clone https://github.com/oukunhei/Kubernetes_Lab_Platform.git
cd Kubernetes_Lab_Platform
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

---

## Manual Setup (Without Docker)

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
| Delete namespace | Platform admin only | ❌ | ❌ |

> **Design Rationale**: Developers are not granted access to `secrets`, reducing credential leakage risk if their kubeconfig is compromised. They also cannot modify platform-level controls (RBAC, quotas, network policies), preventing privilege escalation or accidental breaking of isolation. Tenant admins can manage resources within their own namespace, while namespace creation/deletion remains a platform-admin action.

---

## Resource Isolation Design

Although the project requirement only asks for either `ResourceQuota` or `LimitRange`, this platform implements both because they solve different multi-tenant risks:

- `ResourceQuota` caps total namespace consumption so tenants share the cluster fairly.
- `LimitRange` applies per-container, per-pod, and per-PVC defaults and bounds so accidental misconfiguration is rejected early.

Together, they prevent both resource exhaustion and unsafe workload specifications.

### ResourceQuota per Tenant

| Resource | Hard Limit |
|----------|-----------|
| requests.cpu | 2 |
| requests.memory | 4Gi |
| requests.ephemeral-storage | 8Gi |
| limits.cpu | 4 |
| limits.memory | 8Gi |
| limits.ephemeral-storage | 16Gi |
| requests.storage | 20Gi |
| pods | 20 |
| services | 10 |
| services.nodeports | 0 |
| services.loadbalancers | 0 |
| persistentvolumeclaims | 5 |
| count/deployments.apps | 10 |
| count/statefulsets.apps | 3 |
| count/jobs.batch | 10 |
| count/cronjobs.batch | 5 |
| count/ingresses.networking.k8s.io | 5 |

### LimitRange Guardrails

| Type | Guardrail | Value | Purpose |
|------|-----------|-------|---------|
| Container | default request | cpu 200m, memory 256Mi, ephemeral-storage 512Mi | Makes pods without explicit requests schedulable and quota-counted |
| Container | default limit | cpu 500m, memory 1Gi, ephemeral-storage 1Gi | Prevents unlimited containers |
| Container | max | cpu 2, memory 4Gi, ephemeral-storage 4Gi | Stops one container from dominating a tenant |
| Container | min | cpu 50m, memory 64Mi, ephemeral-storage 128Mi | Rejects unrealistic tiny specs |
| Container | maxLimitRequestRatio | cpu 4, memory 4 | Prevents huge burst limits over tiny requests |
| Pod | max | cpu 2, memory 4Gi, ephemeral-storage 6Gi | Catches oversized multi-container pods |
| PVC | min / max | 1Gi / 10Gi | Prevents tiny unusable claims and oversized single claims |

---

## Network Isolation Design

Every tenant namespace gets three NetworkPolicies:

1. **default-deny-ingress**: Blocks all inbound traffic by default
2. **allow-same-namespace**: Allows pods within the same namespace to communicate in both directions
3. **allow-dns**: Allows DNS queries to CoreDNS over UDP/TCP 53; other cross-namespace egress remains blocked once egress policy is selected

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
| **Role Login** | Simulated admin/developer/viewer login with role-aware UI controls |
| **Dashboard** | Cluster overview: nodes, namespaces, pods, tenant count |
| **Tenant Management** | One-click create/delete tenants with full isolation stack |
| **Resource Monitoring** | Per-namespace ResourceQuota usage bars, LimitRange rules, Pod list |
| **Role Action Lab** | Browser buttons to create/delete a demo workload and run live RBAC permission checks |
| **Resource Settings** | Admin can update tenant ResourceQuota/LimitRange from the browser; developer/viewer get read-only access |
| **Kubeconfig Generator** | Web UI to download role-appropriate admin/dev/view kubeconfig files |
| **Permissions Viewer** | Visual matrix showing what each role can/cannot do |

---

## Demo & Verification

### 0. Web Portal Role Demo

Use this flow to demonstrate resource and permission differences directly in the browser:

1. Log in as `admin`.
2. Open **Tenants** and create `team-alpha`.
3. Open **Resources** for `team-alpha`.
4. In **Role Action Lab**, click **Create Admin Demo**. A `lab-demo-admin` Deployment and Service are created, and the Pod list refreshes.
5. Click **Run Permission Checks**. The page runs `kubectl auth can-i --as=system:serviceaccount:lab-platform-users:team-alpha-admin ...` and shows which actions are allowed or denied.
6. Log out, log in as `developer`, and open the same namespace. The developer can create/delete its own `lab-demo-developer` workload while `lab-demo-admin` remains separate; permission checks show denial for `secrets`, `resourcequotas`, and RBAC modification.
7. Log out, log in as `viewer`, and open the same namespace. The viewer can inspect resources and run permission checks, while create/delete workload buttons are disabled.

This browser flow shows both platform UX controls and live Kubernetes RBAC checks.

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

### 1b. Verify RBAC (Tenant Admin)

```bash
export KUBECONFIG=./team-alpha-admin-kubeconfig

# Should SUCCEED inside the tenant namespace
kubectl get secrets
kubectl get resourcequota
kubectl get roles

# Should FAIL because namespace lifecycle is reserved for the platform admin
kubectl delete namespace team-alpha
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
# Expected: the first pods may be accepted, then a later pod is denied with "exceeded quota"
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
- **Pod Safety Defaults**: Pod Security labels enforce the baseline policy and warn/audit restricted-policy violations
- **Token Lifecycle**: Uses TokenRequest API (1-year expiry), no static secrets

### Known Limitations

| Limitation | Impact | Future Improvement |
|-----------|--------|-------------------|
| Single-cluster | No node-level fault tolerance | Multi-node K3s or K3s HA |
| No persistent identity | Tokens expire, no SSO | Integrate OIDC/Keycloak |
| L3/L4 network only | Cannot filter by HTTP path | Add Istio/Linkerd service mesh |
| No audit logging | Cannot trace who did what | Enable K8s Audit Policy |
| No storage isolation | Tenants share StorageClass | Add Rook/Ceph per tenant |
| Pod Security is baseline-only | Some risky-but-baseline-compliant pods may still run | Move tenants to `restricted` after validating lab workloads |

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
| `docker-compose: command not found` | Install standalone Compose or run `docker compose up -d --build` with the Compose plugin |
| Docker build fails with `curl exit code 56` while downloading `kubectl` | Network/CDN issue. Set `KUBECTL_BASE_URL=https://mirrors.aliyun.com/kubernetes-release/release` in `.env`, then rebuild with `docker compose build --no-cache web` |
| Portal starts but tenant creation says `kubectl is not available` | Install kubectl on the host and set `HOST_KUBECTL_PATH=/usr/local/bin/kubectl` or `/usr/bin/kubectl` in `.env`, then run `docker compose up -d --force-recreate` |
| `/host/kubeconfig not found` | Create `.env` and set `KUBECONFIG_HOST_PATH` to your kubeconfig path |
| NetworkPolicy not working | Verify Calico is running: `kubectl get pods -n calico-system` |
| Web portal shows "Disconnected" | Check K3s status: `sudo systemctl status k3s`. Ensure `/etc/rancher/k3s/k3s.yaml` exists |
| Token creation fails | Ensure K3s API is reachable from container (host network mode should handle this) |
| Cannot delete namespace | Namespace may be stuck in Terminating. Force: `kubectl delete namespace <name> --force` |

---

## License

MIT — Educational use encouraged.
