import os
import subprocess
import yaml
from datetime import datetime, timezone
from kubernetes import client, config
from config import SYSTEM_NAMESPACES


class K8sClient:
    def __init__(self):
        self.kubeconfig_path = os.environ.get('KUBECONFIG', os.path.expanduser('~/.kube/config'))
        self.connected = False
        self.v1 = None
        self.rbac_v1 = None
        self.apps_v1 = None
        self.net_v1 = None
        self._init_client()

    def _init_client(self):
        try:
            if os.path.exists(self.kubeconfig_path):
                config.load_kube_config(self.kubeconfig_path)
            else:
                config.load_incluster_config()

            self.v1 = client.CoreV1Api()
            self.rbac_v1 = client.RbacAuthorizationV1Api()
            self.apps_v1 = client.AppsV1Api()
            self.net_v1 = client.NetworkingV1Api()
            self.connected = True
        except Exception:
            self.connected = False

    def is_connected(self):
        if not self.connected:
            return False
        try:
            self.v1.get_api_resources()
            return True
        except Exception:
            return False

    def get_cluster_info(self):
        if not self.connected:
            return {'error': 'Not connected to Kubernetes', 'nodes': 0, 'namespaces': 0, 'pods': 0, 'ready_nodes': 0}
        try:
            nodes = self.v1.list_node()
            namespaces = self.v1.list_namespace()
            pods = self.v1.list_pod_for_all_namespaces()

            ready_nodes = 0
            for n in nodes.items:
                if n.status.conditions:
                    for c in n.status.conditions:
                        if c.type == 'Ready' and c.status == 'True':
                            ready_nodes += 1
                            break

            return {
                'nodes': len(nodes.items),
                'namespaces': len(namespaces.items),
                'pods': len(pods.items),
                'ready_nodes': ready_nodes
            }
        except Exception as e:
            return {'error': str(e), 'nodes': 0, 'namespaces': 0, 'pods': 0, 'ready_nodes': 0}

    def list_tenants(self):
        if not self.connected:
            return {'error': 'Not connected to Kubernetes'}
        try:
            ns_list = self.v1.list_namespace()
            tenants = []
            for ns in ns_list.items:
                name = ns.metadata.name
                if name not in SYSTEM_NAMESPACES:
                    tenants.append({
                        'name': name,
                        'created': ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else '',
                        'status': ns.status.phase
                    })
            return sorted(tenants, key=lambda x: x['name'])
        except Exception as e:
            return {'error': str(e)}

    def create_tenant(self, name):
        script_dir = os.path.join(os.path.dirname(__file__), '..')
        script_path = os.path.join(script_dir, 'onboard-team.sh')

        if not os.path.exists(script_path):
            raise FileNotFoundError(f"onboard-team.sh not found at {script_path}")

        result = subprocess.run(
            ['bash', script_path, name],
            cwd=script_dir,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(f"Onboarding failed: {result.stderr}")

        return result.stdout

    def delete_tenant(self, name):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')
        if name in SYSTEM_NAMESPACES:
            raise ValueError("Cannot delete system namespace")

        self.v1.delete_namespace(name=name)
        return f"Namespace {name} deleted"

    def get_namespace_resources(self, namespace):
        if not self.connected:
            return {'error': 'Not connected to Kubernetes'}

        result = {'namespace': namespace}

        # ResourceQuota
        try:
            quotas = self.v1.list_namespaced_resource_quota(namespace)
            if quotas.items:
                q = quotas.items[0]
                result['quota'] = {
                    'name': q.metadata.name,
                    'hard': dict(q.spec.hard) if q.spec.hard else {},
                    'used': dict(q.status.used) if q.status and q.status.used else {}
                }
            else:
                result['quota'] = None
        except Exception as e:
            result['quota_error'] = str(e)

        # LimitRange
        try:
            limits = self.v1.list_namespaced_limit_range(namespace)
            if limits.items:
                lr = limits.items[0]
                result['limitrange'] = {
                    'name': lr.metadata.name,
                    'limits': [l.to_dict() for l in lr.spec.limits] if lr.spec.limits else []
                }
            else:
                result['limitrange'] = None
        except Exception as e:
            result['limitrange_error'] = str(e)

        # Pods
        try:
            pods = self.v1.list_namespaced_pod(namespace)
            pod_list = []
            for p in pods.items:
                container_statuses = p.status.container_statuses or []
                ready = sum(1 for c in container_statuses if c.ready)

                requests = {}
                limits = {}
                if p.spec.containers:
                    for c in p.spec.containers:
                        if c.resources:
                            if c.resources.requests:
                                for k, v in c.resources.requests.items():
                                    requests[k] = requests.get(k, 0) + self._parse_resource(v)
                            if c.resources.limits:
                                for k, v in c.resources.limits.items():
                                    limits[k] = limits.get(k, 0) + self._parse_resource(v)

                pod_list.append({
                    'name': p.metadata.name,
                    'status': p.status.phase,
                    'ready': f"{ready}/{len(container_statuses)}",
                    'restarts': sum(c.restart_count for c in container_statuses),
                    'age': self._format_age(p.metadata.creation_timestamp),
                    'requests': requests,
                    'limits': limits
                })
            result['pods'] = pod_list
        except Exception as e:
            result['pods_error'] = str(e)
            result['pods'] = []

        # NetworkPolicies
        try:
            np = self.net_v1.list_namespaced_network_policy(namespace)
            result['networkpolicies'] = [n.metadata.name for n in np.items]
        except Exception:
            result['networkpolicies'] = []

        return result

    def _parse_resource(self, value):
        if not value:
            return 0
        if isinstance(value, (int, float)):
            return value
        val = str(value)
        if val.endswith('m'):
            return int(val[:-1]) / 1000
        if val.endswith('Mi'):
            return int(val[:-2])
        if val.endswith('Gi'):
            return int(val[:-2]) * 1024
        if val.endswith('Ki'):
            return int(val[:-2]) / 1024
        try:
            return int(val)
        except ValueError:
            return 0

    def _format_age(self, dt):
        if not dt:
            return ''
        now = datetime.now(timezone.utc)
        delta = now - dt.replace(tzinfo=timezone.utc)
        if delta.days > 0:
            return f"{delta.days}d"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h"
        mins = delta.seconds // 60
        return f"{mins}m"

    def generate_kubeconfig(self, namespace, role):
        if role not in ('dev', 'view'):
            raise ValueError("Role must be 'dev' or 'view'")

        sa_name = 'dev-user' if role == 'dev' else 'view-user'
        context_name = f"{role}-context"

        with open(self.kubeconfig_path) as f:
            kubeconfig = yaml.safe_load(f)

        current_ctx = next(c for c in kubeconfig['contexts']
                          if c['name'] == kubeconfig['current-context'])
        cluster_name = current_ctx['context']['cluster']
        cluster = next(c for c in kubeconfig['clusters'] if c['name'] == cluster_name)

        server = cluster['cluster']['server']
        ca_data = cluster['cluster'].get('certificate-authority-data', '')

        # Generate token using kubectl (most reliable across K3s versions)
        result = subprocess.run(
            ['kubectl', 'create', 'token', sa_name, '-n', namespace, '--duration=8760h'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create token: {result.stderr}")
        token = result.stdout.strip()

        new_kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{
                'name': cluster_name,
                'cluster': {
                    'server': server,
                    'certificate-authority-data': ca_data
                }
            }],
            'users': [{
                'name': sa_name,
                'user': {'token': token}
            }],
            'contexts': [{
                'name': context_name,
                'context': {
                    'cluster': cluster_name,
                    'user': sa_name,
                    'namespace': namespace
                }
            }],
            'current-context': context_name
        }

        return yaml.dump(new_kubeconfig, default_flow_style=False)


# Singleton instance
k8s = K8sClient()
