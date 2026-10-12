/* Утилиты VHS Analytics */

function showToast(message, type = 'info', timeout = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container position-fixed top-0 end-0 p-3';
        container.style.zIndex = '1100';
        document.body.appendChild(container);
    }

    const el = document.createElement('div');
    el.className = `toast align-items-center text-bg-${type} border-0`;
    el.setAttribute('role', 'alert');
    el.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto"
                    data-bs-dismiss="toast"></button>
        </div>
    `;
    container.appendChild(el);
    const toast = new bootstrap.Toast(el, { delay: timeout });
    toast.show();
    el.addEventListener('hidden.bs.toast', () => el.remove());
}

async function apiCall(url, options = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showToast(data.error || `Ошибка ${resp.status}`, 'danger');
            return null;
        }
        return data;
    } catch (e) {
        showToast('Сетевая ошибка: ' + e.message, 'danger');
        return null;
    }
}