<script>
  import {
    listPipelines,
    loadPipeline,
    savePipeline,
    validatePipeline,
  } from '$lib/api.js';
  import { WORKFLOW_BUILDER_BUSY_STATE } from '$lib/uiEnums.js';

  // -- Props --
  let { onSaved = null } = $props();

  // -- State --
  /** @type {Array<{pipeline_id: string}>} */
  let pipelineList = $state([]);

  /** @type {string} */
  let selectedPipelineId = $state('');

  /** @type {string} */
  let pipelineName = $state('New Pipeline');

  /**
   * @typedef {{ node_id: string, node_type: string, params: Record<string, unknown>, x: number, y: number }} CanvasNode
   * @typedef {{ from_node: string, to_node: string, condition: string }} CanvasEdge
   */

  /** @type {CanvasNode[]} */
  let nodes = $state([]);

  /** @type {CanvasEdge[]} */
  let edges = $state([]);

  /** @type {CanvasNode | null} */
  let selectedNode = $state(null);

  /** @type {string} */
  let edgeFromNode = $state('');

  /** @type {string} */
  let statusMessage = $state('');

  /** @type {'idle'|'saving'|'loading'|'validating'} */
  let busy = $state(WORKFLOW_BUILDER_BUSY_STATE.IDLE);

  /** @type {string[]} */
  let validationErrors = $state([]);

  // -- Drag state --
  /** @type {CanvasNode | null} */
  let dragging = $state(null);

  /** @type {number} */
  let dragOffsetX = $state(0);

  /** @type {number} */
  let dragOffsetY = $state(0);

  /** @type {boolean} */
  let dragMoved = $state(false);

  // -- Node modal state --
  /** @type {boolean} */
  let showNodeModal = $state(false);

  /** @type {string | null} */
  let editingNodeId = $state(null);

  /** @type {string} */
  let modalNodeId = $state('');

  /** @type {string} */
  let modalNodeType = $state('task');

  /** @type {string} */
  let modalParamsJson = $state('{}');

  /** @type {string} */
  let modalError = $state('');

  // -- Derived --
  let hasNodes = $derived(nodes.length > 0);

  // -- Lifecycle --
  $effect(() => {
    _refreshPipelineList();
  });

  // -- Helpers --

  async function _refreshPipelineList() {
    try {
      const resp = await listPipelines();
      pipelineList = (resp?.data?.pipeline_ids ?? []).map(
        /** @param {string} id */ (id) => ({ pipeline_id: id }),
      );
    } catch (err) {
      statusMessage = `Could not load pipeline list: ${err.message}`;
    }
  }

  function _nextNodeId() {
    const idx = nodes.length + 1;
    return `node-${idx}`;
  }

  /** @param {CanvasNode} [existingNode] */
  function openNodeModal(existingNode) {
    if (existingNode) {
      editingNodeId = existingNode.node_id;
      modalNodeId = existingNode.node_id;
      modalNodeType = existingNode.node_type;
      try {
        modalParamsJson = JSON.stringify(existingNode.params, null, 2);
      } catch {
        modalParamsJson = '{}';
      }
    } else {
      editingNodeId = null;
      modalNodeId = _nextNodeId();
      modalNodeType = 'task';
      modalParamsJson = '{}';
    }
    modalError = '';
    showNodeModal = true;
  }

  function submitNodeModal() {
    const trimmedId = modalNodeId.trim();
    if (!trimmedId) {
      modalError = 'Node ID is required.';
      return;
    }

    let parsedParams;
    try {
      parsedParams = JSON.parse(modalParamsJson || '{}');
    } catch {
      modalError = 'Parameters must be valid JSON.';
      return;
    }

    if (editingNodeId) {
      // Update existing node
      nodes = nodes.map((n) =>
        n.node_id === editingNodeId
          ? { ...n, node_id: trimmedId, node_type: modalNodeType, params: parsedParams }
          : n,
      );
      // Update edges referencing the old id if node_id changed
      if (trimmedId !== editingNodeId) {
        edges = edges.map((e) => ({
          ...e,
          from_node: e.from_node === editingNodeId ? trimmedId : e.from_node,
          to_node: e.to_node === editingNodeId ? trimmedId : e.to_node,
        }));
      }
      if (selectedNode?.node_id === editingNodeId) {
        const updated = nodes.find((n) => n.node_id === trimmedId) ?? null;
        selectedNode = updated;
      }
    } else {
      // Check for duplicate id
      if (nodes.some((n) => n.node_id === trimmedId)) {
        modalError = `A node with id "${trimmedId}" already exists.`;
        return;
      }
      // Add new node at canvas center
      const newNode = {
        node_id: trimmedId,
        node_type: modalNodeType,
        params: parsedParams,
        x: 80 + nodes.length * 140,
        y: 200,
      };
      nodes = [...nodes, newNode];
      selectedNode = newNode;
    }

    showNodeModal = false;
  }

  function closeNodeModal() {
    showNodeModal = false;
  }

  // -- Toolbar actions --

  function addNode() {
    openNodeModal(undefined);
  }

  function deleteSelectedNode() {
    if (!selectedNode) return;
    if (!window.confirm(`Delete node "${selectedNode.node_id}"?`)) return;
    deleteNode(selectedNode.node_id);
  }

  function deleteNode(nodeId) {
    nodes = nodes.filter((n) => n.node_id !== nodeId);
    edges = edges.filter((e) => e.from_node !== nodeId && e.to_node !== nodeId);
    if (selectedNode?.node_id === nodeId) {
      selectedNode = null;
    }
    if (edgeFromNode === nodeId) {
      edgeFromNode = '';
    }
  }

  function confirmDeleteNode(nodeId) {
    if (!window.confirm(`Delete node "${nodeId}"?`)) return;
    deleteNode(nodeId);
  }

  async function handleLoad() {
    if (!selectedPipelineId) return;
    busy = WORKFLOW_BUILDER_BUSY_STATE.LOADING;
    statusMessage = '';
    try {
      const resp = await loadPipeline(selectedPipelineId);
      const p = resp?.data?.pipeline;
      if (!p) throw new Error('Empty pipeline response');
      pipelineName = p.name;
      nodes = (p.nodes ?? []).map(
        /** @param {Record<string, unknown>} n @param {number} i */ (n, i) => ({
          node_id: /** @type {string} */ (n.node_id),
          node_type: /** @type {string} */ (n.node_type ?? 'task'),
          params: /** @type {Record<string, unknown>} */ (n.params ?? {}),
          x: 80 + i * 140,
          y: 120,
        }),
      );
      edges = (p.edges ?? []).map(
        /** @param {Record<string, unknown>} e */ (e) => ({
          from_node: /** @type {string} */ (e.from_node),
          to_node: /** @type {string} */ (e.to_node),
          condition: /** @type {string} */ (e.condition ?? ''),
        }),
      );
      selectedNode = null;
      statusMessage = `Loaded pipeline "${pipelineName}".`;
    } catch (err) {
      statusMessage = `Load failed: ${err.message}`;
    } finally {
      busy = WORKFLOW_BUILDER_BUSY_STATE.IDLE;
    }
  }

  async function handleSave() {
    busy = WORKFLOW_BUILDER_BUSY_STATE.SAVING;
    statusMessage = '';
    try {
      const payload = {
        name: pipelineName,
        nodes: nodes.map(({ node_id, node_type, params }) => ({ node_id, node_type, params })),
        edges: edges.map(({ from_node, to_node, condition }) => ({ from_node, to_node, condition: condition || null })),
      };
      const resp = await savePipeline(payload);
      const savedId = resp?.data?.pipeline_id;
      statusMessage = `Saved as "${savedId}".`;
      await _refreshPipelineList();
      if (onSaved) onSaved(savedId);
    } catch (err) {
      statusMessage = `Save failed: ${err.message}`;
    } finally {
      busy = WORKFLOW_BUILDER_BUSY_STATE.IDLE;
    }
  }

  async function handleValidate() {
    if (!selectedPipelineId) {
      statusMessage = 'Select a pipeline to validate.';
      return;
    }
    busy = WORKFLOW_BUILDER_BUSY_STATE.VALIDATING;
    validationErrors = [];
    statusMessage = '';
    try {
      const resp = await validatePipeline(selectedPipelineId);
      validationErrors = resp?.data?.errors ?? [];
      statusMessage = validationErrors.length === 0
        ? 'Pipeline is valid.'
        : `${validationErrors.length} validation error(s) found.`;
    } catch (err) {
      statusMessage = `Validation failed: ${err.message}`;
    } finally {
      busy = WORKFLOW_BUILDER_BUSY_STATE.IDLE;
    }
  }

  // -- Edge creation --

  function startEdge(nodeId) {
    edgeFromNode = nodeId;
    statusMessage = `Edge from "${nodeId}" - select a target node to connect.`;
  }

  function completeEdge(toNodeId) {
    if (!edgeFromNode || edgeFromNode === toNodeId) {
      edgeFromNode = '';
      statusMessage = '';
      return;
    }
    const from = edgeFromNode;
    const already = edges.some(
      (e) => e.from_node === from && e.to_node === toNodeId,
    );
    if (!already) {
      edges = [...edges, { from_node: from, to_node: toNodeId, condition: '' }];
    }
    edgeFromNode = '';
    statusMessage = `Connected "${from}" -> "${toNodeId}".`;
  }

  // -- Drag handlers --

  /** @param {PointerEvent} e @param {CanvasNode} node */
  function onNodePointerDown(e, node) {
    // Ignore secondary buttons
    if (e.button !== 0) return;
    e.stopPropagation();
    dragging = node;
    dragOffsetX = e.clientX - node.x;
    dragOffsetY = e.clientY - node.y;
    dragMoved = false;
    /** @type {HTMLElement} */ (e.currentTarget).setPointerCapture(e.pointerId);
  }

  /** @param {PointerEvent} e */
  function onCanvasPointerMove(e) {
    if (!dragging) return;
    const newX = e.clientX - dragOffsetX;
    const newY = e.clientY - dragOffsetY;
    // Clamp to non-negative coordinates so nodes stay on canvas
    const clampedX = Math.max(0, newX);
    const clampedY = Math.max(0, newY);
    nodes = nodes.map((n) =>
      n.node_id === dragging.node_id ? { ...n, x: clampedX, y: clampedY } : n,
    );
    // Keep selectedNode ref in sync
    if (selectedNode?.node_id === dragging.node_id) {
      selectedNode = { ...selectedNode, x: clampedX, y: clampedY };
    }
    dragMoved = true;
  }

  /** @param {PointerEvent} e @param {CanvasNode} node */
  function onNodePointerUp(e, node) {
    if (!dragMoved) {
      // This was a click, not a drag; handle edge/select logic.
      if (edgeFromNode) {
        completeEdge(node.node_id);
      } else {
        selectedNode = node;
      }
    }
    dragging = null;
    dragMoved = false;
  }

  function handleNodeKeydown(event, node) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    if (edgeFromNode) {
      completeEdge(node.node_id);
    } else {
      selectedNode = node;
    }
  }

  function onCanvasPointerUp() {
    dragging = null;
    dragMoved = false;
  }

  // -- Node param editing --

  function updateSelectedNodeParam(key, value) {
    if (!selectedNode) return;
    nodes = nodes.map((n) =>
      n.node_id === selectedNode.node_id
        ? { ...n, params: { ...n.params, [key]: value } }
        : n,
    );
    selectedNode = { ...selectedNode, params: { ...selectedNode.params, [key]: value } };
  }

  function updateSelectedNodeType(type) {
    if (!selectedNode) return;
    nodes = nodes.map((n) =>
      n.node_id === selectedNode.node_id ? { ...n, node_type: type } : n,
    );
    selectedNode = { ...selectedNode, node_type: type };
  }
