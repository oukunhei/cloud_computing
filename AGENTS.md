# AGENTS.md — Multi-Tenant K3s Lab Platform

> This file is intended for AI coding agents. It describes the project architecture, conventions, and workflows so you can be productive without prior context.

---

## Project Overview

This is a **lightweight, secure, multi-tenant Kubernetes lab platform** built on **K3s**. It provides namespace-level isolation for teaching labs or student projects using a defense-in-depth strategy:

- **RBAC** — Role/RoleBinding per tenant namespace
- **ResourceQuota + LimitRange** — Prevents resource exhaustion
- **NetworkPolicy** — Blocks cross-namespace traffic
- **TokenRequest API** — Short-lived, revocable ServiceAccount tokens

The project includes:
- A **Flask web portal** for one-click tenant onboarding, resource monitoring, and kubeconfig generation.
- A **CLI onboarding script** (`onboard-team.sh`) that provisions all tenant resources.
- **Demo/test YAMLs** for verifying RBAC, quotas, and network isolation.

---

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Runtime | Python | 3.11 |
| Web Framework | Flask | 3.0.0 |
| K8s Client | kubernetes (Python) | 29.0.0 |
| YAML Parsing | PyYAML | 6.0.1 |
| Frontend | Bootstrap + vanilla JS | 5.3.2 |
| Container | Docker + Docker Compose | — |
| Orchestrator | K3s | v1.24+ |

**No package managers like npm, pipenv, or poetry are used.** Dependencies are pinned in `web-portal/requirements.txt`.

---

## Directory Structure

```
.
├── docker-compose.yml              # Deploy web portal with host network
├── onboard-team.sh                 # CLI tenant onboarding script
├── README.md                       # Human-facing documentation
├── AGENTS.md                       # This file
├── rbac/                           # Kubernetes Role definitions
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
└── web-portal/                     # Flask application
    ├── Dockerfile
    ├── entrypoint.sh               # Container bootstrap
    ├── requirements.txt
    ├── app.py                      # Flask routes
    ├── k8s_client.py               # Kubernetes API wrapper
    ├── config.py                   # Constants and role permissions matrix
    ├── static/
    │   ├── css/style.css
    │   └── js/main.js
    └── templates/                  # Jinja2 HTML templates
        ├── base.html
        ├── dashboard.html
        ├── tenants.html
        ├── resources.html
        ├── kubeconfig.html
        └── permissions.html
```

---

## Build and Run Commands

### Prerequisites
- K3s v1.24+ installed and running
- Docker 20.10+ with Docker Compose
- `/etc/rancher/k3s/k3s.yaml` must exist (host kubeconfig)

### Deploy with Docker Compose (Recommended)

```bash
docker-compose up -d --build
```

The portal runs on port `8080`. The container uses `network_mode: host` and mounts the host K3s kubeconfig read-only.

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

### Stop

```bash
docker-compose down
```

---

## Code Organization

### Backend (`web-portal/`)

- **`app.py`** — Flask application entry point. Defines server-rendered pages and JSON API endpoints. Uses `render_template` for HTML and `jsonify`/`Response` for APIs.
- **`k8s_client.py`** — `K8sClient` singleton class wrapping the Kubernetes Python client. Handles:
  - Cluster info aggregation
  - Tenant listing, creation (delegates to `onboard-team.sh`), and deletion
  - Namespace resource inspection (quota, limitrange, pods, network policies)
  - Kubeconfig YAML generation with TokenRequest API tokens
- **`config.py`** — Constants:
  - `SYSTEM_NAMESPACES`: namespaces excluded from tenant listings
  - `USER_NAMESPACE`: namespace that stores isolated user ServiceAccounts
  - `ROLE_PERMISSIONS`: human-readable permission matrix for the UI
  - `FLASK_PORT`: defaults to 8080, overridable via env var

### Frontend

- **Templates** extend `base.html` and use Bootstrap 5 classes. No JS frameworks — all interactivity is vanilla JS.
- **`static/js/main.js`** provides `apiGet`, `apiPost`, `apiDelete`, toast notifications, and a K8s connectivity status checker.
- **Bootstrap Icons** are loaded from CDN.

### Infrastructure Manifests

All YAMLs in `rbac/`, `resources/`, and `networkpolicies/` are **templates** processed by `sed` in `onboard-team.sh`. They are **not** applied directly with `kubectl apply -f <dir>`.

---

## Key Conventions

