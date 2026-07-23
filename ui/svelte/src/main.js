import { mount } from 'svelte';
import App from './App.svelte';
import { installTauriFetchBridge } from '$lib/tauri_fetch_bridge.js';
import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import './styles/components.css';

installTauriFetchBridge();

const app = mount(App, {
  target: document.getElementById('svelte-root'),
});

export default app;
