<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import Icon from '$lib/a11y/Icon.svelte';
  import { integer } from '$lib/utils/format.js';

  function toggleSidebar() {
    appState.sidebarCollapsed = !appState.sidebarCollapsed;
  }

  function toggleTheme() {
    appState.theme = appState.theme === 'dark' ? 'light' : 'dark';
  }

  function openCommandPalette() {
    appState.commandPaletteOpen = true;
  }

  let searchQuery = $state('');

  $effect(() => {
    appState.commandPaletteQuery = searchQuery;
  });
</script>

<header class="header">
  <div class="header-left">
    <button
      class="btn btn-ghost"
      id="sidebarToggle"
      onclick={toggleSidebar}
      aria-expanded={!appState.sidebarCollapsed}
      aria-controls="main-sidebar"
      aria-label="Toggle sidebar"
      title="Toggle sidebar (Ctrl+B)"
    >
      <Icon name="bars" aria-hidden="true" />
    </button>
  </div>

  <div class="header-center">
    <div class="search-container">
      <Icon name="search" class="search-icon" aria-hidden="true" />
      <input
        type="text"
        class="input search-input"
        placeholder="Search... (Ctrl+K)"
        bind:value={searchQuery}
        onfocus={openCommandPalette}
        aria-label="Global search"
      />
    </div>
  </div>

  <div class="header-right">
    <span class="token-counter" title="Session tokens used" role="status" aria-label={`Session tokens used: ${integer(appState.sessionTokens)}`}>
      <Icon name="coins" aria-hidden="true" />
      {integer(appState.sessionTokens)}
    </span>

    <button
      class="btn btn-ghost"
      onclick={toggleTheme}
      title="Toggle theme"
      aria-label="Toggle light/dark theme"
    >
      <Icon name={appState.theme === 'dark' ? 'moon' : 'sun'} aria-hidden="true" />
    </button>

    <button
      class="btn btn-ghost"
      onclick={() => { appState.currentView = 'models'; }}
      title="Discover models"
      aria-label="Discover models"
    >
      <Icon name="compass" aria-hidden="true" />
    </button>
  </div>
</header>
