from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "clients" / "nextcloud" / "sunaq"


def test_sunaq_client_is_packaged_for_nextcloud_23_plus():
    root = ET.parse(APP / "appinfo" / "info.xml").getroot()
    assert root.findtext("version") == "0.3.16"
    dependency = root.find("./dependencies/nextcloud")
    assert dependency is not None
    assert dependency.attrib.get("min-version") == "23"


def test_sunaq_client_has_safe_markdown_and_branch_controls():
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    assert "appendMarkdown" in js
    assert "textContent" in js
    assert "innerHTML = content" not in js
    assert "Erneut senden" in js
    assert "Bearbeiten & erneut senden" in js
    assert "messages.slice(0, index)" in js


def test_sunaq_client_keeps_api_key_server_side_and_sends_scoped_user_header():
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    assert "api_key_encrypted" in proxy
    assert "X-RAG-User-ID" in proxy
    assert "X-RAG-User-Groups" in proxy
    assert "IGroupManager" in proxy
    assert "getUserGroups" in proxy
    assert "X-RAG-Web-Allowed" in proxy


def test_sunaq_client_registers_navigation_and_custom_icon():
    root = ET.parse(APP / "appinfo" / "info.xml").getroot()
    nav = root.find("./navigations/navigation")
    assert nav is not None
    assert nav.findtext("route") == "sunaq.page.index"
    assert nav.findtext("icon") == "app.svg"
    icon = (APP / "img" / "app.svg").read_text(encoding="utf-8")
    assert "<svg" in icon and "viewBox" in icon


def test_sunaq_024_persists_timestamps_and_renders_tables():
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    main = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    assert "table" in js.lower()
    assert "timestamp" in js.lower()
    assert "/help" in main
    assert "SunaQ-Chats" in store
    assert "chat_archive_path" in store
    assert "setArchivePath" in store
    assert "LEGACY_FOLDER" not in store


def test_sunaq_chat_routes_use_nextcloud23_compatible_noadmin_docblocks():
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")

    # Nextcloud 23's ControllerMethodReflector matches annotations only on
    # dedicated docblock lines of the form " * @NoAdminRequired".
    for method in ("send", "stream", "models", "status", "listChats", "load", "rename", "delete"):
        marker = f"public function {method}"
        method_pos = controller.index(marker)
        doc_start = controller.rfind("/**", 0, method_pos)
        doc_end = controller.find("*/", doc_start, method_pos)
        assert doc_start >= 0
        assert doc_end >= 0
        docblock = controller[doc_start:doc_end + 2]
        assert "\n     * @NoAdminRequired\n" in docblock

    assert "/** @NoAdminRequired */" not in controller



def test_sunaq_025_marks_context_boundary_and_persists_effective_source_scopes():
    main = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")

    assert "Chats sind kontextsensitiv" in main
    assert "nutzen Sie bitte einen neuen Chat" in main
    assert "source_scopes" in proxy
    assert "preg_match_all" in proxy
    assert "explicit user source selection wins over UI state" in proxy
    assert "direct/special commands are not source-scoped" in proxy
    assert "source_scopes" in controller
    assert "source_scopes" in store
    assert "Quellen:" in js
    assert "sunaq-message-scopes" in js


