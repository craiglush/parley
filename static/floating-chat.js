// --- Floating Chat Widget ---
let floatingChatHistory = [];
let floatingChatScope = { mode: 'auto', context: null };
let floatingChatSelectedMeetings = new Set();
let floatingChatAbortController = null;
let floatingChatOpen = false;

function toggleFloatingChat() {
  floatingChatOpen = !floatingChatOpen;
  document.getElementById('floatingChatFab').classList.toggle('hidden', floatingChatOpen);
  document.getElementById('floatingChatPanel').classList.toggle('open', floatingChatOpen);
  if (floatingChatOpen) updateFloatingChatScope();
}

function updateFloatingChatScope() {
  const scopeEl = document.getElementById('fcScopeValue');
  if (!scopeEl) return;

  if (floatingChatScope.mode === 'auto') {
    if (currentMeetingId) {
      const m = allMeetingsCache.find(x => x.id === currentMeetingId);
      const title = m ? m.title : currentMeetingId.slice(0, 8);
      scopeEl.textContent = title;
      floatingChatScope.context = { scope: 'meeting', meeting_id: currentMeetingId };
    } else {
      scopeEl.textContent = 'All Meetings (no meeting selected)';
      floatingChatScope.context = { scope: 'global' };
    }
  } else if (floatingChatScope.mode === 'all') {
    scopeEl.textContent = 'All Meetings';
    floatingChatScope.context = { scope: 'global' };
  } else if (floatingChatScope.mode === 'custom') {
    const ids = Array.from(floatingChatSelectedMeetings);
    scopeEl.textContent = ids.length ? `${ids.length} meeting${ids.length > 1 ? 's' : ''} selected` : 'Pick meetings...';
    floatingChatScope.context = ids.length ? { scope: 'custom', meeting_ids: ids } : { scope: 'global' };
  }
}

function setFloatingChatMode(mode) {
  floatingChatScope.mode = mode;
  document.querySelectorAll('.fc-preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  const picker = document.getElementById('fcCustomPicker');
  picker.classList.toggle('open', mode === 'custom');
  if (mode === 'custom') renderFloatingMeetingList('');
  updateFloatingChatScope();
}

function renderFloatingMeetingList(filter) {
  const list = document.getElementById('fcPickerList');
  if (!list) return;
  const lf = (filter || '').toLowerCase();
  const completed = allMeetingsCache.filter(m => m.status === 'complete');
  const filtered = lf ? completed.filter(m => (m.title || '').toLowerCase().includes(lf)) : completed;

  list.innerHTML = filtered.map(m => {
    const checked = floatingChatSelectedMeetings.has(m.id) ? 'checked' : '';
    const date = m.date ? new Date(m.date).toLocaleDateString() : '';
    return `<label class="fc-picker-item">
      <input type="checkbox" ${checked} onchange="toggleFloatingMeetingSelection('${m.id}')">
      <span class="fcp-title">${escHtml(m.title || m.id)}</span>
      <span class="fcp-date">${date}</span>
    </label>`;
  }).join('');
}

function toggleFloatingMeetingSelection(id) {
  if (floatingChatSelectedMeetings.has(id)) {
    floatingChatSelectedMeetings.delete(id);
  } else {
    floatingChatSelectedMeetings.add(id);
  }
  const summary = document.getElementById('fcPickerSummary');
  if (summary) summary.textContent = `${floatingChatSelectedMeetings.size} meeting${floatingChatSelectedMeetings.size !== 1 ? 's' : ''} selected`;
  updateFloatingChatScope();
}

async function sendFloatingChatMessage() {
  const input = document.getElementById('fcInput');
  const sendBtn = document.getElementById('fcSendBtn');
  if (!input || !input.value.trim()) return;

  const message = input.value.trim();
  input.value = '';
  input.style.height = 'auto';

  floatingChatHistory.push({ role: 'user', content: message });
  appendFloatingChatMessage('user', message);

  const empty = document.querySelector('#fcMessages .fc-empty');
  if (empty) empty.remove();

  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;

  const assistantEl = appendFloatingChatMessage('assistant', '');
  const contentEl = assistantEl.querySelector('.chat-msg-text');
  const badgeEl = assistantEl.querySelector('.chat-context-badge');

  floatingChatAbortController = new AbortController();
  let fullResponse = '';

  try {
    const resp = await fetch(`${API}/meetings/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        context: floatingChatScope.context || { scope: 'global' },
        history: floatingChatHistory.slice(0, -1).slice(-20),
      }),
      signal: floatingChatAbortController.signal,
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6));
          if (event.type === 'context') {
            if (badgeEl) badgeEl.textContent = `${event.chunks_used} chunks from ${event.meetings_searched} meeting${event.meetings_searched !== 1 ? 's' : ''}`;
          } else if (event.type === 'token') {
            fullResponse += event.content;
            if (contentEl) contentEl.textContent = fullResponse;
            const msgs = document.getElementById('fcMessages');
            if (msgs) msgs.scrollTop = msgs.scrollHeight;
          } else if (event.type === 'error') {
            if (contentEl) contentEl.textContent = `Error: ${event.content}`;
          } else if (event.type === 'done') {
            break;
          }
        } catch (e) {}
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      if (contentEl) contentEl.textContent = `Error: ${err.message}`;
    }
  }

  if (fullResponse) {
    floatingChatHistory.push({ role: 'assistant', content: fullResponse });
  }

  floatingChatAbortController = null;
  if (sendBtn) sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}

function appendFloatingChatMessage(role, content) {
  const msgs = document.getElementById('fcMessages');
  if (!msgs) return document.createElement('div');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div><div class="chat-context-badge"></div>`;
  } else {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div>`;
  }
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function clearFloatingChat() {
  floatingChatHistory = [];
  if (floatingChatAbortController) { floatingChatAbortController.abort(); floatingChatAbortController = null; }
  const msgs = document.getElementById('fcMessages');
  if (msgs) msgs.innerHTML = '<div class="fc-empty">Ask questions about your meetings</div>';
}

// Event listeners
document.getElementById('floatingChatFab').addEventListener('click', toggleFloatingChat);
document.getElementById('fcScopeToggle').addEventListener('click', () => {
  document.getElementById('fcMeetingSelector').classList.toggle('open');
});
