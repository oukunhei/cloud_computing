import os
from flask import Flask, render_template, jsonify, request, Response
from k8s_client import k8s
from config import ROLE_PERMISSIONS, FLASK_PORT

app = Flask(__name__, template_folder='templates', static_folder='static')


@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/tenants')
def tenants():
    return render_template('tenants.html')


@app.route('/resources/<namespace>')
def resources_page(namespace):
    return render_template('resources.html', namespace=namespace)


@app.route('/kubeconfig')
def kubeconfig_page():
    return render_template('kubeconfig.html')


@app.route('/permissions')
def permissions():
    return render_template('permissions.html', roles=ROLE_PERMISSIONS)


# API Routes
@app.route('/api/cluster/info')
def api_cluster_info():
    return jsonify(k8s.get_cluster_info())


@app.route('/api/tenants', methods=['GET'])
def api_list_tenants():
    return jsonify(k8s.list_tenants())


@app.route('/api/tenants', methods=['POST'])
def api_create_tenant():
    data = request.get_json() or {}
    name = data.get('name', '').strip().lower()

    if not name:
        return jsonify({'error': 'Namespace name is required'}), 400

    if not all(c.isalnum() or c == '-' for c in name):
        return jsonify({'error': 'Invalid namespace name. Use lowercase letters, numbers, and hyphens.'}), 400

    try:
        output = k8s.create_tenant(name)
        return jsonify({'success': True, 'output': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tenants/<name>', methods=['DELETE'])
def api_delete_tenant(name):
    try:
        result = k8s.delete_tenant(name)
        return jsonify({'success': True, 'message': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/namespaces/<namespace>/resources')
def api_namespace_resources(namespace):
    return jsonify(k8s.get_namespace_resources(namespace))


@app.route('/api/namespaces/<namespace>/kubeconfig')
def api_generate_kubeconfig(namespace):
    role = request.args.get('role', 'dev')
    if role not in ('dev', 'view'):
        return jsonify({'error': 'Invalid role. Must be "dev" or "view"'}), 400

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
