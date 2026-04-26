// API helpers
async function apiGet(url) {
    const resp = await fetch(url);
    return resp.json();
}

async function apiPost(url, data) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    return resp.json();
}

async function apiDelete(url) {
    const resp = await fetch(url, { method: 'DELETE' });
    return resp.json();
}

// Format ISO date string
function formatDate(isoString) {
    if (!isoString) return '-';
    const d = new Date(isoString);
    return d.toLocaleString();
}

// Toast notification
function showToast(message, type = 'info') {
    const container = document.querySelector('.toast-container') || (() => {
        const c = document.createElement('div');
        c.className = 'toast-container';
        document.body.appendChild(c);
        return c;
    })();

    const toast = document.createElement('div');
    toast.className = `toast align-items-center text-white bg-${type === 'success' ? 'success' : type === 'error' ? 'danger' : 'primary'}`;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
        </div>
    `;
    container.appendChild(toast);
    const bsToast = new bootstrap.Toast(toast, { delay: 4000 });
    bsToast.show();
    toast.addEventListener('hidden.bs.toast', () => toast.remove());
}

function showError(message) {
    showToast(message, 'error');
}

// K8s connectivity status
async function checkK8sStatus() {
    const el = document.getElementById('k8s-status');
    if (!el) return;
    try {
        const info = await apiGet('/api/cluster/info');
        if (info.error) {
            el.innerHTML = '<span class="badge bg-danger"><i class="bi bi-circle-fill"></i> Disconnected</span>';
        } else {
            el.innerHTML = '<span class="badge bg-success"><i class="bi bi-circle-fill"></i> Connected</span>';
        }
    } catch (e) {
        el.innerHTML = '<span class="badge bg-danger"><i class="bi bi-circle-fill"></i> Disconnected</span>';
    }
}

document.addEventListener('DOMContentLoaded', checkK8sStatus);