def test_sunaq_026_archive_registration_is_bounded_and_damaged_chats_remain_deletable():
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    readme = (APP / "README.md").read_text(encoding="utf-8")

    assert "'timeout' => 3" in proxy
    assert "'connect_timeout' => 2" in proxy
    assert "'X-RAG-User-ID' => $uid" in proxy
    assert "Damaged metadata must not block saving a new message" in store
    assert "class ChatMetadataCorruptionException extends \\RuntimeException" in store
    assert "throw new ChatMetadataCorruptionException" in store
    assert store.count("catch (ChatMetadataCorruptionException $e)") >= 2
    assert "recoverMarkdownName" in store
    assert "count($matches) > 1" in store
    assert 'strpos($content, "\\n- Chat-ID: " . $id . "\\n")' in store
    assert store.count("$this->recoverMarkdownName($folder, $id)") >= 2
    assert "archive_layout' => 'research_dossier_v1'" in store
    assert "$this->dossierFolder($folder, $id, true)" in store
    assert "elseif ($metadataCorrupt || is_array($record))" in store
    assert "Storage/read failures still propagate." in store
    assert "Gespeicherter Chat ist beschädigt und kann nicht umbenannt werden." in store
    assert "$item['sources'] = $sources;" in store
    assert "### Quellen" not in store
    assert readme.startswith("# SunaQ Recherche 0.3.16")
    assert "persistent per-user chat history as Markdown plus metadata" in readme
    assert "Legacy-HTML" in readme


def test_sunaq_033_filters_source_chips_from_authenticated_server_capabilities():
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    main = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    css = (APP / "css" / "style.css").read_text(encoding="utf-8")

    assert "'source_capabilities'" in controller
    assert "$models['user_settings'] = $settings;" in controller
    assert "$baseUrl . '/v1/user-settings'" in proxy
    assert "applySourceCapabilities" in js
    assert "data-sunaq-available" in js
    assert "labelNode.hidden = !available" in js
    assert "labelNode.style.display = available ? '' : 'none'" in js
    assert 'value="mailarchive"' in main and 'class="sunaq-scope-chip" hidden' in main
    assert ".sunaq-scope-chip[hidden]" in css
    assert "display: none !important" in css
    assert "Administrativ nicht verfügbare Quellen ausgeblendet" not in js
    assert "node.disabled = busy || !scopeAvailable(node)" in js


def test_sunaq_markdown_strong_weight_is_browser_independent():
    css = (APP / "css" / "style.css").read_text(encoding="utf-8")
    assert ".sunaq-content strong" in css
    assert "font-weight: 700" in css


def test_sunaq_client_exposes_user_allowed_sunaq_models_and_sends_selection():
    routes = (APP / "appinfo" / "routes.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    main = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")

    assert "chat#models" in routes
    assert "'/models'" in routes
    assert "public function models()" in controller
    assert "public function send($messages = [], $sourceScopes = [], $conversationId = '', $model = '', $requestId = '')" in controller
    assert "$this->proxy->chat($messages, $sourceScopes, $conversationId, $model, $requestId)" in controller

    assert "$baseUrl . '/v1/models'" in proxy
    assert "'X-RAG-User-ID' => $uid" in proxy
    assert "public function chat(array $messages, array $sourceScopes = [], $conversationId = '', $modelId = '', $requestId = '')" in proxy
    assert "$payload['model'] = $modelId" in proxy

    assert 'id="sunaq-model"' in main
    assert "apiRequest('/models', 'GET')" in js
    assert "modelSelect = document.getElementById('sunaq-model')" in js
    assert "model: modelSelect ? modelSelect.value : ''" in js


def test_sunaq_client_polls_identity_scoped_pipeline_progress():
    routes = (APP / "appinfo" / "routes.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")

    assert "chat#status" in routes
    assert "/status/{requestId}" in routes
    assert "public function status($requestId = '')" in controller
    assert "$this->proxy->status($requestId)" in controller
    assert "public function chat(array $messages, array $sourceScopes = [], $conversationId = '', $modelId = '', $requestId = '')" in proxy
    assert "$headers['X-RAG-Request-ID'] = $requestId" in proxy
    assert "$baseUrl . '/v1/status/' . rawurlencode($requestId)" in proxy
    assert "'X-RAG-User-ID' => $uid" in proxy

    assert "function newRequestId()" in js
    assert "function startProgress(requestId)" in js
    assert "function scheduleProgressPoll(requestId)" in js
    assert "activeRequestId !== requestId" in js
    assert "requestId: requestId" in js
    assert "apiRequest('/status/' + encodeURIComponent(requestId), 'GET')" in js
    assert "window.setTimeout" in js
    assert "800" in js