### Naming
- Tenant namespaces must be DNS-compatible: lowercase alphanumeric and hyphens only. Validated in `app.py` and `onboard-team.sh`.
- User ServiceAccounts are stored in `lab-platform-users` by default and named `<namespace>-admin`, `<namespace>-dev`, and `<namespace>-view`.
- Generated kubeconfig files: `<namespace>-admin-kubeconfig`, `<namespace>-dev-kubeconfig`, and `<namespace>-view-kubeconfig`.

### Python Style
- Standard Flask conventions.
- No formal linter or formatter configuration is present. Follow PEP 8 and existing patterns.
- Use single quotes for strings unless escaping is required.
- Imports are grouped: stdlib, third-party, local.

### Bash Style
- `onboard-team.sh` and `entrypoint.sh` use `set -e` (or `set -euo pipefail`) for strict error handling.
- Colorized output with ANSI escape codes is standard in `onboard-team.sh`.

### Kubernetes Patterns
- **Least-privilege RBAC**: Kubernetes RBAC is additive and has no explicit deny rule. Developer/viewer roles omit sensitive permissions instead of trying to deny them.
- **Portal admin scoping**: `cluster-admin` is the platform operator role for tenant lifecycle. `admin` is the tenant-admin role and must pass `can_use_namespace()` before any namespace-scoped portal API operation.
- **Template substitution**: `onboard-team.sh` uses `sed` to inject the tenant namespace and user names into YAML manifests before applying them.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirects to Dashboard |
| GET | `/dashboard` | Cluster overview page |
| GET | `/tenants` | Tenant management page |
| GET | `/resources/<namespace>` | Namespace resource monitor page |
| GET | `/kubeconfig` | Kubeconfig download page |
| GET | `/permissions` | RBAC matrix page |
| GET | `/api/cluster/info` | JSON: nodes, namespaces, pods counts |
| GET | `/api/tenants` | JSON: list of non-system namespaces |
| POST | `/api/tenants` | JSON: create tenant (`{"name": "..."}`); cluster admin only |
| DELETE | `/api/tenants/<name>` | JSON: delete tenant namespace; cluster admin only |
| GET | `/api/namespaces/<namespace>/resources` | JSON: quota, limitrange, pods, netpols |
| GET | `/api/namespaces/<namespace>/kubeconfig` | Download kubeconfig YAML; role query param: `admin`, `dev`, or `view` |

---

## Testing Strategy

**There is no automated test suite** (no pytest, jest, or similar). Verification is manual using the demo manifests and kubectl.

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

When modifying RBAC rules, quota limits, or network policies, run the corresponding verification steps.

---

## Security Considerations

### Credential Handling
- The web portal reads the **host K3s admin kubeconfig** (`/etc/rancher/k3s/k3s.yaml`) mounted read-only into the container.
- Tenant kubeconfigs are generated dynamically via the **TokenRequest API** (`kubectl create token`) with a 1-year duration. No long-lived static ServiceAccount secrets are used.
- Generated kubeconfig files are written with `chmod 600`.

### RBAC Hardening
- **Developers** can read `resourcequotas` and `limitranges` but are not granted write access to them, and are not granted: `secrets`, `roles`, `rolebindings`, `networkpolicies`.
- **Viewers** can read `resourcequotas` and `limitranges` but are denied all write operations, `pods/exec`, `pods/portforward`, `pods/attach`, `pods/proxy`, `secrets`, `roles`, `rolebindings`.
- Denial is implemented by omission: Kubernetes RBAC is additive and has no explicit deny rule.

### Isolation Layers
1. **Access Control**: RBAC Roles + RoleBindings
2. **Resource Guard**: ResourceQuota + LimitRange per namespace
3. **Network Wall**: NetworkPolicies (deny ingress, allow intra-namespace, allow DNS egress)

### What to Avoid
- **Never** expose the web portal to untrusted networks without an authentication layer. The portal has admin-level cluster access.
- **Never** check generated `*-kubeconfig` files into version control.
- **Never** modify `SYSTEM_NAMESPACES` in `config.py` without understanding the impact on tenant listing.

---

## Deployment Notes

### Docker Compose
- `network_mode: host` is used so the container can reach the K3s API server directly.
- The `entrypoint.sh` installs `kubectl` if missing, copies the kubeconfig, and waits up to 30 seconds for the K8s API to be reachable before starting Flask.
- `FLASK_PORT` can be overridden via environment variable.

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
```

---

*Last updated: 2026-05-02*
