// ================= ORCHESTRATOR DASHBOARD APPLICATION =================
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

let lastStatus = null;
let currentTab = 'dashboard';
let currentDetailProjectId = null;
let currentTaskFilter = 'all';
let currentConfigTab = 'agents';
let localEventsCleared = false;
let searchQuery = '';

// API Helper
async function api(path, opts = {}) {
  const next = { ...opts };
  if ((next.method || 'GET').toUpperCase() === 'POST') {
    next.headers = { ...(next.headers || {}), 'X-Orchestrator-UI': '1' };
  }
  const res = await fetch(path, next);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || res.statusText || 'Yêu cầu không thành công');
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return 'Chưa có';
  try {
    return new Date(ts * 1000).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

// Global Banner
function showBlock(msg) {
  const b = document.getElementById('blockerBanner');
  document.getElementById('blockerText').textContent = msg;
  b.classList.remove('hidden');
}

function clearBlock() {
  document.getElementById('blockerBanner').classList.add('hidden');
}

// Tab Navigation
function switchTab(tabId) {
  currentTab = tabId;
  document.querySelectorAll('.nav-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabId);
  });
  document.querySelectorAll('.view-section').forEach(sec => {
    sec.classList.remove('active');
  });

  if (tabId === 'dashboard') {
    document.getElementById('viewDashboard').classList.add('active');
  } else if (tabId === 'detail') {
    document.getElementById('viewDetail').classList.add('active');
    renderProjectDetail();
  } else if (tabId === 'runtime') {
    document.getElementById('viewRuntime').classList.add('active');
  }
}

// Stepper Component Generator
function renderLifecycleStepper(lc = {}) {
  const stages = lc.stepper_stages || [
    { label: 'Ready' },
    { label: 'Implement' },
    { label: 'Validate' },
    { label: 'QA' },
    { label: 'GPT Review' },
    { label: 'Done' }
  ];
  const idx = Number.isInteger(lc.stage_index) ? lc.stage_index : 0;
  const fill = Math.max(0, Math.min(100, (idx / (stages.length - 1)) * 100));

  const nodes = stages.map((s, i) => {
    let cls = 'upcoming';
    let icon = String(i + 1);
    if (lc.terminal || i < idx) {
      cls = 'completed';
      icon = '✓';
    } else if (i === idx) {
      cls = 'active';
      icon = '●';
    }
    return `
      <div class="step-node ${cls}">
        <div class="node-dot">${icon}</div>
        <div class="node-text">${esc(s.label)}</div>
      </div>
    `;
  }).join('');

  let banner = '';
  if (lc.is_rework) {
    banner = '<div class="rework-banner">↺ <b>REWORK</b> · Cần điều chỉnh và làm lại</div>';
  } else if (lc.is_blocked) {
    banner = '<div class="blocked-banner">⚠️ <b>BLOCKED</b> · Đang bị nghẽn/chờ can thiệp</div>';
  } else if (lc.stage_index === 4) {
    banner = '<div class="external-gate-banner">⏳ <b>GPT Review</b> · Đang chờ duyệt mã nguồn</div>';
  } else if (lc.waiting_user) {
    banner = '<div class="rework-banner">👤 <b>User Gate</b> · Đang chờ xác nhận từ bạn</div>';
  }

  return `
    <div class="task-lifecycle-stepper">
      <div class="stepper-nodes">
        <div class="stepper-line-bg">
          <div class="stepper-line-fill" style="width: ${fill}%;"></div>
        </div>
        ${nodes}
      </div>
      <div class="stepper-progress-row">
        <div class="stepper-progress-track">
          <div class="stepper-progress-bar" style="width: ${lc.progress_percent || 0}%;"></div>
        </div>
        <span class="stepper-pct">${lc.progress_percent || 0}%</span>
      </div>
      ${banner}
    </div>
  `;
}

// ================= TAB 1: DASHBOARD LOGIC =================
function renderDashboardKPIs(status) {
  const projects = status.projects || [];
  const issues = status.issues || [];
  const metrics = status.metrics || {};

  document.getElementById('dashTotalProjects').textContent = projects.length;
  document.getElementById('dashTotalTasks').textContent = issues.length;
  document.getElementById('dashReadyTasks').textContent = metrics.ready || 0;
  document.getElementById('dashActiveTasks').textContent = (metrics.in_progress || 0) + (metrics.review || 0);

  document.getElementById('navProjectCount').textContent = projects.length;
  document.getElementById('projectsPill').textContent = `${projects.length} dự án`;
}

