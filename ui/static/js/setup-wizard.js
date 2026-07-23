/**
 * First-boot setup wizard for Vetinari.
 * Guides new users through a 6-step configuration flow:
 * 1. Welcome + Hardware Detection
 * 2. Models Directory
 * 3. Model Discovery (local + HuggingFace search)
 * 4. API Keys (Optional — HuggingFace + cloud provider)
 * 5. Preferences (autonomy, theme, notifications)
 * 6. Done / Summary
 *
 * Persists partial progress to VStore so refresh resumes mid-wizard.
 * Exposed as window.SetupWizard (class, instantiate per use).
 */
(function() {
    'use strict';

    var TOTAL_STEPS = 6;
    var PROGRESS_KEY = 'wizardProgress';
    var SECRET_FIELDS = ['hfToken', 'cloudApiKey'];

    // ── Step labels for the progress indicator ──
    var STEP_LABELS = [
        'Welcome',
        'Models Dir',
        'Models',
        'API Keys',
        'Preferences',
        'Done'
    ];

    /**
     * SetupWizard constructor.
     * Caches DOM references, restores saved progress, and binds events.
     */
    function SetupWizard() {
        this._currentStep = 1;
        this._data = {
            modelsDir: '',
            models: [],
            hfToken: '',
            cloudApiKey: '',
            autonomyLevel: 'assisted',
            notifications: true,
            theme: 'dark'
        };
        this._allModels = [];
        this._overlay = document.getElementById('setupWizard');
        this._steps = [];
        this._dots = [];

        // Cache step elements
        for (var i = 1; i <= TOTAL_STEPS; i++) {
            this._steps.push(document.getElementById('wizardStep' + i));
        }

        // Cache dot elements
        var dotsContainer = document.querySelector('.setup-wizard__progress');
        if (dotsContainer) {
            this._dots = Array.from(dotsContainer.querySelectorAll('.setup-wizard__dot'));
        }

        // Restore saved progress
        this._restoreProgress();

        // Bind navigation
        var self = this;
        var prevBtn = document.getElementById('wizardPrev');
        var nextBtn = document.getElementById('wizardNext');
        var skipBtn = document.getElementById('wizardSkip');
        if (prevBtn) prevBtn.addEventListener('click', function() { self.prevStep(); });
        if (nextBtn) nextBtn.addEventListener('click', function() { self.nextStep(); });
        if (skipBtn) skipBtn.addEventListener('click', function() { self._skipWizard(); });

        // Bind pill selectors
        this._bindPillSelectors();

        // Bind skip links
        this._bindSkipLinks();

        // Bind scan button
        var scanBtn = document.getElementById('wizardScanBtn');
        if (scanBtn) scanBtn.addEventListener('click', function() { self._scanModelsDir(); });

        // Bind browse button — uses native folder picker, falls back to suggestion list
        var browseBtn = document.getElementById('wizardBrowseBtn');
        var dirPicker = document.getElementById('wizardDirPicker');
        if (browseBtn && dirPicker) {
            browseBtn.addEventListener('click', function() {
                dirPicker.click();
            });
            dirPicker.addEventListener('change', function() {
                if (dirPicker.files && dirPicker.files.length > 0) {
                    // Extract the directory path from the first file's webkitRelativePath
                    var firstFile = dirPicker.files[0];
                    var relPath = firstFile.webkitRelativePath || '';
                    var dirName = relPath.split('/')[0] || '';
                    // The browser won't give us the full absolute path for security reasons,
                    // so show the folder name and let user confirm/edit
                    var dirInput = document.getElementById('wizardModelsDir');
                    if (dirInput && dirName) {
                        dirInput.value = dirName;
                        var resultEl = document.getElementById('wizardScanResult');
                        if (resultEl) {
                            resultEl.innerHTML = '<i class="fas fa-info-circle" style="color:var(--primary)"></i> ' +
                                'Selected folder: <strong>' + _escapeHtml(dirName) + '</strong>. ' +
                                'Browsers hide the full path for security. Please verify or type the full path (e.g. C:\\models\\' + _escapeHtml(dirName) + '), then click Scan.';
                        }
                    }
                }
            });
        } else if (browseBtn) {
            // Fallback: use server-side directory suggestions
            browseBtn.addEventListener('click', function() { self._browseDirectory(); });
        }

        // Bind model search (client-side filter + HF search)
        var searchInput = document.getElementById('wizardModelSearch');
        if (searchInput) {
            searchInput.addEventListener('input', function() { self._filterLocalModels(); });
        }
        var hfSearchBtn = document.getElementById('wizardHfSearchBtn');
        if (hfSearchBtn) {
            hfSearchBtn.addEventListener('click', function() { self._searchHuggingFace(); });
        }
    }

    /**
     * Display the wizard overlay and initialise the current step.
     */
    SetupWizard.prototype.show = function() {
        if (!this._overlay) return;
        this._overlay.style.display = 'flex';
        this._goToStep(this._currentStep);
        this._detectHardware();
        this._restoreFormFields();
    };

    /**
     * Skip the entire setup wizard.
     * Closes the modal and sets a localStorage flag so the wizard is not
     * shown again on subsequent visits.  Normal app usage is unblocked.
     */
    SetupWizard.prototype._skipWizard = function() {
        try {
            localStorage.setItem('vetinari_wizard_skipped', 'true');
        } catch (e) {
            // localStorage may be unavailable in private-browsing contexts
        }
        this._clearProgress();
        this.hide();
        if (window.ToastManager) {
            ToastManager.show('Setup skipped. You can run it again from Settings.', 'info');
        }
    };

    /**
     * Hide the wizard overlay.
     */
    SetupWizard.prototype.hide = function() {
        if (this._overlay) {
            this._overlay.style.display = 'none';
        }
    };

    /**
     * Advance to the next step after validating current inputs.
     */
    SetupWizard.prototype.nextStep = function() {
        if (!this._validateCurrentStep()) return;

        if (this._currentStep >= TOTAL_STEPS) {
            this._complete();
            return;
        }

        this._goToStep(this._currentStep + 1);

        // Trigger step-specific initialisers
        if (this._currentStep === 3) {
            this._loadModels();
        }
        if (this._currentStep === 6) {
            this._renderSummary();
        }
    };

    /**
     * Navigate to the previous step, preserving entered data.
     */
    SetupWizard.prototype.prevStep = function() {
        if (this._currentStep <= 1) return;
        this._goToStep(this._currentStep - 1);
    };

    // ── Private methods ──

    /**
     * Switch to a specific step number and persist progress.
     */
    SetupWizard.prototype._goToStep = function(step) {
        this._currentStep = step;

        // Show/hide step panels
        for (var i = 0; i < this._steps.length; i++) {
            if (this._steps[i]) {
                if (i + 1 === step) {
                    this._steps[i].classList.add('active');
                } else {
                    this._steps[i].classList.remove('active');
                }
            }
        }

        this._updateProgress();
        this._updateNavButtons();
        this._saveProgress();
    };

    /**
     * Update the progress dot indicators.
     */
    SetupWizard.prototype._updateProgress = function() {
        for (var i = 0; i < this._dots.length; i++) {
            this._dots[i].classList.remove('active', 'completed');
            if (i + 1 === this._currentStep) {
                this._dots[i].classList.add('active');
            } else if (i + 1 < this._currentStep) {
                this._dots[i].classList.add('completed');
            }
        }

        // Update step label
        var label = document.getElementById('wizardStepLabel');
        if (label) {
            label.textContent = 'Step ' + this._currentStep + ' of ' + TOTAL_STEPS + ' \u2014 ' + STEP_LABELS[this._currentStep - 1];
        }
    };

    /**
     * Update prev/next button state and labels.
     */
    SetupWizard.prototype._updateNavButtons = function() {
        var prevBtn = document.getElementById('wizardPrev');
        var nextBtn = document.getElementById('wizardNext');

        if (prevBtn) {
            prevBtn.disabled = this._currentStep <= 1;
        }

        if (nextBtn) {
            nextBtn.disabled = false;
            if (this._currentStep === TOTAL_STEPS) {
                nextBtn.innerHTML = '<i class="fas fa-rocket"></i> Start Chatting';
            } else {
                nextBtn.innerHTML = 'Next <i class="fas fa-arrow-right"></i>';
            }
        }
    };

    /**
     * Validate inputs for the current step. Returns true if valid.
     */
    SetupWizard.prototype._validateCurrentStep = function() {
        // Step 2: models directory — use default if empty (do not block skip)
        if (this._currentStep === 2) {
            var dirInput = document.getElementById('wizardModelsDir');
            if (dirInput) {
                this._data.modelsDir = dirInput.value.trim();
            }
            if (!this._data.modelsDir) {
                // Default to ~/.vetinari/models/ so the user can skip this step
                this._data.modelsDir = '~/.vetinari/models';
            }
            this._hideFieldError('wizardModelsDirError');
        }

        // Step 4: collect API keys (optional — always valid)
        if (this._currentStep === 4) {
            var hfInput = document.getElementById('wizardHfToken');
            if (hfInput) {
                this._data.hfToken = hfInput.value.trim();
            }
            var cloudInput = document.getElementById('wizardCloudApiKey');
            if (cloudInput) {
                this._data.cloudApiKey = cloudInput.value.trim();
            }
        }

        // Step 5: collect preferences
        if (this._currentStep === 5) {
            this._collectPreferences();
        }

        return true;
    };

    // ── Step 1: Hardware Detection ──

    /**
     * Fetch /api/status and display hardware information.
     */
    SetupWizard.prototype._detectHardware = function() {
        var self = this;
        var infoEl = document.getElementById('wizardHardwareInfo');
        if (!infoEl) return;

        infoEl.innerHTML = '<div class="wizard-status"><i class="fas fa-spinner fa-spin"></i> Detecting hardware...</div>';

        // Fetch system resources, GPU info, and config status in parallel
        Promise.allSettled([
            fetch('/api/v1/system/resources').then(function(r) { return r.json(); }),
            fetch('/api/v1/system/gpu').then(function(r) { return r.json(); }),
            fetch('/api/v1/status').then(function(r) { return r.json(); })
        ]).then(function(results) {
            var resources = results[0].status === 'fulfilled' ? results[0].value : null;
            var gpuData   = results[1].status === 'fulfilled' ? results[1].value : null;
            var config    = results[2].status === 'fulfilled' ? results[2].value : null;

            // RAM info
            var ramText = 'Detection unavailable';
            if (resources && resources.ram && resources.ram.total_mb) {
                var ramGb = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(resources.ram.total_mb / 1024);
                ramText = ramGb + ' GB';
            }

            // CPU info
            var cpuText = 'Detection unavailable';
            if (resources && resources.cpu) {
                cpuText = (resources.cpu.cores || '?') + ' cores';
            }

            // GPU info
            var gpuText = 'No GPU detected';
            var gpuIcon = 'fa-times-circle';
            var gpuClass = '';
            if (gpuData && gpuData.gpu_available && gpuData.gpus && gpuData.gpus.length > 0) {
                var gpu = gpuData.gpus[0];
                var vramMb = gpu.vram_total_mb != null ? gpu.vram_total_mb : 0;
                self._detectedVram = (typeof vramMb === 'number' && !isNaN(vramMb)) ? vramMb / 1024 : 0;
                var vramGb = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(self._detectedVram);
                gpuText = _escapeHtml(gpu.name) + ' (' + vramGb + ' GB)';
                gpuIcon = 'fa-check-circle';
                gpuClass = ' wizard-hw-card--success';
            } else if (gpuData && gpuData.error) {
                gpuText = _escapeHtml(gpuData.error);
            }

            // Models dir from config
            var modelsDir = (config && config.models_dir) || 'Not configured';

            infoEl.innerHTML =
                '<div class="wizard-hw-grid">' +
                    '<div class="wizard-hw-card">' +
                        '<div class="wizard-hw-card__icon"><i class="fas fa-memory"></i></div>' +
                        '<div class="wizard-hw-card__label">System RAM</div>' +
                        '<div class="wizard-hw-card__value">' + ramText + '</div>' +
                    '</div>' +
                    '<div class="wizard-hw-card">' +
                        '<div class="wizard-hw-card__icon"><i class="fas fa-microchip"></i></div>' +
                        '<div class="wizard-hw-card__label">CPU</div>' +
                        '<div class="wizard-hw-card__value">' + cpuText + '</div>' +
                    '</div>' +
                    '<div class="wizard-hw-card' + gpuClass + '">' +
                        '<div class="wizard-hw-card__icon"><i class="fas ' + gpuIcon + '"></i></div>' +
                        '<div class="wizard-hw-card__label">GPU</div>' +
                        '<div class="wizard-hw-card__value">' + gpuText + '</div>' +
                    '</div>' +
                    '<div class="wizard-hw-card">' +
                        '<div class="wizard-hw-card__icon"><i class="fas fa-folder"></i></div>' +
                        '<div class="wizard-hw-card__label">Models Dir</div>' +
                        '<div class="wizard-hw-card__value" title="' + _escapeHtml(modelsDir) + '">' + _truncatePath(modelsDir) + '</div>' +
                    '</div>' +
                '</div>';

            // Pre-populate models dir input from detected value
            var dirInput = document.getElementById('wizardModelsDir');
            if (dirInput && config && config.models_dir && !dirInput.value) {
                dirInput.value = config.models_dir;
            }
        }).catch(function() {
            infoEl.innerHTML = '<div class="wizard-status wizard-status--error"><i class="fas fa-exclamation-triangle"></i> Could not detect hardware. Server may be starting up.</div>';
        });
    };

    // ── Step 2: Models Directory Scan ──

    /**
     * Save the models directory to server config, then scan for models.
     */
    SetupWizard.prototype._scanModelsDir = function() {
        var dirInput = document.getElementById('wizardModelsDir');
        var resultEl = document.getElementById('wizardScanResult');
        var scanBtn = document.getElementById('wizardScanBtn');
        if (!dirInput || !resultEl) return;

        this._data.modelsDir = dirInput.value.trim();
        if (!this._data.modelsDir) {
            this._showFieldError('wizardModelsDirError', 'Enter a directory path first');
            return;
        }
        this._hideFieldError('wizardModelsDirError');

        // Disable scan button with loading state
        _setButtonLoading(scanBtn, true, 'Scanning...');
        resultEl.textContent = '';

        var modelsDir = this._data.modelsDir;

        // First validate the path, then save and scan
        fetch('/api/v1/validate-path', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: modelsDir })
        })
        .then(function(res) { return res.json(); })
        .then(function(validation) {
            if (!validation.exists) {
                resultEl.innerHTML = '<i class="fas fa-times-circle" style="color:var(--danger)"></i> Directory not found: ' + _escapeHtml(modelsDir);
                _setButtonLoading(scanBtn, false, '<i class="fas fa-search"></i> Scan');
                return Promise.reject('invalid_path');
            }
            if (!validation.is_directory) {
                resultEl.innerHTML = '<i class="fas fa-times-circle" style="color:var(--danger)"></i> Path exists but is not a directory';
                _setButtonLoading(scanBtn, false, '<i class="fas fa-search"></i> Scan');
                return Promise.reject('invalid_path');
            }
            if (!validation.readable) {
                resultEl.innerHTML = '<i class="fas fa-times-circle" style="color:var(--danger)"></i> Directory is not readable (check permissions)';
                _setButtonLoading(scanBtn, false, '<i class="fas fa-search"></i> Scan');
                return Promise.reject('invalid_path');
            }

            // Path is valid — save config and trigger model scan
            return fetch('/api/v1/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ models_dir: modelsDir })
            }).then(function() {
                return fetch('/api/v1/models/refresh', { method: 'POST' });
            }).then(function(res) { return res.json(); })
            .then(function(data) {
                var count = 0;
                if (data.models && Array.isArray(data.models)) {
                    count = data.models.length;
                } else if (typeof data.count === 'number') {
                    count = data.count;
                } else if (typeof data.discovered === 'number') {
                    count = data.discovered;
                }
                if (count > 0) {
                    resultEl.innerHTML = '<i class="fas fa-check-circle" style="color:var(--success)"></i> Found ' + count + ' model' + (count !== 1 ? 's' : '');
                } else if (validation.gguf_count > 0) {
                    resultEl.innerHTML = '<i class="fas fa-check-circle" style="color:var(--success)"></i> Found ' + validation.gguf_count + ' .gguf file' + (validation.gguf_count !== 1 ? 's' : '') + ' (may exceed memory budget)';
                } else {
                    resultEl.innerHTML = '<i class="fas fa-exclamation-triangle" style="color:var(--warning)"></i> Directory exists but no .gguf model files found. You can download models in the next step.';
                }
            });
        })
        .catch(function(err) {
            if (err === 'invalid_path') return; // Already handled above
            resultEl.innerHTML = '<i class="fas fa-times-circle" style="color:var(--danger)"></i> Scan failed \u2014 could not connect to server';
        })
        .finally(function() {
            _setButtonLoading(scanBtn, false, '<i class="fas fa-search"></i> Scan');
        });
    };

    /**
     * Browse for suggested model directories from the backend.
     */
    SetupWizard.prototype._browseDirectory = function() {
        var dirInput = document.getElementById('wizardModelsDir');
        var resultEl = document.getElementById('wizardScanResult');
        var browseBtn = document.getElementById('wizardBrowseBtn');
        if (!dirInput) return;

        _setButtonLoading(browseBtn, true, 'Finding...');

        fetch('/api/v1/browse-directory', { method: 'POST' })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.suggestions && data.suggestions.length > 0) {
                    // Find best suggestion (one with models, or default)
                    var best = data.default || data.suggestions[0].path;

                    // Show dropdown of suggestions
                    var html = '<div class="wizard-browse-suggestions">';
                    data.suggestions.forEach(function(s) {
                        var badge = s.gguf_count > 0
                            ? '<span style="color:var(--success)">' + s.gguf_count + ' models</span>'
                            : s.exists
                                ? '<span style="color:var(--text-secondary)">empty</span>'
                                : '<span style="color:var(--text-secondary)">not found</span>';
                        html += '<div class="wizard-browse-item" data-path="' + _escapeHtml(s.path) + '" style="cursor:pointer;padding:0.5rem;border-radius:var(--radius);display:flex;justify-content:space-between;align-items:center;">' +
                            '<code style="font-size:0.85rem;">' + _escapeHtml(s.path) + '</code>' +
                            badge +
                        '</div>';
                    });
                    html += '</div>';
                    if (resultEl) resultEl.innerHTML = html;

                    // Click handler for suggestions
                    if (resultEl) {
                        resultEl.querySelectorAll('.wizard-browse-item').forEach(function(item) {
                            item.addEventListener('click', function() {
                                dirInput.value = item.dataset.path;
                                resultEl.textContent = '';
                            });
                            item.addEventListener('mouseenter', function() {
                                item.style.background = 'var(--dark-tertiary)';
                            });
                            item.addEventListener('mouseleave', function() {
                                item.style.background = '';
                            });
                        });
                    }

                    // Also set the best one
                    if (!dirInput.value) {
                        dirInput.value = best;
                    }
                }
            })
            .catch(function() {
                if (resultEl) resultEl.innerHTML = '<span style="color:var(--danger)">Could not detect directories</span>';
            })
            .finally(function() {
                _setButtonLoading(browseBtn, false, '<i class="fas fa-folder-open"></i> Browse');
            });
    };

    // ── Step 3: Model Discovery ──

    /**
     * Fetch local models AND popular downloadable models for step 3.
     */
    SetupWizard.prototype._loadModels = function() {
        var self = this;
        var listEl = document.getElementById('wizardModelList');
        var hfSection = document.getElementById('wizardHfSection');
        var hfResults = document.getElementById('wizardHfResults');
        if (!listEl) return;

        listEl.innerHTML = '<div class="wizard-model-empty"><i class="fas fa-spinner fa-spin"></i> Loading models...</div>';

        // Load local models and popular models in parallel
        Promise.allSettled([
            fetch('/api/v1/models').then(function(r) { return r.json(); }),
            fetch('/api/v1/models/popular?vram_gb=' + (self._detectedVram || 0)).then(function(r) { return r.json(); })
        ]).then(function(results) {
            // Local models
            var localData = results[0].status === 'fulfilled' ? results[0].value : {};
            self._allModels = localData.models || localData.items || [];
            self._renderLocalModels(self._allModels);

            // Popular models — show in HF section like LM Studio browse
            var popularData = results[1].status === 'fulfilled' ? results[1].value : {};
            var popularModels = popularData.models || [];
            if (popularModels.length > 0 && hfSection && hfResults) {
                hfSection.style.display = 'block';
                self._renderPopularModels(hfResults, popularModels);
            }
        }).catch(function() {
            listEl.innerHTML =
                '<div class="wizard-model-empty">' +
                    '<i class="fas fa-exclamation-triangle"></i> ' +
                    'Could not load models. You can configure them later.' +
                '</div>';
        });
    };

    /**
     * Render the local models list, optionally filtered by query.
     */
    SetupWizard.prototype._renderLocalModels = function(models) {
        var listEl = document.getElementById('wizardModelList');
        if (!listEl) return;

        if (!Array.isArray(models) || models.length === 0) {
            listEl.innerHTML =
                '<div class="wizard-model-empty">' +
                    '<i class="fas fa-cube"></i> ' +
                    'No models found. You can add models later from the Models tab.' +
                '</div>';
            return;
        }

        var html = '';
        models.forEach(function(m) {
            var name = m.name || m.id || m.model_id || 'Unknown';
            var size = m.size_gb ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(m.size_gb) + ' GB' : m.size || '';
            var active = m.active || m.is_active;

            html += '<div class="wizard-model-item">' +
                '<span class="wizard-model-item__name" title="' + _escapeHtml(name) + '">' + _escapeHtml(name) + '</span>';
            if (size) {
                html += '<span class="wizard-model-item__size">' + _escapeHtml(size) + '</span>';
            }
            if (active) {
                html += '<span class="wizard-model-item__badge">Active</span>';
            }
            html += '</div>';
        });

        listEl.innerHTML = html;
    };

    /**
     * Render popular downloadable models in an LM-Studio-style expandable list.
     * Each model shows its variants (quantizations) with size and download button.
     */
    SetupWizard.prototype._renderPopularModels = function(container, models) {
        if (!container || !models || models.length === 0) return;

        var html = '<div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:0.75rem;">' +
            '<i class="fas fa-fire" style="color:var(--warning)"></i> Popular <span class="vt-tooltip" data-tooltip="GGUF (GPT-Generated Unified Format) — a compact model file format optimized for fast local inference on consumer hardware">GGUF</span> Models — click to expand and download' +
        '</div>';

        models.forEach(function(model, idx) {
            var catIcon = model.category === 'coding' ? 'fa-code' : 'fa-comments';
            var catColor = model.category === 'coding' ? 'var(--primary)' : 'var(--success)';
            var fileCount = (model.files || []).length;

            html += '<div class="wizard-popular-model" style="border:1px solid var(--border-default);border-radius:var(--radius);margin-bottom:0.5rem;overflow:hidden;">';

            // Header (clickable to expand)
            html += '<div class="wizard-popular-header" data-idx="' + idx + '" style="padding:0.75rem;cursor:pointer;display:flex;align-items:center;gap:0.75rem;transition:background 0.15s;">';
            html += '<i class="fas ' + catIcon + '" style="color:' + catColor + ';font-size:1.1rem;width:20px;text-align:center;"></i>';
            html += '<div style="flex:1;min-width:0;">';
            html += '<div style="font-weight:600;">' + _escapeHtml(model.name) + ' <span style="color:var(--text-secondary);font-weight:400;font-size:0.8rem;">' + _escapeHtml(model.parameters) + '</span></div>';
            html += '<div style="color:var(--text-secondary);font-size:0.8rem;">' + _escapeHtml(model.description) + '</div>';
            html += '</div>';
            html += '<span style="color:var(--text-secondary);font-size:0.8rem;">' + fileCount + ' variant' + (fileCount !== 1 ? 's' : '') + '</span>';
            html += '<i class="fas fa-chevron-down wizard-popular-chevron" style="color:var(--text-secondary);transition:transform 0.2s;"></i>';
            html += '</div>';

            // File list (initially hidden)
            html += '<div class="wizard-popular-files" data-idx="' + idx + '" style="display:none;border-top:1px solid var(--border-default);background:var(--dark-tertiary);padding:0.5rem;">';
            if (model.files && model.files.length > 0) {
                model.files.forEach(function(f) {
                    var sizeText = f.size_gb ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(f.size_gb) + ' GB' : 'unknown size';
                    if (f.vram_needed_gb) sizeText += ' (~' + f.vram_needed_gb + ' GB <span class="vt-tooltip" data-tooltip="VRAM (Video RAM) — dedicated memory on your GPU used to load and run models. Larger models need more VRAM.">VRAM</span>)';
                    // Fit indicator
                    var fitIcon = '';
                    if (f.fits === true) {
                        fitIcon = '<i class="fas fa-check-circle" style="color:var(--success);margin-right:4px;" title="Fits in your GPU VRAM (Video RAM — dedicated memory on your GPU used to load and run models)"></i>';
                    } else if (f.fits === false) {
                        fitIcon = '<i class="fas fa-exclamation-triangle" style="color:var(--warning);margin-right:4px;" title="May not fit in GPU VRAM (Video RAM — dedicated memory on your GPU used to load and run models)"></i>';
                    }
                    // Extract quantization from filename (e.g., Q4_K_M, Q5_K_S, Q8_0)
                    var quantMatch = f.filename.match(/(Q\d[_.]\w+|[Ff]16|[Ff]32|IQ\d[_.]\w+)/i);
                    var quantLabel = quantMatch ? quantMatch[1] : '';

                    html += '<div style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0.5rem;border-radius:var(--radius);transition:background 0.1s;" class="wizard-file-row">';
                    html += '<code style="flex:1;font-size:0.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + _escapeHtml(f.filename) + '">';
                    if (quantLabel) {
                        html += '<span style="background:var(--primary);color:#fff;padding:1px 6px;border-radius:3px;font-size:0.75rem;margin-right:0.5rem;">' + _escapeHtml(quantLabel) + '</span>';
                    }
                    html += _escapeHtml(f.filename) + '</code>';
                    html += '<span style="color:var(--text-secondary);font-size:0.8rem;white-space:nowrap;">' + fitIcon + sizeText + '</span>';
                    html += '<button class="btn btn-small btn-primary wizard-dl-btn" type="button" ' +
                        'data-repo="' + _escapeHtml(model.repo_id) + '" ' +
                        'data-filename="' + _escapeHtml(f.filename) + '" ' +
                        'style="padding:0.25rem 0.75rem;font-size:0.8rem;">' +
                        '<i class="fas fa-download"></i></button>';
                    html += '</div>';
                });
            } else {
                html += '<div style="color:var(--text-secondary);font-size:0.8rem;padding:0.5rem;">Could not load file variants. Use Search to find this model.</div>';
            }
            html += '</div>';
            html += '</div>';
        });

        container.innerHTML = html;

        // Bind expand/collapse
        container.querySelectorAll('.wizard-popular-header').forEach(function(header) {
            header.addEventListener('click', function() {
                var idx = header.dataset.idx;
                var files = container.querySelector('.wizard-popular-files[data-idx="' + idx + '"]');
                var chevron = header.querySelector('.wizard-popular-chevron');
                if (files) {
                    var isOpen = files.style.display !== 'none';
                    files.style.display = isOpen ? 'none' : 'block';
                    if (chevron) chevron.style.transform = isOpen ? '' : 'rotate(180deg)';
                }
            });
            header.addEventListener('mouseenter', function() { header.style.background = 'var(--dark-tertiary)'; });
            header.addEventListener('mouseleave', function() { header.style.background = ''; });
        });

        // Bind hover on file rows
        container.querySelectorAll('.wizard-file-row').forEach(function(row) {
            row.addEventListener('mouseenter', function() { row.style.background = 'rgba(255,255,255,0.05)'; });
            row.addEventListener('mouseleave', function() { row.style.background = ''; });
        });

        // Bind download buttons
        container.querySelectorAll('.wizard-dl-btn').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var repo = btn.dataset.repo;
                var fname = btn.dataset.filename;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

                fetch('/api/v1/models/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ repo_id: repo, filename: fname })
                })
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    if (data.download_id) {
                        btn.innerHTML = '<i class="fas fa-check"></i>';
                        btn.style.background = 'var(--success)';
                        if (window.ToastManager) {
                            ToastManager.show('Downloading ' + fname, 'success');
                        }
                    } else {
                        btn.innerHTML = '<i class="fas fa-times"></i>';
                        btn.disabled = false;
                    }
                })
                .catch(function() {
                    btn.innerHTML = '<i class="fas fa-times"></i>';
                    btn.disabled = false;
                });
            });
        });
    };

    /**
     * Filter the local models list by the search input value.
     */
    SetupWizard.prototype._filterLocalModels = function() {
        var searchInput = document.getElementById('wizardModelSearch');
        var query = searchInput ? searchInput.value.trim().toLowerCase() : '';

        if (!query) {
            this._renderLocalModels(this._allModels);
            return;
        }

        var filtered = this._allModels.filter(function(m) {
            var name = (m.name || m.id || m.model_id || '').toLowerCase();
            return name.indexOf(query) !== -1;
        });
        this._renderLocalModels(filtered);
    };

    /**
     * Search HuggingFace for models matching the search input.
     */
    SetupWizard.prototype._searchHuggingFace = function() {
        var searchInput = document.getElementById('wizardModelSearch');
        var hfBtn = document.getElementById('wizardHfSearchBtn');
        var hfSection = document.getElementById('wizardHfSection');
        var hfResults = document.getElementById('wizardHfResults');
        var query = searchInput ? searchInput.value.trim() : '';

        if (!query || !hfResults || !hfSection) return;

        _setButtonLoading(hfBtn, true, 'Searching...');
        hfSection.style.display = 'block';
        hfResults.innerHTML = '<div class="wizard-status"><i class="fas fa-spinner fa-spin"></i> Searching HuggingFace...</div>';

        fetch('/api/v1/models/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query, limit: 10 })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            // Backend returns 'candidates', fallback to other fields for compatibility
            var results = data.candidates || data.results || data.models || data.items || [];
            if (!Array.isArray(results) || results.length === 0) {
                hfResults.innerHTML = '<div class="wizard-model-empty"><i class="fas fa-search"></i> No results for "' + _escapeHtml(query) + '"</div>';
                return;
            }

            var html = '';
            results.forEach(function(m) {
                var name = m.name || m.model_id || m.repo_id || m.id || 'Unknown';
                var size = m.size_gb ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(m.size_gb) + ' GB' : m.size || '';
                var repoId = m.repo_id || m.model_id || name;
                var filename = m.filename || m.recommended_file || '';
                var downloads = m.downloads ? m.downloads.toLocaleString() + ' downloads' : '';
                var author = m.author || (repoId.indexOf('/') !== -1 ? repoId.split('/')[0] : '');

                html += '<div class="wizard-model-item" style="flex-wrap:wrap;gap:0.25rem;">' +
                    '<div style="flex:1;min-width:200px;">' +
                        '<span class="wizard-model-item__name" title="' + _escapeHtml(repoId) + '">' + _escapeHtml(name) + '</span>';
                if (author) {
                    html += '<span style="color:var(--text-secondary);font-size:0.8rem;margin-left:0.5rem;">by ' + _escapeHtml(author) + '</span>';
                }
                html += '</div>';
                if (size) {
                    html += '<span class="wizard-model-item__size">' + _escapeHtml(size) + '</span>';
                }
                if (downloads) {
                    html += '<span style="color:var(--text-secondary);font-size:0.8rem;">' + downloads + '</span>';
                }
                html += '<button class="wizard-model-item__action wizard-download-btn" type="button" ' +
                    'data-repo="' + _escapeHtml(repoId) + '" ' +
                    'data-filename="' + _escapeHtml(filename) + '" ' +
                    'title="Download this model">' +
                    '<i class="fas fa-download"></i> Download' +
                '</button>';
                html += '</div>';
            });

            hfResults.innerHTML = html;

            // Bind download buttons
            hfResults.querySelectorAll('.wizard-download-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var repo = btn.dataset.repo;
                    var fname = btn.dataset.filename;
                    btn.disabled = true;
                    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Starting...';

                    fetch('/api/v1/models/download', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ repo_id: repo, filename: fname })
                    })
                    .then(function(res) { return res.json(); })
                    .then(function(dlData) {
                        if (dlData.download_id) {
                            btn.innerHTML = '<i class="fas fa-check"></i> Downloading...';
                            btn.style.background = 'var(--success)';
                            btn.style.color = '#fff';
                            if (window.ToastManager) {
                                ToastManager.show('Download started for ' + repo, 'success');
                            }
                        } else if (dlData.error) {
                            btn.innerHTML = '<i class="fas fa-times"></i> ' + _escapeHtml(dlData.error);
                            btn.disabled = false;
                        }
                    })
                    .catch(function() {
                        btn.innerHTML = '<i class="fas fa-times"></i> Failed';
                        btn.disabled = false;
                    });
                });
            });
        })
        .catch(function() {
            hfResults.innerHTML = '<div class="wizard-model-empty"><i class="fas fa-exclamation-triangle"></i> Search failed. Try again later.</div>';
        })
        .finally(function() {
            _setButtonLoading(hfBtn, false, '<i class="fas fa-cloud-download-alt"></i> Search HF');
        });
    };

    // ── Event Binding ──

    /**
     * Bind click events on all pill selector options.
     */
    SetupWizard.prototype._bindPillSelectors = function() {
        var pills = document.querySelectorAll('#setupWizard .pill-selector__option');
        pills.forEach(function(pill) {
            pill.addEventListener('click', function() {
                var parent = pill.parentElement;
                parent.querySelectorAll('.pill-selector__option').forEach(function(sib) {
                    sib.classList.remove('active');
                });
                pill.classList.add('active');
            });
        });
    };

    /**
     * Bind skip link buttons to advance to the next step.
     */
    SetupWizard.prototype._bindSkipLinks = function() {
        var self = this;
        document.querySelectorAll('#setupWizard .wizard-skip-link').forEach(function(link) {
            link.addEventListener('click', function(e) {
                e.preventDefault();
                self.nextStep();
            });
        });
    };

    // ── Data Collection ──

    /**
     * Collect preference selections from step 5.
     */
    SetupWizard.prototype._collectPreferences = function() {
        var autonomyPill = document.querySelector('#wizardStep5 .pill-selector[data-field="autonomyLevel"] .pill-selector__option.active');
        if (autonomyPill) {
            this._data.autonomyLevel = autonomyPill.dataset.value || 'assisted';
        }

        var themePill = document.querySelector('#wizardStep5 .pill-selector[data-field="theme"] .pill-selector__option.active');
        if (themePill) {
            this._data.theme = themePill.dataset.value || 'dark';
        }

        var notifToggle = document.getElementById('wizardNotifications');
        if (notifToggle) {
            this._data.notifications = notifToggle.checked;
        }
    };

    /**
     * Render the completion summary on step 6.
     */
    SetupWizard.prototype._renderSummary = function() {
        var container = document.getElementById('wizardSummary');
        if (!container) return;

        this._collectPreferences();

        var items = [
            { icon: 'fa-folder', label: 'Models Directory', value: this._data.modelsDir || 'Default' },
            { icon: 'fa-key', label: 'HuggingFace Token', value: this._data.hfToken ? 'Configured' : 'Skipped' },
            { icon: 'fa-cloud', label: 'Cloud API Key', value: this._data.cloudApiKey ? 'Configured' : 'Skipped' },
            { icon: 'fa-sliders-h', label: 'Autonomy Level', value: _capitalize(this._data.autonomyLevel) },
            { icon: 'fa-palette', label: 'Theme', value: _capitalize(this._data.theme) },
            { icon: 'fa-bell', label: 'Notifications', value: this._data.notifications ? 'Enabled' : 'Disabled' }
        ];

        var html = '';
        items.forEach(function(item) {
            html +=
                '<div class="wizard-summary__item">' +
                    '<i class="fas ' + item.icon + '"></i>' +
                    '<div>' +
                        '<div class="wizard-summary__label">' + item.label + '</div>' +
                        '<div class="wizard-summary__value">' + _escapeHtml(item.value) + '</div>' +
                    '</div>' +
                '</div>';
        });

        container.innerHTML = html;
    };

    // ── Completion ──

    /**
     * Save all wizard data and close the wizard.
     */
    SetupWizard.prototype._complete = function() {
        var nextBtn = document.getElementById('wizardNext');
        _setButtonLoading(nextBtn, true, 'Finishing...');

        // Save to VStore (client-side)
        if (window.VStore) {
            VStore.set(VStore.KEYS.SETUP_COMPLETE, true);
            VStore.set(VStore.KEYS.THEME, this._data.theme);
        }

        // Save to server-side preferences
        var prefs = {
            setupComplete: true,
            autonomyLevel: this._data.autonomyLevel,
            notificationPreferences: this._data.notifications ? 'all' : 'none',
            allowModelDownload: true,
            allowProjectExecute: true
        };

        var configUpdates = {};
        if (this._data.modelsDir) {
            configUpdates.models_dir = this._data.modelsDir;
        }
        if (this._data.hfToken) {
            configUpdates.hf_token = this._data.hfToken;
        }
        if (this._data.cloudApiKey) {
            configUpdates.api_token = this._data.cloudApiKey;
        }

        // Fire preferences and config saves in parallel
        var saves = [
            fetch('/api/v1/preferences', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(prefs)
            }).catch(function(err) {
                console.error('Failed to save wizard preferences:', err);
            })
        ];

        if (Object.keys(configUpdates).length > 0) {
            saves.push(
                fetch('/api/v1/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(configUpdates)
                }).catch(function(err) {
                    console.error('Failed to save wizard config:', err);
                })
            );
        }

        var self = this;
        Promise.all(saves).then(function() {
            // Apply theme immediately
            if (self._data.theme === 'light') {
                document.documentElement.setAttribute('data-theme', 'light');
            } else {
                document.documentElement.removeAttribute('data-theme');
            }

            // Clear saved wizard progress
            self._clearProgress();

            // Hide wizard
            self.hide();

            // Show welcome toast
            if (window.ToastManager) {
                ToastManager.show('Welcome to Vetinari! Your setup is complete.', 'success');
            }

            // Switch to chat view
            if (window.VApp && VApp.switchView) {
                VApp.switchView('prompt');
            }
        });
    };

    // ── Progress Persistence ──

    /**
     * Save current step and data to VStore for resume on refresh.
     */
    SetupWizard.prototype._saveProgress = function() {
        if (!window.VStore) return;
        // Collect current form field values before saving
        this._collectFormFields();
        var progressData = Object.assign({}, this._data);
        SECRET_FIELDS.forEach(function(field) {
            if (progressData[field]) progressData[field] = '';
        });
        VStore.set(PROGRESS_KEY, {
            step: this._currentStep,
            data: progressData
        });
    };

    /**
     * Restore saved progress from VStore.
     */
    SetupWizard.prototype._restoreProgress = function() {
        if (!window.VStore) return;
        var saved = VStore.get(PROGRESS_KEY);
        if (saved && saved.step && saved.data) {
            this._currentStep = saved.step;
            // Restore data fields, keeping defaults for any missing keys
            var keys = Object.keys(this._data);
            for (var i = 0; i < keys.length; i++) {
                if (saved.data[keys[i]] !== undefined) {
                    this._data[keys[i]] = saved.data[keys[i]];
                }
            }
        }
    };

    /**
     * Restore form field values from saved data after DOM is ready.
     */
    SetupWizard.prototype._restoreFormFields = function() {
        var dirInput = document.getElementById('wizardModelsDir');
        if (dirInput && this._data.modelsDir) dirInput.value = this._data.modelsDir;

        var hfInput = document.getElementById('wizardHfToken');
        if (hfInput && this._data.hfToken) hfInput.value = this._data.hfToken;

        var cloudInput = document.getElementById('wizardCloudApiKey');
        if (cloudInput && this._data.cloudApiKey) cloudInput.value = this._data.cloudApiKey;

        // Restore pill selections
        if (this._data.autonomyLevel) {
            _selectPill('#wizardStep5 .pill-selector[data-field="autonomyLevel"]', this._data.autonomyLevel);
        }
        if (this._data.theme) {
            _selectPill('#wizardStep5 .pill-selector[data-field="theme"]', this._data.theme);
        }

        var notifToggle = document.getElementById('wizardNotifications');
        if (notifToggle) notifToggle.checked = this._data.notifications;
    };

    /**
     * Collect current form field values into _data without validation.
     */
    SetupWizard.prototype._collectFormFields = function() {
        var dirInput = document.getElementById('wizardModelsDir');
        if (dirInput) this._data.modelsDir = dirInput.value.trim();

        var hfInput = document.getElementById('wizardHfToken');
        if (hfInput) this._data.hfToken = hfInput.value.trim();

        var cloudInput = document.getElementById('wizardCloudApiKey');
        if (cloudInput) this._data.cloudApiKey = cloudInput.value.trim();
    };

    /**
     * Clear saved wizard progress from VStore.
     */
    SetupWizard.prototype._clearProgress = function() {
        if (window.VStore) {
            VStore.remove(PROGRESS_KEY);
        }
    };

    // ── Field Errors ──

    /**
     * Show a field-level validation error.
     */
    SetupWizard.prototype._showFieldError = function(elementId, message) {
        var el = document.getElementById(elementId);
        if (el) {
            el.textContent = message;
            el.style.display = 'block';
        }
    };

    /**
     * Hide a field-level validation error.
     */
    SetupWizard.prototype._hideFieldError = function(elementId) {
        var el = document.getElementById(elementId);
        if (el) {
            el.style.display = 'none';
        }
    };

    // ── Utility helpers ──

    function _escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function _truncatePath(path) {
        if (!path || path.length <= 30) return _escapeHtml(path || '');
        return _escapeHtml('...' + path.slice(-27));
    }

    function _capitalize(str) {
        if (!str) return '';
        return str.charAt(0).toUpperCase() + str.slice(1);
    }

    /**
     * Toggle a button between loading and normal state.
     */
    function _setButtonLoading(btn, loading, label) {
        if (!btn) return;
        btn.disabled = loading;
        if (label) btn.innerHTML = loading ? '<i class="fas fa-spinner fa-spin"></i> ' + label : label;
    }

    /**
     * Select a pill option by value within a pill-selector container.
     */
    function _selectPill(containerSelector, value) {
        var container = document.querySelector(containerSelector);
        if (!container) return;
        container.querySelectorAll('.pill-selector__option').forEach(function(pill) {
            pill.classList.toggle('active', pill.dataset.value === value);
        });
    }

    /**
     * Copy text to clipboard using the Clipboard API with textarea fallback.
     */
    function _copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(function() {
                _fallbackCopy(text);
            });
        } else {
            _fallbackCopy(text);
        }
    }

    function _fallbackCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) { /* best effort */ }
        document.body.removeChild(ta);
    }

    // ── Export ──
    window.SetupWizard = SetupWizard;
})();
