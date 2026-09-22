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

function fmtDateTime(ts, dtStr) {
  if (dtStr) return dtStr;
  if (!ts) return 'Chưa có';
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleString('vi-VN');
  } catch {
    return String(ts);
  }
}

function fmtTime(ts) {
  return fmtDateTime(ts);
}

async function saveGithubToken() {
  const input = document.getElementById('inputGithubToken');
  const token = (input?.value || '').trim();
  if (!token) {
    alert('Vui lòng nhập GitHub Token (ghp_...)');
    return;
  }
  try {
    const res = await api('/api/auth/token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
    if (res.ok && res.auth?.connected) {
      document.getElementById('githubAuthBanner')?.classList.add('hidden');
      alert(`Đã kết nối GitHub thành công và lưu thông tin vào Git credentials để tự động sử dụng cho các lần sau! Tài khoản: ${res.auth.login}`);
      await refresh();
    } else {
      alert(`Kết nối thất bại: ${res.auth?.error || 'Token không hợp lệ'}`);
    }
  } catch (err) {
    alert(`Lỗi kết nối GitHub: ${err.message}`);
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
    const reason = lc.blocked_reason || 'Đang bị nghẽn, cần can thiệp xử lý';
    const output = lc.blocked_output || '';
    banner = `
      <div class="blocked-banner-card">
        <div class="blocked-banner-header">
          <div class="blocked-banner-title">
            <span class="blocked-pulse-dot"></span>
            <b>BLOCKED</b> · Đang bị nghẽn / Chờ can thiệp
          </div>
          <button class="btn btn-sm btn-primary btn-retry-blocked" onclick="retryActiveTask(event)">
            🔄 Thử Lại (Retry)
          </button>
        </div>
        <div class="blocked-reason-row">
          <span class="blocked-reason-label">Lý do:</span>
          <span class="blocked-reason-val font-mono">${esc(reason)}</span>
        </div>
        ${output ? `
          <div class="blocked-log-container">
            <div class="blocked-log-bar">
              <span class="blocked-log-title">📋 Chi Tiết Lỗi & Log Hệ Thống (Local Only):</span>
              <button class="btn-copy-log" onclick="copyBlockedLog(this, event)">📋 Copy Log</button>
            </div>
            <pre class="blocked-log-view font-mono">${esc(output)}</pre>
          </div>
        ` : ''}
      </div>`;
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
  const cap = status.worker_capacity || { active: 0, max: 2 };

  document.getElementById('dashTotalProjects').textContent = projects.length;
  document.getElementById('dashTotalTasks').textContent = issues.length;
  document.getElementById('dashReadyTasks').textContent = metrics.ready || 0;
  document.getElementById('dashActiveTasks').textContent = (metrics.in_progress || 0) + (metrics.review || 0);

  const workerBadge = document.getElementById('workerBadge');
  if (workerBadge) {
    workerBadge.innerHTML = `<span class="status-dot"></span> Workers: ${cap.active}/${cap.max}`;
    workerBadge.title = `Đang chạy: ${cap.active} task song song · Tối đa: ${cap.max} (ORCH_MAX_WORKERS)`;
  }

  document.getElementById('navProjectCount').textContent = projects.length;
  document.getElementById('projectsPill').textContent = `${projects.length} dự án`;

  // Render Active / Blocked Task Cards on Dashboard
  const activeTaskSection = document.getElementById('dashActiveTaskSection');
  if (activeTaskSection) {
    const activeList = (status.active_tasks && status.active_tasks.length > 0)
      ? status.active_tasks
      : (status.active && status.active.issue_number ? [status.active] : []);

    if (activeList.length > 0) {
      activeTaskSection.className = 'active-tasks-container';
      activeTaskSection.innerHTML = activeList.map(task => {
        const p = task.payload || {};
        const lc = task.lifecycle || {};
        const isBlocked = task.status === 'BLOCKED' || lc.is_blocked;
        const statusBadgeCls = isBlocked ? 'blocked' : 'running';
        const statusBadgeText = isBlocked ? '⚠️ BLOCKED' : '⚡ RUNNING';
        return `
          <div class="active-task-hero-card ${isBlocked ? 'blocked-active' : ''}">
            <div class="active-task-header">
              <div class="active-task-title-group">
                <span class="active-task-badge ${statusBadgeCls}">${statusBadgeText}</span>
                <span class="active-task-name font-mono">#${esc(task.issue_number)} · ${esc(p.task_id || 'TASK')} (${esc(p.project || p.issue_repo)})</span>
              </div>
              <div class="active-task-actions">
                ${isBlocked ? `<button class="btn btn-sm btn-warning" onclick="retryActiveTask(event)">🔄 Thử Lại</button>` : ''}
                <a href="https://github.com/${esc(p.issue_repo || '')}/issues/${esc(task.issue_number)}" target="_blank" class="btn btn-sm btn-outline">Mở Issue trên GitHub ↗</a>
              </div>
            </div>
            ${renderLifecycleStepper(lc)}
          </div>
        `;
      }).join('');
    } else {
      activeTaskSection.className = 'hidden';
      activeTaskSection.innerHTML = '';
    }
  }
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
          <span class="meta-chip">🕒 Đồng bộ: <b>${fmtDateTime(p.last_sync_at, p.last_sync_datetime)}</b></span>
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

// Copy blocked log
function copyBlockedLog(btn, e) {
  if (e) e.stopPropagation();
  const pre = btn.closest('.blocked-log-container')?.querySelector('.blocked-log-view');
  if (pre) {
    navigator.clipboard.writeText(pre.textContent).then(() => {
      const orig = btn.textContent;
      btn.textContent = '✓ Đã Copy';
      setTimeout(() => btn.textContent = orig, 2000);
    }).catch(err => {
      alert('Không thể sao chép: ' + err);
    });
  }
}

// Retry active task
async function retryActiveTask(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  try {
    const res = await api('/api/control/retry', { method: 'POST' });
    alert(res.message || 'Đã kích hoạt thử lại task!');
    await refresh();
  } catch (err) {
    alert('Lỗi khi thử lại task: ' + err.message);
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
    if (currentTaskFilter === 'waiting') return lc.status === 'WAITING_CONDITION';
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
    const isWaiting = lc.status === 'WAITING_CONDITION';
    const conditions = task.condition || [];
    return `
      <div class="task-item">
        <div class="task-item-header">
          <span class="task-ref font-mono">
            #${esc(item.number)} · ${esc(task.task_id || 'ISSUE')}
          </span>
          <span class="task-state-badge ${isWaiting ? 'waiting' : 'highlight-cyan'} font-mono">
            ${isWaiting ? '⏳ CHỜ PHỤ THUỘC' : esc(lc.status || 'UNKNOWN')}
          </span>
        </div>
        <div class="task-title">
          <a href="${esc(item.url || '#')}" target="_blank">
            ${esc(item.title)} ↗
          </a>
        </div>
        ${conditions.length > 0 ? `
          <div class="task-condition-row">
            <span class="condition-label">Phụ thuộc:</span>
            ${conditions.map(c => `<span class="condition-pill font-mono">${esc(c)}</span>`).join(' ')}
          </div>
        ` : ''}
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
  const container = document.getElementById('configContent');
  if (!container) return;
  if (!project) {
    container.innerHTML = '<div class="placeholder-msg">Chưa có dữ liệu dự án</div>';
    return;
  }

  const currentModel = lastStatus?.agy_config?.model || 'gemini-3.8-flash-high';
  const availableModels = lastStatus?.agy_config?.available_models || [
    { id: 'gemini-3.8-flash-high', name: 'Gemini 3.8 Flash (High)' }
  ];

  if (currentConfigTab === 'tasks') {
    const tasks = project.task_profile_configs || {};
    const taskKeys = Object.keys(tasks);
    if (taskKeys.length === 0) {
      container.innerHTML = '<div class="placeholder-msg">Chưa có Task Profile nào được định nghĩa</div>';
      return;
    }
    container.innerHTML = `
      <div class="step-cards-container">
        ${taskKeys.map(k => {
          const t = tasks[k] || {};
          const types = t.task_types || [];
          return `
            <div class="step-card">
              <div class="step-card-header">
                <div class="step-title-group">
                  <span class="step-badge">Bước (Task Profile)</span>
                  <span class="step-id font-mono">${esc(k)}</span>
                </div>
                <div class="step-types">
                  ${types.map(typ => `<span class="step-type-pill">${esc(typ)}</span>`).join('')}
                </div>
              </div>
              <div class="step-agents-row">
                <div class="step-agent-box">
                  <div class="step-agent-role">⚙️ Executor Agent</div>
                  <div class="step-agent-name">${esc(t.executor_profile || 'Chưa gán')}</div>
                  <div class="step-agent-model">🤖 Model: <strong>${esc(t.executor_model || currentModel)}</strong></div>
                </div>
                <div class="step-agent-box">
                  <div class="step-agent-role">🔍 Reviewer Agent</div>
                  <div class="step-agent-name">${esc(t.reviewer_profile || 'Chưa gán')}</div>
                  <div class="step-agent-model">🤖 Model: <strong>${esc(t.reviewer_model || currentModel)}</strong></div>
                </div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  } else if (currentConfigTab === 'agents') {
    const agents = project.agent_profiles || {};
    const agentKeys = Object.keys(agents);
    if (agentKeys.length === 0) {
      container.innerHTML = '<div class="placeholder-msg">Chưa có Agent Profile nào được cấu hình</div>';
      return;
    }
    container.innerHTML = `
      <div class="agent-card-grid">
        ${agentKeys.map(k => {
          const a = agents[k] || {};
          const instrs = a.instructions || [];
          const isInherited = a.is_model_inherited !== false;
          const effectiveModel = a.model || currentModel;
          return `
            <div class="agent-profile-card">
              <div class="agent-card-top">
                <span class="agent-card-id font-mono">🤖 ${esc(k)}</span>
                <div class="agent-model-select-group">
                  <label class="agent-model-lbl">Model:</label>
                  <select class="agent-model-select" onchange="onAgentModelChange(this, '${esc(k)}')">
                    <option value="" ${isInherited ? 'selected' : ''}>Kế thừa mặc định (${esc(currentModel)})</option>
                    ${availableModels.map(m => `
                      <option value="${esc(m.id)}" ${(!isInherited && a.configured_model === m.id) ? 'selected' : ''}>
                        ${esc(m.name || m.id)}
                      </option>
                    `).join('')}
                  </select>
                  <span class="agent-model-tag ${isInherited ? 'inherited' : 'custom'}">
                    ${isInherited ? 'Mặc định' : 'Tùy biến'}
                  </span>
                </div>
              </div>
              <div class="agent-meta-row">
                <span>Vai trò: <strong class="highlight-cyan">${esc(a.role || 'executor')}</strong></span>
                <span>AGY Agent: <code class="font-mono">${esc(a.agy_agent || 'default')}</code></span>
                <span>Effort: <strong class="highlight-amber">${esc(a.effort || 'medium')}</strong></span>
                <span>Áp dụng: <strong class="highlight-purple font-mono">${esc(effectiveModel)}</strong></span>
              </div>
              ${instrs.length > 0 ? `
                <ul class="agent-instructions-list">
                  ${instrs.map(ins => `<li>${esc(ins)}</li>`).join('')}
                </ul>
              ` : ''}
            </div>
          `;
        }).join('')}
      </div>
    `;
  } else if (currentConfigTab === 'plans') {
    const plans = project.plans || [];
    if (plans.length === 0) {
      container.innerHTML = '<div class="placeholder-msg">Không tìm thấy tài liệu kế hoạch (plan) nào</div>';
      return;
    }
    container.innerHTML = `
      <div class="step-cards-container">
        ${plans.map(p => `
          <div class="step-card">
            <div class="step-title-group">
              <span class="step-badge">📄 Kế hoạch</span>
              <span class="font-mono">${esc(p)}</span>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } else if (currentConfigTab === 'raw') {
    container.innerHTML = `<pre id="configDisplay">${esc(JSON.stringify(project, null, 2))}</pre>`;
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
  document.getElementById('liveSyncVal').textContent = fmtDateTime(status.auto_sync?.last_sync_at, status.auto_sync?.last_sync_datetime);
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

    const retryBtn = document.getElementById('btnRetryActiveTask');
    if (retryBtn) {
      const isBlocked = s.active && s.active.status === 'BLOCKED';
      retryBtn.classList.toggle('hidden', !isBlocked);
    }

    const ghBadge = document.getElementById('githubBadge');
    const auth = s.github_auth || {};
    const ghBanner = document.getElementById('githubAuthBanner');
    if (auth.connected) {
      ghBadge.className = 'status-pill';
      ghBadge.innerHTML = `<span class="status-dot"></span> GitHub: ${esc(auth.login || 'Đã kết nối')}`;
      if (ghBanner) ghBanner.classList.add('hidden');
    } else {
      ghBadge.className = 'status-pill offline';
      ghBadge.innerHTML = '<span class="status-dot"></span> GitHub: Ngoại tuyến';
      ghBadge.title = auth.error || 'Cần cấu hình GITHUB_TOKEN hoặc chạy gh auth login';
      if (ghBanner) ghBanner.classList.remove('hidden');
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

    // Sync AGY Model Config
    syncAgyModelUI(s);

    if (s.issues_error && !String(s.issues_error).includes('GitHub not connected')) {
      showBlock(s.issues_error);
    }
  } catch (err) {
    showBlock(`Lỗi cập nhật trạng thái: ${err.message}`);
  }
}

function syncAgyModelUI(s) {
  const agyCfg = s?.agy_config || {};
  const currentModel = agyCfg.model || 'gemini-3.8-flash-high';
  const available = agyCfg.available_models || [
    { id: 'gemini-3.8-flash-high', name: 'Gemini 3.8 Flash (High)', default: true }
  ];

  const selectNav = document.getElementById('selectAgyModel');
  const selectCard = document.getElementById('selectAgyModelCard');
  const currentLabel = document.getElementById('currentAgyModelLabel');

  if (currentLabel) {
    currentLabel.textContent = currentModel;
  }

  const populateSelect = (selectEl) => {
    if (!selectEl) return;
    if (selectEl.options.length <= 1 || selectEl.dataset.loadedCount != available.length) {
      selectEl.innerHTML = available.map(m => {
        const isDef = m.id === 'gemini-3.8-flash-high' ? ' (Mặc định)' : '';
        return `<option value="${esc(m.id)}">${esc(m.name)}${isDef}</option>`;
      }).join('');
      selectEl.dataset.loadedCount = available.length;
    }
    if (document.activeElement !== selectEl) {
      selectEl.value = currentModel;
    }
  };

  populateSelect(selectNav);
  populateSelect(selectCard);
}

async function handleAgyModelChange(newModel) {
  if (!newModel) return;
  try {
    const res = await api('/api/config/agy-model', {
      method: 'POST',
      body: JSON.stringify({ model: newModel })
    });
    if (res.ok) {
      const toast = document.getElementById('modelSaveToast');
      if (toast) {
        toast.classList.remove('hidden');
        setTimeout(() => toast.classList.add('hidden'), 2500);
      }
      await refresh();
    }
  } catch (err) {
    showBlock(`Lỗi cấu hình AGY Model: ${err.message}`);
  }
}

window.onAgentModelChange = async function(selectEl, agentId) {
  const model = selectEl.value;
  try {
    const res = await api('/api/config/agent-model', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, model: model })
    });
    if (res.ok) {
      const toast = document.getElementById('modelSaveToast');
      if (toast) {
        toast.textContent = `✓ Đã lưu model: ${agentId}`;
        toast.classList.remove('hidden');
        setTimeout(() => toast.classList.add('hidden'), 2500);
      }
      await refresh();
    }
  } catch (err) {
    showBlock(`Lỗi cấu hình Model cho Agent ${agentId}: ${err.message}`);
  }
};

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

  // Model AGY Selector Listeners
  const selectNav = document.getElementById('selectAgyModel');
  const selectCard = document.getElementById('selectAgyModelCard');
  const btnSaveCard = document.getElementById('btnSaveAgyModel');

  if (selectNav) {
    selectNav.addEventListener('change', (e) => {
      if (selectCard) selectCard.value = e.target.value;
      handleAgyModelChange(e.target.value);
    });
  }
  if (selectCard) {
    selectCard.addEventListener('change', (e) => {
      if (selectNav) selectNav.value = e.target.value;
      handleAgyModelChange(e.target.value);
    });
  }
  if (btnSaveCard) {
    btnSaveCard.addEventListener('click', () => {
      const val = selectCard ? selectCard.value : (selectNav ? selectNav.value : '');
      if (val) handleAgyModelChange(val);
    });
  }

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