def test_sunaq_app_id_migration_reads_legacy_akirag_configuration():
    info = (APP / "appinfo" / "info.xml").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    settings = (APP / "lib" / "Settings" / "Admin.php").read_text(encoding="utf-8")
    admin = (APP / "lib" / "Controller" / "AdminController.php").read_text(encoding="utf-8")

    assert "<id>sunaq</id>" in info
    assert "<namespace>Sunaq</namespace>" in info
    assert "OCA\\Sunaq" in proxy
    assert "getAppValue('akirag'" in proxy
    assert "getAppValue('akirag'" in settings
    assert "getAppValue('akirag'" in admin


def test_sunaq_chat_store_uses_one_configurable_archive_and_accepts_legacy_sidecars():
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    assert "'.sunaq.json'" in store
    assert "'.akirag.json'" in store
    assert "FOLDER = 'SunaQ-Chats'" in store
    assert "LEGACY_FOLDER" not in store
    assert "chat_archive_path" in store
    assert "setArchivePath" in store
    assert "$baseUrl . '/v1/user-settings'" in proxy
    assert "syncArchivePath" in controller


def test_sunaq_client_requires_https_for_provider_credentials_unless_admin_opts_in():
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    admin = (APP / "lib" / "Controller" / "AdminController.php").read_text(encoding="utf-8")
    template = (APP / "templates" / "admin.php").read_text(encoding="utf-8")
    js = (APP / "js" / "admin.js").read_text(encoding="utf-8")

    assert "assertCredentialTransport" in proxy
    assert "allow_insecure_http" in proxy
    assert "scheme === 'https'" in proxy
    assert "credentialRedirectProtocols" not in proxy
    assert proxy.count("'allow_redirects' => false") >= 5
    assert "userSettingsBackoffActive" in proxy
    assert "user_settings_sync_failed_at" in proxy
    assert "allowInsecureHttp" in admin
    assert "HTTP würde den Provider-API-Key unverschlüsselt übertragen" in admin
    assert "sunaq-allow-insecure-http" in template
    assert "allowInsecureHttp" in js


def test_sunaq_032_obeys_server_chat_archive_policy_and_cleans_manual_deletes():
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    application = (APP / "lib" / "AppInfo" / "Application.php").read_text(encoding="utf-8")
    listener = (APP / "lib" / "Listener" / "ChatArchiveDeleteListener.php").read_text(encoding="utf-8")

    assert "chat_archive_enabled" in controller
    assert "syncArchiveSettings" in controller
    assert "if (!empty($archiveSettings['enabled']))" in controller
    assert "fail closed for new archive writes" in controller
    rename_block = controller[
        controller.index("public function rename"):
        controller.index("public function delete")
    ]
    assert "$settings = $this->syncArchiveSettings();" in rename_block
    assert "empty($settings['enabled'])" in rename_block

    assert "BeforeNodeDeletedEvent::class" in application
    assert "ChatArchiveDeleteListener::class" in application
    assert "$event->getNode()" in listener
    assert "'.' . $id . '.sunaq.json'" in listener
    assert "'.' . $id . '.akirag.json'" in listener
    assert "Never block the user's requested file deletion" in listener

    assert "resolveArchiveFile" in store
    assert "deleteMetadataFiles" in store
    assert "stale hidden metadata must not keep the chat" in store


def test_sunaq_client_forwards_stable_conversation_id():
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")

    assert "$headers['X-RAG-Conversation-ID'] = $conversationId" in proxy
    assert "$this->proxy->chat($messages, $sourceScopes, $conversationId, $model, $requestId)" in controller
    assert "function newConversationId()" in js
    assert "if (!conversationId)" in js
    assert "conversationId = newConversationId();" in js