function renderProjectsGrid(projects) {
  const container = document.getElementById('projectsGrid');
  if (!projects || projects.length === 0) {
    container.innerHTML = `
      <div class="empty-state card">
        <div class="empty-icon">📁</div>
        <h3>Chưa có dự án nào được import</h3>
        <p>Hãy dán URL hoặc tên GitHub repository vào khung trên và nhấn "Bắt Đầu Import".</p>
      </div>
    `;
    return;
  }

  const query = searchQuery.trim().toLowerCase();
  const filtered = projects.filter(p => {
    if (!query) return true;
    return (p.id && p.id.toLowerCase().includes(query)) ||
           (p.repo && p.repo.toLowerCase().includes(query));
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="empty-state card">
        <div class="empty-icon">🔍</div>
        <h3>Không tìm thấy dự án phù hợp</h3>
        <p>Không có dự án nào khớp với từ khóa "${esc(searchQuery)}".</p>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(p => {
    const tm = p.task_metrics || {
      total: p.total_tasks || 0,
      ready: p.ready_tasks || 0,
      in_progress: p.in_progress_tasks || 0,
      review: p.review_tasks || 0,
      done: p.done_tasks || 0
    };

    let statusCls = 'synced';
    let statusText = 'ĐÃ ĐỒNG BỘ';
    if (p.error) {
      statusCls = 'error';
      statusText = 'LỖI CẤU HÌNH';
    } else if (p.import_status === 'needs_sync' || !p.synced) {
      statusCls = 'needs_sync';
      statusText = 'CHƯA ĐỒNG BỘ';
    }

    const repoInfo = p.repo_info || {};
    const branch = repoInfo.branch || p.default_branch || 'main';
    const isDirty = repoInfo.dirty;

    return `
      <div class="project-card card" data-id="${esc(p.id)}">
        <div class="project-card-header">
          <div class="project-identity">
            <div class="project-icon">📦</div>
            <div>
              <div class="project-id-text">${esc(p.id)}</div>
              <a href="https://github.com/${esc(p.repo)}" target="_blank" class="project-repo-link" title="Mở trên GitHub">
                ${esc(p.repo)} ↗
              </a>
            </div>
          </div>
          <span class="project-status-badge ${statusCls}">
            ● ${esc(statusText)}
          </span>
        </div>

        <div class="project-card-meta font-mono">
          <span class="meta-chip">🌿 Branch: <b>${esc(branch)}</b></span>
          <span class="meta-chip">📁 Tree: <b>${isDirty ? '<span class="highlight-amber">DIRTY</span>' : '<span class="highlight-emerald">CLEAN</span>'}</b></span>
        </div>

        <!-- Task Metrics Row -->
        <div class="project-task-summary">
          <div class="task-stat-box">
            <div class="task-stat-num highlight-cyan">${tm.total}</div>
            <div class="task-stat-lbl">Tổng Task</div>
          </div>
          <div class="task-stat-box ${tm.ready > 0 ? 'highlight' : ''}">
            <div class="task-stat-num ${tm.ready > 0 ? 'highlight-amber' : ''}">${tm.ready}</div>
            <div class="task-stat-lbl">Chờ xử lý</div>
          </div>
          <div class="task-stat-box">
            <div class="task-stat-num highlight-gold">${tm.in_progress}</div>
            <div class="task-stat-lbl">Đang chạy</div>
          </div>
          <div class="task-stat-box">
            <div class="task-stat-num highlight-emerald">${tm.done}</div>
            <div class="task-stat-lbl">Hoàn thành</div>
          </div>
        </div>

        <div class="project-card-actions">
          <button class="btn btn-sm btn-view-detail" onclick="openProjectDetail('${esc(p.id)}')">
            🔍 Xem Chi Tiết & Tasks →
          </button>
          <div class="card-icon-actions">
            <button class="btn-icon-action" title="Đồng bộ mã nguồn dự án này" onclick="syncProject('${esc(p.id)}')">
              🔄
            </button>
            <button class="btn-icon-action danger" title="Hủy quản lý dự án" onclick="removeProject('${esc(p.id)}')">
              🗑️
            </button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

// Batch Import Handler
async function handleBatchImport() {
  const input = document.getElementById('importInput');
  const rawText = input.value.trim();
  if (!rawText) {
    alert('Vui lòng nhập ít nhất 1 repository URL hoặc dạng owner/repo.');
    return;
  }

  const btn = document.getElementById('btnStartImport');
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">⏳</span> Đang Import…';

  const feedbackBox = document.getElementById('importFeedback');
  const feedbackList = document.getElementById('feedbackList');
  feedbackBox.classList.remove('hidden');
  feedbackList.innerHTML = '<div class="feedback-item">Đang kết nối GitHub và clone repository…</div>';

  try {
    const res = await api('/api/projects/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repositories: rawText })
    });

    const results = res.results || [];
    let feedbackHtml = '';

    results.forEach(r => {
      if (r.ok) {
        const p = r.project || {};
        const isAlready = p.already_registered;
        const badgeCls = isAlready ? 'already' : 'success';
        const badgeTxt = isAlready ? 'ĐÃ TỒN TẠI' : 'THÀNH CÔNG';
        feedbackHtml += `
          <div class="feedback-item ${badgeCls}">
            <div class="feedback-item-left">
              <span class="feedback-badge ${badgeCls}">✓ ${badgeTxt}</span>
              <span class="feedback-repo">${esc(p.repo || r.source)}</span>
            </div>
            <div class="feedback-info">
              ${isAlready ? 'Đã cập nhật phiên bản mới nhất' : 'Đã clone và thêm vào danh sách quản lý'}
              · Path: <span class="font-mono">${esc(p.managed_path || 'OK')}</span>
            </div>
          </div>
        `;
      } else {
        feedbackHtml += `
          <div class="feedback-item error">
            <div class="feedback-item-left">
              <span class="feedback-badge error">✕ LỖI</span>
              <span class="feedback-repo">${esc(r.source)}</span>
            </div>
            <div class="feedback-info highlight-rose">${esc(r.error || 'Thất bại')}</div>
          </div>
        `;
      }
    });

    feedbackList.innerHTML = feedbackHtml;
    input.value = '';
    updateInputCounter();
    await refresh();
  } catch (err) {
    feedbackList.innerHTML = `
      <div class="feedback-item error">
        <span class="feedback-badge error">✕ LỖI</span>
        <div class="feedback-info highlight-rose">${esc(err.message)}</div>
      </div>
    `;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">🚀</span> Bắt Đầu Import';
  }
}

// Project Actions
async function syncProject(projectId) {
  try {
    clearBlock();
    await api(`/api/projects/${encodeURIComponent(projectId)}/sync`, { method: 'POST' });
    await refresh();
  } catch (err) {
    showBlock(`Lỗi đồng bộ dự án ${projectId}: ${err.message}`);
  }
}

async function removeProject(projectId) {
  if (!confirm(`Bạn có chắc muốn xóa dự án "${projectId}" khỏi danh sách quản lý?`)) {
    return;
  }
  try {
    clearBlock();
    await api('/api/projects/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId })
    });
    if (currentDetailProjectId === projectId) {
      currentDetailProjectId = null;
      switchTab('dashboard');
    }
    await refresh();
  } catch (err) {
    showBlock(`Lỗi xóa dự án ${projectId}: ${err.message}`);
  }
}

// ================= TAB 2: DETAIL VIEW LOGIC =================
function openProjectDetail(projectId) {
  currentDetailProjectId = projectId;
  switchTab('detail');
}

function syncProjectSelect(projects) {
  const sel = document.getElementById('projectDetailSelect');
  if (!projects || projects.length === 0) {
    sel.innerHTML = '<option value="">Chưa có dự án nào</option>';
    currentDetailProjectId = null;
    return;
  }

  sel.innerHTML = projects.map(p => `
    <option value="${esc(p.id)}" ${p.id === currentDetailProjectId ? 'selected' : ''}>
      ${esc(p.id)} (${esc(p.repo)})
    </option>
  `).join('');

  if (!currentDetailProjectId || !projects.some(p => p.id === currentDetailProjectId)) {
    currentDetailProjectId = projects[0].id;
    sel.value = currentDetailProjectId;
  }

  document.getElementById('detailActivePill').textContent = currentDetailProjectId || 'Chưa chọn';
}

function renderProjectDetail() {
  if (!lastStatus || !lastStatus.projects) return;
  const projects = lastStatus.projects || [];
  const project = projects.find(p => p.id === currentDetailProjectId) || projects[0];
  if (!project) {
    document.getElementById('viewDetail').innerHTML = `
      <div class="empty-state card">
        <h3>Chưa chọn dự án</h3>
        <p>Vui lòng quay lại Dashboard và chọn một dự án để xem chi tiết.</p>
        <button class="btn btn-primary" onclick="switchTab('dashboard')">← Quay lại Dashboard</button>
      </div>
    `;
    return;
  }

  currentDetailProjectId = project.id;
  document.getElementById('detailActivePill').textContent = project.id;
  document.getElementById('detailTabTitle').textContent = `Chi Tiết: ${project.id}`;

  // Hero Card Info
  document.getElementById('detailProjectId').textContent = project.id;
  document.getElementById('detailRepoName').textContent = project.repo;
  const repoInfo = project.repo_info || {};
  document.getElementById('detailBranch').textContent = repoInfo.branch || project.default_branch || 'main';
  document.getElementById('detailGitDirty').innerHTML = repoInfo.dirty
    ? '<span class="highlight-amber">Có thay đổi chưa commit (Dirty)</span>'
    : '<span class="highlight-emerald">Sạch sẽ (Clean)</span>';

  const g = project.graphify || {};
  let graphStatus = 'Tắt';
  if (g.enabled) {
    graphStatus = g.graph_exists ? 'Sẵn sàng' : (g.cli_available ? 'Chưa tạo' : 'Thiếu CLI');
  }
  document.getElementById('detailGraphify').textContent = graphStatus;
  document.getElementById('detailPath').textContent = project.managed_path || '--';
  document.getElementById('btnOpenGithub').href = `https://github.com/${project.repo}`;

  // Stats Counters
  const tm = project.task_metrics || {
    total: project.total_tasks || 0,
    ready: project.ready_tasks || 0,
    in_progress: project.in_progress_tasks || 0,
    review: project.review_tasks || 0,
    done: project.done_tasks || 0
  };

  document.getElementById('detailTotalTasks').textContent = tm.total;
  document.getElementById('detailReadyTasks').textContent = tm.ready;
  document.getElementById('detailInProgressTasks').textContent = tm.in_progress;
  document.getElementById('detailReviewTasks').textContent = tm.review;
  document.getElementById('detailDoneTasks').textContent = tm.done;

  // Render Tasks for this project
  renderDetailTasks(project);

  // Render Config Tab for this project
  renderConfigDisplay(project);
}

function renderDetailTasks(project) {
  const container = document.getElementById('detailTaskList');
  const allProjectIssues = project.issues || [];

  const filtered = allProjectIssues.filter(item => {
    const lc = item.lifecycle || {};
    if (currentTaskFilter === 'ready') return lc.status === 'READY';
    if (currentTaskFilter === 'in_progress') return [1, 2, 3].includes(lc.stage_index);
    if (currentTaskFilter === 'review') return lc.stage_index === 4;
    if (currentTaskFilter === 'done') return lc.stage_index === 5;
    return true;
  });

  document.getElementById('detailTaskFilterCount').textContent = `${filtered.length} tasks`;

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="placeholder-msg">
        Không có task nào trong trạng thái "${esc(currentTaskFilter)}".
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(item => {
    const task = item.task || {};
    const lc = item.lifecycle || {};
    return `
      <div class="task-item">
        <div class="task-item-header">
          <span class="task-ref font-mono">
            #${esc(item.number)} · ${esc(task.task_id || 'ISSUE')}
          </span>
          <span class="task-state-badge highlight-cyan font-mono">
            ${esc(lc.status || 'UNKNOWN')}
          </span>
        </div>
        <div class="task-title">
          <a href="${esc(item.url || '#')}" target="_blank">
            ${esc(item.title)} ↗
          </a>
        </div>
        ${renderLifecycleStepper(lc)}
        <div class="task-item-footer">
          <span><b>Loại:</b> ${esc(task.type || 'Chưa định nghĩa')}</span>
          <span><b>Priority:</b> ${esc(task.priority ?? '--')}</span>
          <span><b>Rev:</b> ${esc(task.revision ?? '--')}</span>
          ${item.task_error ? `<span class="highlight-rose font-mono">${esc(item.task_error)}</span>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function renderConfigDisplay(project) {
  const pre = document.getElementById('configDisplay');
  if (!project) {
    pre.textContent = 'Chưa có dữ liệu';
    return;
  }

  if (currentConfigTab === 'agents') {
    pre.textContent = JSON.stringify(project.agent_profiles || {}, null, 2);
  } else if (currentConfigTab === 'tasks') {
    pre.textContent = JSON.stringify(project.task_profile_configs || {}, null, 2);
  } else if (currentConfigTab === 'plans') {
    pre.textContent = JSON.stringify(project.plans || [], null, 2);
  } else if (currentConfigTab === 'raw') {
    pre.textContent = JSON.stringify(project, null, 2);
  }
}

// ================= TAB 3: RUNTIME & TELEMETRY =================
function renderTelemetry(status) {
  const active = status.active;
  const p = active?.payload || {};
  const dot = document.getElementById('liveDot');
  const navDot = document.getElementById('navLiveDot');

  const isLive = Boolean(active);
  dot.className = `status-indicator-dot ${isLive ? 'live' : ''}`;
  navDot.className = `live-dot ${isLive ? 'live' : ''}`;

  document.getElementById('liveLifecycleBadge').textContent = `STATE: ${active?.status || 'IDLE'}`;
  document.getElementById('liveProjectBadge').textContent = `PROJECT: ${p.project || 'NONE'}`;
  document.getElementById('liveIssueBadge').textContent = `ISSUE: ${active ? '#' + active.issue_number : 'NONE'}`;
  document.getElementById('liveReviewBadge').textContent = `REVIEW: ${p.review_cycle || 0}`;

  document.getElementById('liveTaskVal').textContent = p.task_id || 'None';
  document.getElementById('liveBranchVal').textContent = p.branch || 'None';
  document.getElementById('livePrVal').textContent = p.pr_number ? `#${p.pr_number}` : 'None';
  document.getElementById('liveSyncVal').textContent = fmtTime(status.auto_sync?.last_sync_at);
}

function renderTerminalEvents(events) {
  if (localEventsCleared) return;
  const terminal = document.getElementById('liveTerminal');
  if (!events || events.length === 0) {
    terminal.innerHTML = '<div class="term-line term-system">[SYSTEM] Đang chờ runtime events…</div>';
    return;
  }

  terminal.innerHTML = events.slice().reverse().map(e => `
    <div class="term-line">
      <span class="term-time">[${new Date(e.created_at * 1000).toLocaleTimeString()}]</span>
      <span class="term-type">[${esc(e.type)}]</span>
      <span class="term-payload">${esc(JSON.stringify(e.payload))}</span>
    </div>
  `).join('');
  terminal.scrollTop = terminal.scrollHeight;
}

// Global Refresh
async function refresh() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    clearBlock();

    // System Badges
    const modeBadge = document.getElementById('modeBadge');
    modeBadge.innerHTML = `<span class="status-dot"></span> Engine: ${s.paused ? 'TẠM DỪNG' : 'TỰ ĐỘNG'}`;
    const pauseBtn = document.getElementById('btnPauseResume');
    pauseBtn.textContent = s.paused ? '▶ Khởi động' : '⏸ Tạm dừng';

    const ghBadge = document.getElementById('githubBadge');
    const auth = s.github_auth || {};
    if (auth.connected) {
      ghBadge.className = 'status-pill';
      ghBadge.innerHTML = `<span class="status-dot"></span> GitHub: ${esc(auth.login || 'Đã kết nối')}`;
    } else {
      ghBadge.className = 'status-pill offline';
      ghBadge.innerHTML = '<span class="status-dot"></span> GitHub: Ngoại tuyến';
      ghBadge.title = auth.error || 'Cần chạy gh auth login';
    }

    document.getElementById('dashManagedRoot').textContent = `Thư mục quản lý: ${s.system?.managed_root || 'Chưa cấu hình'}`;

    // Render Dashboard
    renderDashboardKPIs(s);
    renderProjectsGrid(s.projects || []);

    // Render Detail
    syncProjectSelect(s.projects || []);
    if (currentTab === 'detail') {
      renderProjectDetail();
    }

    // Render Runtime
    renderTelemetry(s);
    renderTerminalEvents(s.events || []);

    if (s.issues_error && !String(s.issues_error).includes('GitHub not connected')) {
      showBlock(s.issues_error);
    }
  } catch (err) {
    showBlock(`Lỗi cập nhật trạng thái: ${err.message}`);
  }
}

function updateInputCounter() {
  const val = document.getElementById('importInput').value.trim();
  if (!val) {
    document.getElementById('importInputCounter').textContent = '0 dòng';
    return;
  }
  const lines = val.split(/[\r\n,]+/).map(s => s.trim()).filter(Boolean);
  document.getElementById('importInputCounter').textContent = `${lines.length} dự án phát hiện`;
}

// Event Listeners Initialization
function initEventListeners() {
  // Tab Switching
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btnBackToDashboard').addEventListener('click', () => switchTab('dashboard'));

  // KPI & Global Actions
  document.getElementById('btnPauseResume').addEventListener('click', async () => {
    try {
      const isPaused = lastStatus?.paused;
      await api(isPaused ? '/api/control/resume' : '/api/control/pause', { method: 'POST' });
      await refresh();
    } catch (e) {
      showBlock(e.message);
    }
  });

  document.getElementById('btnSyncAll').addEventListener('click', async () => {
    try {
      await api('/api/control/sync', { method: 'POST' });
      await refresh();
    } catch (e) {
      showBlock(e.message);
    }
  });

  document.getElementById('btnRefreshList').addEventListener('click', refresh);

  // Import Box
  document.getElementById('importInput').addEventListener('input', updateInputCounter);
  document.getElementById('btnStartImport').addEventListener('click', handleBatchImport);
  document.getElementById('btnClearImport').addEventListener('click', () => {
    document.getElementById('importInput').value = '';
    updateInputCounter();
  });
  document.getElementById('btnCloseFeedback').addEventListener('click', () => {
    document.getElementById('importFeedback').classList.add('hidden');
  });

  // Search filter
  document.getElementById('searchProjects').addEventListener('input', e => {
    searchQuery = e.target.value;
    if (lastStatus) renderProjectsGrid(lastStatus.projects || []);
  });

  // Project Detail Controls
  document.getElementById('projectDetailSelect').addEventListener('change', e => {
    currentDetailProjectId = e.target.value;
    renderProjectDetail();
  });

  document.getElementById('btnSyncCurrentProject').addEventListener('click', () => {
    if (currentDetailProjectId) syncProject(currentDetailProjectId);
  });

  document.getElementById('btnUpdateGraphify').addEventListener('click', async () => {
    if (!currentDetailProjectId) return;
    try {
      await api(`/api/projects/${encodeURIComponent(currentDetailProjectId)}/graphify/update`, { method: 'POST' });
      alert('Đã yêu cầu cập nhật Graphify knowledge context!');
      await refresh();
    } catch (e) {
      showBlock(e.message);
    }
  });

  document.getElementById('btnRemoveCurrentProject').addEventListener('click', () => {
    if (currentDetailProjectId) removeProject(currentDetailProjectId);
  });

  // Task Filters
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentTaskFilter = btn.dataset.filter;
      if (lastStatus && currentDetailProjectId) {
        const p = (lastStatus.projects || []).find(x => x.id === currentDetailProjectId);
        if (p) renderDetailTasks(p);
      }
    });
  });

  // Config Tabs
  document.querySelectorAll('.cfg-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cfg-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentConfigTab = btn.dataset.cfg;
      if (lastStatus && currentDetailProjectId) {
        const p = (lastStatus.projects || []).find(x => x.id === currentDetailProjectId);
        if (p) renderConfigDisplay(p);
      }
    });
  });

  // Terminal Controls
  document.getElementById('btnClearEvents').addEventListener('click', () => {
    localEventsCleared = true;
    document.getElementById('liveTerminal').innerHTML = '<div class="term-line term-system">[SYSTEM] Đã xóa màn hình sự kiện.</div>';
  });
}

// Start
initEventListeners();
refresh();
setInterval(refresh, 5000);
