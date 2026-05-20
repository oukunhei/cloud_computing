# AGENTS.md — Multi-Tenant K3s Lab Platform

> This file is intended for AI coding agents. It describes the project architecture, conventions, and workflows so you can be productive without prior context.

---

## Project Overview

This is a **lightweight, secure, multi-tenant Kubernetes lab platform** built on **K3s**. It provides namespace-level isolation for teaching labs or student projects using a defense-in-depth strategy:

- **RBAC** — Role/RoleBinding per tenant namespace
- **ResourceQuota + LimitRange** — Prevents resource exhaustion
- **NetworkPolicy** — Blocks cross-namespace traffic
- **Pod Security Admission** — Baseline enforce, restricted warn/audit
- **TokenRequest API** — Short-lived, revocable ServiceAccount tokens

The project includes:
- A **Flask web portal** for one-click tenant onboarding, resource monitoring, kubeconfig generation, Grafana embedding, and pod diagnostics.
- **Prometheus + Grafana** stack for metrics collection and visualization.
- A **CLI onboarding script** (`onboard-team.sh`) that provisions all tenant resources.
- **Validation scripts** (`scripts/validate-isolation.sh`, `scripts/validate-resilience.sh`) for automated RBAC, network, quota, and HPA verification.
- **Demo/test YAMLs** for verifying RBAC, quotas, and network isolation.

---

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Runtime | Python | 3.11 |
| Web Framework | Flask | 3.0.0 |
| K8s Client | kubernetes (Python) | 29.0.0 |
| YAML Parsing | PyYAML | 6.0.1 |
| Metrics | prometheus_client | 0.19.0 |
| Frontend | Bootstrap + vanilla JS | 5.3.2 |
| Container | Docker + Docker Compose | — |
| Orchestrator | K3s | v1.24+ |
| Monitoring | Prometheus | v2.52.0 |
| Dashboards | Grafana | 10.4.0 |

**No package managers like npm, pipenv, or poetry are used.** Dependencies are pinned in `web-portal/requirements.txt`.

---

## Directory Structure

```
.
├── docker-compose.yml              # Deploy web, prometheus, grafana with host network
├── .env.example                    # Template for host kubeconfig path and portal env vars
├── onboard-team.sh                 # CLI tenant onboarding script
├── scripts/
│   ├── check-prereqs.sh            # Host prerequisite checker before docker-compose
│   ├── start-lab-platform.sh       # Full cluster + portal + monitoring startup workflow
│   ├── fix-k3s-flannel.sh          # K3s CNI repair helper
│   ├── validate-isolation.sh       # Automated RBAC + NetworkPolicy validation
│   └── validate-resilience.sh      # Automated ResourceQuota + LimitRange + HPA validation
├── rbac/                           # Kubernetes Role definitions
│   ├── admin-role.yaml
│   ├── developer-role.yaml
│   ├── viewer-role.yaml
│   └── rolebinding-template.yaml
├── resources/                      # Quota and limit defaults
│   ├── quota.yaml
│   └── limitrange.yaml
├── networkpolicies/                # Network isolation manifests
│   ├── default-deny-ingress.yaml
│   ├── allow-same-namespace.yaml
│   └── allow-dns.yaml
├── demo/                           # Verification/test pods
│   ├── test-pod.yaml
│   ├── test-quota-pod.yaml
│   └── network-test.yaml
├── monitoring/                     # Prometheus + Grafana provisioning
│   ├── prometheus/prometheus.yml
│   └── grafana/
│       ├── datasources/datasource.yml
│       └── dashboards/
│           ├── dashboard-provider.yml
│           └── namespace-resources.json
└── web-portal/                     # Flask application
    ├── Dockerfile
    ├── entrypoint.sh               # Container bootstrap (kubectl install, kubeconfig copy, API wait)
    ├── requirements.txt
    ├── app.py                      # Flask routes, metrics collector, Grafana proxy
    ├── k8s_client.py               # Kubernetes API wrapper (cluster ops, kubeconfig gen, diagnostics)
    ├── config.py                   # Constants, role permissions matrix, env defaults
    ├── static/
    │   ├── css/style.css
    │   └── js/main.js
    └── templates/                  # Jinja2 HTML templates
        ├── base.html
        ├── login.html
        ├── dashboard.html
        ├── tenants.html
        ├── resources.html
        ├── kubeconfig.html
        ├── permissions.html
        └── settings.html
```