def test_sunaq_chat_archive_uses_stable_research_dossier_layout():
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    listener = (APP / "lib" / "Listener" / "ChatArchiveDeleteListener.php").read_text(encoding="utf-8")

    assert "LEGACY_DOSSIER_CHAT_FILE = 'chat.md'" in store
    assert "DOSSIER_META_FILE = '.conversation.json'" in store
    assert "DOSSIER_RUNS_FOLDER = 'runs'" in store
    assert "archive_layout' => 'research_dossier_v1'" in store
    assert "$this->dossierFolder($folder, $id, true)" in store
    assert "$this->ensureRunsFolder($dossier)" in store
    assert "$chatFile = $this->markdownName($record)" in store
    assert "$record['archive_file'] = $chatFile" in store
    assert "$record['dossier_path'] = $this->archivePath() . '/' . $id" in store
    assert "$record['runs_path'] = $record['dossier_path'] . '/' . self::DOSSIER_RUNS_FOLDER" in store
    assert "$record['archive_path'] = $record['dossier_path'] . '/' . $chatFile" in store
    assert "Existing chat.md" in store
    assert "Legacy flat archive compatibility" in store
    assert "migrated to the dossier layout" in store

    assert "$parent->nodeExists('.conversation.json')" in listener
    assert "$archiveFile === $name" in listener
    assert "(string)$parent->getName() === $id" in listener
    assert "['.conversation.json', 'runs']" in listener
    assert "lifecycle anchor" in listener


def test_sunaq_dossier_preserves_server_run_before_first_client_save():
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")

    assert "A server-side research run may intentionally create runs/ before" in store
    assert "if (!$dossier->nodeExists(self::DOSSIER_META_FILE))" in store
    assert "$this->recoverDossierMarkdownName($dossier, $id) === ''" in store
    assert "!$dossier->nodeExists(self::DOSSIER_RUNS_FOLDER)" in store


def test_sunaq_server_guarantees_first_turn_conversation_id():
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    store = (APP / "lib" / "Service" / "ChatStore.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")

    assert "public function ensureConversationId($id)" in store
    ensure = "$conversationId = $this->store->ensureConversationId($conversationId);"
    handoff = "$this->proxy->chat($messages, $sourceScopes, $conversationId, $model, $requestId)"
    assert ensure in controller
    assert handoff in controller
    assert controller.index(ensure) < controller.index(handoff)
    assert "$headers['X-RAG-Conversation-ID'] = $conversationId;" in proxy

def test_sunaq_markdown_tables_wrap_long_cell_content():
    css = (APP / "css" / "style.css").read_text(encoding="utf-8")
    assert ".sunaq-table" in css
    assert "table-layout: fixed" in css
    assert "white-space: normal" in css
    assert "overflow-wrap: anywhere" in css
    assert "hyphens: auto" in css

def test_sunaq_source_picker_has_no_separate_webarchive_category():
    main = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    assert 'value="webarchive"' not in main
    assert "webarchive: 'Webarchiv'" not in js
    for scope in ("documents", "mailarchive", "chatarchive", "web"):
        assert f'value="{scope}"' in main



