import os
import re
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response, redirect, session, url_for
from k8s_client import k8s
from config import ROLE_PERMISSIONS, FLASK_PORT, SECRET_KEY

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = SECRET_KEY
DNS_LABEL_RE = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
VALID_ROLES = {'admin', 'developer', 'viewer'}


def current_identity():
    return {
        'role': session.get('role'),
        'namespace': session.get('namespace'),
        'is_logged_in': bool(session.get('role'))
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
        if session.get('role') != 'admin':
            return jsonify({'error': 'Only the simulated admin role can perform this action.'}), 403
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_identity():
    return {'identity': current_identity()}


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
    return render_template('resources.html', namespace=namespace)


@app.route('/kubeconfig')
@require_login
def kubeconfig_page():
    return render_template('kubeconfig.html')


@app.route('/permissions')
@require_login
def permissions():
    return render_template('permissions.html', roles=ROLE_PERMISSIONS)


# API Routes
@app.route('/api/cluster/info')
def api_cluster_info():
    return jsonify(k8s.get_cluster_info())


@app.route('/api/tenants', methods=['GET'])
@require_login
def api_list_tenants():
    return jsonify(k8s.list_tenants())


@app.route('/api/tenants', methods=['POST'])
@require_login
@require_admin_api
def api_create_tenant():
    data = request.get_json() or {}
    name = data.get('name', '').strip().lower()

    if not name:
        return jsonify({'error': 'Namespace name is required'}), 400

    if len(name) > 63 or not DNS_LABEL_RE.match(name):
        return jsonify({'error': 'Invalid namespace name. Use a DNS-compatible name: lowercase letters, numbers, hyphens, max 63 chars, and no leading/trailing hyphen.'}), 400

    try:
        output = k8s.create_tenant(name)
        return jsonify({'success': True, 'output': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tenants/<name>', methods=['DELETE'])
@require_login
@require_admin_api
def api_delete_tenant(name):
    try:
        result = k8s.delete_tenant(name)
        return jsonify({'success': True, 'message': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/resources')
@require_login
def api_namespace_resources(namespace):
    return jsonify(k8s.get_namespace_resources(namespace))


@app.route('/api/namespaces/<namespace>/kubeconfig')
@require_login
def api_generate_kubeconfig(namespace):
    role = request.args.get('role', 'dev')
    if role not in ('admin', 'dev', 'view'):
        return jsonify({'error': 'Invalid role. Must be "admin", "dev", or "view"'}), 400

    current_role = session.get('role')
    allowed_downloads = {
        'admin': {'admin', 'dev', 'view'},
        'developer': {'dev'},
        'viewer': {'view'}
    }
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False)