</script>

<section class="workflow-builder" aria-label="Workflow pipeline builder">
  <!-- Toolbar -->
  <header class="wb-toolbar">
    <div class="wb-toolbar-left">
      <label for="wb-name" class="sr-only">Pipeline name</label>
      <input
        id="wb-name"
        class="wb-name-input"
        type="text"
        bind:value={pipelineName}
        placeholder="Pipeline name"
        aria-label="Pipeline name"
      />
    </div>
    <div class="wb-toolbar-actions">
      <button type="button" onclick={addNode} disabled={busy !== WORKFLOW_BUILDER_BUSY_STATE.IDLE} title="Add a new task node">
        Add Node
      </button>
      <button
        onclick={handleSave}
        disabled={busy !== WORKFLOW_BUILDER_BUSY_STATE.IDLE || !hasNodes}
        title="Save the current pipeline"
      >
        {busy === WORKFLOW_BUILDER_BUSY_STATE.SAVING ? 'Saving...' : 'Save'}
      </button>
      <span class="wb-toolbar-sep" aria-hidden="true"></span>
      <label for="wb-load-select" class="sr-only">Select pipeline</label>
      <select
        id="wb-load-select"
        bind:value={selectedPipelineId}
        aria-label="Select saved pipeline"
      >
        <option value="">-- Load pipeline --</option>
        {#each pipelineList as p (p.pipeline_id)}
          <option value={p.pipeline_id}>{p.pipeline_id}</option>
        {/each}
      </select>
      <button
        onclick={handleLoad}
        disabled={busy !== WORKFLOW_BUILDER_BUSY_STATE.IDLE || !selectedPipelineId}
        title="Load selected pipeline"
      >
        {busy === WORKFLOW_BUILDER_BUSY_STATE.LOADING ? 'Loading...' : 'Load'}
      </button>
      <button
        onclick={handleValidate}
        disabled={busy !== WORKFLOW_BUILDER_BUSY_STATE.IDLE || !selectedPipelineId}
        title="Validate selected pipeline"
      >
        {busy === WORKFLOW_BUILDER_BUSY_STATE.VALIDATING ? 'Validating...' : 'Validate'}
      </button>
    </div>
  </header>

  {#if statusMessage}
    <div class="wb-status" role="status" aria-live="polite">{statusMessage}</div>
  {/if}

  {#if validationErrors.length > 0}
    <ul class="wb-validation-errors" aria-label="Validation errors">
      {#each validationErrors as err (err)}
        <li>{err}</li>
      {/each}
    </ul>
  {/if}

  <!-- Main area: canvas + inspector -->
  <div class="wb-main">
    <!-- Canvas -->
    <div
      class="wb-canvas"
      role="application"
      aria-label="Pipeline canvas"
      onpointermove={onCanvasPointerMove}
      onpointerup={onCanvasPointerUp}
    >
      {#if nodes.length === 0}
        <p class="wb-empty-hint">Click "Add Node" to start building your pipeline.</p>
      {:else}
        <!-- Edges rendered as SVG overlay -->
        <svg class="wb-edges-svg" aria-hidden="true">
          {#each edges as edge (`${edge.from_node}→${edge.to_node}`)}
            {@const fromNode = nodes.find((n) => n.node_id === edge.from_node)}
            {@const toNode = nodes.find((n) => n.node_id === edge.to_node)}
            {#if fromNode && toNode}
              <line
                x1={fromNode.x + 60}
                y1={fromNode.y + 22}
                x2={toNode.x + 60}
                y2={toNode.y + 22}
                stroke="#6b7280"
                stroke-width="2"
                marker-end="url(#arrowhead)"
              />
            {/if}
          {/each}
          <defs>
            <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" fill="#6b7280" />
            </marker>
          </defs>
        </svg>

        <!-- Nodes -->
        {#each nodes as node (node.node_id)}
          <div
            class="wb-node"
            class:wb-node--selected={selectedNode?.node_id === node.node_id}
            class:wb-node--edge-source={edgeFromNode === node.node_id}
            class:wb-node--dragging={dragging?.node_id === node.node_id}
            style="left: {node.x}px; top: {node.y}px"
          >
            <button
              type="button"
              class="wb-node-select"
              aria-label="Node {node.node_id} ({node.node_type}){selectedNode?.node_id === node.node_id ? ', selected' : ''}{edgeFromNode === node.node_id ? ', linking from this node' : ''}"
              aria-pressed={selectedNode?.node_id === node.node_id}
              title="Drag to move; use node action buttons to edit, link, or delete"
              onpointerdown={(e) => onNodePointerDown(e, node)}
              onpointerup={(e) => onNodePointerUp(e, node)}
              ondblclick={() => startEdge(node.node_id)}
              onkeydown={(e) => handleNodeKeydown(e, node)}
            >
              <span class="wb-node-type">{node.node_type}</span>
              <span class="wb-node-id">{node.node_id}</span>
            </button>
            <div class="wb-node-actions">
              <button
                type="button"
                class="wb-node-action-btn"
                title="Edit node"
                aria-label="Edit node {node.node_id}"
                onclick={(e) => { e.stopPropagation(); openNodeModal(node); }}
                onpointerdown={(e) => e.stopPropagation()}
              >
                Edit
              </button>
              <button
                type="button"
                class="wb-node-action-btn"
                title="Start edge from node"
                aria-label="Start edge from node {node.node_id}"
                aria-pressed={edgeFromNode === node.node_id}
                onclick={(e) => { e.stopPropagation(); startEdge(node.node_id); }}
                onpointerdown={(e) => e.stopPropagation()}
              >
                Link
              </button>
              <button
                type="button"
                class="wb-node-action-btn wb-node-action-btn--delete"
                title="Delete node"
                aria-label="Delete node {node.node_id}"
                onclick={(e) => {
                  e.stopPropagation();
                  confirmDeleteNode(node.node_id);
                }}
                onpointerdown={(e) => e.stopPropagation()}
              >
                Delete
              </button>
            </div>
          </div>
        {/each}
      {/if}
    </div>

    <!-- Inspector panel -->
    <aside class="wb-inspector" aria-label="Node inspector">
      {#if selectedNode}
        <h2 class="wb-inspector-title">Node: {selectedNode.node_id}</h2>

        <label for="wb-node-type">Type</label>
        <select
          id="wb-node-type"
          value={selectedNode.node_type}
          onchange={(e) => updateSelectedNodeType(e.currentTarget.value)}
        >
          <option value="task">task</option>
          <option value="decision">decision</option>
          <option value="loop">loop</option>
        </select>

        <fieldset class="wb-params">
          <legend>Parameters</legend>
          {#each Object.entries(selectedNode.params) as [key, val] (`${selectedNode.node_id}:${key}`)}
            <div class="wb-param-row">
              <label for="wb-param-{key}" class="wb-param-key">{key}</label>
              <input
                id="wb-param-{key}"
                type="text"
                value={String(val)}
                oninput={(e) => updateSelectedNodeParam(key, e.currentTarget.value)}
              />
            </div>
          {/each}
          <button
            type="button"
            class="wb-add-param"
            onclick={() => {
              const k = prompt('Parameter name');
              if (k) updateSelectedNodeParam(k, '');
            }}
          >
            + Add parameter
          </button>
        </fieldset>

        <button type="button" class="wb-delete-node" onclick={deleteSelectedNode}>
          Delete node
        </button>
      {:else}
        <p class="wb-inspector-hint">Select a node to edit its properties.</p>
      {/if}
    </aside>
  </div>
</section>

<!-- Add / Edit Node Modal -->
{#if showNodeModal}
  <div
    class="wb-modal-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="{editingNodeId ? 'Edit' : 'Add'} node"
  >
    <div class="wb-modal">
      <h2 class="wb-modal-title">{editingNodeId ? 'Edit Node' : 'Add Node'}</h2>

      {#if modalError}
        <p class="wb-modal-error" role="alert">{modalError}</p>
      {/if}

      <div class="wb-modal-field">
        <label for="modal-node-id">Node ID</label>
        <input
          id="modal-node-id"
          type="text"
          bind:value={modalNodeId}
          placeholder="e.g. node-1"
          autocomplete="off"
        />
      </div>

      <div class="wb-modal-field">
        <label for="modal-node-type">Type</label>
        <select id="modal-node-type" bind:value={modalNodeType}>
          <option value="task">task</option>
          <option value="decision">decision</option>
          <option value="loop">loop</option>
        </select>
      </div>

      <div class="wb-modal-field">
        <label for="modal-params">Parameters (JSON)</label>
        <textarea
          id="modal-params"
          bind:value={modalParamsJson}
          rows="5"
          placeholder="&#123;&#125;"
          class="wb-modal-textarea"
        ></textarea>
      </div>

      <div class="wb-modal-actions">
        <button type="button" class="wb-modal-btn-primary" onclick={submitNodeModal}>
          {editingNodeId ? 'Save' : 'Add'}
        </button>
        <button type="button" onclick={closeNodeModal}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .workflow-builder {
    display: flex;
    flex-direction: column;
    height: 100%;
    font-size: 0.875rem;
    color: var(--color-text, #1f2937);
  }

  /* Toolbar */
  .wb-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--color-border, #e5e7eb);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .wb-toolbar-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .wb-toolbar-actions {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
  }

  .wb-toolbar-sep {
    display: inline-block;
    width: 1px;
    height: 1.25rem;
    background: var(--color-border, #e5e7eb);
    margin: 0 0.25rem;
  }

  .wb-name-input {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--color-border, #d1d5db);
    border-radius: 4px;
    font-size: 0.875rem;
    min-width: 180px;
  }

  button,
  select {
    min-height: 44px;
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--color-border, #d1d5db);
    border-radius: 4px;
    background: var(--color-bg-button, #f9fafb);
    cursor: pointer;
    font-size: 0.8125rem;
  }

  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  /* Status / errors */
  .wb-status {
    padding: 0.3rem 0.75rem;
    background: var(--color-info-bg, #eff6ff);
    border-bottom: 1px solid var(--color-info-border, #bfdbfe);
    font-size: 0.8125rem;
  }

  .wb-validation-errors {
    margin: 0;
    padding: 0.3rem 0.75rem 0.3rem 2rem;
    background: var(--color-error-bg, #fef2f2);
    border-bottom: 1px solid var(--color-error-border, #fecaca);
    font-size: 0.8125rem;
    color: var(--color-error, #b91c1c);
  }

  /* Main layout */
  .wb-main {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* Canvas */
  .wb-canvas {
    position: relative;
    flex: 1;
    overflow: auto;
    background: var(--color-canvas-bg, #f3f4f6);
    height: 600px;
    touch-action: none;
  }

  .wb-edges-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }

  .wb-empty-hint {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    color: var(--color-muted, #9ca3af);
    pointer-events: none;
  }

  /* Nodes */
  .wb-node {
    position: absolute;
    width: 130px;
    min-height: 52px;
    padding: 0.35rem 0.5rem 0.25rem;
    border-radius: 6px;
    background: var(--color-bg, #ffffff);
    border: 2px solid var(--color-border, #d1d5db);
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.125rem;
    cursor: grab;
    text-align: left;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
    transition: border-color 0.15s;
    user-select: none;
  }

  .wb-node--selected {
    border-color: var(--color-accent, #3b82f6);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
  }

  .wb-node--edge-source {
    border-color: var(--color-warning, #f59e0b);
  }

  .wb-node--dragging {
    cursor: grabbing;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
    z-index: 10;
  }

  .wb-node-type {
    font-size: 0.7rem;
    text-transform: uppercase;
    color: var(--color-muted, #6b7280);
    letter-spacing: 0.04em;
  }

  .wb-node-id {
    font-size: 0.8125rem;
    font-weight: 500;
    word-break: break-all;
  }

  .wb-node-actions {
    display: flex;
    gap: 0.125rem;
    margin-top: 0.25rem;
  }

  .wb-node-select {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.125rem;
    width: 100%;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: inherit;
    font: inherit;
    padding: 0;
    text-align: left;
  }

  .wb-node-select:focus-visible {
    border-radius: 4px;
    outline: 2px solid var(--color-accent, #3b82f6);
    outline-offset: 2px;
  }

  .wb-node-action-btn {
    min-width: 24px;
    min-height: 24px;
    padding: 0.1rem 0.25rem;
    font-size: 0.7rem;
    border: 1px solid transparent;
    background: transparent;
    border-radius: 3px;
    cursor: pointer;
    line-height: 1;
    opacity: 0.6;
  }

  .wb-node-action-btn:hover {
    background: var(--color-bg-button, #f3f4f6);
    opacity: 1;
  }

  .wb-node-action-btn--delete:hover {
    background: var(--color-error-bg, #fef2f2);
    color: var(--color-error, #b91c1c);
  }

  /* Inspector */
  .wb-inspector {
    width: 220px;
    flex-shrink: 0;
    border-left: 1px solid var(--color-border, #e5e7eb);
    padding: 0.75rem;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .wb-inspector-title {
    font-size: 0.875rem;
    font-weight: 600;
    margin: 0;
  }

  .wb-inspector-hint {
    color: var(--color-muted, #9ca3af);
    font-size: 0.8125rem;
  }

  .wb-params {
    border: 1px solid var(--color-border, #e5e7eb);
    border-radius: 4px;
    padding: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .wb-params legend {
    font-size: 0.75rem;
    text-transform: uppercase;
    color: var(--color-muted, #6b7280);
    letter-spacing: 0.04em;
    padding: 0 0.25rem;
  }

  .wb-param-row {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .wb-param-key {
    font-size: 0.75rem;
    color: var(--color-muted, #6b7280);
  }

  .wb-add-param {
    margin-top: 0.25rem;
    font-size: 0.75rem;
    padding: 0.2rem 0.4rem;
    background: transparent;
    border: 1px dashed var(--color-border, #d1d5db);
    color: var(--color-muted, #6b7280);
  }

  .wb-delete-node {
    margin-top: auto;
    background: var(--color-error-bg, #fef2f2);
    border-color: var(--color-error-border, #fecaca);
    color: var(--color-error, #b91c1c);
  }

  /* Modal */
  .wb-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.35);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }

  .wb-modal {
    background: var(--color-bg, #ffffff);
    border-radius: 8px;
    padding: 1.25rem;
    width: 360px;
    max-width: 92vw;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
  }

  .wb-modal-title {
    font-size: 1rem;
    font-weight: 600;
    margin: 0;
  }

  .wb-modal-error {
    color: var(--color-error, #b91c1c);
    font-size: 0.8125rem;
    margin: 0;
  }

  .wb-modal-field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .wb-modal-field label {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--color-text, #1f2937);
  }

  .wb-modal-field input,
  .wb-modal-field select {
    width: 100%;
    box-sizing: border-box;
  }

  .wb-modal-textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.35rem 0.5rem;
    border: 1px solid var(--color-border, #d1d5db);
    border-radius: 4px;
    font-family: monospace;
    font-size: 0.8125rem;
    resize: vertical;
  }

  .wb-modal-actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    margin-top: 0.25rem;
  }

  .wb-modal-btn-primary {
    background: var(--color-accent, #3b82f6);
    color: #fff;
    border-color: var(--color-accent, #3b82f6);
  }

  .wb-modal-btn-primary:hover {
    background: #2563eb;
  }

  /* Accessibility */
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
