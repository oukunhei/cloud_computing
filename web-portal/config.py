import os

# System namespaces to exclude from tenant listing
SYSTEM_NAMESPACES = {
    'default', 'kube-system', 'kube-public', 'kube-node-lease',
    'calico-system', 'calico-apiserver', 'tigera-operator',
    'ingress-nginx', 'cert-manager'
}

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
        ]
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
        ]
    },
    'admin': {
        'description': 'Full cluster administration access',
        'can_read': ['ALL RESOURCES'],
        'can_write': ['ALL RESOURCES'],
        'can_exec': True,
        'denied': []
    }
}

FLASK_PORT = int(os.environ.get('FLASK_PORT', 8080))