---

## Build and Run Commands

### Prerequisites
- K3s v1.24+ installed and running
- Docker 20.10+ with Docker Compose
- `/etc/rancher/k3s/k3s.yaml` must exist (host kubeconfig)

### One-Command Startup (Recommended)

```bash
./scripts/start-lab-platform.sh
```

To also create a demo tenant after startup:

```bash
CREATE_DEMO_TENANT=true ./scripts/start-lab-platform.sh
```

To force a full reset:

```bash
FULL_RESET=true ./scripts/start-lab-platform.sh
```

### Deploy with Docker Compose (Manual)

```bash
cp .env.example .env
./scripts/check-prereqs.sh
docker-compose up -d --build
```

The portal runs on port `8080`. Prometheus runs on `9090`. Grafana runs on `3000` and is proxied through the portal at `/grafana`. All containers use `network_mode: host`.

### Run Manually (Development)

```bash
cd web-portal
pip install -r requirements.txt
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
python app.py
```

### Create a Tenant via CLI

```bash
./onboard-team.sh team-alpha
```

This generates `team-alpha-admin-kubeconfig`, `team-alpha-dev-kubeconfig`, and `team-alpha-view-kubeconfig` in the working directory.

### Automated Validation

```bash
# RBAC + NetworkPolicy isolation tests
./scripts/validate-isolation.sh

# ResourceQuota + LimitRange + HPA resilience tests
./scripts/validate-resilience.sh
```

### Stop

```bash
docker-compose down
```

---

## Code Organization

### Backend (`web-portal/`)

- **`app.py`** — Flask application entry point. Defines server-rendered pages, JSON API endpoints, and a Prometheus metrics collector. Contains role-based access decorators (`require_login`, `require_admin_api`, `require_namespace_access`, `require_workload_write`, `require_tenant_admin_workload_delete`). Implements a Grafana reverse proxy at `/grafana`.
- **`k8s_client.py`** — `K8sClient` singleton class wrapping the Kubernetes Python client. Handles:
  - Cluster info aggregation and node-level USE metrics
  - Tenant listing, creation (delegates to `onboard-team.sh`), and deletion (with force/cleanup)
  - Namespace resource inspection (quota, limitrange, pods, network policies)
  - Live namespace usage from the Kubernetes Metrics API (metrics-server)
  - Kubeconfig YAML generation with TokenRequest API tokens
  - Demo workload creation/deletion (Deployment + Service + HPA)
  - Custom Pod creation/deletion with validation and wait logic
  - Pod diagnostics with event analysis, taint checking, and repair flows
  - Monitoring diagnostics (Grafana, Prometheus, portal metrics health checks)
  - Resource settings read/update (ResourceQuota and LimitRange)
  - Permission checks mapped to `ROLE_PERMISSIONS`
- **`config.py`** — Constants:
  - `SYSTEM_NAMESPACES`: namespaces excluded from tenant listings
  - `USER_NAMESPACE`: namespace that stores isolated user ServiceAccounts (`lab-platform-users`)
  - `ROLE_PERMISSIONS`: human-readable permission matrix for the UI
  - `FLASK_PORT`, `SECRET_KEY`, Grafana/Prometheus base URLs

### Frontend