def test_sunaq_0315_streams_only_bundled_nextcloud_client_and_keeps_json_fallback():
    routes = (APP / "appinfo" / "routes.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "ChatController.php").read_text(encoding="utf-8")
    proxy = (APP / "lib" / "Service" / "RagProxy.php").read_text(encoding="utf-8")
    response = (APP / "lib" / "Http" / "SseResponse.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    readme = (APP / "README.md").read_text(encoding="utf-8")

    assert "chat#stream" in routes
    assert "'/chat/stream'" in routes
    assert "public function stream(" in controller
    assert "$this->proxy->streamChat(" in controller
    assert "public function send(" in controller
    assert "$this->proxy->chat(" in controller

    assert "'stream' => false" in proxy
    assert "public function streamChat(" in proxy
    assert "'stream' => true" in proxy
    assert "'Accept: text/event-stream'" in proxy
    assert "CURLOPT_WRITEFUNCTION" in proxy
    assert "CURLOPT_FOLLOWLOCATION => false" in proxy
    assert "CURLOPT_CAINFO" in proxy
    assert "getAbsoluteBundlePath" in proxy
    assert "CURLOPT_PROXY" in proxy
    assert "CURLOPT_NOPROXY" in proxy
    assert "$this->status($requestId)" in proxy

    assert "implements ICallbackResponse" in response
    assert "text/event-stream; charset=utf-8" in response
    assert "X-Accel-Buffering" in response
    assert "ob_flush" in response

    assert "function streamChatRequest(" in js
    assert "response.body.getReader" in js
    assert "window.TextDecoder" in js
    assert "appendMarkdown(streamContentNode, content)" in js
    assert "sunaq_meta" in js
    assert "sunaqStreamFallback" in js
    assert "apiRequest('/chat', 'POST', requestPayload)" in js

    assert "OpenWebUI and other OpenAI-compatible clients" in readme
    assert "legacy JSON `/chat` path remains available" in readme


def test_sunaq_0316_registers_filename_action_across_nextcloud_files_generations():
    routes = (APP / "appinfo" / "routes.php").read_text(encoding="utf-8")
    application = (APP / "lib" / "AppInfo" / "Application.php").read_text(encoding="utf-8")
    listener = (APP / "lib" / "Listener" / "FilesLoadAdditionalScriptsListener.php").read_text(encoding="utf-8")
    controller = (APP / "lib" / "Controller" / "FileActionController.php").read_text(encoding="utf-8")
    legacy = (APP / "js" / "files-actions.js").read_text(encoding="utf-8")
    modern = (APP / "js" / "files-actions-modern.js").read_text(encoding="utf-8")

    assert "fileAction#suggestName" in routes
    assert "'/files/suggest-name'" in routes
    assert "fileAction#rename" in routes
    assert "'/files/rename'" in routes

    assert "LoadAdditionalScriptsEvent::class" in application
    assert "FilesLoadAdditionalScriptsListener::class" in application
    assert "$major > 0 && $major < 26" in application
    assert "loadAdditionalScripts" in application
    assert "method_exists(Util::class, 'addInitScript')" in listener
    assert "Util::addInitScript('sunaq', 'files-actions-modern')" in listener
    assert "Util::addScript('sunaq', 'files-actions-modern')" in listener

    assert "/use:" in controller
    assert "files:" in controller
    assert "normalizeFileId" in controller
    assert "preg_match('/^[0-9]{1,24}$/'" in controller
    assert "$this->rootFolder->getUserFolder" in controller
    assert "(string)$node->getId() !== $fileId" in controller
    assert "$node->move($targetPath)" in controller
    assert "normalizeRenameCandidate" in controller

    assert "OC.Plugins.register('OCA.Files.FileList', plugin)" in legacy
    assert "window._nc_fileactions" in modern
    assert "window._nc_files_scope.v4_0" in modern
    assert "String(node.id)" in modern
    assert "postJson('/files/suggest-name'" in modern
    assert "postJson('/files/rename'" in modern
    assert "typeof node.rename === 'function'" in modern


def test_sunaq_file_action_controller_keeps_noadmin_annotations_nc23_compatible():
    controller = (APP / "lib" / "Controller" / "FileActionController.php").read_text(encoding="utf-8")
    for method in ("suggestName", "rename"):
        marker = f"public function {method}"
        method_pos = controller.index(marker)
        doc_start = controller.rfind("/**", 0, method_pos)
        doc_end = controller.find("*/", doc_start, method_pos)
        assert doc_start >= 0
        assert doc_end >= 0
        docblock = controller[doc_start:doc_end + 2]
        assert "\n     * @NoAdminRequired\n" in docblock
