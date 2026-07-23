<script>
  /**
   * Projects view — list, search, create, archive, and delete projects.
   *
   * Displays a searchable project grid with status badges and quick actions.
   */
  import { appState } from '$lib/stores/app.svelte.js';
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import ProjectReceiptStrip from '$components/projects/ProjectReceiptStrip.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  // -- State -------------------------------------------------------------------

  let projects = $state([]);
  let loading = $state(true);
  /** Set to a message string when the initial project fetch fails, distinct from "no projects". */
  let loadError = $state(null);
  let searchQuery = $state('');
  let statusFilter = $state('all');
  let showCreateForm = $state(false);
  let actionPending = $state(false);

  let newProject = $state({
    name: '',
    description: '',
    model: '',
  });

  // -- Derived -----------------------------------------------------------------

  let filteredProjects = $derived(
    projects
      .filter((p) => {
        if (statusFilter !== 'all') {
          const s = p.status ?? 'pending';
          // normalise complete/completed so the Done filter matches both spellings
          const normalised = (s === 'complete') ? 'completed' : s;
          if (normalised !== statusFilter) return false;
        }
        if (searchQuery.trim()) {
          const q = searchQuery.toLowerCase();
          return (
            (p.name ?? '').toLowerCase().includes(q) ||
            (p.description ?? '').toLowerCase().includes(q) ||
            (p.id ?? p.project_id ?? '').toLowerCase().includes(q)
          );
        }
        return true;
      })
      .sort((a, b) => {
        const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
        const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
        return tb - ta;
      })
  );

  let statusCounts = $derived({
    all: projects.length,
    in_progress: projects.filter((p) => p.status === 'in_progress').length,
    completed: projects.filter((p) => p.status === 'completed' || p.status === 'complete').length,
    pending: projects.filter((p) => !p.status || p.status === 'pending').length,
    failed: projects.filter((p) => p.status === 'failed').length,
  });

  // -- Data loading ------------------------------------------------------------

  async function loadProjects() {
    loading = true;
    loadError = null;
    try {
      // No inline .catch() here — let real errors propagate to the catch block
      // so we can distinguish a fetch failure from a legitimately empty list.
      const data = await api.listProjects();
      projects = data?.projects ?? (Array.isArray(data) ? data : []);
    } catch (err) {
      loadError = err?.message ?? 'Failed to load projects';
      showToast(`Failed to load projects: ${err.message}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => { loadProjects(); });

  // -- Actions -----------------------------------------------------------------

  async function handleCreate(e) {
    e.preventDefault();
    if (!newProject.name.trim()) {
      showToast('Project name is required', 'warning');
      return;
    }
    actionPending = true;
    try {
      const result = await api.createProject(newProject);
      const projectId = result?.project_id ?? result?.id;
      showToast(`Project "${newProject.name}" created`, 'success');
      showCreateForm = false;
      newProject = { name: '', description: '', model: '' };
      await loadProjects();
      if (projectId) {
        appState.currentProjectId = projectId;
        appState.currentView = 'prompt';
      }
    } catch (err) {
      showToast(`Failed to create project: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handlePauseProject(projectId, name) {
    try {
      await api.pauseProject(projectId);
      await loadProjects();
      showToast(`Project "${name}" paused`, 'info');
    } catch (err) {
      showToast(`Pause failed: ${err.message}`, 'error');
    }
  }

  async function handleResumeProject(projectId, name) {
    try {
      await api.resumeProject(projectId);
      await loadProjects();
      showToast(`Project "${name}" resumed`, 'success');
    } catch (err) {
      showToast(`Resume failed: ${err.message}`, 'error');
    }
  }

  async function handleArchive(projectId, name) {
    if (!confirm(`Archive project "${name}"?`)) return;
    try {
      await api.archiveProject(projectId);
      await loadProjects();
      showToast('Project archived', 'info');
    } catch (err) {
      showToast(`Archive failed: ${err.message}`, 'error');
    }
  }

  async function handleDelete(projectId, name) {
    if (!confirm(`Permanently delete project "${name}"? This cannot be undone.`)) return;
    try {
      await api.deleteProject(projectId);
      projects = projects.filter((p) => (p.id ?? p.project_id) !== projectId);
      if (appState.currentProjectId === projectId) {
        appState.currentProjectId = null;
      }
      showToast('Project deleted', 'info');
    } catch (err) {
      showToast(`Delete failed: ${err.message}`, 'error');
    }
  }

  function openProject(projectId) {
    appState.currentProjectId = projectId;
    appState.currentView = 'prompt';
  }

  function statusColor(status) {
    const map = {
      in_progress: 'primary',
      running: 'primary',
      paused: 'warning',
      completed: 'success',
      complete: 'success',
      failed: 'danger',
      archived: 'muted',
      pending: 'warning',
    };
    return map[status ?? 'pending'] ?? 'muted';
  }

  function statusLabel(status) {
    const map = {
      in_progress: 'In Progress',
      running: 'Running',
      paused: 'Paused',
      completed: 'Completed',
      complete: 'Completed',
      failed: 'Failed',
      archived: 'Archived',
      pending: 'Pending',
    };
    return map[status ?? 'pending'] ?? (status ?? 'Unknown');
  }
</script>

<div class="projects-view">
  <div class="view-header">
    <h2>
      <i class="fas fa-folder-open"></i>
      Projects
      <span class="project-count">{projects.length}</span>
    </h2>
    <HelpPopover
      title="Projects"
      body="Each project is an isolated Workbench context with its own run history, memory store, training corpus, gateway policy profile, and resource lease namespace. Switching the active project changes all panels. Creating a project provisions a persistent spine record and an empty memory store; it does not start any runs automatically. Archiving a project stops future runs but preserves history — archived projects can be reopened."
      severity="info"
    />
    <div class="header-actions">
      <button
        class="btn btn-primary"
        onclick={() => { showCreateForm = !showCreateForm; }}
        aria-expanded={showCreateForm}
        aria-label="Create new project"
      >
        <i class="fas fa-plus"></i>
        New Project
      </button>
      <button
        class="btn btn-secondary btn-sm"
        onclick={loadProjects}
        disabled={loading}
        aria-label="Refresh projects"
      >
        <i class="fas fa-sync-alt" class:fa-spin={loading}></i>
      </button>
    </div>
  </div>

  <!-- Create form -->
  {#if showCreateForm}
    <section class="card create-form-card" aria-label="Create project form">
      <h3 class="form-title"><i class="fas fa-plus-circle"></i> New Project</h3>
      <form onsubmit={handleCreate} class="create-form" aria-label="New project details">
        <div class="form-row">
          <label class="form-group" style="flex: 2">
            <span class="form-label">Project Name</span>
            <input
              type="text"
              class="input"
              bind:value={newProject.name}
              placeholder="My AI project..."
              required
              aria-required="true"
              aria-label="Project name"
            />
          </label>
          <label class="form-group" style="flex: 1">
            <span class="form-label">Model (optional)</span>
            <input
              type="text"
              class="input"
              bind:value={newProject.model}
              placeholder="e.g. mistral-7b"
              aria-label="Model override"
            />
          </label>
        </div>
        <label class="form-group">
          <span class="form-label">Description (optional)</span>
          <textarea
            class="input textarea"
            bind:value={newProject.description}
            rows="3"
            placeholder="Describe what this project will do..."
            aria-label="Project description"
          ></textarea>
        </label>
        <div class="form-actions">
          <button
            type="submit"
            class="btn btn-primary"
            disabled={actionPending || !newProject.name.trim()}
            aria-label="Create project"
          >
            <i class="fas fa-rocket"></i> Create
          </button>
          <button
            type="button"
            class="btn btn-secondary"
            onclick={() => { showCreateForm = false; }}
            aria-label="Cancel"
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  {/if}

  <!-- Search + filter bar -->
  <div class="filter-bar">
    <div class="search-wrap">
      <i class="fas fa-search search-icon" aria-hidden="true"></i>
      <input
        type="search"
        class="input search-input"
        bind:value={searchQuery}
        placeholder="Search projects..."
        aria-label="Search projects"
      />
    </div>

    <div class="status-filters" role="group" aria-label="Filter by status">
      {#each [
        { key: 'all', label: 'All' },
        { key: 'in_progress', label: 'Active' },
        { key: 'pending', label: 'Pending' },
        { key: 'completed', label: 'Done' },
        { key: 'failed', label: 'Failed' },
      ] as f (f.key)}
        <button
          class="filter-btn"
          class:active={statusFilter === f.key}
          onclick={() => { statusFilter = f.key; }}
          aria-pressed={statusFilter === f.key}
          aria-label="Filter: {f.label} ({statusCounts[f.key] ?? 0})"
        >
          {f.label}
          <span class="filter-count">{statusCounts[f.key] ?? 0}</span>
        </button>
      {/each}
    </div>
  </div>

  <!-- Content -->
  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <i class="fas fa-spinner fa-spin"></i>
      Loading projects...
    </div>
  {:else if loadError}
    <div class="error-state" role="alert">
      <i class="fas fa-exclamation-triangle"></i>
      <p>Could not load projects: {loadError}</p>
      <button class="btn btn-secondary btn-sm" onclick={loadProjects}>
        <i class="fas fa-redo"></i> Retry
      </button>
    </div>
  {:else if filteredProjects.length === 0}
    <div class="empty-state">
      <i class="fas fa-folder"></i>
      <p>{searchQuery || statusFilter !== 'all' ? 'No projects match your filters.' : 'No projects yet.'}</p>
      {#if !showCreateForm}
        <button class="btn btn-primary" onclick={() => { showCreateForm = true; }}>
          <i class="fas fa-plus"></i> Create your first project
        </button>
      {/if}
    </div>
  {:else}
    <div class="projects-grid" role="list" aria-label="Projects">
      {#each filteredProjects as project ((project.id ?? project.project_id))}
        {@const pid = project.id ?? project.project_id}
        {@const isActive = appState.currentProjectId === pid}
        <article
          class="project-card"
          class:active={isActive}
          role="listitem"
          aria-label="Project: {project.name}"
          aria-current={isActive ? 'true' : undefined}
        >
          <div class="project-card-header">
            <div class="project-title-row">
              <button
                class="project-name-btn"
                onclick={() => openProject(pid)}
                aria-label="Open project {project.name}"
              >
                {project.name ?? 'Untitled Project'}
              </button>
              <span class="status-badge status-{statusColor(project.status)}">
                {statusLabel(project.status)}
              </span>
            </div>
            {#if project.description}
              <p class="project-desc">{project.description}</p>
            {/if}
          </div>

          <div class="project-meta">
            {#if project.model}
              <span class="meta-item">
                <i class="fas fa-microchip"></i>
                {project.model}
              </span>
            {/if}
            {#if project.task_count != null}
              <span class="meta-item">
                <i class="fas fa-tasks"></i>
                {fmt.integer(project.task_count)} tasks
              </span>
            {/if}
            <span class="meta-item">
              <i class="fas fa-clock"></i>
              {fmt.relativeTime(project.updated_at ?? project.created_at)}
            </span>
          </div>

          <ProjectReceiptStrip projectId={pid} />

          {#if project.progress != null && project.status === 'in_progress'}
            <div class="project-progress">
              <div
                class="progress-bar"
                role="progressbar"
                aria-valuenow={project.progress}
                aria-valuemin="0"
                aria-valuemax="100"
                aria-label="Progress: {project.progress}%"
              >
                <div class="progress-fill" style="width: {Math.min(100, project.progress)}%"></div>
              </div>
              <span class="progress-pct">{fmt.percent(project.progress, 0)}</span>
            </div>
          {/if}

          <div class="project-actions">
            <button
              class="btn btn-primary btn-sm"
              onclick={() => openProject(pid)}
              aria-label="Open project {project.name}"
            >
              <i class="fas fa-external-link-alt"></i>
              Open
            </button>
            {#if project.status === 'in_progress' || project.status === 'running'}
              <button
                class="btn btn-secondary btn-sm"
                onclick={() => handlePauseProject(pid, project.name)}
                aria-label="Pause project {project.name}"
                title="Pause"
              >
                <i class="fas fa-pause"></i>
              </button>
            {/if}
            {#if project.status === 'paused'}
              <button
                class="btn btn-secondary btn-sm"
                onclick={() => handleResumeProject(pid, project.name)}
                aria-label="Resume project {project.name}"
                title="Resume"
              >
                <i class="fas fa-play"></i>
              </button>
            {/if}
            {#if project.status !== 'archived'}
              <button
                class="btn btn-secondary btn-sm"
                onclick={() => handleArchive(pid, project.name)}
                aria-label="Archive project {project.name}"
                title="Archive"
              >
                <i class="fas fa-archive"></i>
              </button>
            {/if}
            <button
              class="btn btn-danger btn-sm"
              onclick={() => handleDelete(pid, project.name)}
              aria-label="Delete project {project.name}"
              title="Delete"
            >
              <i class="fas fa-trash-alt"></i>
            </button>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .projects-view {
    padding: 24px;
    max-width: 1400px;
  }

  .view-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    gap: 12px;
  }

  .view-header h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .view-header h2 i { color: var(--warning); }

  .project-count {
    background: var(--surface-hover);
    color: var(--text-muted);
    border-radius: var(--radius-full);
    padding: 1px 8px;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .header-actions { display: flex; gap: 8px; }

  /* Create form */
  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
  }

  .create-form-card { margin-bottom: 20px; }

  .form-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 14px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .create-form { display: flex; flex-direction: column; gap: 12px; }

  .form-row { display: flex; gap: 12px; flex-wrap: wrap; }
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-label { font-size: 0.75rem; font-weight: 500; color: var(--text-secondary); }

  .input {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 0.875rem;
    padding: 6px 10px;
    width: 100%;
    box-sizing: border-box;
  }

  .input:focus {
    outline: 2px solid transparent;
    outline-offset: 2px;
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-muted);
  }

  .input:focus-visible {
    outline-color: var(--primary);
  }

  .textarea { resize: vertical; }
  .form-actions { display: flex; gap: 8px; }

  /* Filter bar */
  .filter-bar {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    align-items: center;
    flex-wrap: wrap;
  }

  .search-wrap {
    position: relative;
    flex: 1;
    min-width: 200px;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-muted);
    pointer-events: none;
    font-size: 0.8125rem;
  }

  .search-input { padding-left: 32px; }

  .status-filters {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }

  .filter-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 6px 12px;
    background: none;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    font-size: 0.8125rem;
    font-weight: 500;
    font-family: inherit;
    color: var(--text-muted);
    cursor: pointer;
    transition: background var(--transition-base), color var(--transition-base), border-color var(--transition-base);
  }

  .filter-btn:hover { background: var(--surface-hover); color: var(--text-primary); }
  .filter-btn.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }

  .filter-count {
    background: currentColor;
    color: var(--surface-bg);
    border-radius: var(--radius-full);
    padding: 0 5px;
    font-size: 0.6875rem;
    line-height: 1.4;
  }

  /* Loading / empty / error */
  .loading-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
  }

  .error-state {
    text-align: center;
    padding: 64px 24px;
    color: var(--danger);
    font-size: 0.9375rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }

  .error-state i { font-size: 2.5rem; opacity: 0.7; }
  .error-state p { margin: 0; }

  .empty-state {
    text-align: center;
    padding: 64px 24px;
    color: var(--text-muted);
    font-size: 0.9375rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }

  .empty-state i {
    font-size: 2.5rem;
    opacity: 0.35;
  }

  .empty-state p { margin: 0; }

  /* Project grid */
  .projects-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 14px;
  }

  .project-card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color var(--transition-base), box-shadow var(--transition-base);
  }

  .project-card:hover {
    border-color: var(--border-strong);
    box-shadow: var(--shadow-md);
  }

  .project-card.active {
    border-color: var(--primary);
    box-shadow: 0 0 0 1px var(--primary), var(--shadow-md);
  }

  .project-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }

  .project-name-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
    font-family: inherit;
    text-align: left;
    padding: 0;
  }

  .project-name-btn:hover { color: var(--primary); }

  .project-desc {
    font-size: 0.8125rem;
    color: var(--text-muted);
    margin: 6px 0 0 0;
    line-height: var(--leading-relaxed);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .project-meta {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .meta-item {
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  /* Progress */
  .project-progress {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .progress-bar {
    flex: 1;
    height: 4px;
    background: var(--surface-hover);
    border-radius: var(--radius-full);
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary);
    border-radius: var(--radius-full);
    transition: width var(--transition-slow);
  }

  .progress-pct {
    font-size: 0.75rem;
    color: var(--text-muted);
    flex-shrink: 0;
    min-width: 36px;
    text-align: right;
  }

  /* Card actions */
  .project-actions {
    display: flex;
    gap: 8px;
    margin-top: 4px;
  }

  /* Status badges */
  .status-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: var(--radius-full);
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }

  .status-primary { background: var(--primary-muted); color: var(--primary); }
  .status-success { background: var(--success-muted); color: var(--success); }
  .status-warning { background: var(--warning-muted); color: var(--warning); }
  .status-danger  { background: var(--danger-muted);  color: var(--danger);  }
  .status-muted   { background: var(--surface-hover); color: var(--text-muted); }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: none;
    transition: background var(--transition-base);
  }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-sm { padding: 6px 10px; font-size: 0.8125rem; }

  .btn-primary { background: var(--primary); color: var(--text-on-primary); }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240,98,98,0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240,98,98,0.2); }

  @media (max-width: 640px) {
    .projects-grid { grid-template-columns: 1fr; }
    .filter-bar { flex-direction: column; align-items: stretch; }
  }
</style>
