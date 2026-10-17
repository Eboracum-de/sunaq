(function () {
    'use strict';

    var messages = [];
    var conversationId = '';
    var editingIndex = null;
    var form;
    var input;
    var sendButton;
    var cancelEditButton;
    var status;
    var retrievalStrategy;
    var retrievalStrategyContent;
    var messageList;
    var retryButton;
    var chatList;
    var modelSelect;
    var modelSourceCapabilities = {};
    var fallbackSourceCapabilities = {};
    var sidebar;
    var sidebarToggle;
    var activeRequestId = '';
    var progressTimer = null;
    var scopeInputs = [];
    var sourceScopeLabels = {
        documents: 'Dokumente',
        mailarchive: 'Mailarchiv',
        chatarchive: 'Chatarchiv',
        web: 'Web'
    };

    function appendInlineMarkdown(container, text) {
        var tokenRe = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]{1,240}\]\((?:https?:\/\/)[^\s)]+\)|https?:\/\/[^\s<]+)/g;
        var last = 0;
        var match;
        while ((match = tokenRe.exec(text)) !== null) {
            if (match.index > last) {
                container.appendChild(document.createTextNode(text.slice(last, match.index)));
            }
            var token = match[0];
            if (token.charAt(0) === '`' && token.charAt(token.length - 1) === '`') {
                var code = document.createElement('code');
                code.textContent = token.slice(1, -1);
                container.appendChild(code);
            } else if (token.slice(0, 2) === '**' && token.slice(-2) === '**') {
                var strong = document.createElement('strong');
                strong.textContent = token.slice(2, -2);
                container.appendChild(strong);
            } else if ((token.charAt(0) === '*' && token.charAt(token.length - 1) === '*')
                    || (token.charAt(0) === '_' && token.charAt(token.length - 1) === '_')) {
                var em = document.createElement('em');
                em.textContent = token.slice(1, -1);
                container.appendChild(em);
            } else {
                var linkMatch = token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
                var href = linkMatch ? linkMatch[2] : token;
                var label = linkMatch ? linkMatch[1] : token;
                var trailing = '';
                if (!linkMatch) {
                    while (/[.,;:!?)]$/.test(href)) {
                        trailing = href.slice(-1) + trailing;
                        href = href.slice(0, -1);
                    }
                }
                try {
                    var url = new URL(href, window.location.href);
                    if (url.protocol === 'http:' || url.protocol === 'https:') {
                        var a = document.createElement('a');
                        a.href = url.href;
                        a.textContent = label;
                        a.target = '_blank';
                        a.rel = 'noopener noreferrer';
                        container.appendChild(a);
                    } else {
                        container.appendChild(document.createTextNode(token));
                    }
                } catch (e) {
                    container.appendChild(document.createTextNode(token));
                }
                if (trailing) {
                    container.appendChild(document.createTextNode(trailing));
                }
            }
            last = tokenRe.lastIndex;
        }
        if (last < text.length) {
            container.appendChild(document.createTextNode(text.slice(last)));
        }
    }

    function splitMarkdownTableRow(line) {
        var value = String(line || '').trim();
        if (value.charAt(0) === '|') {
            value = value.slice(1);
        }
        if (value.charAt(value.length - 1) === '|') {
            value = value.slice(0, -1);
        }
        return value.split('|').map(function (cell) { return cell.trim(); });
    }

    function markdownTableAlignment(cell) {
        var value = String(cell || '').trim();
        if (!/^:?-{3,}:?$/.test(value)) {
            return null;
        }
        if (value.charAt(0) === ':' && value.charAt(value.length - 1) === ':') {
            return 'center';
        }
        if (value.charAt(value.length - 1) === ':') {
            return 'right';
        }
        if (value.charAt(0) === ':') {
            return 'left';
        }
        return '';
    }

    function isMarkdownTableStart(lines, index) {
        if (index + 1 >= lines.length || lines[index].indexOf('|') === -1) {
            return false;
        }
        var header = splitMarkdownTableRow(lines[index]);
        var separator = splitMarkdownTableRow(lines[index + 1]);
        if (header.length < 2 || separator.length !== header.length) {
            return false;
        }
        return separator.every(function (cell) {
            return markdownTableAlignment(cell) !== null;
        });
    }

    function appendMarkdownTable(container, lines, index) {
        var headers = splitMarkdownTableRow(lines[index]);
        var separators = splitMarkdownTableRow(lines[index + 1]);
        var alignments = separators.map(markdownTableAlignment);
        var wrapper = document.createElement('div');
        wrapper.className = 'sunaq-table-wrap';
        var table = document.createElement('table');
        table.className = 'sunaq-table';
        var thead = document.createElement('thead');
        var headRow = document.createElement('tr');
        headers.forEach(function (header, column) {
            var th = document.createElement('th');
            if (alignments[column]) { th.style.textAlign = alignments[column]; }
            appendInlineMarkdown(th, header);
            headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        var i = index + 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') !== -1) {
            var cells = splitMarkdownTableRow(lines[i]);
            if (cells.length !== headers.length) {
                break;
            }
            var tr = document.createElement('tr');
            cells.forEach(function (cell, column) {
                var td = document.createElement('td');
                if (alignments[column]) { td.style.textAlign = alignments[column]; }
                appendInlineMarkdown(td, cell);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
            i += 1;
        }
        table.appendChild(tbody);
        wrapper.appendChild(table);
        container.appendChild(wrapper);
        return i;
    }

    function appendMarkdown(container, text) {
        var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
        var i = 0;

        function appendParagraph(parts) {
            if (!parts.length) {
                return;
            }
            var p = document.createElement('p');
            appendInlineMarkdown(p, parts.join(' '));
            container.appendChild(p);
        }

        while (i < lines.length) {
            var line = lines[i];
            if (!line.trim()) {
                i += 1;
                continue;
            }

            if (isMarkdownTableStart(lines, i)) {
                i = appendMarkdownTable(container, lines, i);
                continue;
            }

            if (/^```/.test(line.trim())) {
                var language = line.trim().slice(3).trim();
                var codeLines = [];
                i += 1;
                while (i < lines.length && !/^```/.test(lines[i].trim())) {
                    codeLines.push(lines[i]);
                    i += 1;
                }
                if (i < lines.length) {
                    i += 1;
                }
                var pre = document.createElement('pre');
                var code = document.createElement('code');
                if (language) {
                    code.setAttribute('data-language', language);
                }
                code.textContent = codeLines.join('\n');
                pre.appendChild(code);
                container.appendChild(pre);
                continue;
            }

            if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
                container.appendChild(document.createElement('hr'));
                i += 1;
                continue;
            }

            var heading = line.match(/^(#{1,6})\s+(.+)$/);
            if (heading) {
                var h = document.createElement('h' + Math.min(6, heading[1].length + 2));
                appendInlineMarkdown(h, heading[2]);
                container.appendChild(h);
                i += 1;
                continue;
            }

            if (/^\s*[-*+]\s+/.test(line)) {
                var ul = document.createElement('ul');
                while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
                    var li = document.createElement('li');
                    appendInlineMarkdown(li, lines[i].replace(/^\s*[-*+]\s+/, ''));
                    ul.appendChild(li);
                    i += 1;
                }
                container.appendChild(ul);
                continue;
            }

            if (/^\s*\d+\.\s+/.test(line)) {
                var ol = document.createElement('ol');
                while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
                    var oli = document.createElement('li');
                    appendInlineMarkdown(oli, lines[i].replace(/^\s*\d+\.\s+/, ''));
                    ol.appendChild(oli);
                    i += 1;
                }
                container.appendChild(ol);
                continue;
            }

            if (/^\s*>\s?/.test(line)) {
                var quote = document.createElement('blockquote');
                var quoteParts = [];
                while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                    quoteParts.push(lines[i].replace(/^\s*>\s?/, ''));
                    i += 1;
                }
                appendInlineMarkdown(quote, quoteParts.join(' '));
                container.appendChild(quote);
                continue;
            }

            var paragraph = [];
            while (i < lines.length && lines[i].trim()
                && !isMarkdownTableStart(lines, i)
                && !/^(#{1,6})\s+/.test(lines[i])
                && !/^```/.test(lines[i].trim())
                && !/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(lines[i])
                && !/^\s*[-*+]\s+/.test(lines[i])
                && !/^\s*\d+\.\s+/.test(lines[i])
                && !/^\s*>\s?/.test(lines[i])) {
                paragraph.push(lines[i].trim());
                i += 1;
            }
            appendParagraph(paragraph);
        }
    }

    function formatTimestamp(value) {
        if (!value) {
            return '';
        }
        var date = new Date(value);
        if (isNaN(date.getTime())) {
            return '';
        }
        try {
            return new Intl.DateTimeFormat('de-DE', {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            }).format(date);
        } catch (e) {
            return date.toLocaleString();
        }
    }

    function formatSourceScopes(scopes) {
        if (!Array.isArray(scopes) || !scopes.length) {
            return '';
        }
        var labels = [];
        scopes.forEach(function (scope) {
            var value = String(scope || '').toLowerCase();
            var label = sourceScopeLabels[value];
            if (label && labels.indexOf(label) === -1) {
                labels.push(label);
            }
        });
        return labels.length ? 'Quellen: ' + labels.join(', ') : '';
    }

    function makeSmallButton(label, title, handler) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'button sunaq-message-action';
        button.textContent = label;
        button.title = title;
        button.addEventListener('click', handler);
        return button;
    }

    function applySuggestedModel(modelId) {
        if (!modelSelect || !modelId) {
            return;
        }
        var wanted = String(modelId);
        var exists = Array.prototype.some.call(modelSelect.options, function (option) {
            return String(option.value) === wanted;
        });
        if (exists) {
            modelSelect.value = wanted;
        }
    }

    function previousUserIndex(beforeIndex) {
        for (var i = Number(beforeIndex) - 1; i >= 0; i -= 1) {
            if (messages[i] && messages[i].role === 'user') {
                return i;
            }
        }
        return -1;
    }

    function runSuggestion(suggestion, messageIndex) {
        if (!suggestion || sendButton.disabled) {
            return;
        }
        var action = String(suggestion.action || '').toLowerCase();
        var query = String(suggestion.query || '').trim();
        applySuggestedModel(String(suggestion.model || ''));

        if (action === 'rerun' && query) {
            var userIndex = previousUserIndex(messageIndex);
            sendBranch(userIndex >= 0 ? userIndex : null, query);
            return;
        }
        if (action === 'query' && query) {
            submitQuery(query);
            return;
        }

        if (query) {
            input.value = query;
        }
        status.textContent = 'Bitte Suchkriterien präzisieren und erneut senden.';
        input.focus();
    }

    function addMessage(role, content, index, createdAt, sourceScopes, suggestions) {
        var wrapper = document.createElement('div');
        wrapper.className = 'sunaq-message ' + (role === 'user' ? 'sunaq-user' : 'sunaq-assistant');

        var metaNode = document.createElement('div');
        metaNode.className = 'sunaq-message-meta';
        var roleNode = document.createElement('span');
        roleNode.className = 'sunaq-role';
        roleNode.textContent = role === 'user' ? 'Sie' : 'SunaQ';
        metaNode.appendChild(roleNode);
        var timestamp = formatTimestamp(createdAt);
        if (timestamp) {
            var timeNode = document.createElement('time');
            timeNode.className = 'sunaq-message-time';
            timeNode.dateTime = createdAt;
            timeNode.textContent = timestamp;
            metaNode.appendChild(timeNode);
        }
        var sourceScopeText = role === 'assistant' ? formatSourceScopes(sourceScopes) : '';
        if (sourceScopeText) {
            var scopesNode = document.createElement('span');
            scopesNode.className = 'sunaq-message-scopes';
            scopesNode.textContent = sourceScopeText;
            metaNode.appendChild(scopesNode);
        }

        var contentNode = document.createElement('div');
        contentNode.className = 'sunaq-content';
        appendMarkdown(contentNode, content);

        wrapper.appendChild(metaNode);
        wrapper.appendChild(contentNode);

        if (role === 'user' && typeof index === 'number') {
            var controls = document.createElement('div');
            controls.className = 'sunaq-message-actions';
            controls.appendChild(makeSmallButton('Erneut senden', 'Diese Frage ab diesem Punkt erneut an SunaQ senden', function () {
                resendAt(index);
            }));
            controls.appendChild(makeSmallButton('Bearbeiten & erneut senden', 'Diese Frage ändern und den Verlauf ab hier neu erzeugen', function () {
                beginEdit(index);
            }));
            wrapper.appendChild(controls);
        }

        if (role === 'assistant' && Array.isArray(suggestions) && suggestions.length) {
            var suggestionControls = document.createElement('div');
            suggestionControls.className = 'sunaq-message-actions sunaq-suggestions';
            suggestions.slice(0, 4).forEach(function (suggestion) {
                if (!suggestion || !suggestion.label) {
                    return;
                }
                suggestionControls.appendChild(makeSmallButton(
                    String(suggestion.label),
                    'Vorgeschlagene Folgeaktion',
                    function () { runSuggestion(suggestion, index); }
                ));
            });
            if (suggestionControls.childNodes.length) {
                wrapper.appendChild(suggestionControls);
            }
        }

        messageList.appendChild(wrapper);
        return wrapper;
    }

    function renderConversation() {
        messageList.innerHTML = '';
        if (!messages.length) {
            addMessage('assistant', 'Was möchten Sie finden?');
        } else {
            messages.forEach(function (message, index) {
                addMessage(
                    message.role,
                    message.content,
                    index,
                    message.created_at || '',
                    message.source_scopes || [],
                    message.suggestions || []
                );
            });
        }
        messageList.scrollTop = messageList.scrollHeight;
    }

    function scopeAvailable(inputNode) {
        return inputNode.getAttribute('data-sunaq-available') !== 'false';
    }

    function selectedScopes() {
        return scopeInputs.filter(function (inputNode) {
            return inputNode.checked && scopeAvailable(inputNode);
        }).map(function (inputNode) {
            return inputNode.value;
        });
    }

    function applySourceCapabilities(capabilities) {
        if (!capabilities || typeof capabilities !== 'object') {
            return;
        }
        scopeInputs.forEach(function (inputNode) {
            if (!Object.prototype.hasOwnProperty.call(capabilities, inputNode.value)) {
                return;
            }
            var available = capabilities[inputNode.value] !== false;
            inputNode.setAttribute('data-sunaq-available', available ? 'true' : 'false');
            inputNode.disabled = !available;
            if (!available) {
                inputNode.checked = false;
            }
            var labelNode = inputNode.closest ? inputNode.closest('label') : inputNode.parentNode;
            if (labelNode) {
                labelNode.hidden = !available;
                labelNode.style.display = available ? '' : 'none';
                labelNode.setAttribute('aria-hidden', available ? 'false' : 'true');
            }
        });
    }

    function applyScopes(scopes) {
        var selected = Array.isArray(scopes) ? scopes : [];
        scopeInputs.forEach(function (inputNode) {
            inputNode.checked = scopeAvailable(inputNode) && selected.indexOf(inputNode.value) !== -1;
        });
    }

    function refreshSelectedModelCapabilities() {
        var selected = modelSelect ? String(modelSelect.value || '') : '';
        applySourceCapabilities(modelSourceCapabilities[selected] || fallbackSourceCapabilities);
    }

    function loadModels() {
        if (!modelSelect) {
            return Promise.resolve();
        }
        var previous = modelSelect.value;
        return apiRequest('/models', 'GET').then(function (data) {
            var models = Array.isArray(data.models) ? data.models : [];
            var defaultModel = String(data.default_model || '');
            var userSettings = data.user_settings && typeof data.user_settings === 'object'
                ? data.user_settings
                : {};
            fallbackSourceCapabilities = userSettings.source_capabilities || {};
            modelSourceCapabilities = {};
            modelSelect.innerHTML = '';
            models.forEach(function (model) {
                if (!model || !model.id) {
                    return;
                }
                var option = document.createElement('option');
                option.value = String(model.id);
                if (model.source_capabilities && typeof model.source_capabilities === 'object') {
                    modelSourceCapabilities[String(model.id)] = model.source_capabilities;
                }
                option.textContent = String(model.name || model.id);
                if (model.description) {
                    option.title = String(model.description);
                }
                modelSelect.appendChild(option);
            });
            if (!models.length) {
                var fallback = document.createElement('option');
                fallback.value = '';
                fallback.textContent = 'Schnell';
                modelSelect.appendChild(fallback);
                return;
            }
            var available = models.some(function (model) {
                return String(model.id) === previous;
            });
            modelSelect.value = available ? previous : defaultModel;
            if (!modelSelect.value && models[0] && models[0].id) {
                modelSelect.value = String(models[0].id);
            }
            refreshSelectedModelCapabilities();
        }).catch(function (error) {
            status.textContent = 'Modellliste nicht verfügbar: ' + error.message;
        });
    }

    function setBusy(busy, text) {
        sendButton.disabled = busy;
        input.disabled = busy;
        retryButton.disabled = busy;
        cancelEditButton.disabled = busy;
        scopeInputs.forEach(function (node) {
            node.disabled = busy || !scopeAvailable(node);
        });
        if (modelSelect) { modelSelect.disabled = busy; }
        Array.prototype.forEach.call(messageList.querySelectorAll('button'), function (button) {
            button.disabled = busy;
        });
        status.textContent = text || '';
    }

    function setRetryVisible(visible) {
        retryButton.hidden = !visible;
    }

    function apiRequest(path, method, payload) {
        var options = {
            method: method || 'GET',
            credentials: 'same-origin',
            headers: {'requesttoken': OC.requestToken}
        };
        if (payload !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(payload);
        }
        return fetch(OC.generateUrl('/apps/sunaq' + path), options).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok) {
                    throw new Error(data.error || ('HTTP ' + response.status));
                }
                return data;
            });
        });
    }


    function streamFallbackError(message) {
        var error = new Error(message || 'Streaming nicht verfügbar.');
        error.sunaqStreamFallback = true;
        return error;
    }

    function cleanStreamContent(value) {
        var text = String(value || '');
        text = text.replace(/<!--rag-source:\d+:[^>]+-->/g, '');
        text = text.replace(/<!--rag-source:[\s\S]*$/, '');
        return text.replace(/[ \t]+\n/g, '\n');
    }

    function parseSseBlock(block) {
        var eventName = 'message';
        var data = [];
        String(block || '').split('\n').forEach(function (line) {
            if (line.indexOf('event:') === 0) {
                eventName = line.slice(6).trim() || 'message';
            } else if (line.indexOf('data:') === 0) {
                data.push(line.slice(5).replace(/^\s/, ''));
            }
        });
        return {event: eventName, data: data.join('\n')};
    }

    function streamChatRequest(payload, onContent) {
        var options = {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'requesttoken': OC.requestToken,
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream'
            },
            body: JSON.stringify(payload)
        };

        return fetch(OC.generateUrl('/apps/sunaq/chat/stream'), options).then(function (response) {
            if (!response.ok) {
                if (response.status === 404 || response.status === 405) {
                    throw streamFallbackError('Streaming-Endpunkt ist nicht verfügbar.');
                }
                return response.json().catch(function () { return {}; }).then(function (data) {
                    throw new Error(data.error || ('HTTP ' + response.status));
                });
            }
            if (
                !response.body
                || typeof response.body.getReader !== 'function'
                || typeof window.TextDecoder !== 'function'
            ) {
                throw streamFallbackError('Dieser Browser unterstützt Streaming nicht.');
            }

            var reader = response.body.getReader();
            var decoder = new window.TextDecoder('utf-8');
            var buffer = '';
            var rawContent = '';
            var meta = {};
            var streamError = null;

            function handleBlock(block) {
                var frame = parseSseBlock(block);
                if (!frame.data && frame.event === 'message') {
                    return;
                }
                if (frame.event === 'sunaq_meta') {
                    try {
                        meta = JSON.parse(frame.data || '{}') || {};
                    } catch (e) {
                        meta = {};
                    }
                    return;
                }
                if (frame.event === 'sunaq_error') {
                    try {
                        var errorData = JSON.parse(frame.data || '{}') || {};
                        streamError = new Error(errorData.error || 'Streaming fehlgeschlagen.');
                    } catch (e) {
                        streamError = new Error('Streaming fehlgeschlagen.');
                    }
                    return;
                }
                if (frame.event === 'sunaq_done' || frame.data === '[DONE]') {
                    return;
                }

                try {
                    var payloadChunk = JSON.parse(frame.data || '{}');
                    var choice = payloadChunk.choices && payloadChunk.choices[0];
                    var delta = choice && choice.delta ? choice.delta : {};
                    if (typeof delta.content === 'string' && delta.content) {
                        rawContent += delta.content;
                        onContent(cleanStreamContent(rawContent));
                    }
                } catch (e) {
                    // Ignore malformed/non-OpenAI SSE frames. The server-side
                    // metadata event still determines the final saved message.
                }
            }

            function consumeBuffer(finalPass) {
                buffer = buffer.replace(/\r\n/g, '\n');
                var marker;
                while ((marker = buffer.indexOf('\n\n')) !== -1) {
                    var block = buffer.slice(0, marker);
                    buffer = buffer.slice(marker + 2);
                    if (block.trim()) {
                        handleBlock(block);
                    }
                }
                if (finalPass && buffer.trim()) {
                    handleBlock(buffer);
                    buffer = '';
                }
            }

            function pump() {
                return reader.read().then(function (part) {
                    if (part.done) {
                        buffer += decoder.decode();
                        consumeBuffer(true);
                        if (streamError) {
                            throw streamError;
                        }
                        if (!meta.content) {
                            meta.content = cleanStreamContent(rawContent).trim();
                        }
                        return meta;
                    }
                    buffer += decoder.decode(part.value, {stream: true});
                    consumeBuffer(false);
                    if (streamError) {
                        return reader.cancel().then(function () {
                            throw streamError;
                        });
                    }
                    return pump();
                });
            }

            return pump();
        });
    }

    function newRequestId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        var randomPart = '';
        if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
            var values = new Uint32Array(2);
            window.crypto.getRandomValues(values);
            randomPart = values[0].toString(16) + values[1].toString(16);
        } else {
            randomPart = Math.random().toString(36).slice(2);
        }
        return 'req-' + Date.now().toString(36) + '-' + randomPart;
    }

    function newConversationId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        var randomPart = '';
        if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
            var values = new Uint32Array(2);
            window.crypto.getRandomValues(values);
            randomPart = values[0].toString(16) + values[1].toString(16);
        } else {
            randomPart = Math.random().toString(36).slice(2);
        }
        return 'chat-' + Date.now().toString(36) + '-' + randomPart;
    }

    function clearRetrievalDecision() {
        if (retrievalStrategyContent) {
            retrievalStrategyContent.textContent = '';
        }
        if (retrievalStrategy) {
            retrievalStrategy.hidden = true;
            retrievalStrategy.open = false;
        }
    }

    function renderRetrievalDecision(record) {
        if (!retrievalStrategy || !retrievalStrategyContent || !record || typeof record !== 'object') {
            return;
        }
        var planner = record.planner || {};
        var policy = record.policy || {};
        var plan = record.effective_plan || {};
        var lines = [];

        function add(label, value) {
            if (Array.isArray(value)) {
                value = value.join(', ');
            }
            value = String(value || '').trim();
            if (value) {
                lines.push(label + ': ' + value);
            }
        }

        lines.push('Analyse');
        add('Anfrage', record.normalized_query || record.original_query);
        add('Entitäten', planner.entities);
        add('Konzepte', planner.concepts);
        if (Array.isArray(planner.concept_groups) && planner.concept_groups.length) {
            add('Konzeptgruppen', planner.concept_groups.map(function (item) {
                if (!Array.isArray(item) || item.length < 2) {
                    return String(item);
                }
                var required = item[0] ? 'MUSS' : 'OPTIONAL';
                var terms = Array.isArray(item[1]) ? item[1].join(' | ') : String(item[1] || '');
                return required + ': ' + terms;
            }));
        }
        if (Array.isArray(planner.constraints) && planner.constraints.length) {
            add('Constraints', planner.constraints.map(function (item) {
                return Array.isArray(item) ? item.join(' = ') : String(item);
            }));
        }

        lines.push('');
        lines.push('Planner');
        add('Status', planner.status);
        add('Semantic', planner.semantic_query);
        add('Verifikation', planner.verification_requirements);

        lines.push('');
        lines.push('Policy');
        add('Architektur', policy.architecture_tier);
        add('Profil', policy.model_id);
        add('Retriever', policy.retrieval_arms);
        add('Quellen', policy.source_scopes);
        if (policy.web_requested) {
            lines.push('Web angefordert: ja');
            lines.push('Web freigegeben: ' + (policy.web_enabled ? 'ja' : 'nein'));
        }
        add('Resolver angefordert', policy.requested_resolver_sources);
        if (policy.resolver_research_requested) {
            lines.push(
                'Resolver freigegeben: ' +
                (policy.resolver_research_enabled ? 'ja' : 'nein')
            );
        }
        add('Resolver effektiv', policy.resolver_sources);
        add('Entscheidungsgründe', policy.reason_codes);

        lines.push('');
        lines.push('Effektiver Plan');
        add('Retriever', plan.retrieval_arms);
        add('Quellen', plan.source_scopes);
        add('Resolver', plan.resolver_sources);
        add('Runden', plan.max_rounds);
        add('Queries pro Runde', plan.max_queries_per_round);
        add('Verifier-Fenster', plan.verification_candidate_limit);
        add('Max. vollständige Dokumente', plan.max_complete_documents);

        retrievalStrategyContent.textContent = lines.join('\n');
        retrievalStrategy.hidden = false;
    }

    function stopProgress(requestId) {
        if (requestId && activeRequestId !== requestId) {
            return;
        }
        if (progressTimer !== null) {
            window.clearTimeout(progressTimer);
            progressTimer = null;
        }
        if (!requestId || activeRequestId === requestId) {
            activeRequestId = '';
        }
    }

    function scheduleProgressPoll(requestId) {
        if (!requestId || activeRequestId !== requestId) {
            return;
        }
        progressTimer = window.setTimeout(function () {
            if (activeRequestId !== requestId) {
                return;
            }
            apiRequest('/status/' + encodeURIComponent(requestId), 'GET').then(function (data) {
                if (activeRequestId !== requestId) {
                    return;
                }
                var label = String(data.label || '').trim();
                if (label) {
                    status.textContent = label;
                }
                if (data.retrieval_decision) {
                    renderRetrievalDecision(data.retrieval_decision);
                }
                if (String(data.stage || '') === 'complete') {
                    stopProgress(requestId);
                    return;
                }
                scheduleProgressPoll(requestId);
            }).catch(function () {
                // Progress is a best-effort UX channel. A transient polling
                // failure must never fail or cancel the actual research request.
                if (activeRequestId === requestId) {
                    scheduleProgressPoll(requestId);
                }
            });
        }, 800);
    }

    function startProgress(requestId) {
        stopProgress();
        clearRetrievalDecision();
        activeRequestId = requestId;
        status.textContent = 'Anfrage wird vorbereitet …';
        scheduleProgressPoll(requestId);
    }

    function renderChatList(chats) {
        chatList.innerHTML = '';
        if (!Array.isArray(chats) || !chats.length) {
            var empty = document.createElement('span');
            empty.className = 'sunaq-muted';
            empty.textContent = 'Noch keine gespeicherten Recherchen.';
            chatList.appendChild(empty);
            return;
        }
        chats.forEach(function (chat) {
            var row = document.createElement('div');
            row.className = 'sunaq-chat-row' + (chat.id === conversationId ? ' is-active' : '');
            var open = document.createElement('button');
            open.type = 'button';
            open.className = 'sunaq-chat-open';
            open.textContent = chat.title || 'Recherche';
            open.title = chat.updated_at || '';
            open.addEventListener('click', function () { loadChat(chat.id); });
            var tools = document.createElement('span');
            tools.className = 'sunaq-chat-tools';
            var rename = makeSmallButton('✎', 'Recherche umbenennen', function (event) {
                event.stopPropagation();
                var title = window.prompt('Titel der Recherche', chat.title || '');
                if (title && title.trim()) {
                    apiRequest('/chats/' + encodeURIComponent(chat.id) + '/rename', 'POST', {title: title.trim()})
                        .then(loadChatList)
                        .catch(function (error) { status.textContent = error.message; });
                }
            });
            var remove = makeSmallButton('×', 'Recherche löschen', function (event) {
                event.stopPropagation();
                if (!window.confirm('Diese Recherche löschen?')) { return; }
                apiRequest('/chats/' + encodeURIComponent(chat.id), 'DELETE').then(function () {
                    if (conversationId === chat.id) { resetSearch(); }
                    loadChatList();
                }).catch(function (error) { status.textContent = error.message; });
            });
            tools.appendChild(rename);
            tools.appendChild(remove);
            row.appendChild(open);
            row.appendChild(tools);
            chatList.appendChild(row);
        });
    }

    function loadChatList() {
        return apiRequest('/chats', 'GET').then(function (data) {
            renderChatList(data.chats || []);
        }).catch(function (error) {
            chatList.textContent = 'Chatarchiv nicht verfügbar: ' + error.message;
        });
    }

    function loadChat(id) {
        setBusy(true, 'Recherche wird geladen …');
        apiRequest('/chats/' + encodeURIComponent(id), 'GET').then(function (data) {
            var chat = data.chat || {};
            conversationId = chat.id || id;
            messages = Array.isArray(chat.messages) ? chat.messages : [];
            if (Array.isArray(chat.scopes) && chat.scopes.length) {
                applyScopes(chat.scopes);
            }
            editingIndex = null;
            input.value = '';
            sendButton.title = 'Senden';
            sendButton.setAttribute('aria-label', 'Senden');
            cancelEditButton.hidden = true;
            renderConversation();
            setBusy(false, data.archive_warning || '');
            loadChatList();
            input.focus();
        }).catch(function (error) {
            setBusy(false, 'Fehler: ' + error.message);
        });
    }

    function runQueryRequest() {
        if (!conversationId) {
            conversationId = newConversationId();
        }
        var requestId = newRequestId();
        var requestPayload = {
            messages: messages,
            sourceScopes: selectedScopes(),
            conversationId: conversationId,
            model: modelSelect ? modelSelect.value : '',
            requestId: requestId
        };

        setRetryVisible(false);
        setBusy(true, 'Anfrage wird vorbereitet …');
        startProgress(requestId);

        var streamWrapper = addMessage('assistant', '');
        var streamContentNode = streamWrapper.querySelector('.sunaq-content');

        function renderStreamedContent(content) {
            if (!streamContentNode) {
                return;
            }
            streamContentNode.innerHTML = '';
            appendMarkdown(streamContentNode, content);
            messageList.scrollTop = messageList.scrollHeight;
        }

        function finish(data) {
            stopProgress(requestId);
            var answer = data.content || 'Keine Antwort erhalten.';
            var assistant = {
                role: 'assistant',
                content: answer,
                created_at: data.message_created_at || new Date().toISOString(),
                source_scopes: Array.isArray(data.source_scopes) ? data.source_scopes : []
            };
            if (Array.isArray(data.sources) && data.sources.length) {
                assistant.sources = data.sources;
            }
            if (Array.isArray(data.suggestions) && data.suggestions.length) {
                assistant.suggestions = data.suggestions.slice(0, 4);
            }
            messages.push(assistant);
            if (data.conversation && data.conversation.id) {
                conversationId = data.conversation.id;
            }
            renderConversation();
            setBusy(false, data.archive_warning || '');
            loadChatList();
            return data;
        }

        function legacyFallback() {
            if (streamWrapper && streamWrapper.parentNode) {
                streamWrapper.parentNode.removeChild(streamWrapper);
            }
            return apiRequest('/chat', 'POST', requestPayload).then(finish);
        }

        return streamChatRequest(requestPayload, renderStreamedContent).then(finish).catch(function (error) {
            if (error && error.sunaqStreamFallback) {
                return legacyFallback();
            }
            stopProgress(requestId);
            renderConversation();
            addMessage('assistant', 'Fehler: ' + error.message);
            messageList.scrollTop = messageList.scrollHeight;
            setBusy(false, '');
            setRetryVisible(true);
            return null;
        });
    }

    function sendBranch(index, text) {
        var prefix = typeof index === 'number' ? messages.slice(0, index) : messages.slice();
        messages = prefix;
        messages.push({role: 'user', content: text, created_at: new Date().toISOString()});
        editingIndex = null;
        cancelEditButton.hidden = true;
        sendButton.title = 'Senden';
        sendButton.setAttribute('aria-label', 'Senden');
        input.value = '';
        renderConversation();
        return runQueryRequest();
    }

    function submitQuery(text) {
        if (editingIndex !== null) {
            return sendBranch(editingIndex, text);
        }
        return sendBranch(null, text);
    }

    function resendAt(index) {
        if (!messages[index] || messages[index].role !== 'user') {
            return;
        }
        sendBranch(index, messages[index].content);
    }

    function beginEdit(index) {
        if (!messages[index] || messages[index].role !== 'user') {
            return;
        }
        editingIndex = index;
        input.value = messages[index].content;
        sendButton.title = 'Geänderte Anfrage senden';
        sendButton.setAttribute('aria-label', 'Geänderte Anfrage senden');
        cancelEditButton.hidden = false;
        status.textContent = 'Frage wird ab diesem Punkt neu gesendet.';
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
    }

    function cancelEdit() {
        editingIndex = null;
        input.value = '';
        sendButton.title = 'Senden';
        sendButton.setAttribute('aria-label', 'Senden');
        cancelEditButton.hidden = true;
        status.textContent = '';
        input.focus();
    }

    function resetSearch() {
        stopProgress();
        messages = [];
        conversationId = '';
        editingIndex = null;
        input.value = '';
        sendButton.title = 'Senden';
        sendButton.setAttribute('aria-label', 'Senden');
        cancelEditButton.hidden = true;
        setRetryVisible(false);
        renderConversation();
        status.textContent = '';
        clearRetrievalDecision();
        loadChatList();
        input.focus();
    }

    function init() {
        form = document.getElementById('sunaq-form');
        if (!form) {
            return;
        }
        input = document.getElementById('sunaq-input');
        sendButton = document.getElementById('sunaq-send');
        cancelEditButton = document.getElementById('sunaq-cancel-edit');
        status = document.getElementById('sunaq-status');
        retrievalStrategy = document.getElementById('sunaq-retrieval-strategy');
        retrievalStrategyContent = document.getElementById('sunaq-retrieval-strategy-content');
        messageList = document.getElementById('sunaq-messages');
        retryButton = document.getElementById('sunaq-retry');
        chatList = document.getElementById('sunaq-chat-list');
        modelSelect = document.getElementById('sunaq-model');
        if (modelSelect) {
            modelSelect.addEventListener('change', refreshSelectedModelCapabilities);
        }
        sidebar = document.getElementById('sunaq-sidebar');
        sidebarToggle = document.getElementById('sunaq-sidebar-toggle');
        scopeInputs = Array.prototype.slice.call(document.querySelectorAll('input[name="sunaq-scope"]'));

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var text = input.value.trim();
            if (!text || sendButton.disabled) {
                return;
            }
            submitQuery(text);
        });

        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                } else {
                    sendButton.click();
                }
            }
        });

        document.getElementById('sunaq-new').addEventListener('click', resetSearch);
        if (sidebarToggle && sidebar) {
            sidebarToggle.addEventListener('click', function () {
                var collapsed = document.getElementById('sunaq-app').classList.toggle('sunaq-sidebar-collapsed');
                sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            });
        }
        cancelEditButton.addEventListener('click', cancelEdit);
        retryButton.addEventListener('click', function () {
            if (!retryButton.disabled && messages.length && messages[messages.length - 1].role === 'user') {
                runQueryRequest();
            }
        });
        renderConversation();
        loadChatList();
        loadModels();
        input.focus();
    }

    document.addEventListener('DOMContentLoaded', init);
}());
