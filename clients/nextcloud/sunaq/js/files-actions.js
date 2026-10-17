(function () {
    'use strict';

    if (!window.OC || !window.OCA || !OCA.Files || !OC.Plugins) {
        return;
    }

    function notify(message) {
        if (OC.Notification && typeof OC.Notification.showTemporary === 'function') {
            OC.Notification.showTemporary(message);
        }
    }

    function showResult(message) {
        if (OC.dialogs && typeof OC.dialogs.alert === 'function') {
            OC.dialogs.alert(message, 'SunaQ');
            return;
        }
        window.alert(message);
    }

    function currentDirectory(context) {
        if (context && typeof context.dir === 'string' && context.dir) {
            return context.dir;
        }
        if (
            context
            && context.fileList
            && typeof context.fileList.getCurrentDirectory === 'function'
        ) {
            return context.fileList.getCurrentDirectory() || '/';
        }
        return '/';
    }

    function selectedPath(fileName, context) {
        var dir = String(currentDirectory(context) || '/');
        var name = String(fileName || '');
        if (dir === '/') {
            return '/' + name.replace(/^\/+/, '');
        }
        return dir.replace(/\/+$/, '') + '/' + name.replace(/^\/+/, '');
    }

    function parentDirectory(path) {
        var value = String(path || '');
        var index = value.lastIndexOf('/');
        if (index <= 0) {
            return '/';
        }
        return value.substring(0, index);
    }

    function joinPath(dir, name) {
        var base = String(dir || '/');
        var leaf = String(name || '').replace(/^\/+/, '');
        if (base === '/') {
            return '/' + leaf;
        }
        return base.replace(/\/+$/, '') + '/' + leaf;
    }

    function selectedFileId(fileName, context) {
        if (context && context.$file && typeof context.$file.attr === 'function') {
            var rowId = Number(context.$file.attr('data-id') || 0);
            if (rowId > 0) {
                return rowId;
            }
        }

        var model = context && context.fileInfoModel ? context.fileInfoModel : null;
        if (model && typeof model.get === 'function') {
            var modelId = Number(model.get('id') || 0);
            if (modelId > 0) {
                return modelId;
            }
        }

        if (
            context
            && context.fileList
            && typeof context.fileList.getModelForFile === 'function'
        ) {
            model = context.fileList.getModelForFile(fileName);
            if (model && typeof model.get === 'function') {
                var lookupId = Number(model.get('id') || 0);
                if (lookupId > 0) {
                    return lookupId;
                }
            }
        }

        if (context && context.fileData && context.fileData.id) {
            return Number(context.fileData.id || 0);
        }
        return 0;
    }

    function isDirectory(context) {
        if (context && context.fileInfoModel && typeof context.fileInfoModel.get === 'function') {
            return String(context.fileInfoModel.get('type') || '') === 'dir';
        }
        if (context && context.fileData) {
            return String(context.fileData.type || '') === 'dir';
        }
        return false;
    }

    function fileListFromContext(context) {
        if (context && context.fileList) {
            return context.fileList;
        }
        if (OCA.Files.App && OCA.Files.App.fileList) {
            return OCA.Files.App.fileList;
        }
        return null;
    }

    function errorText(error) {
        if (error && error.message) {
            return String(error.message);
        }
        return String(error || 'Unbekannter Fehler');
    }

    function validateRename(fileName, newName, fileList) {
        var candidate = String(newName || '').trim();
        if (!candidate) {
            throw new Error('Der Dateiname darf nicht leer sein.');
        }
        if (candidate === fileName) {
            return candidate;
        }

        if (
            OCA.Files.Files
            && typeof OCA.Files.Files.isFileNameValid === 'function'
        ) {
            OCA.Files.Files.isFileNameValid(candidate);
        } else if (candidate.indexOf('/') !== -1) {
            throw new Error('"/" ist in einem Dateinamen nicht erlaubt.');
        }

        return candidate;
    }

    function renameSelectedFile(fileName, sourcePath, newName, context) {
        var fileList = fileListFromContext(context);
        if (!fileList || !fileList.filesClient || typeof fileList.filesClient.move !== 'function') {
            showResult('Umbenennen ist in dieser Nextcloud-Ansicht nicht verfügbar.');
            return;
        }

        var candidate;
        try {
            candidate = validateRename(fileName, newName, fileList);
        } catch (error) {
            showResult('Dateiname kann nicht übernommen werden:\n\n' + errorText(error));
            return;
        }

        if (candidate === fileName) {
            notify('Dateiname unverändert.');
            return;
        }

        var sourceDir = parentDirectory(sourcePath);
        var targetPath = joinPath(sourceDir, candidate);
        notify('SunaQ benennt die Datei um …');

        var row = typeof fileList.findFileEl === 'function'
            ? fileList.findFileEl(fileName)
            : null;
        var fileInfo = null;
        if (
            row
            && row.length
            && Array.isArray(fileList.files)
            && row.index() >= 0
        ) {
            fileInfo = fileList.files[row.index()] || null;
        }
        if (
            row
            && row.length
            && typeof fileList.showFileBusyState === 'function'
        ) {
            fileList.showFileBusyState(row, true);
        }

        var operation = fileList.filesClient.move(sourcePath, targetPath);
        if (!operation || typeof operation.done !== 'function') {
            if (
                row
                && row.length
                && typeof fileList.showFileBusyState === 'function'
            ) {
                fileList.showFileBusyState(row, false);
            }
            showResult('Umbenennen konnte nicht gestartet werden.');
            return;
        }

        operation.done(function () {
            if (
                row
                && row.length
                && fileInfo
                && typeof fileList.updateRow === 'function'
                && typeof fileList.getCurrentDirectory === 'function'
                && String(fileList.getCurrentDirectory() || '/') === sourceDir
            ) {
                fileInfo.name = candidate;
                fileList.updateRow(row, fileInfo, {
                    updateSummary: false,
                    silent: true
                });
            }
            notify('Datei umbenannt in ' + candidate);
        }).fail(function (status) {
            if (
                row
                && row.length
                && typeof fileList.showFileBusyState === 'function'
            ) {
                fileList.showFileBusyState(row, false);
            }
            var code = status && typeof status.status === 'number' ? status.status : status;
            if (code === 412) {
                showResult('Der Dateiname "' + candidate + '" ist bereits vergeben.');
                return;
            }
            if (code === 403) {
                showResult('Die Datei darf nicht umbenannt werden.');
                return;
            }
            if (code === 404) {
                showResult('Die Datei wurde nicht gefunden. Möglicherweise wurde sie zwischenzeitlich verschoben.');
                return;
            }
            showResult('Umbenennen fehlgeschlagen.');
        });
    }

    function prefillPrompt(counter, suggestion) {
        var input = document.getElementById('oc-dialog-' + counter + '-content-input');
        if (!input) {
            return;
        }
        input.value = suggestion;
        input.setAttribute('size', '80');
        input.style.display = 'block';
        input.style.boxSizing = 'border-box';
        input.style.width = '100%';
        input.style.maxWidth = 'none';
        input.style.marginTop = '8px';

        var content = document.getElementById('oc-dialog-' + counter + '-content');
        if (content) {
            content.style.width = '100%';
            content.style.maxWidth = 'none';
            if (
                window.jQuery
                && typeof window.jQuery(content).ocdialog === 'function'
            ) {
                var dialogWidth = Math.max(
                    360,
                    Math.min(window.innerWidth - 40, 760)
                );
                window.jQuery(content).ocdialog(
                    'option',
                    'width',
                    dialogWidth + 'px'
                );
            }
        }

        input.focus();
        if (typeof input.setSelectionRange === 'function') {
            var end = suggestion.lastIndexOf('.');
            if (end <= 0) {
                end = suggestion.length;
            }
            input.setSelectionRange(0, end);
        }
    }

    function showEditableSuggestion(suggestion, fileName, sourcePath, context) {
        if (
            OC.dialogs
            && typeof OC.dialogs.prompt === 'function'
            && typeof OC.dialogs.dialogsCounter === 'number'
        ) {
            var counter = OC.dialogs.dialogsCounter;
            var prompt = OC.dialogs.prompt(
                'Vorgeschlagenen Dateinamen prüfen, bei Bedarf bearbeiten und anschließend übernehmen.',
                'SunaQ – Dateiname',
                function (accepted, value) {
                    if (!accepted) {
                        notify('Dateinamenvorschlag verworfen.');
                        return;
                    }
                    renameSelectedFile(fileName, sourcePath, value, context);
                },
                true,
                'Dateiname',
                false
            );
            if (prompt && typeof prompt.then === 'function') {
                prompt.then(function () {
                    prefillPrompt(counter, suggestion);
                });
            } else {
                prefillPrompt(counter, suggestion);
            }
            return;
        }

        var edited = window.prompt(
            'Vorgeschlagenen Dateinamen bearbeiten und übernehmen:',
            suggestion
        );
        if (edited === null) {
            notify('Dateinamenvorschlag verworfen.');
            return;
        }
        renameSelectedFile(fileName, sourcePath, edited, context);
    }

    function installLegacyDropdownBinding(fileList) {
        if (
            !fileList
            || !fileList.$el
            || !fileList.$el.length
            || fileList._sunaqLegacyDropdownBound
        ) {
            return;
        }

        var root = fileList.$el[0];
        if (!root || typeof root.addEventListener !== 'function') {
            return;
        }

        fileList._sunaqLegacyDropdownBound = true;
        root.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || !window.jQuery) {
                return;
            }

            var action = window.jQuery(target).closest(
                '.fileActionsMenu a.action[data-action="SunaqSuggestFilename"]'
            );
            if (!action.length) {
                return;
            }

            // NC23's FileActionsMenu resolves the clicked action through the
            // mutable global fileActions.currentFile. That state can point at a
            // different row by the time the dropdown item is clicked. Bind the
            // SunaQ action to the row that actually owns this menu instead.
            event.preventDefault();
            event.stopPropagation();
            if (typeof event.stopImmediatePropagation === 'function') {
                event.stopImmediatePropagation();
            }

            var row = action.closest('tr');
            if (!row.length) {
                showResult('Die ausgewählte Datei konnte nicht bestimmt werden.');
                return;
            }

            var fileName = String(row.attr('data-file') || '');
            if (!fileName) {
                showResult('Die ausgewählte Datei konnte nicht bestimmt werden.');
                return;
            }

            var callContext = {
                $file: row,
                fileActions: fileList.fileActions,
                fileList: fileList,
                dir: row.attr('data-path')
                    || (
                        typeof fileList.getCurrentDirectory === 'function'
                            ? fileList.getCurrentDirectory()
                            : '/'
                    )
            };
            if (typeof fileList.getModelForFile === 'function') {
                callContext.fileInfoModel = fileList.getModelForFile(fileName);
            }

            if (window.console && typeof window.console.info === 'function') {
                window.console.info('[SunaQ file action] legacy dropdown click', {
                    fileName: fileName,
                    fileId: Number(row.attr('data-id') || 0),
                    dir: callContext.dir
                });
            }

            if (window.OC && typeof OC.hideMenus === 'function') {
                OC.hideMenus();
            }
            requestSuggestion(fileName, callContext);
        }, true);
    }

    function requestSuggestion(fileName, context) {
        if (isDirectory(context)) {
            showResult('Bitte eine Datei auswählen.');
            return;
        }

        var path = selectedPath(fileName, context);
        var fileId = selectedFileId(fileName, context);
        if (window.console && typeof window.console.info === 'function') {
            window.console.info('[SunaQ file action] request', {
                fileName: fileName,
                path: path,
                fileId: fileId,
                rowId: context && context.$file && typeof context.$file.attr === 'function'
                    ? context.$file.attr('data-id')
                    : null
            });
        }
        notify('SunaQ analysiert ' + fileName + ' …');

        fetch(OC.generateUrl('/apps/sunaq/files/suggest-name'), {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'requesttoken': OC.requestToken,
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            body: JSON.stringify({
                path: path,
                fileId: fileId
            })
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok) {
                    throw new Error(data.error || ('HTTP ' + response.status));
                }
                return data;
            });
        }).then(function (data) {
            if (window.console && typeof window.console.info === 'function') {
                window.console.info('[SunaQ file action] response', {
                    fileName: fileName,
                    fileId: fileId,
                    reference: data.reference || '',
                    fallbackUsed: Boolean(data.fallback_used)
                });
            }
            var suggestion = String(data.suggested_name || '').trim();
            if (!suggestion) {
                throw new Error('SunaQ hat keinen Dateinamen vorgeschlagen.');
            }
            showEditableSuggestion(suggestion, fileName, path, context);
        }).catch(function (error) {
            showResult('Dateinamenvorschlag fehlgeschlagen:\n\n' + errorText(error));
        });
    }

    var plugin = {
        attach: function (fileList) {
            if (!fileList || !fileList.fileActions) {
                return;
            }
            installLegacyDropdownBinding(fileList);
            fileList.fileActions.registerAction({
                name: 'SunaqSuggestFilename',
                displayName: 'Dateinamen mit SunaQ vorschlagen',
                mime: 'all',
                permissions: OC.PERMISSION_UPDATE,
                iconClass: 'icon-rename',
                order: 1500,
                actionHandler: requestSuggestion
            });

            // NC23's legacy dropdown keeps mutable/global FileActions state.
            // Keep the menu action for comparison, but expose the same handler
            // inline so the selected row is bound directly at click time.
            if (
                OCA.Files.FileActions
                && typeof OCA.Files.FileActions.TYPE_INLINE !== 'undefined'
            ) {
                fileList.fileActions.registerAction({
                    name: 'SunaqSuggestFilenameInline',
                    displayName: '',
                    altText: 'Dateinamen mit SunaQ vorschlagen',
                    mime: 'all',
                    permissions: OC.PERMISSION_UPDATE,
                    iconClass: 'icon-rename',
                    order: 1490,
                    type: OCA.Files.FileActions.TYPE_INLINE,
                    actionHandler: requestSuggestion
                });
            }
        }
    };

    OC.Plugins.register('OCA.Files.FileList', plugin);
})();
