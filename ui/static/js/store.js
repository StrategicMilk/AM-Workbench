/**
 * Central reactive state store for Vetinari UI.
 * Provides typed localStorage persistence + cross-module change events.
 *
 * Usage:
 *   VStore.set('theme', 'dark');
 *   VStore.get('theme');  // 'dark'
 *   document.addEventListener('store:change', (e) => { ... });
 */
(function() {
    'use strict';

    // ── Key registry: single source of truth for all localStorage keys ──
    const KEYS = Object.freeze({
        // Existing keys (preserve backward compat with current localStorage)
        THEME: 'theme',
        SIDEBAR_COLLAPSED: 'sidebarCollapsed',
        REDUCED_MOTION: 'reducedMotion',
        COMPACT_MODE: 'compactMode',
        SETUP_COMPLETE: 'setupComplete',

        // New keys (Phase 1+)
        INTERFACE_MODE: 'interfaceMode',
        ACCENT_COLOR: 'accentColor',
        FONT_SIZE: 'fontSize',
        CHAT_BUBBLE_STYLE: 'chatBubbleStyle',
        FOCUS_MODE: 'focusModeEnabled',
        USER_PATTERNS: 'userPatterns',
        PROMPT_PRESETS_CACHE: 'promptPresetsCache',
    });

    const DEFAULTS = Object.freeze({
        [KEYS.THEME]: 'dark',
        [KEYS.SIDEBAR_COLLAPSED]: false,
        [KEYS.REDUCED_MOTION]: false,
        [KEYS.COMPACT_MODE]: false,
        [KEYS.SETUP_COMPLETE]: false,
        [KEYS.INTERFACE_MODE]: 'standard',
        [KEYS.ACCENT_COLOR]: '#4e9af9',
        [KEYS.FONT_SIZE]: 14,
        [KEYS.CHAT_BUBBLE_STYLE]: 'flat',
        [KEYS.FOCUS_MODE]: false,
        [KEYS.USER_PATTERNS]: null,
        [KEYS.PROMPT_PRESETS_CACHE]: null,
    });

    const VStore = {
        KEYS: KEYS,

        get(key) {
            try {
                const raw = localStorage.getItem(key);
                if (raw === null) return DEFAULTS[key] !== undefined ? DEFAULTS[key] : null;
                try { return JSON.parse(raw); } catch { return raw; }
            } catch {
                // localStorage blocked (e.g. tracking prevention) — use in-memory fallback
                return this._memStore[key] !== undefined ? this._memStore[key] : (DEFAULTS[key] !== undefined ? DEFAULTS[key] : null);
            }
        },

        set(key, value) {
            const old = this.get(key);
            try {
                localStorage.setItem(key, JSON.stringify(value));
            } catch {
                // localStorage blocked — store in memory as fallback
                this._memStore[key] = value;
            }
            if (old !== value) {
                document.dispatchEvent(new CustomEvent('store:change', {
                    detail: { key, value, oldValue: old }
                }));
            }
        },

        // In-memory fallback when localStorage is blocked
        _memStore: {},

        remove(key) {
            const oldValue = this.get(key);
            try { localStorage.removeItem(key); } catch { delete this._memStore[key]; }
            document.dispatchEvent(new CustomEvent('store:change', {
                detail: { key, value: null, oldValue }
            }));
        },

        /** Subscribe to changes on a specific key */
        onChange(key, callback) {
            const handler = (e) => {
                if (e.detail.key === key) callback(e.detail.value, e.detail.oldValue);
            };
            document.addEventListener('store:change', handler);
            return () => document.removeEventListener('store:change', handler);
        },

        /** Get all stored values with defaults applied */
        getAll() {
            const result = { ...DEFAULTS };
            for (const key of Object.values(KEYS)) {
                const raw = localStorage.getItem(key);
                if (raw !== null) {
                    try { result[key] = JSON.parse(raw); } catch { result[key] = raw; }
                }
            }
            return result;
        }
    };

    window.VStore = VStore;
})();
