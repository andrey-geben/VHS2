/* ==================================================================== */
/*  Утилиты                                                              */
/* ==================================================================== */

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

/* ==================================================================== */
/*  Управление камерами                                                  */
/* ==================================================================== */

async function cameraAction(cameraId, action) {
    const data = await apiCall(`/api/camera/${cameraId}/${action}`, { method: 'POST' });
    if (data) {
        showToast(action === 'start' ? 'Камера запущена' : 'Камера остановлена', 'success');
    }
}

async function toggleRecord(cameraId, currentlyRecording) {
    const action = currentlyRecording ? 'stop' : 'start';
    const data = await apiCall(
        `/api/camera/${cameraId}/record/${action}`,
        { method: 'POST' }
    );
    if (data) {
        showToast(action === 'start' ? 'Запись запущена' : 'Запись остановлена', 'success');
        const btn = document.getElementById('recBtn');
        if (btn) {
            if (data.recording) {
                btn.classList.remove('btn-outline-danger');
                btn.classList.add('btn-danger');
                btn.innerHTML = '<i class="bi bi-stop-circle"></i> Остановить запись';
                btn.onclick = () => toggleRecord(cameraId, true);
            } else {
                btn.classList.remove('btn-danger');
                btn.classList.add('btn-outline-danger');
                btn.innerHTML = '<i class="bi bi-record-circle"></i> Начать запись';
                btn.onclick = () => toggleRecord(cameraId, false);
            }
        }
    }
}

async function reloadConfig() {
    const data = await apiCall('/api/config/reload', { method: 'POST' });
    if (data) {
        showToast('Конфигурация перезагружена. Обновите страницу.', 'success', 4000);
    }
}

/* ==================================================================== */
/*  PTZ                                                                  */
/* ==================================================================== */

async function ptz(cameraId, direction) {
    const speedEl = document.getElementById('ptzSpeed');
    const speed = speedEl ? parseFloat(speedEl.value) : 0.5;
    await apiCall(`/api/camera/${cameraId}/ptz`, {
        method: 'POST',
        body: JSON.stringify({ direction, speed }),
    });
}

async function ptzStop(cameraId) {
    await apiCall(`/api/camera/${cameraId}/ptz/stop`, { method: 'POST' });
}

/* ==================================================================== */
/*  Полноэкранный режим                                                  */
/* ==================================================================== */

function toggleFullscreen() {
    if (!document.fullscreenElement && !document.body.classList.contains('is-fullscreen')) {
        enterFullscreen();
    } else {
        exitFullscreen();
    }
}

function enterFullscreen() {
    const el = document.documentElement;
    if (el.requestFullscreen) {
        el.requestFullscreen().catch(err => {
            console.warn('Fullscreen API недоступно, включаю CSS-режим', err);
            document.body.classList.add('is-fullscreen');
        });
    } else {
        document.body.classList.add('is-fullscreen');
    }
}

function exitFullscreen() {
    if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
    }
    document.body.classList.remove('is-fullscreen');
    document.body.classList.remove('is-single-fullscreen');
    document.querySelectorAll('[data-fullscreen-hidden]').forEach(el => {
        el.style.display = '';
        el.removeAttribute('data-fullscreen-hidden');
    });
}

function fullscreenSingle(cameraId, cameraName) {
    if (document.body.classList.contains('is-single-fullscreen')) {
        exitFullscreen();
        return;
    }

    document.body.classList.add('is-single-fullscreen');

    const onCameraPage = document.getElementById('cameraStream') !== null;
    if (!onCameraPage) {
        sessionStorage.setItem('autoFullscreenCamera', cameraId);
        location.href = '/camera/' + cameraId;
        return;
    }

    if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(() => {});
    }
}

document.addEventListener('fullscreenchange', () => {
    const isFs = !!document.fullscreenElement;
    document.body.classList.toggle('is-fullscreen', isFs);
    if (!isFs) {
        document.body.classList.remove('is-single-fullscreen');
    }
});

document.addEventListener('DOMContentLoaded', () => {
    const autoCam = sessionStorage.getItem('autoFullscreenCamera');
    if (autoCam && document.getElementById('cameraStream')) {
        sessionStorage.removeItem('autoFullscreenCamera');
        setTimeout(() => {
            document.body.classList.add('is-single-fullscreen');
            if (document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen().catch(() => {});
            }
        }, 300);
    }
});

/* ==================================================================== */
/*  Горячие клавиши                                                      */
/* ==================================================================== */
document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const isInput = tag === 'input' || tag === 'textarea' || tag === 'select';

    if (!isInput && (e.key === 'f' || e.key === 'F')
            && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
        toggleFullscreen();
        return;
    }

    if (e.key === 'Escape') {
        document.body.classList.remove('is-single-fullscreen');
    }

    // Стрелки для PTZ на странице камеры
    if (!document.getElementById('ptzSpeed')) return;
    if (isInput) return;

    const cameraId = document.body.dataset.cameraId;
    if (!cameraId) return;

    const map = {
        ArrowUp: 'up',
        ArrowDown: 'down',
        ArrowLeft: 'left',
        ArrowRight: 'right',
        '+': 'zoom_in',
        '-': 'zoom_out',
    };
    if (map[e.key]) {
        e.preventDefault();
        ptz(cameraId, map[e.key]);
    } else if (e.key === ' ') {
        e.preventDefault();
        ptzStop(cameraId);
    }
});

/* ==================================================================== */
/*  Синхронизация с F11 (родной fullscreen браузера)                    */
/* ==================================================================== */
let _f11State = false;
window.addEventListener('resize', () => {
    const nearlyFullscreen = window.innerHeight >= screen.height - 2;
    if (nearlyFullscreen && !_f11State) {
        _f11State = true;
        document.body.classList.add('is-fullscreen');
    } else if (!nearlyFullscreen && _f11State) {
        _f11State = false;
        document.body.classList.remove('is-fullscreen');
        document.body.classList.remove('is-single-fullscreen');
    }
});