# SunaQ Recherche 0.3.16

Schlankes Nextcloud-23+-Frontend für SunaQ / Eboracum Research Gateway 0.8.6.

## Current behaviour

- server-side proxy with the current Nextcloud UID (`X-RAG-User-ID`); the provider key never reaches browser JavaScript;
- authenticated model discovery through `/v1/models`; the selector exposes only profiles allowed for the current user;
- compact chat composer with model selection, source chips and Enter-to-send;
- end-to-end SSE streaming for the bundled browser client: the answer is rendered incrementally while the legacy JSON `/chat` path remains available as a compatibility fallback;
- streaming is a Nextcloud-client transport choice only; OpenWebUI and other OpenAI-compatible clients continue to choose their own `stream` request value;
- collapsible per-user research sidebar;
- safe framework-free Markdown subset (no raw HTML);
- request progress polling through the identity-scoped SunaQ status endpoint;
- verifier progress can report the current document batch, for example
  `Prüfe Dokumente 11–15 von 48 …`;
- per user question: **Erneut senden** and **Bearbeiten & erneut senden**;
- provider follow-up actions are rendered as compact buttons below the current
  answer, for example **Retry with Deep** (`Mit Tief erneut suchen`) or **Refine query** (`Anfrage präzisieren`);
- a stronger-model suggestion is shown only when a strictly stronger profile is
  currently allowed for that user;
- visible context-boundary hint: unrelated topics should use a new chat;
- effective source scopes are shown next to each SunaQ answer;
- source chips are filtered by authenticated server capabilities. Optional
  sources that are globally disabled or not enabled for the current user are not
  shown in the UI and are also rejected server-side when requested explicitly;
- persistent per-user chat history as Markdown plus metadata when both the global
  and per-user chat archive capability are enabled below one
  administrator-selected Nextcloud path (default: `SunaQ-Chats/`);
- legacy `.akirag.json` / Legacy-HTML records remain readable when they are
  inside the selected archive path. SunaQ does not scan `SunaQ-Chats/` and `AKI-Chats/`
  in parallel. For an older RC archive, either configure that user's target as
  `AKI-Chats` or move the files once to the selected SunaQ path.

The model list is loaded when the app starts. If an administrator changes a
user's model entitlement while an existing browser tab remains open, reload the
app to refresh the selector.

Follow-up suggestions are deliberately transient UI actions. They are returned
with the live provider response but are not persisted in the archived chat,
because a later session may have different model entitlements or retrieval
state.

## Source selection

The provider's client-neutral implicit default is **Documents only**.

The SunaQ app intentionally starts with **Documents only** selected. Other
OpenAI-compatible clients that send no source directive use the same provider
default. Mail/Web/chat scopes remain explicit choices, and administrator
capability gates remain authoritative.

Explicit directives in the user request override the UI selection:

- `/documents`
- `/mailarchive`
- `/webarchive`
- `/chatarchive`
- `/web`

## Chat branching

When a user edits an earlier question, the conversation is branched at that
question. The old question and its later answers are not sent together with the
edited question.

## Installation

Copy the `sunaq` app directory into the Nextcloud `apps/` tree and enable it:

```bash
sudo -u <web-user> php occ app:enable sunaq
```

Configure **SunaQ URL** and **Provider API key** under **Settings →
Administration → Additional settings**. The app registers a normal Nextcloud
navigation entry and ships its own `img/app.svg`.

For an intentionally internal SunaQ URL, Nextcloud may require
`allow_local_remote_servers => true`. The Nextcloud host must trust the TLS CA
used by the middleware endpoint. Credential-bearing requests require HTTPS by
default. Plain HTTP is available only through the explicit **Unsicheres HTTP**
administrator opt-in for controlled lab/test networks. Users still complete the middleware's one-time
Nextcloud Login Flow; the resulting app credential is reusable for live ACL and
optional Kontakt-DB synchronization.

## Compatibility history

- 0.2.1: Nextcloud navigation registration and app icon.
- 0.2.3: safe Markdown tables, timestamps, persistent saved-chat sidebar and
  `/help` hint.
- 0.2.4: Nextcloud-23-compatible non-admin chat-history endpoints.
- 0.2.5: context-boundary hint and persisted/effective source scopes.
- 0.2.6: readable Markdown chat archives with stable provenance.
- 0.3.0: SunaQ app id/name, model selector, request progress, source chips and
  provider follow-up actions.
- 0.3.1: per-user single chat-archive path and HTTPS-by-default provider
  credential transport with an explicit lab-only HTTP opt-in.
- 0.3.2: server-controlled chat persistence plus lifecycle cleanup for manually
  deleted managed chat Markdown files.
- 0.3.3: authenticated source-capability filtering and a per-user chat-archive
  enable/disable switch layered below the global administrator gate.
- 0.3.4: optional source chips are hidden by default and shown only after an
  authenticated capability check confirms that the source is available.
- 0.3.7: inspectable pre-retrieval search strategy via the identity-scoped
  request-status channel; fixes client request-ID correlation.
- 0.3.8: retrieval strategy distinguishes requested resolver capabilities from
  the effective administrator-granted resolver plan.
- 0.3.9: strategy inspection also records planner-not-run/planner-failed states
  and shows the effective retriever/source plan after hard policy gates.

- 0.3.15: adds an NC23-compatible SSE proxy and incremental browser rendering; keeps the existing JSON chat endpoint as fallback and preserves provider follow-up actions through the request-status channel.
- 0.3.16: adds the SunaQ filename-suggestion action to Nextcloud Files, keeping the NC23-25 legacy adapter and adding separate current Files adapters for NC26-32 and NC33+; rename remains user-confirmed and is executed through Nextcloud's Node API.
