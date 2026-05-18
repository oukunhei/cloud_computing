import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote_plus
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response, redirect, session, url_for
from prometheus_client import Gauge, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
from k8s_client import k8s
from config import (
    ROLE_PERMISSIONS,
    FLASK_PORT,
    SECRET_KEY,
    SYSTEM_NAMESPACES,
    USER_NAMESPACE,
    GRAFANA_PUBLIC_BASE_URL,
    GRAFANA_INTERNAL_BASE_URL
)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# ── Prometheus metrics ──────────────────────────────────────────────
prom_registry = CollectorRegistry()

METRIC_QUOTA_HARD = Gauge(
    'k8s_namespace_resource_quota_hard',
    'ResourceQuota hard limit per namespace',
    ['namespace', 'resource'],
    registry=prom_registry
)
METRIC_QUOTA_USED = Gauge(
    'k8s_namespace_resource_quota_used',
    'ResourceQuota current usage per namespace',
    ['namespace', 'resource'],
    registry=prom_registry
)
METRIC_QUOTA_PERCENT = Gauge(
    'k8s_namespace_resource_quota_percent',
    'ResourceQuota usage percentage (0-100) per namespace',
    ['namespace', 'resource'],
    registry=prom_registry
)
METRIC_POD_COUNT = Gauge(
    'k8s_namespace_pod_count',
    'Number of pods in the namespace',
    ['namespace'],
    registry=prom_registry
)
METRIC_POD_STATUS = Gauge(
    'k8s_namespace_pod_status',
    'Pod status (1=RUNNING, 2=PENDING, 3=FAILED, 0=UNKNOWN)',
    ['namespace', 'pod'],
    registry=prom_registry
)
METRIC_NETPOL_COUNT = Gauge(
    'k8s_namespace_networkpolicy_count',
    'Number of NetworkPolicies in the namespace',
    ['namespace'],
    registry=prom_registry
)
METRIC_NS_CPU_MILLICORES = Gauge(
    'k8s_namespace_cpu_usage_millicores',
    'Live CPU usage in millicores from the Kubernetes Metrics API',
    ['namespace'],
    registry=prom_registry
)
METRIC_NS_MEMORY_MIB = Gauge(
    'k8s_namespace_memory_usage_mib',
    'Live memory usage in MiB from the Kubernetes Metrics API',
    ['namespace'],
    registry=prom_registry
)
METRIC_POD_CPU_MILLICORES = Gauge(
    'k8s_pod_cpu_usage_millicores',
    'Live pod CPU usage in millicores from the Kubernetes Metrics API',
    ['namespace', 'pod'],
    registry=prom_registry
)
METRIC_POD_MEMORY_MIB = Gauge(
    'k8s_pod_memory_usage_mib',
    'Live pod memory usage in MiB from the Kubernetes Metrics API',
    ['namespace', 'pod'],
    registry=prom_registry
)

# ── Node-level USE metrics ──────────────────────────────────────────

