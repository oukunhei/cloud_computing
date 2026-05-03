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
        'description': 'Full tenant namespace administration access',
        'can_read': ['ALL NAMESPACED RESOURCES'],
        'can_write': ['ALL NAMESPACED RESOURCES'],
        'can_exec': True,
        'denied': ['cluster-scoped resources', 'namespace deletion'],
        'note': 'Tenant admin is powerful inside one namespace; platform admin kubeconfig is still required for cluster lifecycle operations.'
    }
}

FLASK_PORT = int(os.environ.get('FLASK_PORT', 8080))
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-only-change-me')