- **Templates** extend `base.html` and use Bootstrap 5 classes. No JS frameworks — all interactivity is vanilla JS.
- **`static/js/main.js`** provides `apiGet`, `apiPost`, `apiDelete`, toast notifications, and a K8s connectivity status checker.
- **Bootstrap Icons** are loaded from CDN.

### Infrastructure Manifests

All YAMLs in `rbac/`, `resources/`, and `networkpolicies/` are **templates** processed by `sed` in `onboard-team.sh`. They are **not** applied directly with `kubectl apply -f <dir>`.

### Monitoring Stack

- **Prometheus** scrapes the portal's `/metrics` endpoint every 15s.
- **Grafana** is provisioned with a Prometheus datasource and a namespace-resources dashboard. It is served through the Flask portal proxy at `/grafana` to avoid CORS and cross-origin cookie issues.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirects to Dashboard (or Login if unauthenticated) |
| GET | `/login` | Role login page (simulated cluster-admin/admin/developer/viewer) |
| POST | `/login` | Submit role + namespace + password |
| GET | `/logout` | Clear session |
| GET | `/dashboard` | Cluster overview page |
| GET | `/tenants` | Tenant management page |
| GET | `/resources/<namespace>` | Namespace resource monitor page (with Grafana embed) |
| GET | `/kubeconfig` | Kubeconfig download page |
| GET | `/permissions` | RBAC matrix page |
| GET | `/settings` | Resource settings page (quota/limitrange editor) |
| GET/POST/PUT/PATCH/DELETE/OPTIONS | `/grafana/<path>` | Grafana reverse proxy |
| GET | `/api/cluster/info` | JSON: nodes, namespaces, pods counts |
| GET | `/api/portal/version` | JSON: build ID and connectivity status |
| GET | `/api/tenants` | JSON: list of non-system namespaces |
| POST | `/api/tenants` | JSON: create tenant (`{"name": "...", "quota": {...}}`); cluster admin only |
| DELETE | `/api/tenants/<name>` | JSON: delete tenant namespace; cluster admin only |
| GET | `/api/namespaces/<namespace>/resources` | JSON: quota, limitrange, pods, netpols |
| GET | `/api/namespaces/<namespace>/live-usage` | JSON: live CPU/memory usage from metrics-server |
| GET | `/api/namespaces/<namespace>/monitoring-diagnostics` | JSON: Grafana/Prometheus/metrics API health |
| GET | `/api/namespaces/<namespace>/resource-settings` | JSON: current quota and limitrange settings |
| POST | `/api/namespaces/<namespace>/resource-settings` | JSON: update quota and limitrange; admin only |
| POST | `/api/namespaces/<namespace>/demo-workload` | JSON: create demo Deployment+Service+HPA; admin/dev only |
| DELETE | `/api/namespaces/<namespace>/demo-workload` | JSON: delete demo workload; admin/dev only |
| POST | `/api/namespaces/<namespace>/pods` | JSON: create a custom Pod; admin/dev only |
| DELETE | `/api/namespaces/<namespace>/pods/<pod_name>` | JSON: delete a Pod; admin only |
| GET | `/api/namespaces/<namespace>/pods/<pod_name>/diagnostics` | JSON: pod diagnostics, events, repair flow |
| GET | `/api/namespaces/<namespace>/permission-checks` | JSON: live RBAC permission matrix for session role |
| GET | `/api/namespaces/<namespace>/kubeconfig` | Download kubeconfig YAML; role query param: `admin`, `dev`, or `view` |
| GET | `/metrics` | Prometheus metrics endpoint |

---

## Key Conventions

### Naming
- Tenant namespaces must be DNS-compatible: lowercase alphanumeric and hyphens only, max 63 chars. Validated in `app.py` and `onboard-team.sh`.
- User ServiceAccounts are stored in `lab-platform-users` by default and named `<namespace>-admin`, `<namespace>-dev`, and `<namespace>-view`.
- Generated kubeconfig files: `<namespace>-admin-kubeconfig`, `<namespace>-dev-kubeconfig`, and `<namespace>-view-kubeconfig`.

