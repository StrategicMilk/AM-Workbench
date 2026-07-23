/**
 * Resilient SSE connection manager with exponential backoff.
 * Wraps native EventSource with:
 * - Exponential backoff (1s → 2s → 4s → ... → 30s max) with ±20% jitter
 * - Connection lifecycle tracking
 * - Central registry (prevents duplicate connections)
 */
(function() {
    'use strict';

    const _connections = {};
    const BASE_DELAY = 1000;
    const MAX_DELAY = 30000;
    const JITTER = 0.2;

    function connect(id, url, eventHandlers, options = {}) {
        if (_connections[id]) close(id);

        let retryCount = 0;

        function createSource() {
            const es = new EventSource(url);
            _connections[id] = { es, retryCount, url };

            // Register typed event handlers
            for (const [event, handler] of Object.entries(eventHandlers)) {
                es.addEventListener(event, (e) => {
                    retryCount = 0; // Reset on successful event
                    handler(e);
                });
            }

            es.onerror = () => {
                if (es.readyState === EventSource.CLOSED) {
                    delete _connections[id];
                    if (options.onClose) options.onClose();
                    return;
                }
                // Exponential backoff with jitter
                es.close();
                retryCount++;
                const delay = Math.min(BASE_DELAY * Math.pow(2, retryCount - 1), MAX_DELAY);
                const jittered = delay * (1 + (Math.random() * 2 - 1) * JITTER);
                const timer = setTimeout(createSource, jittered);
                _connections[id] = { es: null, retryCount, url, timer };
                if (options.onRetry) options.onRetry(retryCount, jittered);
            };

            es.onopen = () => {
                retryCount = 0;
                if (options.onOpen) options.onOpen();
            };
        }

        createSource();
        return id;
    }

    function close(id) {
        const conn = _connections[id];
        if (conn) {
            if (conn.timer) clearTimeout(conn.timer);
            if (conn.es) conn.es.close();
            delete _connections[id];
        }
    }

    function closeAll() {
        for (const id of Object.keys(_connections)) close(id);
    }

    function isConnected(id) {
        const conn = _connections[id];
        return conn && conn.es != null && conn.es.readyState === EventSource.OPEN;
    }

    window.SSEManager = { connect, close, closeAll, isConnected };
})();
