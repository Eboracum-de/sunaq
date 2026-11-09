(function () {
    'use strict';

    function init() {
        var root = document.getElementById('sunaq-admin');
        if (!root) {
            return;
        }
        var save = document.getElementById('sunaq-save');
        var url = document.getElementById('sunaq-middleware-url');
        var key = document.getElementById('sunaq-api-key');
        var allowInsecureHttp = document.getElementById('sunaq-allow-insecure-http');
        var analysisMode = document.getElementById('sunaq-analysis-mode');
        var showSearchStrategy = document.getElementById('sunaq-show-search-strategy');
        var status = document.getElementById('sunaq-admin-status');
        var testButton = document.getElementById('sunaq-test-connection');
        var connectionSummary = document.getElementById('sunaq-connection-summary');
        var connectionDetails = document.getElementById('sunaq-connection-details');
        var serverAdminLink = document.getElementById('sunaq-server-admin-link');
        var runtimeInfo = document.getElementById('sunaq-runtime-info');
        var runtimeStatus = document.getElementById('sunaq-runtime-status');
        var runtimeVersion = document.getElementById('sunaq-runtime-version');
        var runtimeArchitecture = document.getElementById('sunaq-runtime-architecture');
        var runtimeModels = document.getElementById('sunaq-runtime-models');
        var runtimeSources = document.getElementById('sunaq-runtime-sources');

        function renderRuntime(data) {
            if (!runtimeInfo) {
                return;
            }
            if (!data || !data.ok) {
                runtimeInfo.hidden = true;
                return;
            }
            runtimeInfo.hidden = false;
            runtimeStatus.textContent = data.maintenance ? 'Maintenance' : 'Betriebsbereit';
            runtimeVersion.textContent = data.version || '–';
            runtimeArchitecture.textContent = data.architectureTier || '–';
            if (typeof data.modelCount === 'number') {
                runtimeModels.textContent = (
                    typeof data.configuredModelCount === 'number'
                        ? data.modelCount + ' freigegeben / ' + data.configuredModelCount + ' konfiguriert'
                        : data.modelCount + ' freigegeben'
                );
            } else {
                runtimeModels.textContent = data.maintenance ? 'im Maintenance-Modus nicht geladen' : '–';
            }
            runtimeSources.textContent = Array.isArray(data.sourceScopes) && data.sourceScopes.length
                ? data.sourceScopes.join(', ')
                : '–';
        }

        function renderConnection(data) {
            data = data || {};
            if (!data.ok) {
                connectionSummary.textContent = data.error || 'SunaQ ist nicht erreichbar.';
                connectionSummary.className = 'sunaq-admin-muted sunaq-admin-error';
                connectionDetails.textContent = '';
                serverAdminLink.hidden = true;
                renderRuntime(data);
                return;
            }

            if (data.maintenance) {
                connectionSummary.textContent = 'SunaQ ist erreichbar; Maintenance-Modus aktiv. Der API-Key ist gültig.';
                connectionSummary.className = 'sunaq-admin-muted';
            } else {
                connectionSummary.textContent = 'SunaQ ist erreichbar und betriebsbereit. Der API-Key ist gültig.';
                connectionSummary.className = 'sunaq-admin-muted sunaq-admin-ok';
            }
            var details = [];
            if (data.version) {
                details.push('Version ' + data.version);
            }
            if (data.architectureTier) {
                details.push('Architektur ' + data.architectureTier);
            }
            if (!data.maintenance && typeof data.modelCount === 'number') {
                if (typeof data.configuredModelCount === 'number' && data.configuredModelCount >= data.modelCount) {
                    details.push(data.modelCount + ' von ' + data.configuredModelCount + ' Modellen für diesen Nutzer freigegeben');
                } else {
                    details.push(data.modelCount + ' Modell(e) für diesen Nutzer freigegeben');
                }
            }
            if (Array.isArray(data.sourceScopes) && data.sourceScopes.length) {
                details.push('Quellen: ' + data.sourceScopes.join(', '));
            }
            connectionDetails.textContent = details.join(' · ');
            renderRuntime(data);
            if (data.adminUrl) {
                serverAdminLink.href = data.adminUrl;
                serverAdminLink.hidden = false;
            } else {
                serverAdminLink.hidden = true;
            }
        }

        function testConnection() {
            testButton.disabled = true;
            connectionSummary.textContent = 'Prüfe Verbindung …';
            connectionSummary.className = 'sunaq-admin-muted';
            connectionDetails.textContent = '';

            return fetch(OC.generateUrl('/apps/sunaq/admin/status'), {
                method: 'GET',
                credentials: 'same-origin',
                headers: {
                    'Accept': 'application/json',
                    'requesttoken': OC.requestToken
                }
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    if (!response.ok && !data.error) {
                        data.error = 'HTTP ' + response.status;
                    }
                    return data;
                });
            }).then(function (data) {
                renderConnection(data);
                return data;
            }).catch(function (error) {
                renderConnection({ ok: false, error: 'Statusprüfung fehlgeschlagen: ' + error.message });
            }).then(function (data) {
                testButton.disabled = false;
                return data;
            });
        }

        testButton.addEventListener('click', testConnection);

        save.addEventListener('click', function () {
            save.disabled = true;
            status.textContent = 'Speichere …';

            fetch(OC.generateUrl('/apps/sunaq/admin/config'), {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'requesttoken': OC.requestToken
                },
                body: JSON.stringify({
                    middlewareUrl: url.value.trim(),
                    apiKey: key.value.trim(),
                    allowInsecureHttp: !!(allowInsecureHttp && allowInsecureHttp.checked),
                    analysisMode: !!(analysisMode && analysisMode.checked),
                    showSearchStrategy: !!(showSearchStrategy && showSearchStrategy.checked)
                })
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    if (!response.ok) {
                        throw new Error(data.error || ('HTTP ' + response.status));
                    }
                    return data;
                });
            }).then(function () {
                key.value = '';
                key.placeholder = 'API-Key ist gespeichert – leer lassen zum Beibehalten';
                status.textContent = 'Gespeichert.';
                save.disabled = false;
                testConnection();
            }).catch(function (error) {
                status.textContent = 'Fehler: ' + error.message;
                save.disabled = false;
            });
        });

        testConnection();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