### Python Style
- Standard Flask conventions.
- No formal linter or formatter configuration is present. Follow PEP 8 and existing patterns.
- Use single quotes for strings unless escaping is required.
- Imports are grouped: stdlib, third-party, local.
- The CI runs `flake8` with `--max-line-length=200 --ignore=E501,W503,E128,E741,E241,F401`.

### Bash Style
- `onboard-team.sh` and `entrypoint.sh` use `set -e` (or `set -euo pipefail`) for strict error handling.
- Colorized output with ANSI escape codes is standard in `onboard-team.sh` and `scripts/*.sh`.
- The CI runs `shellcheck` on `onboard-team.sh`, `scripts/*.sh`, and `web-portal/entrypoint.sh`.

### YAML Style
- The CI runs `yamllint -d relaxed -d "{rules: {line-length: disable}}"` on `rbac/`, `resources/`, `networkpolicies/`, and `demo/`.

### Kubernetes Patterns
- **Least-privilege RBAC**: Kubernetes RBAC is additive and has no explicit deny rule. Developer/viewer roles omit sensitive permissions instead of trying to deny them.
- **Portal admin scoping**: `cluster-admin` is the platform operator role for tenant lifecycle. `admin` is the tenant-admin role and must pass `can_use_namespace()` before any namespace-scoped portal API operation.
- **Template substitution**: `onboard-team.sh` uses `sed` to inject the tenant namespace and user names into YAML manifests before applying them.
- **Pod Security**: Namespaces are labeled with `pod-security.kubernetes.io/enforce=baseline`, `audit=restricted`, `warn=restricted`.

---

## Testing Strategy

**There is no automated unit test suite** (no pytest, jest, or similar). Verification is a mix of linting in CI and manual/automated integration tests using shell scripts and kubectl.

### CI Testing (GitHub Actions)

The `.github/workflows/ci.yml` runs on every push and pull request:

```yaml
matrix:
  tool: [yamllint, shellcheck, flake8]
```

- **yamllint**: Validates YAML syntax in `rbac/`, `resources/`, `networkpolicies/`, `demo/`.
- **shellcheck**: Validates Bash scripts for common pitfalls.
- **flake8**: Validates Python code in `web-portal/`.

### Manual Verification Workflow

1. **RBAC (Developer)**
   ```bash
   export KUBECONFIG=./team-alpha-dev-kubeconfig
   kubectl get pods          # should succeed
   kubectl get secrets       # should fail (forbidden)
   ```

2. **RBAC (Viewer)**
   ```bash
   export KUBECONFIG=./team-alpha-view-kubeconfig
   kubectl get pods          # should succeed
   kubectl create deploy ... # should fail (forbidden)
   ```

3. **ResourceQuota**
   ```bash
   kubectl apply -f demo/test-quota-pod.yaml  # should fail (exceeded quota)
   ```

4. **LimitRange Defaults**
   ```bash
   kubectl apply -f demo/test-pod.yaml
   kubectl describe pod no-resources-pod      # should show default requests/limits
   ```

5. **Network Isolation**
   ```bash
   kubectl run test-nginx --image=nginx --namespace team-alpha
   # From another namespace, wget to team-alpha pod IP should fail
   ```

### Automated Validation Scripts

- **`scripts/validate-isolation.sh`** — Creates `team-alpha` and `team-beta`, then runs:
  - RBAC lateral movement tests (cross-namespace access, namespace deletion, deployment creation, pod deletion)
  - Network micro-segmentation tests (cross-tenant Pod IP blocked, cross-tenant Service DNS blocked, same-namespace allowed, DNS resolution allowed)

