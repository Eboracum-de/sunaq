(function () {
    'use strict';

    if (!window.OC) {
        return;
    }

    var ACTION_ID = 'sunaq-suggest-filename';
    var PERMISSION_UPDATE = 2;
    var PERMISSION_DELETE = 8;
    var ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25m17.71-10.04a1.003 1.003 0 0 0 0-1.42l-2.5-2.5a1.003 1.003 0 0 0-1.42 0l-1.96 1.96 3.75 3.75 2.13-1.79Z"/></svg>';

    function notify(message) {
        if (OC.Notification && typeof OC.Notification.showTemporary === 'function') {
            OC.Notification.showTemporary(message);
        }
    }

    function showError(message) {
        if (OC.dialogs && typeof OC.dialogs.alert === 'function') {
            OC.dialogs.alert(message, 'SunaQ');
            return;
        }
        window.alert(message);
    }

    function errorText(error) {
        return String(error && error.message ? error.message : error || 'Unbekannter Fehler');
    }

    function postJson(path, payload) {
        return fetch(OC.generateUrl('/apps/sunaq' + path), {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                requesttoken: OC.requestToken,
                'Content-Type': 'application/json',
                Accept: 'application/json'
            },
            body: JSON.stringify(payload || {})
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok) {
                    throw new Error(data.error || ('HTTP ' + response.status));
                }
                return data;
            });
        });
    }

    function nodeId(node) {
        if (!node) {
            return '';
        }
        if (node.id !== undefined && node.id !== null && String(node.id) !== '') {
            return String(node.id);
        }
        if (node.fileid !== undefined && node.fileid !== null && String(node.fileid) !== '') {
            return String(node.fileid);
        }
        return '';
    }

    function nodePath(node) {
        if (!node) {
            return '';
        }
        return String(node.path || '');
    }

    function isFile(node) {
        if (!node) {
            return false;
        }
        var type = String(node.type || '').toLowerCase();
        return type !== 'folder' && type !== 'dir';
    }

    function mayRename(node, view) {
        if (!isFile(node)) {
            return false;
        }
        if (view && String(view.id || '') === 'public-file-share') {
            return false;
        }
        var permissions = Number(node.permissions || 0);
        return Boolean(permissions & PERMISSION_UPDATE) || Boolean(permissions & PERMISSION_DELETE);
    }

    function editSuggestion(suggestion) {
        return Promise.resolve(window.prompt(
            'Vorgeschlagenen Dateinamen prüfen, bei Bedarf bearbeiten und anschließend übernehmen:',
            suggestion
        ));
    }

    function executeForNode(node) {
        var path = nodePath(node);
        var fileId = nodeId(node);
        if (!path) {
            return Promise.reject(new Error('Der Pfad der ausgewählten Datei ist nicht verfügbar.'));
        }

        notify('SunaQ analysiert ' + String(node.basename || node.displayname || 'die Datei') + ' …');
        return postJson('/files/suggest-name', {
            path: path,
            fileId: fileId
        }).then(function (data) {
            var suggestion = String(data.suggested_name || '').trim();
            if (!suggestion) {
                throw new Error('SunaQ hat keinen Dateinamen vorgeschlagen.');
            }
            return editSuggestion(suggestion);
        }).then(function (newName) {
            if (newName === null) {
                notify('Dateinamenvorschlag verworfen.');
                return null;
            }
            newName = String(newName || '').trim();
            if (!newName) {
                throw new Error('Der Dateiname darf nicht leer sein.');
            }
            return postJson('/files/rename', {
                path: path,
                fileId: fileId,
                newName: newName
            });
        }).then(function (result) {
            if (result === null) {
                return null;
            }
            var renamed = String(result.new_name || '').trim();
            if (renamed && typeof node.rename === 'function') {
                node.rename(renamed);
            }
            notify(result.changed === false ? 'Dateiname unverändert.' : 'Datei umbenannt in ' + renamed);
            return true;
        }).catch(function (error) {
            showError('Dateinamenvorschlag/Umbenennen fehlgeschlagen:\n\n' + errorText(error));
            return null;
        });
    }

    function installV3Action() {
        window._nc_fileactions = Array.isArray(window._nc_fileactions) ? window._nc_fileactions : [];
        if (window._nc_fileactions.some(function (action) { return action && action.id === ACTION_ID; })) {
            return;
        }
        window._nc_fileactions.push({
            id: ACTION_ID,
            displayName: function () { return 'Dateinamen mit SunaQ vorschlagen'; },
            title: function () { return 'Dateinamen mit SunaQ vorschlagen'; },
            iconSvgInline: function () { return ICON; },
            enabled: function (files, view) {
                return Array.isArray(files) && files.length === 1 && mayRename(files[0], view);
            },
            exec: function (file) {
                return executeForNode(file);
            },
            order: 1500
        });
    }

    function installV4Action() {
        window._nc_files_scope = window._nc_files_scope || {};
        var scope = window._nc_files_scope.v4_0 = window._nc_files_scope.v4_0 || {};
        scope.fileActions = scope.fileActions instanceof Map ? scope.fileActions : new Map();
        if (scope.fileActions.has(ACTION_ID)) {
            return;
        }

        var action = {
            id: ACTION_ID,
            displayName: function () { return 'Dateinamen mit SunaQ vorschlagen'; },
            title: function () { return 'Dateinamen mit SunaQ vorschlagen'; },
            iconSvgInline: function () { return ICON; },
            enabled: function (context) {
                var nodes = context && Array.isArray(context.nodes) ? context.nodes : [];
                return nodes.length === 1 && mayRename(nodes[0], context && context.view);
            },
            exec: function (context) {
                var nodes = context && Array.isArray(context.nodes) ? context.nodes : [];
                if (nodes.length !== 1) {
                    return Promise.resolve(null);
                }
                return executeForNode(nodes[0]);
            },
            order: 1500
        };

        scope.fileActions.set(ACTION_ID, action);

        // If Files already initialized the v4 registry, notify it as well.
        // Normally addInitScript runs before Files and this branch is unnecessary.
        if (scope.registry && typeof scope.registry.dispatchEvent === 'function') {
            scope.registry.dispatchEvent(new CustomEvent('register:action', { detail: action }));
        }
    }

    // @nextcloud/files 3.x (NC26-32) and 4.x (NC33+) intentionally use
    // incompatible global registries. Seed both from the init script; each
    // Files generation reads only its matching registry.
    installV3Action();
    installV4Action();
})();
