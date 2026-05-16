import os
import subprocess
import shutil
import yaml
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone
from kubernetes import client, config
from config import (
    SYSTEM_NAMESPACES,
    USER_NAMESPACE,
    GRAFANA_INTERNAL_BASE_URL,
    PROMETHEUS_INTERNAL_BASE_URL,
    PORTAL_METRICS_BASE_URL
)


class K8sClient:
    def __init__(self):
        self.kubeconfig_path = os.environ.get('KUBECONFIG', os.path.expanduser('~/.kube/config'))
        self.connected = False
        self.v1 = None
        self.rbac_v1 = None
        self.apps_v1 = None
        self.net_v1 = None
        self.custom_api = None
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
            self.custom_api = client.CustomObjectsApi()
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
        if not shutil.which('kubectl'):
            raise RuntimeError(
                'kubectl is not available in the portal container. '
                'Set HOST_KUBECTL_PATH to a valid host kubectl binary or configure KUBECTL_BASE_URL, then rebuild/restart.'
            )

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

    def delete_tenant(self, name, force=False):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')
        if name in SYSTEM_NAMESPACES:
            raise ValueError("Cannot delete system namespace")

        # Check if namespace still exists
        try:
            ns = self.v1.read_namespace(name=name)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                ns = None
            else:
                raise

        if ns is None:
            # Already gone, just clean up service accounts
            for suffix in ('admin', 'dev', 'view'):
                try:
                    self.v1.delete_namespaced_service_account(
                        name=f"{name}-{suffix}",
                        namespace=USER_NAMESPACE
                    )
                except Exception:
                    pass
            return f"Namespace {name} was already deleted"

        if force:
            # Aggressively delete namespace contents first so that PVCs,
            # pods, and other namespaced resources don't block termination.
            if shutil.which('kubectl'):
                resource_types = [
                    'pods', 'deployments', 'replicasets', 'statefulsets',
                    'daemonsets', 'jobs', 'services',
                    'persistentvolumeclaims', 'configmaps', 'secrets',
                    'serviceaccounts', 'resourcequotas', 'limitranges',
                    'networkpolicies', 'roles', 'rolebindings'
                ]
                for rt in resource_types:
                    try:
                        cmd = [
                            'kubectl', 'delete', rt, '--all',
                            '-n', name,
                            '--grace-period=0', '--force',
                            '--wait=false'
                        ]
                        subprocess.run(
                            cmd, capture_output=True, text=True, timeout=10
                        )
                    except Exception:
                        pass

            # Remove namespace-level finalizers via the finalize sub-resource
            if ns.metadata.finalizers:
                ns.metadata.finalizers = []
                try:
                    self.v1.replace_namespace_finalize(name=name, body=ns)
                except client.exceptions.ApiException:
                    pass

        self.v1.delete_namespace(name=name)

        if force:
            # If namespace is still present, try clearing finalizers once more
            try:
                ns = self.v1.read_namespace(name=name)
                if ns and ns.metadata.finalizers:
                    ns.metadata.finalizers = []
                    self.v1.replace_namespace_finalize(name=name, body=ns)
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    pass

        for suffix in ('admin', 'dev', 'view'):
            try:
                self.v1.delete_namespaced_service_account(
                    name=f"{name}-{suffix}",
                    namespace=USER_NAMESPACE
                )
            except Exception:
                pass
        return f"Namespace {name} deleted"

    def apply_quota_overrides(self, namespace, quota_overrides):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')
        if not quota_overrides:
            return

        hard = {k: str(v) for k, v in quota_overrides.items() if v is not None}
        try:
            quota = self.v1.read_namespaced_resource_quota(name='team-quota', namespace=namespace)
            current_hard = dict(quota.spec.hard) if quota.spec.hard else {}
            current_hard.update(hard)
            quota.spec.hard = current_hard
            self.v1.replace_namespaced_resource_quota(name='team-quota', namespace=namespace, body=quota)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                body = client.V1ResourceQuota(
                    metadata=client.V1ObjectMeta(name='team-quota', namespace=namespace),
                    spec=client.V1ResourceQuotaSpec(hard=hard)
                )
                self.v1.create_namespaced_resource_quota(namespace=namespace, body=body)
            else:
                raise

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

    def get_namespace_live_usage(self, namespace):
        if not self.connected:
            return {'error': 'Not connected to Kubernetes'}

        result = {
            'namespace': namespace,
            'metrics_available': False,
            'pods': [],
            'totals': {
                'cpu_cores': 0,
                'cpu_millicores': 0,
                'memory_mib': 0
            },
            'quota': self._namespace_quota_summary(namespace)
        }

        try:
            pod_metrics = self.custom_api.list_namespaced_custom_object(
                group='metrics.k8s.io',
                version='v1beta1',
                namespace=namespace,
                plural='pods'
            )
        except Exception as e:
            result['metrics_error'] = (
                'Kubernetes Metrics API is not available. Install metrics-server '
                f'or wait for it to publish pod metrics. Detail: {e}'
            )
            return result

        result['metrics_available'] = True
        for item in pod_metrics.get('items', []):
            pod_cpu = 0
            pod_memory = 0
            containers = []
            for c in item.get('containers', []):
                usage = c.get('usage') or {}
                cpu_cores = self._parse_cpu_cores(usage.get('cpu'))
                memory_mib = self._parse_memory_mib(usage.get('memory'))
                pod_cpu += cpu_cores
                pod_memory += memory_mib
                containers.append({
                    'name': c.get('name', ''),
                    'cpu_cores': cpu_cores,
                    'cpu_millicores': round(cpu_cores * 1000, 3),
                    'memory_mib': round(memory_mib, 3)
                })

            result['pods'].append({
                'name': item.get('metadata', {}).get('name', ''),
                'timestamp': item.get('timestamp', ''),
                'cpu_cores': pod_cpu,
                'cpu_millicores': round(pod_cpu * 1000, 3),
                'memory_mib': round(pod_memory, 3),
                'containers': containers
            })
            result['totals']['cpu_cores'] += pod_cpu
            result['totals']['memory_mib'] += pod_memory

        result['pods'] = sorted(result['pods'], key=lambda p: p['name'])
        result['totals']['cpu_cores'] = round(result['totals']['cpu_cores'], 6)
        result['totals']['cpu_millicores'] = round(result['totals']['cpu_cores'] * 1000, 3)
        result['totals']['memory_mib'] = round(result['totals']['memory_mib'], 3)
        self._add_usage_percentages(result)
        return result

    def get_monitoring_diagnostics(self, namespace):
        diagnostics = {
            'namespace': namespace,
            'kubernetes_connected': self.is_connected(),
            'grafana': self._check_http_endpoint(f'{GRAFANA_INTERNAL_BASE_URL}/api/health'),
            'prometheus': self._check_http_endpoint(f'{PROMETHEUS_INTERNAL_BASE_URL}/-/healthy'),
            'portal_metrics': self._check_http_endpoint(f'{PORTAL_METRICS_BASE_URL}/metrics')
        }

        metrics_usage = self.get_namespace_live_usage(namespace)
        diagnostics['metrics_api'] = {
            'available': metrics_usage.get('metrics_available', False),
            'error': metrics_usage.get('metrics_error')
        }

        return diagnostics

    # ── Node-level USE (Utilization / Saturation / Errors) ──────────

    def get_nodes_info(self):
        """Return per-node capacity, allocatable, conditions and pod count.

        Used by the Prometheus collector to expose USE-method node metrics.
        """
        if not self.connected:
            return {'error': 'Not connected to Kubernetes'}

        result = []
        try:
            nodes = self.v1.list_node()
        except Exception as e:
            return {'error': str(e)}

        for n in nodes.items:
            node = {
                'name': n.metadata.name,
                'capacity_cpu_cores': 0.0,
                'capacity_memory_mib': 0.0,
                'capacity_pods': 0,
                'allocatable_cpu_cores': 0.0,
                'allocatable_memory_mib': 0.0,
                'allocatable_pods': 0,
                'conditions': {},
                'pods_on_node': 0
            }

            if n.status.capacity:
                node['capacity_cpu_cores'] = self._parse_cpu_cores(n.status.capacity.get('cpu'))
                node['capacity_memory_mib'] = self._parse_memory_mib(n.status.capacity.get('memory'))
                try:
                    node['capacity_pods'] = int(n.status.capacity.get('pods', '0'))
                except (ValueError, TypeError):
                    node['capacity_pods'] = 0

            if n.status.allocatable:
                node['allocatable_cpu_cores'] = self._parse_cpu_cores(n.status.allocatable.get('cpu'))
                node['allocatable_memory_mib'] = self._parse_memory_mib(n.status.allocatable.get('memory'))
                try:
                    node['allocatable_pods'] = int(n.status.allocatable.get('pods', '0'))
                except (ValueError, TypeError):
                    node['allocatable_pods'] = 0

            if n.status.conditions:
                for c in n.status.conditions:
                    # Ready should be True; MemoryPressure, DiskPressure, PIDPressure should be False
                    node['conditions'][c.type] = 1 if c.status == 'True' else 0

            try:
                pods = self.v1.list_pod_for_all_namespaces(field_selector=f'spec.nodeName={node["name"]}')
                node['pods_on_node'] = len(pods.items)
            except Exception:
                node['pods_on_node'] = 0

            result.append(node)

        return result

    def get_node_metrics(self):
        """Return live node CPU/memory usage from the Kubernetes Metrics API.

        Requires metrics-server to be running in the cluster.
        """
        if not self.connected:
            return {'error': 'Not connected to Kubernetes', 'metrics_available': False}

        try:
            raw = self.custom_api.list_cluster_custom_object(
                group='metrics.k8s.io',
                version='v1beta1',
                plural='nodes'
            )
        except Exception as e:
            return {'error': str(e), 'metrics_available': False}

        nodes = []
        for item in raw.get('items', []):
            usage = item.get('usage') or {}
            cpu_cores = self._parse_cpu_cores(usage.get('cpu', '0'))
            memory_mib = self._parse_memory_mib(usage.get('memory', '0'))
            nodes.append({
                'name': item.get('metadata', {}).get('name', ''),
                'cpu_cores': cpu_cores,
                'cpu_millicores': round(cpu_cores * 1000, 3),
                'memory_mib': round(memory_mib, 3)
            })

        return {'metrics_available': True, 'nodes': nodes}

    def _namespace_quota_summary(self, namespace):
        summary = {'hard': {}, 'used': {}}
        try:
            quotas = self.v1.list_namespaced_resource_quota(namespace)
            for q in quotas.items:
                hard = q.spec.hard or {}
                used = (q.status.used if q.status else {}) or {}
                summary['hard'].update(dict(hard))
                summary['used'].update(dict(used))
        except Exception:
            pass
        return summary

    def _add_usage_percentages(self, usage):
        hard = usage.get('quota', {}).get('hard', {})
        cpu_limit = self._parse_cpu_cores(hard.get('limits.cpu') or hard.get('requests.cpu'))
        memory_limit = self._parse_memory_mib(hard.get('limits.memory') or hard.get('requests.memory'))

        usage['totals']['cpu_percent'] = (
            round(usage['totals']['cpu_cores'] / cpu_limit * 100, 2)
            if cpu_limit > 0 else None
        )
        usage['totals']['memory_percent'] = (
            round(usage['totals']['memory_mib'] / memory_limit * 100, 2)
            if memory_limit > 0 else None
        )
        usage['limits'] = {
            'cpu_cores': cpu_limit,
            'cpu_millicores': round(cpu_limit * 1000, 3),
            'memory_mib': round(memory_limit, 3)
        }

    def _check_http_endpoint(self, url):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return {
                    'reachable': True,
                    'status': response.getcode()
                }
        except urllib.error.HTTPError as e:
            return {
                'reachable': False,
                'status': e.code,
                'error': str(e)
            }
        except Exception as e:
            return {
                'reachable': False,
                'error': str(e)
            }

    def _demo_workload_name(self, owner):
        safe_owner = ''.join(c if c.isalnum() else '-' for c in owner.lower()).strip('-')
        if not safe_owner:
            safe_owner = 'user'
        return f'lab-demo-{safe_owner[:40]}'

    def create_demo_workload(self, namespace, owner):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        name = self._demo_workload_name(owner)
        labels = {
            'app': 'lab-demo',
            'tenant.lab/demo-owner': owner
        }
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels=labels
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(containers=[
                        client.V1Container(
                            name='nginx',
                            image='nginx:alpine',
                            ports=[client.V1ContainerPort(container_port=80)]
                        )
                    ])
                )
            )
        )
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels=labels
            ),
            spec=client.V1ServiceSpec(
                selector=labels,
                ports=[client.V1ServicePort(port=80, target_port=80)]
            )
        )

        try:
            self.apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        try:
            self.v1.create_namespaced_service(namespace=namespace, body=service)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        return f'Demo workload {name} Deployment and Service are present.'

    def create_custom_pod(self, namespace, spec, owner):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        name = self._required_dns_label(spec.get('name'), 'Pod name')
        image = (spec.get('image') or '').strip()
        if not image:
            raise ValueError('Container image is required.')

        if self._pod_exists(namespace, name):
            raise ValueError(f'Pod {name} already exists in namespace {namespace}. Choose a different name.')

        container_name = self._required_dns_label(spec.get('container_name') or name, 'Container name')
        labels = self._custom_pod_labels(spec.get('labels'), owner)
        env = self._custom_pod_env(spec.get('env'))
        ports = self._custom_pod_ports(spec.get('ports'))
        resources = self._custom_pod_resources(spec.get('resources') or {})
        command = self._string_list(spec.get('command'))
        args = self._string_list(spec.get('args'))

        container_kwargs = {
            'name': container_name,
            'image': image,
            'image_pull_policy': spec.get('image_pull_policy') or 'IfNotPresent',
            'env': env,
            'ports': ports,
            'resources': resources
        }
        if command:
            container_kwargs['command'] = command
        if args:
            container_kwargs['args'] = args

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels=labels,
                annotations={
                    'tenant.lab/created-by-role': owner or 'unknown',
                    'tenant.lab/created-by': 'web-portal'
                }
            ),
            spec=client.V1PodSpec(
                restart_policy=spec.get('restart_policy') or 'Always',
                containers=[client.V1Container(**container_kwargs)]
            )
        )

        try:
            created = self.v1.create_namespaced_pod(namespace=namespace, body=pod)
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        return self._pod_summary(created)

    def delete_demo_workload(self, namespace, owner):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        name = self._demo_workload_name(owner)
        deleted = []
        try:
            self.apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
            deleted.append('Deployment')
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        try:
            self.v1.delete_namespaced_service(name=name, namespace=namespace)
            deleted.append('Service')
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        if not deleted:
            return f'Demo workload {name} was already absent.'
        return f"Deleted demo workload {name} {' and '.join(deleted)}."

    def _required_dns_label(self, value, label):
        value = (value or '').strip().lower()
        if not value:
            raise ValueError(f'{label} is required.')
        if len(value) > 63:
            raise ValueError(f'{label} must be 63 characters or fewer.')
        if not self._is_dns_label(value):
            raise ValueError(f'{label} must use lowercase letters, numbers, and hyphens, with no leading or trailing hyphen.')
        return value

    def _is_dns_label(self, value):
        if not value:
            return False
        if value[0] == '-' or value[-1] == '-':
            return False
        return all(c.islower() or c.isdigit() or c == '-' for c in value)

    def _custom_pod_labels(self, labels, owner):
        result = {
            'app': 'custom-pod',
            'tenant.lab/managed-by': 'web-portal',
            'tenant.lab/owner-role': owner or 'unknown'
        }
        for item in labels or []:
            key = (item.get('key') or '').strip()
            value = (item.get('value') or '').strip()
            if key and value:
                result[key] = value
        return result

    def _custom_pod_env(self, env):
        result = []
        for item in env or []:
            name = (item.get('name') or '').strip()
            value = item.get('value')
            if name:
                result.append(client.V1EnvVar(name=name, value='' if value is None else str(value)))
        return result

    def _custom_pod_ports(self, ports):
        result = []
        for item in ports or []:
            raw_port = item.get('container_port')
            if raw_port in (None, ''):
                continue
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                raise ValueError('Container ports must be numbers.')
            if port < 1 or port > 65535:
                raise ValueError('Container ports must be between 1 and 65535.')
            result.append(client.V1ContainerPort(
                name=(item.get('name') or None),
                container_port=port,
                protocol=item.get('protocol') or 'TCP'
            ))
        return result

    def _custom_pod_resources(self, resources):
        requests = self._compact_resource_map(resources.get('requests') or {})
        limits = self._compact_resource_map(resources.get('limits') or {})
        if not requests and not limits:
            return None
        return client.V1ResourceRequirements(requests=requests or None, limits=limits or None)

    def _compact_resource_map(self, values):
        allowed = {'cpu', 'memory', 'ephemeral-storage'}
        return {
            key: str(value).strip()
            for key, value in values.items()
            if key in allowed and str(value).strip()
        }

    def _string_list(self, value):
        if not value:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.splitlines() if part.strip()]
        return [str(part).strip() for part in value if str(part).strip()]

    def _pod_exists(self, namespace, name):
        try:
            self.v1.read_namespaced_pod(name=name, namespace=namespace)
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            raise RuntimeError(self._format_api_exception(e))

    def _pod_summary(self, pod):
        return {
            'name': pod.metadata.name,
            'namespace': pod.metadata.namespace,
            'status': pod.status.phase if pod.status else 'Pending'
        }

    def _format_api_exception(self, exc):
        message = exc.reason or 'Kubernetes API error'
        try:
            body = json.loads(exc.body or '{}')
            if body.get('message'):
                message = body['message']
        except (TypeError, ValueError):
            if exc.body:
                message = exc.body
        if exc.status:
            return f'Kubernetes API rejected the Pod ({exc.status}): {message}'
        return message

    def permission_checks(self, namespace, portal_role):
        from config import ROLE_PERMISSIONS

        suffix_by_role = {
            'cluster-admin': 'admin',
            'admin': 'admin',
            'developer': 'dev',
            'viewer': 'view'
        }
        suffix = suffix_by_role.get(portal_role)
        if not suffix:
            raise ValueError('Unknown portal role')

        subject = f'system:serviceaccount:{USER_NAMESPACE}:{namespace}-{suffix}'
        role_info = ROLE_PERMISSIONS.get(portal_role, {})

        can_read = set(role_info.get('can_read', []))
        can_write = set(role_info.get('can_write', []))
        can_exec = role_info.get('can_exec', False)

        def is_allowed(resource, verb='read'):
            """Check if the role is allowed based on ROLE_PERMISSIONS config."""
            if portal_role in ('cluster-admin', 'admin'):
                # cluster-admin can do everything including delete
                # admin (tenant admin) cannot delete namespaces
                if verb == 'delete':
                    return portal_role == 'cluster-admin'
                return True
            if verb in ('read', 'list', 'get'):
                return resource in can_read
            if verb in ('write', 'create', 'update'):
                return resource in can_write
            if verb == 'exec':
                return can_exec
            if verb == 'delete':
                return False
            return False

        checks = [
            {'label': 'List pods',                    'allowed': is_allowed('pods')},
            {'label': 'Create deployments',           'allowed': is_allowed('deployments', 'create')},
            {'label': 'Create deployments in another namespace', 'allowed': False},
            {'label': 'Read secrets',                 'allowed': portal_role in ('cluster-admin', 'admin')},
            {'label': 'Read ResourceQuota',           'allowed': is_allowed('resourcequotas')},
            {'label': 'Modify ResourceQuota',         'allowed': portal_role in ('cluster-admin', 'admin')},
            {'label': 'Modify NetworkPolicy',         'allowed': portal_role in ('cluster-admin', 'admin')},
            {'label': 'Modify RBAC roles',            'allowed': portal_role in ('cluster-admin', 'admin')},
            {'label': 'Exec into pods',               'allowed': portal_role in ('cluster-admin', 'admin') or can_exec},
            {'label': 'Delete namespaces',            'allowed': is_allowed('namespaces', 'delete')},
        ]

        results = []
        for check in checks:
            results.append({
                'label': check['label'],
                'allowed': check['allowed'],
                'expected': check['allowed'],
                'command': f'kubectl auth can-i --as {subject} ...'
            })

        return {
            'namespace': namespace,
            'role': portal_role,
            'subject': subject,
            'checks': results
        }

    def get_resource_settings(self, namespace):
        if not self.connected:
            return {'error': 'Not connected to Kubernetes'}

        resources = self.get_namespace_resources(namespace)
        if resources.get('error'):
            return resources

        quota = resources.get('quota') or {}
        hard = quota.get('hard') or {}
        limitrange = resources.get('limitrange') or {}
        limits = limitrange.get('limits') or []

        container_limit = self._find_limit(limits, 'Container')
        pod_limit = self._find_limit(limits, 'Pod')
        pvc_limit = self._find_limit(limits, 'PersistentVolumeClaim')

        return {
            'namespace': namespace,
            'quota_name': quota.get('name', 'team-quota'),
            'limitrange_name': limitrange.get('name', 'mem-cpu-limit'),
            'quota': {
                'requests_cpu': hard.get('requests.cpu', '2'),
                'requests_memory': hard.get('requests.memory', '4Gi'),
                'limits_cpu': hard.get('limits.cpu', '4'),
                'limits_memory': hard.get('limits.memory', '8Gi'),
                'pods': hard.get('pods', '20'),
                'services': hard.get('services', '10'),
                'persistentvolumeclaims': hard.get('persistentvolumeclaims', '5'),
                'requests_storage': hard.get('requests.storage', '20Gi')
            },
            'limits': {
                'container_default_cpu': self._resource_value(container_limit, 'default', 'cpu', '500m'),
                'container_default_memory': self._resource_value(container_limit, 'default', 'memory', '1Gi'),
                'container_request_cpu': self._resource_value(container_limit, 'defaultRequest', 'cpu', '200m'),
                'container_request_memory': self._resource_value(container_limit, 'defaultRequest', 'memory', '256Mi'),
                'container_max_cpu': self._resource_value(container_limit, 'max', 'cpu', '2'),
                'container_max_memory': self._resource_value(container_limit, 'max', 'memory', '4Gi'),
                'pod_max_cpu': self._resource_value(pod_limit, 'max', 'cpu', '2'),
                'pod_max_memory': self._resource_value(pod_limit, 'max', 'memory', '4Gi'),
                'pvc_min_storage': self._resource_value(pvc_limit, 'min', 'storage', '1Gi'),
                'pvc_max_storage': self._resource_value(pvc_limit, 'max', 'storage', '10Gi')
            }
        }

    def update_resource_settings(self, namespace, settings):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        quota_name = settings.get('quota_name') or 'team-quota'
        limitrange_name = settings.get('limitrange_name') or 'mem-cpu-limit'
        quota = settings.get('quota') or {}
        limits = settings.get('limits') or {}

        hard = {
            'requests.cpu': quota.get('requests_cpu', '2'),
            'requests.memory': quota.get('requests_memory', '4Gi'),
            'requests.ephemeral-storage': quota.get('requests_ephemeral_storage', '8Gi'),
            'limits.cpu': quota.get('limits_cpu', '4'),
            'limits.memory': quota.get('limits_memory', '8Gi'),
            'limits.ephemeral-storage': quota.get('limits_ephemeral_storage', '16Gi'),
            'requests.storage': quota.get('requests_storage', '20Gi'),
            'persistentvolumeclaims': quota.get('persistentvolumeclaims', '5'),
            'pods': quota.get('pods', '20'),
            'services': quota.get('services', '10'),
            'services.nodeports': quota.get('services_nodeports', '0'),
            'services.loadbalancers': quota.get('services_loadbalancers', '0'),
            'configmaps': quota.get('configmaps', '20'),
            'secrets': quota.get('secrets', '20'),
            'count/deployments.apps': quota.get('count_deployments_apps', '10'),
            'count/replicasets.apps': quota.get('count_replicasets_apps', '20'),
            'count/statefulsets.apps': quota.get('count_statefulsets_apps', '3'),
            'count/jobs.batch': quota.get('count_jobs_batch', '10'),
            'count/cronjobs.batch': quota.get('count_cronjobs_batch', '5'),
            'count/ingresses.networking.k8s.io': quota.get('count_ingresses_networking_k8s_io', '5')
        }

        limit_spec = [
            {
                'type': 'Container',
                'default': {
                    'cpu': limits.get('container_default_cpu', '500m'),
                    'memory': limits.get('container_default_memory', '1Gi'),
                    'ephemeral-storage': limits.get('container_default_ephemeral_storage', '1Gi')
                },
                'defaultRequest': {
                    'cpu': limits.get('container_request_cpu', '200m'),
                    'memory': limits.get('container_request_memory', '256Mi'),
                    'ephemeral-storage': limits.get('container_request_ephemeral_storage', '512Mi')
                },
                'max': {
                    'cpu': limits.get('container_max_cpu', '2'),
                    'memory': limits.get('container_max_memory', '4Gi'),
                    'ephemeral-storage': limits.get('container_max_ephemeral_storage', '4Gi')
                },
                'min': {
                    'cpu': limits.get('container_min_cpu', '50m'),
                    'memory': limits.get('container_min_memory', '64Mi'),
                    'ephemeral-storage': limits.get('container_min_ephemeral_storage', '128Mi')
                },
                'maxLimitRequestRatio': {
                    'cpu': limits.get('container_ratio_cpu', '4'),
                    'memory': limits.get('container_ratio_memory', '4')
                }
            },
            {
                'type': 'Pod',
                'max': {
                    'cpu': limits.get('pod_max_cpu', '2'),
                    'memory': limits.get('pod_max_memory', '4Gi'),
                    'ephemeral-storage': limits.get('pod_max_ephemeral_storage', '6Gi')
                },
                'min': {
                    'cpu': limits.get('pod_min_cpu', '50m'),
                    'memory': limits.get('pod_min_memory', '64Mi'),
                    'ephemeral-storage': limits.get('pod_min_ephemeral_storage', '128Mi')
                }
            },
            {
                'type': 'PersistentVolumeClaim',
                'max': {'storage': limits.get('pvc_max_storage', '10Gi')},
                'min': {'storage': limits.get('pvc_min_storage', '1Gi')}
            }
        ]

        quota_body = {
            'apiVersion': 'v1',
            'kind': 'ResourceQuota',
            'metadata': {
                'name': quota_name,
                'namespace': namespace,
                'labels': {'tenant.lab/control': 'resource-quota'},
                'annotations': {
                    'tenant.lab/purpose': 'Cap total namespace usage so one team cannot exhaust shared cluster capacity.'
                }
            },
            'spec': {'hard': hard}
        }
        limit_body = {
            'apiVersion': 'v1',
            'kind': 'LimitRange',
            'metadata': {
                'name': limitrange_name,
                'namespace': namespace,
                'labels': {'tenant.lab/control': 'limit-range'},
                'annotations': {
                    'tenant.lab/purpose': 'Apply safe defaults and per-object bounds so tenant workloads are schedulable and predictable.'
                }
            },
            'spec': {'limits': limit_spec}
        }

        self._replace_or_create_resource_quota(namespace, quota_name, quota_body)
        self._replace_or_create_limit_range(namespace, limitrange_name, limit_body)
        return f'ResourceQuota {quota_name} and LimitRange {limitrange_name} were updated in {namespace}.'

    def _replace_or_create_resource_quota(self, namespace, name, body):
        try:
            self.v1.replace_namespaced_resource_quota(name=name, namespace=namespace, body=body)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.v1.create_namespaced_resource_quota(namespace=namespace, body=body)
            else:
                raise

    def _replace_or_create_limit_range(self, namespace, name, body):
        try:
            self.v1.replace_namespaced_limit_range(name=name, namespace=namespace, body=body)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.v1.create_namespaced_limit_range(namespace=namespace, body=body)
            else:
                raise

    def _find_limit(self, limits, limit_type):
        for limit in limits:
            if limit.get('type') == limit_type:
                return limit
        return {}

    def _resource_value(self, limit, field, resource, default):
        snake_field = ''.join(['_' + c.lower() if c.isupper() else c for c in field]).lstrip('_')
        values = limit.get(field) or limit.get(snake_field) or {}
        return values.get(resource, default)

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

    def _parse_cpu_cores(self, value):
        if not value:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        val = str(value).strip()
        suffixes = {
            'n': 1_000_000_000,
            'u': 1_000_000,
            'm': 1_000
        }
        for suffix, divisor in suffixes.items():
            if val.endswith(suffix):
                try:
                    return float(val[:-len(suffix)]) / divisor
                except ValueError:
                    return 0.0
        try:
            return float(val)
        except ValueError:
            return 0.0

    def _parse_memory_mib(self, value):
        if not value:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value) / (1024 * 1024)
        val = str(value).strip()
        suffixes = {
            'Ki': 1 / 1024,
            'Mi': 1,
            'Gi': 1024,
            'Ti': 1024 * 1024,
            'K': 1000 / (1024 * 1024),
            'M': 1000 * 1000 / (1024 * 1024),
            'G': 1000 * 1000 * 1000 / (1024 * 1024)
        }
        for suffix, multiplier in suffixes.items():
            if val.endswith(suffix):
                try:
                    return float(val[:-len(suffix)]) * multiplier
                except ValueError:
                    return 0.0
        try:
            return float(val) / (1024 * 1024)
        except ValueError:
            return 0.0

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
        if not shutil.which('kubectl'):
            raise RuntimeError(
                'kubectl is not available in the portal container. '
                'Set HOST_KUBECTL_PATH to a valid host kubectl binary or configure KUBECTL_BASE_URL, then rebuild/restart.'
            )

        service_accounts = {
            'admin': f'{namespace}-admin',
            'dev': f'{namespace}-dev',
            'view': f'{namespace}-view'
        }
        if role not in service_accounts:
            raise ValueError("Role must be 'admin', 'dev', or 'view'")

        sa_name = service_accounts[role]
        context_name = f"{namespace}-{role}"

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
            ['kubectl', 'create', 'token', sa_name, '-n', USER_NAMESPACE, '--duration=8760h'],
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