- **`scripts/validate-resilience.sh`** — Creates `team-alpha` and `team-beta`, then runs:
  - ResourceQuota hard limit test (burst pod creation exceeding quota)
  - Noisy-neighbour isolation test (team-beta unaffected by team-alpha exhaustion)
  - LimitRange default injection test (pod without resources gets defaults)
  - Over-spec interception test (pod exceeding max limits is rejected)
  - HPA scaling test (deployment scales under load, respects quota, no cross-tenant impact)

When modifying RBAC rules, quota limits, or network policies, run the corresponding verification steps.

---

## Security Considerations

### Credential Handling
- The web portal reads the **host K3s admin kubeconfig** (`/etc/rancher/k3s/k3s.yaml`) mounted read-only into the container.
- Tenant kubeconfigs are generated dynamically via the **TokenRequest API** (`kubectl create token`) with a 1-year duration. No long-lived static ServiceAccount secrets are used.
- Generated kubeconfig files are written with `chmod 600`.
- The portal uses Flask sessions with a `SECRET_KEY`. Tenant namespaces store a random 6-digit password in an annotation (`tenant.lab/password`) for the simulated login flow.

### RBAC Hardening
- **Cluster Admin** can create/delete namespaces and manage all tenants. It is not scoped to a single namespace.
- **Tenant Admin** has full control inside one namespace (workloads, RBAC, ResourceQuota, LimitRange, NetworkPolicy) but cannot delete the namespace itself.
- **Developers** can read `resourcequotas` and `limitranges` but are not granted write access to them, and are not granted: `secrets`, `roles`, `rolebindings`, `networkpolicies`.
- **Viewers** can read `resourcequotas` and `limitranges` but are denied all write operations, `pods/exec`, `pods/portforward`, `pods/attach`, `pods/proxy`, `secrets`, `roles`, `rolebindings`.
- Denial is implemented by omission: Kubernetes RBAC is additive and has no explicit deny rule.

### Isolation Layers
1. **Access Control**: RBAC Roles + RoleBindings
2. **Resource Guard**: ResourceQuota + LimitRange per namespace
3. **Network Wall**: NetworkPolicies (deny ingress, allow intra-namespace, allow DNS egress)
4. **Pod Guardrails**: Pod Security Admission labels (baseline enforce, restricted warn/audit)

### What to Avoid
- **Never** expose the web portal to untrusted networks without an authentication layer. The portal has admin-level cluster access.
- **Never** check generated `*-kubeconfig` files into version control.
- **Never** modify `SYSTEM_NAMESPACES` in `config.py` without understanding the impact on tenant listing.

---

## Deployment Notes

### Docker Compose
- `network_mode: host` is used so containers can reach the K3s API server directly.
- The `entrypoint.sh` installs `kubectl` if missing (image-bundled -> host-mounted -> runtime download), copies the kubeconfig, and waits up to 30 seconds for the K8s API to be reachable before starting Flask.
- Source files (`app.py`, `config.py`, `k8s_client.py`, `templates/`, `static/`) are mounted as read-only volumes for rapid development iteration without rebuilds.
- `FLASK_PORT`, `SECRET_KEY`, `USER_NAMESPACE`, and monitoring base URLs can be overridden via `.env`.

### Production Readiness Gaps
The README explicitly lists known limitations. If you are modifying the project, be aware:
- No persistent identity / SSO integration
- No Kubernetes audit logging
- Pod Security is baseline-only (not restricted enforce)
- Single-cluster, single-node K3s (no HA)

---

## Useful Commands

```bash
# Check K3s status
sudo systemctl status k3s

# Quick portal logs
docker-compose logs -f web

# Force-delete a stuck namespace
kubectl delete namespace <name> --force

# Regenerate a tenant developer token manually
kubectl create token team-alpha-dev -n lab-platform-users --duration=8760h

# Run automated validations
./scripts/validate-isolation.sh
./scripts/validate-resilience.sh

# Fix K3s CNI if node is NotReady
sudo ./scripts/fix-k3s-flannel.sh
```

---

*Last updated: 2026-05-20*
