<script>
  import { WorkbenchMigrationPanel } from '$lib/components/workbench/migration';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
</script>

<main data-view="workbench-migration" data-testid="workbench-migration-view">
  <header class="migration-header">
    <div>
      <h1>Migration Wizard</h1>
      <p>Step-by-step guided migration for project {projectId}.</p>
      <HelpPopover
        title="Migration wizard"
        body="Always run a dry-run first: the wizard simulates the full migration and reports conflicts before making any changes. Secret handling: secrets referenced by the source project are not copied automatically — review and re-provision them in the target before completing migration. Backup confirmation: the wizard will prompt you to confirm that a backup exists before executing a destructive step; do not skip this step. Conflict preview: the dry-run output lists every naming conflict, schema mismatch, and missing dependency so you can resolve them before committing the migration."
        severity="warning"
      />
    </div>
  </header>
  <WorkbenchMigrationPanel {projectId} />
</main>

<style>
  main { display: flex; flex-direction: column; }
  .migration-header { padding: 20px 20px 0; }
  .migration-header h1 { margin: 0 0 4px; font-size: 24px; color: var(--text-primary); }
  .migration-header p { margin: 0; color: var(--text-muted); }

  @media (max-width: 720px) {
    main {
      min-width: 0;
      overflow-x: hidden;
    }

    .migration-header {
      padding: 16px 16px 0;
    }

    .migration-header h1 {
      font-size: 20px;
    }
  }
</style>
