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
        import random
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

        password = str(random.randint(100000, 999999))
        try:
            ns = self.v1.read_namespace(name=name)
            if ns.metadata.annotations:
                ns.metadata.annotations['tenant.lab/password'] = password
            else:
                ns.metadata.annotations = {'tenant.lab/password': password}
            self.v1.replace_namespace(name=name, body=ns)
        except Exception as e:
            raise RuntimeError(f"Namespace created but failed to set password: {e}")

        return {'output': result.stdout, 'password': password}

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
            'portal_metrics': self._check_http_endpoint(f'{PORTAL_METRICS_BASE_URL}/metrics'),
            'warnings': []
        }

        metrics_usage = self.get_namespace_live_usage(namespace)
        diagnostics['metrics_api'] = {
            'available': metrics_usage.get('metrics_available', False),
            'error': metrics_usage.get('metrics_error')
        }

        diagnostics['node_health'] = self._cluster_node_health()
        diagnostics['network_plugins'] = self._network_plugin_health()
        diagnostics['system_namespaces'] = self._system_namespace_health()
        diagnostics['recent_cluster_warnings'] = self._recent_cluster_warnings(namespace)
        diagnostics['repair_flow'] = self._monitoring_repair_flow(diagnostics)

        if not diagnostics['network_plugins'].get('ready', False):
            diagnostics['warnings'].append('Cluster network plugin is not ready.')
        if diagnostics['recent_cluster_warnings'].get('cni_not_initialized'):
            diagnostics['warnings'].append('Recent events show CNI is not initialized.')
        if diagnostics['recent_cluster_warnings'].get('projected_volume_errors'):
            diagnostics['warnings'].append('Recent events show projected volume or kube-root-ca mount failures.')
        if not diagnostics['node_health'].get('ready', True):
            diagnostics['warnings'].append('At least one node is not Ready.')

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
                    spec=client.V1PodSpec(
                        tolerations=self._default_workload_tolerations(),
                        containers=[
                            client.V1Container(
                                name='nginx',
                                image='nginx:alpine',
                                ports=[client.V1ContainerPort(container_port=80)],
                                resources=client.V1ResourceRequirements(
                                    requests={'cpu': '100m', 'memory': '128Mi'}
                                )
                            )
                        ]
                    )
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

        # Create HPA for the demo workload
        if shutil.which('kubectl'):
            subprocess.run(
                ['kubectl', 'autoscale', 'deployment', name,
                 '-n', namespace, '--cpu-percent=50', '--min=1', '--max=5'],
                capture_output=True, text=True
            )

        return f'Demo workload {name} Deployment and Service are present.'

    def create_custom_pod(self, namespace, spec, owner):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        self._ensure_namespace_exists(namespace)
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
                tolerations=self._custom_pod_tolerations(spec.get('tolerations')),
                containers=[client.V1Container(**container_kwargs)]
            )
        )

        try:
            created = self.v1.create_namespaced_pod(namespace=namespace, body=pod)
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        confirmed = self._wait_for_pod(namespace, name)
        return self._pod_summary(confirmed or created)

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

        if shutil.which('kubectl'):
            subprocess.run(
                ['kubectl', 'delete', 'hpa', name, '-n', namespace],
                capture_output=True, text=True
            )

        if not deleted:
            return f'Demo workload {name} was already absent.'
        return f"Deleted demo workload {name} {' and '.join(deleted)}."

    def delete_pod(self, namespace, name):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        self._ensure_namespace_exists(namespace)
        name = self._required_dns_label(name, 'Pod name')
        try:
            self.v1.delete_namespaced_pod(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0)
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise ValueError(f'Pod {name} does not exist in namespace {namespace}.')
            raise RuntimeError(self._format_api_exception(e))

        deleted = self._wait_for_pod_deleted(namespace, name)
        if not deleted:
            return f'Pod {name} deletion requested in namespace {namespace}, but it is still terminating.'
        return f'Pod {name} deleted from namespace {namespace}.'

    def diagnose_pod(self, namespace, name):
        if not self.connected:
            raise RuntimeError('Not connected to Kubernetes')

        self._ensure_namespace_exists(namespace)
        name = self._required_dns_label(name, 'Pod name')
        try:
            pod = self.v1.read_namespaced_pod(name=name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise ValueError(f'Pod {name} does not exist in namespace {namespace}.')
            raise RuntimeError(self._format_api_exception(e))

        events = self._pod_events(namespace, name, pod.metadata.uid)
        node_taints = self._schedulable_node_taints()
        unmatched_taints = self._unmatched_taints(pod.spec.tolerations or [], node_taints)
        cluster_health = self._pod_cluster_health(pod, events)
        diagnosis = self._infer_pod_diagnosis(pod, events, unmatched_taints, cluster_health)
        containers = [self._container_diagnostic(c) for c in (pod.status.container_statuses or [])]
        init_containers = [self._container_diagnostic(c) for c in (pod.status.init_container_statuses or [])]

        commands = [
            f'kubectl get pod {name} -n {namespace} -o wide',
            f'kubectl describe pod {name} -n {namespace}',
            f'kubectl get events -n {namespace} --field-selector involvedObject.name={name} --sort-by=.lastTimestamp'
        ]
        if pod.spec.node_name:
            commands.append(f'kubectl describe node {pod.spec.node_name}')
        commands.extend([
            f'kubectl get resourcequota,limitrange -n {namespace}',
            f'kubectl logs {name} -n {namespace} --all-containers --tail=80'
        ])

        return {
            'pod': {
                'name': pod.metadata.name,
                'namespace': pod.metadata.namespace,
                'phase': pod.status.phase if pod.status else 'Unknown',
                'node': pod.spec.node_name or '',
                'pod_ip': pod.status.pod_ip or '',
                'host_ip': pod.status.host_ip or '',
                'qos_class': pod.status.qos_class or '',
                'start_time': pod.status.start_time.isoformat() if pod.status and pod.status.start_time else '',
                'age': self._format_age(pod.metadata.creation_timestamp),
                'created_at': pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else '',
                'tolerations': [self._toleration_summary(t) for t in (pod.spec.tolerations or [])]
            },
            'node_taints': node_taints,
            'unmatched_taints': unmatched_taints,
            'cluster_health': cluster_health,
            'diagnosis': diagnosis,
            'conditions': [self._pod_condition_diagnostic(c) for c in (pod.status.conditions or [])],
            'containers': containers,
            'init_containers': init_containers,
            'events': events,
            'commands': commands
        }

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

    def _default_workload_tolerations(self):
        return [
            client.V1Toleration(
                operator='Exists',
                effect='NoSchedule'
            ),
            client.V1Toleration(
                operator='Exists',
                effect='PreferNoSchedule'
            ),
            client.V1Toleration(
                key='node-role.kubernetes.io/control-plane',
                operator='Exists',
                effect='NoSchedule'
            ),
            client.V1Toleration(
                key='node-role.kubernetes.io/master',
                operator='Exists',
                effect='NoSchedule'
            )
        ]

    def _custom_pod_tolerations(self, tolerations):
        result = self._default_workload_tolerations()
        for item in tolerations or []:
            key = (item.get('key') or '').strip()
            operator = (item.get('operator') or 'Exists').strip()
            effect = (item.get('effect') or 'NoSchedule').strip()
            value = (item.get('value') or '').strip()
            if not key:
                continue
            result.append(client.V1Toleration(
                key=key,
                operator=operator,
                value=value or None,
                effect=effect or None
            ))
        return result

    def _pod_exists(self, namespace, name):
        try:
            self.v1.read_namespaced_pod(name=name, namespace=namespace)
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            raise RuntimeError(self._format_api_exception(e))

    def _ensure_namespace_exists(self, namespace):
        try:
            self.v1.read_namespace(name=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise ValueError(f'Namespace {namespace} does not exist. Create or onboard the namespace first.')
            raise RuntimeError(self._format_api_exception(e))

    def _wait_for_pod(self, namespace, name, attempts=10, delay_seconds=0.5):
        for _ in range(attempts):
            try:
                return self.v1.read_namespaced_pod(name=name, namespace=namespace)
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise RuntimeError(self._format_api_exception(e))
            import time
            time.sleep(delay_seconds)
        return None

    def _wait_for_pod_deleted(self, namespace, name, attempts=12, delay_seconds=0.5):
        for _ in range(attempts):
            try:
                self.v1.read_namespaced_pod(name=name, namespace=namespace)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    return True
                raise RuntimeError(self._format_api_exception(e))
            import time
            time.sleep(delay_seconds)
        return False

    def _pod_events(self, namespace, name, uid):
        try:
            events = self.v1.list_namespaced_event(
                namespace=namespace,
                field_selector=f'involvedObject.name={name}'
            )
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        result = []
        for event in events.items:
            involved = event.involved_object
            if involved and involved.uid and uid and involved.uid != uid:
                continue
            result.append({
                'type': event.type or '',
                'reason': event.reason or '',
                'message': event.message or '',
                'count': event.count or 0,
                'first_seen': self._event_time(event.first_timestamp, event.event_time),
                'last_seen': self._event_time(event.last_timestamp, event.event_time),
                'age': self._format_age(event.last_timestamp or event.first_timestamp or event.event_time)
            })
        return sorted(result, key=lambda e: e.get('last_seen') or e.get('first_seen') or '')

    def _event_time(self, timestamp, event_time):
        value = timestamp or event_time
        return value.isoformat() if value else ''

    def _pod_condition_diagnostic(self, condition):
        return {
            'type': condition.type or '',
            'status': condition.status or '',
            'reason': condition.reason or '',
            'message': condition.message or '',
            'last_transition_time': condition.last_transition_time.isoformat() if condition.last_transition_time else ''
        }

    def _toleration_summary(self, toleration):
        parts = [toleration.key or '<any-key>', toleration.operator or 'Equal']
        if toleration.value:
            parts.append(toleration.value)
        if toleration.effect:
            parts.append(toleration.effect)
        return ' | '.join(parts)

    def _taint_summary(self, taint):
        parts = [taint.key or '<any-key>']
        if taint.value:
            parts.append(taint.value)
        if taint.effect:
            parts.append(taint.effect)
        return ' | '.join(parts)

    def _schedulable_node_taints(self):
        try:
            nodes = self.v1.list_node()
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        result = []
        for node in nodes.items:
            taints = node.spec.taints or []
            for taint in taints:
                if taint.effect in ('NoSchedule', 'PreferNoSchedule'):
                    result.append({
                        'node': node.metadata.name,
                        'key': taint.key or '',
                        'value': taint.value or '',
                        'effect': taint.effect or '',
                        'summary': self._taint_summary(taint)
                    })
        return result

    def _cluster_node_health(self):
        try:
            nodes = self.v1.list_node()
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        result = {
            'ready': True,
            'nodes': []
        }
        for node in nodes.items:
            conditions = []
            node_ready = True
            for condition in node.status.conditions or []:
                conditions.append({
                    'type': condition.type or '',
                    'status': condition.status or '',
                    'reason': condition.reason or '',
                    'message': condition.message or ''
                })
                if condition.type == 'Ready' and condition.status != 'True':
                    node_ready = False
            result['nodes'].append({
                'name': node.metadata.name,
                'ready': node_ready,
                'conditions': conditions,
                'taints': [self._taint_summary(t) for t in (node.spec.taints or [])]
            })
            if not node_ready:
                result['ready'] = False
        return result

    def _network_plugin_health(self):
        checks = [
            ('calico-system', ['calico', 'cni']),
            ('tigera-operator', ['tigera']),
            ('kube-system', ['flannel', 'canal', 'cilium', 'cni'])
        ]
        result = {
            'ready': False,
            'reason': 'No known network plugin pods were found.',
            'namespaces': []
        }
        found_any = False
        not_ready = []

        for namespace, keywords in checks:
            ns_result = self._namespace_workload_health(namespace, keywords)
            result['namespaces'].append(ns_result)
            if ns_result.get('exists') and ns_result.get('pods'):
                found_any = True
            if ns_result.get('exists') and not ns_result.get('all_ready', False):
                not_ready.append(namespace)

        if found_any and not not_ready:
            result['ready'] = True
            result['reason'] = 'Detected network plugin pods are ready.'
        elif found_any:
            result['reason'] = f'Network plugin pods are not ready in: {", ".join(not_ready)}.'
        return result

    def _system_namespace_health(self):
        namespaces = [
            ('kube-system', []),
            ('calico-system', []),
            ('tigera-operator', [])
        ]
        return [self._namespace_workload_health(namespace, keywords) for namespace, keywords in namespaces]

    def _namespace_workload_health(self, namespace, keywords):
        try:
            self.v1.read_namespace(namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return {
                    'namespace': namespace,
                    'exists': False,
                    'all_ready': False,
                    'pods': [],
                    'reason': 'Namespace not found.'
                }
            raise RuntimeError(self._format_api_exception(e))

        try:
            pods = self.v1.list_namespaced_pod(namespace)
        except client.exceptions.ApiException as e:
            raise RuntimeError(self._format_api_exception(e))

        filtered = []
        for pod in pods.items:
            name = pod.metadata.name or ''
            if keywords and not any(keyword in name.lower() for keyword in keywords):
                continue
            filtered.append({
                'name': name,
                'phase': pod.status.phase if pod.status else 'Unknown',
                'ready': self._pod_ready(pod),
                'reason': self._pod_primary_reason(pod)
            })

        all_ready = bool(filtered) and all(p['ready'] for p in filtered)
        reason = 'No matching pods found.' if not filtered else (
            'All matching pods are ready.' if all_ready else 'Some matching pods are not ready.'
        )
        return {
            'namespace': namespace,
            'exists': True,
            'all_ready': all_ready,
            'pods': filtered,
            'reason': reason
        }

    def _pod_ready(self, pod):
        if not pod.status:
            return False
        for condition in pod.status.conditions or []:
            if condition.type == 'Ready':
                return condition.status == 'True'
        return False

    def _pod_primary_reason(self, pod):
        statuses = (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])
        for status in statuses:
            if status.state and status.state.waiting and status.state.waiting.reason:
                return status.state.waiting.reason
            if status.state and status.state.terminated and status.state.terminated.reason:
                return status.state.terminated.reason
        return pod.status.phase if pod.status else 'Unknown'

    def _recent_cluster_warnings(self, namespace):
        result = {
            'cni_not_initialized': False,
            'projected_volume_errors': False,
            'messages': []
        }
        namespaces = ['kube-system', namespace]
        for target_ns in namespaces:
            try:
                events = self.v1.list_namespaced_event(target_ns)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    continue
                raise RuntimeError(self._format_api_exception(e))
            for event in events.items:
                message = (event.message or '').lower()
                if 'cni plugin not initialized' in message:
                    result['cni_not_initialized'] = True
                    result['messages'].append(event.message or '')
                if 'kube-root-ca.crt' in message or 'projected' in message or 'not registered' in message:
                    result['projected_volume_errors'] = True
                    result['messages'].append(event.message or '')
        result['messages'] = result['messages'][:6]
        return result

    def _pod_cluster_health(self, pod, events):
        node_name = pod.spec.node_name or ''
        node_conditions = []
        node_taints = []
        if node_name:
            try:
                node = self.v1.read_node(node_name)
                node_conditions = [self._node_condition_summary(c) for c in (node.status.conditions or [])]
                node_taints = [self._taint_summary(t) for t in (node.spec.taints or [])]
            except client.exceptions.ApiException as e:
                raise RuntimeError(self._format_api_exception(e))

        network_errors = []
        mount_errors = []
        for event in events:
            message = (event.get('message') or '').lower()
            reason = (event.get('reason') or '').lower()
            if reason == 'networknotready' or 'cni plugin not initialized' in message:
                network_errors.append(event.get('message') or '')
            if reason == 'failedmount' and (
                'kube-root-ca.crt' in message or 'projected' in message or 'not registered' in message
            ):
                mount_errors.append(event.get('message') or '')

        network_plugins = self._network_plugin_health()
        remediations = self._cluster_health_remediations(network_errors, mount_errors, network_plugins, node_conditions)

        return {
            'node_name': node_name,
            'node_conditions': node_conditions,
            'node_taints': node_taints,
            'cni_ready': network_plugins.get('ready', False) and not network_errors,
            'cni_reason': network_errors[0] if network_errors else network_plugins.get('reason', ''),
            'network_plugins': network_plugins,
            'kube_root_ca_mount_ok': not mount_errors,
            'projected_volume_errors': mount_errors,
            'remediations': remediations,
            'repair_flow': self._pod_repair_flow(network_errors, mount_errors, network_plugins, node_conditions)
        }

    def _node_condition_summary(self, condition):
        return {
            'type': condition.type or '',
            'status': condition.status or '',
            'reason': condition.reason or '',
            'message': condition.message or '',
            'summary': f'{condition.type}: {condition.status}'
        }

    def _cluster_health_remediations(self, network_errors, mount_errors, network_plugins, node_conditions):
        steps = []
        if network_errors:
            steps.append('Check the node CNI plugin pods in kube-system/calico-system and wait until they are Ready.')
            steps.append('If CNI pods are crashlooping or missing, repair the cluster networking before recreating application Pods.')
        if mount_errors:
            steps.append('Inspect namespace ConfigMap and projected volume setup for kube-root-ca before retrying the Pod.')
            steps.append('Verify the control-plane components finished creating namespace-scoped root CA projection resources.')
        if any(c.get('type') == 'Ready' and c.get('status') != 'True' for c in node_conditions):
            steps.append('Fix node readiness first; Pods will not become healthy until the assigned node reports Ready.')
        if not network_plugins.get('ready', False):
            steps.append('Review system namespace plugin pods from the Cluster Health section for the first not-ready component.')
        if not steps:
            steps.append('Cluster-side blockers were not identified automatically. Inspect the raw Events and node description.')
        return steps

    def _monitoring_repair_flow(self, diagnostics):
        node_health = diagnostics.get('node_health') or {}
        recent = diagnostics.get('recent_cluster_warnings') or {}
        network_plugins = diagnostics.get('network_plugins') or {}
        system_namespaces = diagnostics.get('system_namespaces') or []

        if not node_health.get('ready', True):
            return self._repair_flow_payload(
                status='blocked',
                category='node-not-ready',
                summary='The node is Not Ready, so workloads cannot become healthy.',
                steps=self._node_not_ready_steps(),
                evidence=self._node_not_ready_evidence(node_health)
            )
        if recent.get('cni_not_initialized'):
            return self._repair_flow_payload(
                status='blocked',
                category='cni-not-initialized',
                summary='The cluster network plugin is not initialized.',
                steps=self._cni_repair_steps(network_plugins),
                evidence=self._cluster_warning_evidence(recent)
            )
        if recent.get('projected_volume_errors'):
            return self._repair_flow_payload(
                status='blocked',
                category='projected-volume-registration',
                summary='Projected volume registration is failing in the cluster control plane.',
                steps=self._projected_volume_steps(),
                evidence=self._cluster_warning_evidence(recent)
            )
        missing_or_unready = [item for item in system_namespaces if not item.get('exists') or not item.get('all_ready', False)]
        if missing_or_unready:
            return self._repair_flow_payload(
                status='warning',
                category='system-addon-not-ready',
                summary='One or more system addon namespaces are missing or not ready.',
                steps=self._system_addon_steps(missing_or_unready),
                evidence=[{
                    'kind': 'system-namespace',
                    'summary': f'{item.get("namespace")}: {item.get("reason")}'
                } for item in missing_or_unready]
            )
        return self._repair_flow_payload(
            status='healthy',
            category='unknown',
            summary='No blocking cluster health issues were detected.',
            steps=[],
            evidence=[]
        )

    def _pod_repair_flow(self, network_errors, mount_errors, network_plugins, node_conditions):
        if any(c.get('type') == 'Ready' and c.get('status') != 'True' for c in node_conditions):
            return self._repair_flow_payload(
                status='blocked',
                category='node-not-ready',
                summary='The assigned node is not Ready.',
                steps=self._node_not_ready_steps(),
                evidence=[{
                    'kind': 'node-condition',
                    'summary': f'{c.get("type")}: {c.get("status")} {c.get("reason") or ""}'.strip()
                } for c in node_conditions if c.get('type') == 'Ready' and c.get('status') != 'True']
            )
        if network_errors:
            return self._repair_flow_payload(
                status='blocked',
                category='cni-not-initialized',
                summary='The Pod was scheduled correctly, but node networking is not initialized.',
                steps=self._cni_repair_steps(network_plugins),
                evidence=[{'kind': 'event', 'summary': message} for message in network_errors[:4]]
            )
        if mount_errors:
            return self._repair_flow_payload(
                status='blocked',
                category='projected-volume-registration',
                summary='The Pod was scheduled correctly, but projected volume registration is failing.',
                steps=self._projected_volume_steps(),
                evidence=[{'kind': 'event', 'summary': message} for message in mount_errors[:4]]
            )
        return self._repair_flow_payload(
            status='warning',
            category='unknown',
            summary='Cluster runtime is not clearly blocked, but the Pod is not healthy yet.',
            steps=self._unknown_repair_steps(),
            evidence=[]
        )

    def _repair_flow_payload(self, status, category, summary, steps, evidence):
        return {
            'status': status,
            'primary_category': category,
            'summary': summary,
            'steps': steps,
            'evidence': evidence
        }

    def _node_not_ready_steps(self):
        return [
            self._repair_step(
                'Check K3s service status',
                'A NotReady node often means the K3s service is unhealthy or stopped.',
                ['sudo systemctl status k3s'],
                'The service should be active and recent logs should not show fatal startup errors.'
            ),
            self._repair_step(
                'Restart K3s if needed',
                'Restarting K3s can restore kubelet, container runtime, and addon reconciliation on a single-node lab host.',
                ['sudo systemctl restart k3s'],
                'The node should move toward Ready and system pods should begin recovering.'
            ),
            self._repair_step(
                'Recheck node readiness',
                'Confirm the control plane is publishing a healthy node status again.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes'],
                'The node should report Ready.'
            ),
            self._repair_step(
                'Recheck system pods',
                'Core addons must recover before tenant Pods will start cleanly.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n kube-system'],
                'Core system pods should move to Running/Ready.'
            )
        ]

    def _cni_repair_steps(self, network_plugins):
        namespaces = network_plugins.get('namespaces', []) if network_plugins else []
        focus_namespaces = [item.get('namespace') for item in namespaces if not item.get('all_ready', False) or not item.get('exists', False)]
        namespace_hint = ', '.join(focus_namespaces) if focus_namespaces else 'kube-system, calico-system, tigera-operator'
        return [
            self._repair_step(
                'Inspect network plugin pods',
                'The CNI must be healthy before kubelet can create pod sandboxes.',
                [
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n kube-system',
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n calico-system'
                ],
                f'Focus first on: {namespace_hint}. Network-related pods should be Running and Ready.'
            ),
            self._repair_step(
                'Check K3s service and addon logs',
                'Single-node K3s commonly reports CNI bootstrap failures through the K3s service logs.',
                ['sudo systemctl status k3s'],
                'Look for networking or addon initialization errors to clear before retrying workloads.'
            ),
            self._repair_step(
                'Recheck pod events after CNI recovery',
                'Once networking is back, the same Pod should stop reporting NetworkNotReady.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get events -n kube-system --sort-by=.lastTimestamp'],
                'New events should no longer mention CNI not initialized.'
            )
        ]

    def _projected_volume_steps(self):
        return [
            self._repair_step(
                'Inspect kube-system addon pods',
                'Projected-volume registration errors often come from control-plane or addon reconciliation instability.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n kube-system'],
                'Core addon pods such as coredns, traefik, and control-plane-managed workloads should be healthy.'
            ),
            self._repair_step(
                'Review kube-system events',
                'The failing ConfigMap-backed or projected resources are usually named directly in recent events.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get events -n kube-system --sort-by=.lastTimestamp'],
                'Errors mentioning kube-root-ca.crt or object not registered should stop appearing.'
            ),
            self._repair_step(
                'Retry tenant workloads only after registration errors stop',
                'Recreating Pods too early will just reproduce the same mount failure.',
                ['KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes'],
                'Once the node and kube-system are healthy, tenant Pods can be recreated safely.'
            )
        ]

    def _system_addon_steps(self, namespaces):
        focus = ', '.join(item.get('namespace') for item in namespaces)
        return [
            self._repair_step(
                'Inspect missing or degraded addon namespaces',
                'Tenant workloads depend on system addons being present and ready.',
                [
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n kube-system',
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n calico-system'
                ],
                f'Focus on namespaces: {focus}. Restore addon readiness before re-testing tenant workloads.'
            )
        ]

    def _unknown_repair_steps(self):
        return [
            self._repair_step(
                'Inspect raw cluster and pod events',
                'The automatic rules did not identify a primary runtime blocker.',
                [
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get nodes',
                    'KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get events -n kube-system --sort-by=.lastTimestamp'
                ],
                'The next blocking signal should become obvious from current events.'
            )
        ]

    def _repair_step(self, title, why, command, expected_signal):
        return {
            'title': title,
            'why': why,
            'command': command,
            'expected_signal': expected_signal
        }

    def _node_not_ready_evidence(self, node_health):
        evidence = []
        for node in node_health.get('nodes', []):
            if not node.get('ready', True):
                evidence.append({
                    'kind': 'node',
                    'summary': f'{node.get("name")} is Not Ready'
                })
        return evidence

    def _cluster_warning_evidence(self, recent):
        return [{'kind': 'event', 'summary': message} for message in recent.get('messages', [])[:6]]

    def _unmatched_taints(self, tolerations, node_taints):
        unmatched = []
        for taint in node_taints:
            if not self._taint_is_tolerated(taint, tolerations):
                unmatched.append(taint)
        return unmatched

    def _taint_is_tolerated(self, taint, tolerations):
        for toleration in tolerations:
            effect = toleration.effect or ''
            if effect and effect != taint.get('effect'):
                continue

            operator = toleration.operator or 'Equal'
            key = toleration.key or ''
            value = toleration.value or ''

            if operator == 'Exists':
                if not key or key == taint.get('key'):
                    return True
                continue

            if operator == 'Equal':
                if key == taint.get('key') and value == (taint.get('value') or ''):
                    return True
        return False

    def _container_diagnostic(self, status):
        state_name, state_reason, state_message = self._container_state(status.state)
        running = status.state.running if status.state and status.state.running else None
        terminated = status.state.terminated if status.state and status.state.terminated else None
        return {
            'name': status.name,
            'ready': bool(status.ready),
            'restart_count': status.restart_count or 0,
            'image': status.image or '',
            'image_id': status.image_id or '',
            'state': state_name,
            'reason': state_reason,
            'message': state_message,
            'started_at': self._container_started_at(running, terminated),
            'finished_at': terminated.finished_at.isoformat() if terminated and terminated.finished_at else ''
        }

    def _container_state(self, state):
        if not state:
            return 'Unknown', '', ''
        if state.waiting:
            return 'Waiting', state.waiting.reason or '', state.waiting.message or ''
        if state.running:
            return 'Running', '', ''
        if state.terminated:
            return 'Terminated', state.terminated.reason or '', state.terminated.message or ''
        return 'Unknown', '', ''

    def _container_started_at(self, running, terminated):
        if running and running.started_at:
            return running.started_at.isoformat()
        if terminated and terminated.started_at:
            return terminated.started_at.isoformat()
        return ''

    def _infer_pod_diagnosis(self, pod, events, unmatched_taints=None, cluster_health=None):
        phase = pod.status.phase if pod.status else 'Unknown'
        unmatched_taints = unmatched_taints or []
        cluster_health = cluster_health or {}
        messages = ' '.join(
            [event.get('reason', '') + ' ' + event.get('message', '') for event in events]
        ).lower()
        waiting_reasons = []
        for status in (pod.status.container_statuses or []) + (pod.status.init_container_statuses or []):
            if status.state and status.state.waiting and status.state.waiting.reason:
                waiting_reasons.append(status.state.waiting.reason)
        waiting_text = ' '.join(waiting_reasons).lower()

        if 'insufficient cpu' in messages or 'insufficient memory' in messages or 'insufficient ephemeral-storage' in messages:
            return {
                'category': 'scheduling',
                'level': 'danger',
                'summary': 'Pod cannot be scheduled because node resources are insufficient.',
                'hint': 'Lower the Pod requests/limits, delete unused workloads, or add capacity to the cluster.'
            }
        if 'taint' in messages or "didn't tolerate" in messages:
            if unmatched_taints:
                summaries = ', '.join(t['summary'] for t in unmatched_taints[:3])
                hint = (
                    'This Pod is missing tolerations for: '
                    f'{summaries}. Portal-created Pods now tolerate all NoSchedule/PreferNoSchedule taints in the single-node lab.'
                )
            else:
                hint = (
                    'The scheduler still reports an untolerated taint. Check the node taints section below; '
                    'it likely comes from a custom node taint outside the original control-plane/master pair.'
                )
            return {
                'category': 'scheduling',
                'level': 'danger',
                'summary': 'Pod is blocked by a node taint.',
                'hint': hint
            }
        if 'failedscheduling' in messages or '0/' in messages and 'nodes are available' in messages:
            return {
                'category': 'scheduling',
                'level': 'danger',
                'summary': 'Kubernetes scheduler found no node that can run this Pod.',
                'hint': 'Check the FailedScheduling event message below for the exact predicate.'
            }
        if (
            self._pod_is_scheduled(pod)
            and 'containercreating' in waiting_text
            and ('networknotready' in messages or 'cni plugin not initialized' in messages)
        ):
            return {
                'category': 'runtime-network',
                'level': 'danger',
                'summary': 'Pod was scheduled, but the node network plugin is not ready.',
                'hint': cluster_health.get('cni_reason') or 'The Pod spec is valid. Repair cluster networking and then retry the workload.'
            }
        if 'failedmount' in messages and (
            'kube-root-ca.crt' in messages or 'projected' in messages or 'not registered' in messages
        ):
            return {
                'category': 'runtime-mount',
                'level': 'danger',
                'summary': 'Pod was scheduled, but projected volume setup failed for kube-root-ca.',
                'hint': 'The Pod spec is valid. Fix the cluster-side projected volume/configmap path before retrying.'
            }
        if 'imagepullbackoff' in waiting_text or 'errimagepull' in waiting_text or 'failed to pull image' in messages:
            return {
                'category': 'image-pull',
                'level': 'danger',
                'summary': 'Container image cannot be pulled.',
                'hint': 'Use a registry reachable from the K3s node, configure a registry mirror, or fix the image name/tag.'
            }
        if 'crashloopbackoff' in waiting_text:
            return {
                'category': 'app-startup',
                'level': 'warning',
                'summary': 'Container starts but crashes repeatedly.',
                'hint': 'Check container logs and command/args/env configuration.'
            }
        if not self._pod_is_scheduled(pod) and 'failedscheduling' in messages:
            return {
                'category': 'scheduling',
                'level': 'danger',
                'summary': 'Pod is blocked before scheduling.',
                'hint': 'Review FailedScheduling events first.'
            }
        if phase == 'Pending':
            return {
                'category': 'unknown',
                'level': 'warning',
                'summary': 'Pod is still Pending.',
                'hint': 'Review Events, container waiting reasons, and Cluster Health below; this Pending Pod may already be scheduled and blocked by node runtime issues.'
            }
        if phase == 'Running':
            return {
                'category': 'unknown',
                'level': 'success',
                'summary': 'Pod is Running.',
                'hint': 'If Grafana shows no usage, wait for the next metrics scrape or check metrics-server availability.'
            }
        return {
            'category': 'unknown',
            'level': 'secondary',
            'summary': f'Pod phase is {phase}.',
            'hint': 'Review conditions, containers, and events below for details.'
        }

    def _pod_is_scheduled(self, pod):
        for condition in pod.status.conditions or []:
            if condition.type == 'PodScheduled':
                return condition.status == 'True'
        return False

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

    def get_namespace_password(self, namespace):
        if not self.connected:
            return None
        try:
            ns = self.v1.read_namespace(name=namespace)
            return (ns.metadata.annotations or {}).get('tenant.lab/password')
        except Exception:
            return None

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
