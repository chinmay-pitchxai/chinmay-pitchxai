let liveWs = null;
let liveAudioContext = null;
let liveProcessor = null;
let liveMicrophone = null;

async function startLiveTest() {
    // Cleanup any previous live audio session
    stopLiveAudio();
    const phone = document.getElementById('test-phone').value.trim();
    if (!phone) { showToast('Enter a phone number', 'error'); return; }

    try {
        const res = await fetch(apiUrl('/api/factory/call'), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ phone, role: currentRole }),
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showToast('Live test call initiated', 'success');
            initLiveAudio();
        } else {
            showToast(data.detail || 'Call failed', 'error');
        }
    } catch (e) {
        showToast('Connection error', 'error');
    }
}

function initLiveAudio() {
    // WebSocket and AudioContext logic for live monitoring
    // ... (rest of the voice logic from console.html)
}

function stopLiveAudio() {
    if (liveWs) {
        liveWs.close();
        liveWs = null;
    }
    if (liveMicrophone) {
        liveMicrophone.getTracks().forEach(t => t.stop());
        liveMicrophone = null;
    }
    if (liveAudioContext) {
        liveAudioContext.close().catch(() => {});
        liveAudioContext = null;
    }
    if (liveProcessor) {
        liveProcessor = null;
    }
}
