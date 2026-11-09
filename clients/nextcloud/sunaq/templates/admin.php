<?php
script('sunaq', 'admin');
style('sunaq', 'admin');
?>
<div id="sunaq-admin" class="section">
    <h2>SunaQ Recherche</h2>
    <p>Verbindung zum SunaQ-Server. Der API-Key wird mit dem Nextcloud-Server-Secret verschlüsselt gespeichert.</p>

    <div id="sunaq-connection-status" class="sunaq-admin-card">
        <div class="sunaq-admin-card-main">
            <strong>Serverstatus</strong>
            <div id="sunaq-connection-summary" class="sunaq-admin-muted">Noch nicht geprüft.</div>
            <div id="sunaq-connection-details" class="sunaq-admin-details"></div>
            <dl id="sunaq-runtime-info" class="sunaq-runtime-info" hidden>
                <div><dt>Status</dt><dd id="sunaq-runtime-status">–</dd></div>
                <div><dt>Version</dt><dd id="sunaq-runtime-version">–</dd></div>
                <div><dt>Architektur</dt><dd id="sunaq-runtime-architecture">–</dd></div>
                <div><dt>Modelle</dt><dd id="sunaq-runtime-models">–</dd></div>
                <div><dt>Quellen</dt><dd id="sunaq-runtime-sources">–</dd></div>
            </dl>
        </div>
        <div class="sunaq-admin-actions">
            <button id="sunaq-test-connection" type="button" class="button">Verbindung prüfen</button>
            <a id="sunaq-server-admin-link" class="button" href="#" target="_blank" rel="noopener noreferrer" hidden>Server-Administration</a>
        </div>
    </div>

    <p>
        <label for="sunaq-middleware-url">SunaQ-URL</label><br>
        <input id="sunaq-middleware-url" type="url" class="long" value="<?php p($_['middleware_url']); ?>" placeholder="https://sunaq.example.org">
        <br><em>Basis-URL ohne <code>/v1</code>.</em>
    </p>

    <p>
        <label><input id="sunaq-allow-insecure-http" type="checkbox" <?php if (!empty($_['allow_insecure_http'])) { print_unescaped('checked'); } ?>> Unsicheres HTTP ausdrücklich erlauben</label>
        <br><em>Nur für kontrollierte Test-/Labornetze. Bei HTTP werden Provider-Key und Anfragen unverschlüsselt übertragen.</em>
    </p>

    <p>
        <label for="sunaq-api-key">API-Key</label><br>
        <input id="sunaq-api-key" type="password" class="long" value="" autocomplete="new-password" placeholder="<?php p($_['has_api_key'] ? 'API-Key ist gespeichert – leer lassen zum Beibehalten' : 'Provider-Client API-Key'); ?>">
    </p>

    <h3>Anzeige und Diagnose</h3>
    <p>
        <label>
            <input id="sunaq-analysis-mode" type="checkbox" <?php if (!empty($_['analysis_mode'])) { print_unescaped('checked'); } ?>>
            Analysemodus anzeigen
        </label><br>
        <em>Zeigt im SunaQ-Client die vom Server freigegebenen Pipeline-, Retrieval- und Laufzeitinformationen. Für den Normalbetrieb kann dies deaktiviert bleiben.</em>
    </p>
    <p>
        <label>
            <input id="sunaq-show-search-strategy" type="checkbox" <?php if (!empty($_['show_search_strategy'])) { print_unescaped('checked'); } ?>>
            Suchstrategie anzeigen
        </label><br>
        <em>Zeigt die kompakte, bereinigte Retrieval-Entscheidung unterhalb der Anfrage.</em>
    </p>

    <p>
        <button id="sunaq-save" class="primary">Speichern</button>
        <span id="sunaq-admin-status" class="sunaq-admin-status"></span>
    </p>
</div>