METRIC_NODE_CONDITION = Gauge(
    'k8s_node_condition',
    'Node condition status (1=OK, 0=NotOK)',
    ['node', 'condition'],
    registry=prom_registry
)
METRIC_NODE_POD_COUNT = Gauge(
    'k8s_node_pod_count',
    'Number of pods scheduled on the node',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_POD_CAPACITY = Gauge(
    'k8s_node_pod_capacity',
    'Maximum pods allocatable on the node',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_CPU_ALLOCATABLE = Gauge(
    'k8s_node_cpu_allocatable_cores',
    'Allocatable CPU cores on the node',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_MEM_ALLOCATABLE = Gauge(
    'k8s_node_memory_allocatable_mib',
    'Allocatable memory in MiB on the node',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_CPU_MILLICORES = Gauge(
    'k8s_node_cpu_usage_millicores',
    'Live node CPU usage in millicores from the Kubernetes Metrics API',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_MEMORY_MIB = Gauge(
    'k8s_node_memory_usage_mib',
    'Live node memory usage in MiB from the Kubernetes Metrics API',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_CPU_UTIL_PCT = Gauge(
    'k8s_node_cpu_utilization_percent',
    'Node CPU utilization percentage (usage / allocatable * 100)',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_MEM_UTIL_PCT = Gauge(
    'k8s_node_memory_utilization_percent',
    'Node memory utilization percentage (usage / allocatable * 100)',
    ['node'],
    registry=prom_registry
)
METRIC_NODE_POD_UTIL_PCT = Gauge(
    'k8s_node_pod_utilization_percent',
    'Node pod slot utilization percentage (pods / max_pods * 100)',
    ['node'],
    registry=prom_registry
)

_metrics_lock = threading.Lock()
_metrics_cache_ttl = 15  # seconds
_metrics_cached_data = None
_metrics_cached_at = 0
_metric_pod_status_labels = set()
_metric_pod_usage_labels = set()

METRIC_UP = Gauge(
    'k8s_portal_up',
    'Whether the K8s portal metrics collector is connected (1=connected, 0=disconnected)',
    registry=prom_registry
)


def _resource_to_number(val):
    """Convert a Kubernetes resource string (e.g. '100m', '2Gi', '10') to a float."""
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s.endswith('m'):
        return float(s[:-1]) / 1000.0
    if s.endswith('Gi'):
        return float(s[:-2]) * 1024.0
    if s.endswith('Mi'):
        return float(s[:-2])
    if s.endswith('Ki'):
        return float(s[:-2]) / 1024.0
    if s.endswith('Ti'):
        return float(s[:-2]) * 1024.0 * 1024.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _collect_metrics():
    """Query Kubernetes API and refresh all Prometheus metrics."""
    global _metrics_cached_at

    # Always check connection status and set up metric
    connected = k8s.is_connected()
    METRIC_UP.set(1 if connected else 0)

    if not connected:
        return

    # Check cache: skip K8s API queries if data is fresh
    with _metrics_lock:
        if time.time() - _metrics_cached_at < _metrics_cache_ttl:
            return

    try:
        ns_list = k8s.v1.list_namespace()
    except Exception:
        return

    for ns in ns_list.items:
        namespace = ns.metadata.name

        # --- Pods ---
        current_pods = set()
        try:
            pods = k8s.v1.list_namespaced_pod(namespace)
            METRIC_POD_COUNT.labels(namespace=namespace).set(len(pods.items))
            for p in pods.items:
                pod_name = p.metadata.name
                current_pods.add(pod_name)
                phase = p.status.phase or 'Unknown'
                status_map = {'Running': 1, 'Pending': 2, 'Failed': 3, 'Succeeded': 4}
                METRIC_POD_STATUS.labels(namespace=namespace, pod=pod_name).set(
                    status_map.get(phase, 0)
                )
            for old_namespace, old_pod in list(_metric_pod_status_labels):
                if old_namespace == namespace and old_pod not in current_pods:
                    METRIC_POD_STATUS.remove(old_namespace, old_pod)
                    _metric_pod_status_labels.discard((old_namespace, old_pod))
            _metric_pod_status_labels.update((namespace, pod_name) for pod_name in current_pods)
        except Exception:
            METRIC_POD_COUNT.labels(namespace=namespace).set(0)

        # --- ResourceQuota ---
        try:
            quotas = k8s.v1.list_namespaced_resource_quota(namespace)
            for q in quotas.items:
                hard = q.spec.hard or {}
                used = (q.status.used if q.status else {}) or {}
                for resource_key, hard_val in hard.items():
                    hard_num = _resource_to_number(hard_val)
                    used_num = _resource_to_number(used.get(resource_key, '0'))
                    pct = (used_num / hard_num * 100.0) if hard_num > 0 else 0.0
                    METRIC_QUOTA_HARD.labels(namespace=namespace, resource=resource_key).set(hard_num)
                    METRIC_QUOTA_USED.labels(namespace=namespace, resource=resource_key).set(used_num)
                    METRIC_QUOTA_PERCENT.labels(namespace=namespace, resource=resource_key).set(pct)
        except Exception:
            pass

        # --- NetworkPolicies ---
        try:
            np_list = k8s.net_v1.list_namespaced_network_policy(namespace)
            METRIC_NETPOL_COUNT.labels(namespace=namespace).set(len(np_list.items))
        except Exception:
            METRIC_NETPOL_COUNT.labels(namespace=namespace).set(0)

        # --- Live pod usage from metrics-server ---
        usage = k8s.get_namespace_live_usage(namespace)
        if usage.get('metrics_available'):
            totals = usage.get('totals') or {}
            METRIC_NS_CPU_MILLICORES.labels(namespace=namespace).set(totals.get('cpu_millicores', 0))
            METRIC_NS_MEMORY_MIB.labels(namespace=namespace).set(totals.get('memory_mib', 0))
            current_usage_pods = set()
            for pod in usage.get('pods', []):
                pod_name = pod['name']
                current_usage_pods.add(pod_name)
                METRIC_POD_CPU_MILLICORES.labels(namespace=namespace, pod=pod_name).set(
                    pod.get('cpu_millicores', 0)
                )
                METRIC_POD_MEMORY_MIB.labels(namespace=namespace, pod=pod_name).set(
                    pod.get('memory_mib', 0)
                )
            for old_namespace, old_pod in list(_metric_pod_usage_labels):
                if old_namespace == namespace and old_pod not in current_usage_pods:
                    METRIC_POD_CPU_MILLICORES.remove(old_namespace, old_pod)
                    METRIC_POD_MEMORY_MIB.remove(old_namespace, old_pod)
                    _metric_pod_usage_labels.discard((old_namespace, old_pod))
            _metric_pod_usage_labels.update((namespace, pod_name) for pod_name in current_usage_pods)

    # ── Node-level USE collection ────────────────────────────────────
    # Build a lookup of node -> allocatable from nodes_info so we can
    # compute utilization percentages in one pass.
    node_allocatable = {}  # node_name -> {cpu_cores, memory_mib, pods}

    try:
        nodes_info = k8s.get_nodes_info()
        if not isinstance(nodes_info, dict) or 'error' not in nodes_info:
            for node in nodes_info:
                node_name = node['name']
                node_allocatable[node_name] = {
                    'cpu_cores': node.get('allocatable_cpu_cores', 0),
                    'memory_mib': node.get('allocatable_memory_mib', 0),
                    'pods': node.get('allocatable_pods', 0)
                }
                # Conditions (Errors / Health)
                for cond_name, cond_val in node.get('conditions', {}).items():
                    METRIC_NODE_CONDITION.labels(node=node_name, condition=cond_name).set(cond_val)
                # Pod count / capacity (Saturation)
                METRIC_NODE_POD_COUNT.labels(node=node_name).set(node.get('pods_on_node', 0))
                METRIC_NODE_POD_CAPACITY.labels(node=node_name).set(node.get('allocatable_pods', 0))
                pod_pct = (node.get('pods_on_node', 0) / max(node.get('allocatable_pods', 1), 1) * 100.0) if node.get('allocatable_pods', 0) > 0 else 0.0
                METRIC_NODE_POD_UTIL_PCT.labels(node=node_name).set(pod_pct)
                # Allocatable capacity (Utilization baseline)
                METRIC_NODE_CPU_ALLOCATABLE.labels(node=node_name).set(node.get('allocatable_cpu_cores', 0))
                METRIC_NODE_MEM_ALLOCATABLE.labels(node=node_name).set(node.get('allocatable_memory_mib', 0))
    except Exception:
        pass

    # Node live usage from metrics-server (Utilization)
    try:
        node_metrics = k8s.get_node_metrics()
        if node_metrics.get('metrics_available'):
            for nm in node_metrics.get('nodes', []):
                node_name = nm['name']
                cpu_millicores = nm.get('cpu_millicores', 0)
                memory_mib = nm.get('memory_mib', 0)
                METRIC_NODE_CPU_MILLICORES.labels(node=node_name).set(cpu_millicores)
                METRIC_NODE_MEMORY_MIB.labels(node=node_name).set(memory_mib)
                # Compute utilization % using the allocatable values we already fetched
                alloc = node_allocatable.get(node_name, {})
                alloc_cpu = alloc.get('cpu_cores', 0)
                alloc_mem = alloc.get('memory_mib', 0)
                cpu_pct = (cpu_millicores / 1000.0 / alloc_cpu * 100.0) if alloc_cpu > 0 else 0.0
                mem_pct = (memory_mib / alloc_mem * 100.0) if alloc_mem > 0 else 0.0
                METRIC_NODE_CPU_UTIL_PCT.labels(node=node_name).set(cpu_pct)
                METRIC_NODE_MEM_UTIL_PCT.labels(node=node_name).set(mem_pct)
    except Exception:
        pass

    # Update cache timestamp after successful full scan
    _metrics_cached_at = time.time()


app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = SECRET_KEY
# Force template refresh so mounted template changes are visible without
# relying on Flask debug mode.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
DNS_LABEL_RE = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
VALID_ROLES = {'cluster-admin', 'admin', 'developer', 'viewer'}
PORTAL_BUILD_ID = 'custom-pod-create-v2-pod-delete-v1'


def current_identity():
    namespace = session.get('namespace')
    role = session.get('role')
    return {
        'role': role,
        'namespace': namespace,
        'is_logged_in': bool(role),
        'is_platform_admin': is_platform_admin(),
        'is_tenant_admin': role == 'admin',
        'can_create_pods': role in ('admin', 'developer'),
        'can_delete_pods': role == 'admin',
        'display_role': display_role(role)
    }


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('role'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def require_admin_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_platform_admin():
            return jsonify({'error': 'Only cluster admin can perform this action. Tenant admins are scoped to their own namespace.'}), 403
        return view(*args, **kwargs)
    return wrapped


def is_platform_admin():
    return session.get('role') == 'cluster-admin'


def can_manage_namespace_controls():
    return is_platform_admin() or session.get('role') == 'admin'


def display_role(role):
    labels = {
        'cluster-admin': 'Cluster Admin',
        'admin': 'Tenant Admin',
        'developer': 'Developer',
        'viewer': 'Viewer'
    }
    return labels.get(role, role or '')


def can_use_namespace(namespace):
    selected_namespace = session.get('namespace')
    if is_platform_admin():
        return True
    return bool(selected_namespace) and selected_namespace == namespace


def require_namespace_access(view):
    @wraps(view)
    def wrapped(namespace, *args, **kwargs):
        if not can_use_namespace(namespace):
            return jsonify({
                'error': f'Your simulated {session.get("role")} session is scoped to namespace {session.get("namespace")}.'
            }), 403
        return view(namespace, *args, **kwargs)
    return wrapped


def require_workload_write(view):
    @wraps(view)
    def wrapped(namespace, *args, **kwargs):
        if session.get('role') not in ('admin', 'developer'):
            return jsonify({'error': f'{display_role(session.get("role"))} cannot create or delete tenant workloads from this portal action.'}), 403
        if not can_use_namespace(namespace):
            return jsonify({
                'error': f'Your simulated {session.get("role")} session is scoped to namespace {session.get("namespace")}.'
            }), 403
        return view(namespace, *args, **kwargs)
    return wrapped


def require_tenant_admin_workload_delete(view):
    @wraps(view)
    def wrapped(namespace, *args, **kwargs):
        if session.get('role') != 'admin':
            return jsonify({'error': f'{display_role(session.get("role"))} cannot delete Pods from this portal action.'}), 403
        if not can_use_namespace(namespace):
            return jsonify({
                'error': f'Your simulated admin session is scoped to namespace {session.get("namespace")}.'
            }), 403
        return view(namespace, *args, **kwargs)
    return wrapped


@app.context_processor
def inject_identity():
    return {'identity': current_identity(), 'portal_build_id': PORTAL_BUILD_ID}


@app.after_request
def disable_html_cache(response):
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.route('/')
def index():
    if not session.get('role'):
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        role = request.form.get('role', '').strip().lower()
        namespace = request.form.get('namespace', '').strip().lower()
        password = request.form.get('password', '').strip()

        if role not in VALID_ROLES:
            return render_template(
                'login.html',
                roles=ROLE_PERMISSIONS,
                error='Please select a valid role.'
            ), 400

        if namespace and (len(namespace) > 63 or not DNS_LABEL_RE.match(namespace)):
            return render_template(
                'login.html',
                roles=ROLE_PERMISSIONS,
                error='Namespace must be DNS-compatible.'
            ), 400

        if role == 'cluster-admin' and namespace:
            return render_template(
                'login.html',
                roles=ROLE_PERMISSIONS,
                error='Cluster admin is not scoped to a tenant namespace. Leave namespace empty.'
            ), 400

        if role in ('admin', 'developer', 'viewer') and not namespace:
            return render_template(
                'login.html',
                roles=ROLE_PERMISSIONS,
                error='Tenant admin, developer, and viewer sessions must be scoped to a tenant namespace.'
            ), 400

        if role in ('admin', 'developer', 'viewer') and namespace:
            expected = k8s.get_namespace_password(namespace)
            if expected and password != expected:
                return render_template(
                    'login.html',
                    roles=ROLE_PERMISSIONS,
                    error='Incorrect namespace password.'
                ), 403

        session['role'] = role
        session['namespace'] = namespace
        return redirect(url_for('dashboard'))

    return render_template('login.html', roles=ROLE_PERMISSIONS)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@require_login
def dashboard():
    return render_template('dashboard.html')


@app.route('/tenants')
@require_login
def tenants():
    return render_template('tenants.html')


@app.route('/resources/<namespace>')
@require_login
def resources_page(namespace):
    if not can_use_namespace(namespace):
        return redirect(url_for('dashboard'))
    role = session.get('role')
    can_create_pods = role in ('admin', 'developer')
    can_delete_pods = role == 'admin'
    grafana_base_url = (GRAFANA_PUBLIC_BASE_URL or '/grafana').rstrip('/')
    encoded_ns = quote_plus(namespace)
    grafana_dashboard_url = (
        f'{grafana_base_url}/d/k8s-namespace-resources'
        f'?orgId=1&var-namespace={encoded_ns}&refresh=15s'
    )
    grafana_embed_url = (
        f'{grafana_dashboard_url}'
        '&from=now-1h&to=now&theme=light&kiosk'
    )
    return render_template(
        'resources.html',
        namespace=namespace,
        grafana_dashboard_url=grafana_dashboard_url,
        grafana_embed_url=grafana_embed_url,
        can_create_pods=can_create_pods,
        can_delete_pods=can_delete_pods
    )


@app.route('/kubeconfig')
@require_login
def kubeconfig_page():
    return render_template('kubeconfig.html')


@app.route('/permissions')
@require_login
def permissions():
    return render_template('permissions.html', roles=ROLE_PERMISSIONS)


@app.route('/settings')
@require_login
def settings_page():
    return render_template('settings.html')


@app.route('/grafana/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/grafana/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@require_login
def grafana_proxy(path):
    base = GRAFANA_INTERNAL_BASE_URL.rstrip('/')
    upstream_base = base if base.endswith('/grafana') else f'{base}/grafana'
    query = request.query_string.decode('utf-8')
    target = f'{upstream_base}/{path}'
    if query:
        target = f'{target}?{query}'

    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in ('host', 'connection', 'content-length', 'accept-encoding')
    }
    data = request.get_data() if request.method in ('POST', 'PUT', 'PATCH') else None
    upstream_request = urllib.request.Request(target, data=data, headers=headers, method=request.method)
    opener = urllib.request.build_opener(NoRedirectHandler)

    try:
        with opener.open(upstream_request, timeout=20) as upstream:
            body = upstream.read()
            response_headers = []
            for key, value in upstream.headers.items():
                lower = key.lower()
                if lower in ('content-length', 'connection', 'transfer-encoding', 'content-encoding'):
                    continue
                if lower == 'location':
                    if value.startswith(upstream_base):
                        value = value.replace(upstream_base, '/grafana', 1)
                    elif value.startswith(base):
                        value = value.replace(base, '', 1)
                response_headers.append((key, value))
            return Response(body, status=upstream.status, headers=response_headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        response_headers = []
        for key, value in exc.headers.items():
            lower = key.lower()
            if lower in ('content-length', 'connection', 'transfer-encoding', 'content-encoding'):
                continue
            if lower == 'location':
                if value.startswith(upstream_base):
                    value = value.replace(upstream_base, '/grafana', 1)
                elif value.startswith(base):
                    value = value.replace(base, '', 1)
            response_headers.append((key, value))
        return Response(body, status=exc.code, headers=response_headers)
    except Exception as exc:
        return Response(f'Grafana proxy error: {exc}', status=502, mimetype='text/plain')


# API Routes
@app.route('/api/cluster/info')
def api_cluster_info():
    return jsonify(k8s.get_cluster_info())


@app.route('/api/portal/version')
def api_portal_version():
    return jsonify({
        'build_id': PORTAL_BUILD_ID,
        'custom_pod_api': True,
        'kubernetes_connected': k8s.is_connected()
    })


@app.route('/api/tenants', methods=['GET'])
@require_login
def api_list_tenants():
    tenants = k8s.list_tenants()
    if isinstance(tenants, dict) and tenants.get('error'):
        return jsonify(tenants)
    selected_namespace = session.get('namespace')
    if not is_platform_admin():
        tenants = [tenant for tenant in tenants if tenant.get('name') == selected_namespace]
    return jsonify(tenants)


@app.route('/api/tenants', methods=['POST'])
@require_login
@require_admin_api
def api_create_tenant():
    data = request.get_json() or {}
    name = data.get('name', '').strip().lower()
    quota = data.get('quota') or {}

    if not name:
        return jsonify({'error': 'Namespace name is required'}), 400

    if len(name) > 63 or not DNS_LABEL_RE.match(name):
        return jsonify({'error': 'Invalid namespace name. Use a DNS-compatible name: lowercase letters, numbers, hyphens, max 63 chars, and no leading/trailing hyphen.'}), 400

    try:
        result = k8s.create_tenant(name)
        if quota:
            k8s.apply_quota_overrides(name, quota)
        return jsonify({'success': True, 'output': result.get('output'), 'password': result.get('password')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tenants/<name>', methods=['DELETE'])
@require_login
@require_admin_api
def api_delete_tenant(name):
    force = request.args.get('force', 'false').lower() == 'true'
    try:
        result = k8s.delete_tenant(name, force=force)
        return jsonify({'success': True, 'message': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/resources')
@require_login
@require_namespace_access
def api_namespace_resources(namespace):
    return jsonify(k8s.get_namespace_resources(namespace))


@app.route('/api/namespaces/<namespace>/live-usage')
@require_login
@require_namespace_access
def api_namespace_live_usage(namespace):
    return jsonify(k8s.get_namespace_live_usage(namespace))


@app.route('/api/namespaces/<namespace>/monitoring-diagnostics')
@require_login
@require_namespace_access
def api_monitoring_diagnostics(namespace):
    return jsonify(k8s.get_monitoring_diagnostics(namespace))


@app.route('/api/namespaces/<namespace>/resource-settings', methods=['GET'])
@require_login
@require_namespace_access
def api_get_resource_settings(namespace):
    return jsonify(k8s.get_resource_settings(namespace))


@app.route('/api/namespaces/<namespace>/resource-settings', methods=['POST'])
@require_login
@require_namespace_access
def api_update_resource_settings(namespace):
    if not can_manage_namespace_controls():
        return jsonify({'error': 'Only cluster admin or tenant admin can change ResourceQuota and LimitRange.'}), 403
    data = request.get_json() or {}
    try:
        message = k8s.update_resource_settings(namespace, data)
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/demo-workload', methods=['POST'])
@require_login
@require_workload_write
def api_create_demo_workload(namespace):
    try:
        message = k8s.create_demo_workload(namespace, session.get('role'))
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/pods', methods=['POST'])
@require_login
@require_workload_write
def api_create_custom_pod(namespace):
    data = request.get_json() or {}
    try:
        pod = k8s.create_custom_pod(namespace, data, session.get('role'))
        return jsonify({
            'success': True,
            'message': f'Pod {pod["name"]} created in namespace {pod["namespace"]}.',
            'pod': pod
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error while creating Pod: {e}'}), 500


@app.route('/api/namespaces/<namespace>/pods/<pod_name>', methods=['DELETE'])
@require_login
@require_tenant_admin_workload_delete
def api_delete_pod(namespace, pod_name):
    try:
        global _metrics_cached_at
        message = k8s.delete_pod(namespace, pod_name)
        _metrics_cached_at = 0
        resources = k8s.get_namespace_resources(namespace)
        return jsonify({
            'success': True,
            'message': message,
            'pods': resources.get('pods', []),
            'pods_error': resources.get('pods_error')
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error while deleting Pod: {e}'}), 500


@app.route('/api/namespaces/<namespace>/pods/<pod_name>/diagnostics')
@require_login
@require_namespace_access
def api_pod_diagnostics(namespace, pod_name):
    try:
        return jsonify(k8s.diagnose_pod(namespace, pod_name))
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error while diagnosing Pod: {e}'}), 500


@app.route('/api/namespaces/<namespace>/demo-workload', methods=['DELETE'])
@require_login
@require_workload_write
def api_delete_demo_workload(namespace):
    try:
        message = k8s.delete_demo_workload(namespace, session.get('role'))
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/permission-checks')
@require_login
@require_namespace_access
def api_permission_checks(namespace):
    try:
        return jsonify(k8s.permission_checks(namespace, session.get('role')))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/kubeconfig')
@require_login
@require_namespace_access
def api_generate_kubeconfig(namespace):
    role = request.args.get('role', 'dev')
    if role not in ('admin', 'dev', 'view'):
        return jsonify({'error': 'Invalid role. Must be "admin", "dev", or "view"'}), 400

    current_role = session.get('role')
    allowed_downloads = {'developer': {'dev'}, 'viewer': {'view'}}
    if is_platform_admin() or current_role == 'admin':
        allowed_downloads['admin'] = {'admin', 'dev', 'view'}
    if role not in allowed_downloads.get(current_role, set()):
        return jsonify({'error': 'You cannot download kubeconfig files for a higher-privilege role.'}), 403

    try:
        config_yaml = k8s.generate_kubeconfig(namespace, role)
        filename = f"{namespace}-{role}-kubeconfig.yaml"
        return Response(
            config_yaml,
            mimetype='text/yaml',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/metrics')
def metrics():
    """Prometheus metrics endpoint — scraped by Prometheus for Grafana dashboards."""
    _collect_metrics()
    return Response(generate_latest(prom_registry), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False)
