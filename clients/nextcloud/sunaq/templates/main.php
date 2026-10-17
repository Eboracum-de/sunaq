<?php
script('sunaq', 'app');
style('sunaq', 'style');
?>
<div id="sunaq-app" class="sunaq-workspace">
    <aside id="sunaq-sidebar" class="sunaq-sidebar" aria-label="Rechercheverlauf">
        <div class="sunaq-sidebar-head">
            <strong>Recherchen</strong>
            <button id="sunaq-new" type="button" class="button sunaq-icon-button" title="Neue Recherche" aria-label="Neue Recherche">+</button>
        </div>
        <div id="sunaq-chat-list" class="sunaq-chat-list">
            <span class="sunaq-muted">Noch keine gespeicherten Recherchen.</span>
        </div>
    </aside>

    <section class="sunaq-shell">
        <header class="sunaq-header">
            <button id="sunaq-sidebar-toggle" type="button" class="button sunaq-icon-button sunaq-sidebar-toggle"
                    title="Rechercheverlauf ein-/ausblenden" aria-label="Rechercheverlauf ein-/ausblenden"
                    aria-controls="sunaq-sidebar" aria-expanded="true">☰</button>
            <div class="sunaq-header-copy">
                <h2>SunaQ</h2>
                <p>Recherche in Ihren freigegebenen Quellen · <code>/help</code> zeigt Befehle und Direktiven.</p>
                <p class="sunaq-context-hint">Chats sind kontextsensitiv; für ein unabhängiges Thema nutzen Sie bitte einen neuen Chat.</p>
            </div>
        </header>

        <main id="sunaq-messages" class="sunaq-messages" aria-live="polite">
            <div class="sunaq-message sunaq-assistant">
                <div class="sunaq-role">SunaQ</div>
                <div class="sunaq-content">Was möchten Sie finden?</div>
            </div>
        </main>

        <div id="sunaq-status" class="sunaq-status" aria-live="polite"></div>
        <details id="sunaq-retrieval-strategy" class="sunaq-retrieval-strategy" hidden>
            <summary>Suchstrategie</summary>
            <pre id="sunaq-retrieval-strategy-content"></pre>
        </details>

        <form id="sunaq-form" class="sunaq-composer">
            <div class="sunaq-composer-box">
                <label class="hidden-visually" for="sunaq-input">Suchanfrage</label>
                <textarea id="sunaq-input" rows="3" placeholder="Frage oder Suchanfrage eingeben …" required></textarea>

                <div class="sunaq-composer-toolbar">
                    <div class="sunaq-composer-options">
                        <label class="sunaq-model-control" title="Rechercheprofil">
                            <span class="hidden-visually">SunaQ-Modell</span>
                            <select id="sunaq-model" name="sunaq-model" aria-label="SunaQ-Modell">
                                <option value="">Schnell</option>
                            </select>
                        </label>

                        <div class="sunaq-scopes" aria-label="Quellen">
                            <label class="sunaq-scope-chip"><input type="checkbox" name="sunaq-scope" value="documents" checked><span>Dokumente</span></label>
                            <label class="sunaq-scope-chip" hidden><input type="checkbox" name="sunaq-scope" value="mailarchive"><span>Mail</span></label>
                            <label class="sunaq-scope-chip" hidden><input type="checkbox" name="sunaq-scope" value="chatarchive"><span>Chats</span></label>
                            <label class="sunaq-scope-chip" hidden><input type="checkbox" name="sunaq-scope" value="web"><span>Web</span></label>
                        </div>
                    </div>

                    <div class="sunaq-action-buttons">
                        <button id="sunaq-cancel-edit" type="button" class="button sunaq-quiet-action" hidden>Abbrechen</button>
                        <button id="sunaq-retry" type="button" class="button sunaq-quiet-action" hidden>Wiederholen</button>
                        <button id="sunaq-send" type="submit" class="primary sunaq-send-button" title="Senden" aria-label="Senden">↑</button>
                    </div>
                </div>
            </div>
            <p class="sunaq-composer-hint">Enter sendet · Shift+Enter fügt eine neue Zeile ein · Slash-Directives übersteuern die Quellenauswahl.</p>
        </form>
    </section>
</div>
