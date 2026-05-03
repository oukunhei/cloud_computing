import os

# System namespaces to exclude from tenant listing
SYSTEM_NAMESPACES = {
    'default', 'kube-system', 'kube-public', 'kube-node-lease',
    'calico-system', 'calico-apiserver', 'tigera-operator',
    'ingress-nginx', 'cert-manager', 'lab-platform-users'
}

USER_NAMESPACE = os.environ.get('USER_NAMESPACE', 'lab-platform-users')

# Role permissions matrix for display
ROLE_PERMISSIONS = {
    'cluster-admin': {
        'description': 'Owns tenant namespace lifecycle and platform-wide tenant management',
        'can_read': ['all tenant namespaces', 'cluster overview', 'tenant resource settings'],
        'can_write': ['create namespaces', 'delete namespaces', 'manage all tenants'],
        'can_exec': False,
        'denied': ['day-to-day tenant workload ownership'],
        'note': 'Cluster admin is the platform operator role. It is not bound to one tenant namespace.'
    },
    'developer': {
        'description': 'Can deploy and manage workloads within their namespace',
        'can_read': [
            'pods', 'pods/log', 'services', 'deployments',
            'configmaps', 'ingresses', 'events', 'jobs', 'cronjobs', 'hpa'
        ],
        'can_write': [
            'pods', 'services', 'deployments', 'configmaps',
            'ingresses', 'jobs', 'cronjobs', 'hpa'
        ],
        'can_exec': True,
        'denied': [
            'roles', 'rolebindings', 'resourcequotas',
            'limitranges', 'networkpolicies', 'secrets'
        ],
        'note': 'These are not granted by RBAC; Kubernetes RBAC is additive and has no explicit deny rule.'
    },
    'viewer': {
        'description': 'Read-only access to namespace resources',
        'can_read': [
            'pods', 'pods/log', 'services', 'deployments',
            'configmaps', 'ingresses', 'events', 'jobs', 'cronjobs', 'hpa'
        ],
        'can_write': [],
        'can_exec': False,
        'denied': [
            'pods/exec', 'pods/portforward', 'pods/attach',
            'secrets', 'roles', 'rolebindings'
        ],
        'note': 'Read-only access is implemented by granting only get/list/watch verbs.'
    },
    'admin': {
        'description': 'Tenant admin with full control inside one team namespace',
        'can_read': ['ALL NAMESPACED RESOURCES IN OWN NAMESPACE'],
        'can_write': ['workloads', 'RBAC', 'ResourceQuota', 'LimitRange', 'NetworkPolicy'],
        'can_exec': True,
        'denied': ['other tenant namespaces', 'cluster-scoped resources', 'namespace creation/deletion'],
        'note': 'Tenant admin is powerful inside one namespace but cannot manage tenant lifecycle.'
    }
}

FLASK_PORT = int(os.environ.get('FLASK_PORT', 8080))
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-only-change-me')
