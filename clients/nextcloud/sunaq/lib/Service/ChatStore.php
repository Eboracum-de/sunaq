<?php
namespace OCA\Sunaq\Service;

use OCP\Files\IRootFolder;
use OCP\IUserSession;
use OCP\IConfig;

class ChatMetadataCorruptionException extends \RuntimeException {}

class ChatStore {
    const FOLDER = 'SunaQ-Chats';
    const LEGACY_DOSSIER_CHAT_FILE = 'chat.md';
    const DOSSIER_META_FILE = '.conversation.json';
    const DOSSIER_RUNS_FOLDER = 'runs';
    const MAX_MESSAGES = 80;

    /** @var IRootFolder */
    private $rootFolder;
    /** @var IUserSession */
    private $userSession;
    /** @var IConfig */
    private $config;

    public function __construct(IRootFolder $rootFolder, IUserSession $userSession, IConfig $config) {
        $this->rootFolder = $rootFolder;
        $this->userSession = $userSession;
        $this->config = $config;
    }

    private function userFolder() {
        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        return $this->rootFolder->getUserFolder($user->getUID());
    }

    private function normalizeArchivePath($path) {
        $value = trim((string)$path);
        $value = trim($value, '/');
        if ($value === '') {
            $value = self::FOLDER;
        }
        if (strlen($value) > 240 || strpos($value, '\\') !== false) {
            throw new \InvalidArgumentException('Ungültiger Chatarchiv-Pfad.');
        }
        $parts = explode('/', $value);
        foreach ($parts as &$part) {
            $part = trim((string)$part);
            if ($part === '' || $part === '.' || $part === '..' || preg_match('/[\x00-\x1F\x7F]/', $part)) {
                throw new \InvalidArgumentException('Ungültiger Chatarchiv-Pfad.');
            }
        }
        unset($part);
        return implode('/', $parts);
    }

    private function archivePath() {
        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $stored = $this->config->getUserValue(
            $user->getUID(),
            'sunaq',
            'chat_archive_path',
            self::FOLDER
        );
        return $this->normalizeArchivePath($stored);
    }

    public function setArchivePath($path) {
        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $value = $this->normalizeArchivePath($path);
        $this->config->setUserValue($user->getUID(), 'sunaq', 'chat_archive_path', $value);
        return $value;
    }

    private function archiveFolder($create = true) {
        $folder = $this->userFolder();
        foreach (explode('/', $this->archivePath()) as $segment) {
            if ($folder->nodeExists($segment)) {
                $next = $folder->get($segment);
                if (!method_exists($next, 'getDirectoryListing')) {
                    throw new \RuntimeException('Chatarchiv-Pfad kollidiert mit einer Datei.');
                }
                $folder = $next;
                continue;
            }
            if (!$create) {
                return null;
            }
            $folder = $folder->newFolder($segment);
        }
        return $folder;
    }

    private function dossierFolder($archiveFolder, $id, $create = true) {
        $name = $this->cleanId($id);
        if ($archiveFolder->nodeExists($name)) {
            $node = $archiveFolder->get($name);
            if (!method_exists($node, 'getDirectoryListing')) {
                throw new \RuntimeException('Chat-Dossier kollidiert mit einer Datei.');
            }
            return $node;
        }
        if (!$create) {
            return null;
        }
        return $archiveFolder->newFolder($name);
    }

    private function ensureRunsFolder($dossier) {
        if ($dossier->nodeExists(self::DOSSIER_RUNS_FOLDER)) {
            $node = $dossier->get(self::DOSSIER_RUNS_FOLDER);
            if (!method_exists($node, 'getDirectoryListing')) {
                throw new \RuntimeException('Research-Run-Pfad kollidiert mit einer Datei.');
            }
            return $node;
        }
        return $dossier->newFolder(self::DOSSIER_RUNS_FOLDER);
    }

    private function recoverDossierMarkdownName($dossier, $id) {
        $matches = [];
        foreach ($dossier->getDirectoryListing() as $node) {
            $name = (string)$node->getName();
            if (substr($name, -3) !== '.md' || !method_exists($node, 'getContent')) {
                continue;
            }
            try {
                $content = (string)$node->getContent();
            } catch (\Exception $e) {
                continue;
            }
            if (strpos($content, "\n- Chat-ID: " . $id . "\n") === false) {
                continue;
            }
            $matches[] = $name;
            if (count($matches) > 1) {
                return '';
            }
        }
        return count($matches) === 1 ? $matches[0] : '';
    }

    private function loadDossierRecord($archiveFolder, $id) {
        $dossier = $this->dossierFolder($archiveFolder, $id, false);
        if ($dossier === null) {
            return null;
        }
        if (!$dossier->nodeExists(self::DOSSIER_META_FILE)) {
            // A server-side research run may intentionally create runs/ before
            // the Nextcloud client receives the answer and writes the readable
            // chat Markdown. Preserve that pre-created dossier so the first
            // client save does not erase resolver/document provenance.
            if (
                $this->recoverDossierMarkdownName($dossier, $id) === ''
                && !$dossier->nodeExists(self::DOSSIER_RUNS_FOLDER)
            ) {
                try {
                    $dossier->delete();
                } catch (\Exception $e) {
                    // Best-effort repair of a truly empty/interrupted dossier.
                }
            }
            return null;
        }
        $raw = $dossier->get(self::DOSSIER_META_FILE)->getContent();
        $record = json_decode((string)$raw, true);
        if (!is_array($record)) {
            throw new ChatMetadataCorruptionException('Gespeichertes Chat-Dossier ist beschädigt.');
        }

        // New dossiers use a descriptive Markdown filename. Existing chat.md
        // anchors remain valid and are migrated on the next successful save.
        $archiveFile = trim((string)($record['archive_file'] ?? ''));
        if ($archiveFile === '' || !$dossier->nodeExists($archiveFile)) {
            if ($dossier->nodeExists(self::LEGACY_DOSSIER_CHAT_FILE)) {
                $archiveFile = self::LEGACY_DOSSIER_CHAT_FILE;
            } else {
                $archiveFile = $this->recoverDossierMarkdownName($dossier, $id);
            }
        }
        if ($archiveFile === '') {
            // The readable Markdown is the lifecycle anchor. Removing it removes
            // the dossier semantics, including resolver/web research traces.
            try {
                $dossier->delete();
            } catch (\Exception $e) {
                // A later cleanup/list pass may retry. Never fabricate a live chat.
            }
            return null;
        }

        $record['archive_dir'] = (string)$id;
        $record['archive_file'] = $archiveFile;
        $record['dossier_path'] = $this->archivePath() . '/' . (string)$id;
        $record['runs_path'] = $record['dossier_path'] . '/' . self::DOSSIER_RUNS_FOLDER;
        $record['archive_path'] = $record['dossier_path'] . '/' . $archiveFile;
        return $record;
    }

    private function cleanId($id) {
        $id = (string)$id;
        if ($id !== '' && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $id)) {
            return $id;
        }
        return bin2hex(random_bytes(12));
    }

    /**
     * Return the stable conversation id used by both the provider request and
     * the Nextcloud dossier. This server-side fallback makes first-turn
     * provenance independent of browser asset caches/older JavaScript.
     */
    public function ensureConversationId($id) {
        return $this->cleanId($id);
    }

    private function metaName($id) {
        return '.' . $id . '.sunaq.json';
    }

    private function legacyMetaName($id) {
        return '.' . $id . '.akirag.json';
    }

    private function legacyHtmlName($id) {
        return $id . '.html';
    }

    private function cleanArchiveTitle($title) {
        $value = preg_replace('/[\\x00-\\x1F\\x7F\\/\\\\:*?"<>|#%]+/u', ' ', (string)$title);
        $value = preg_replace('/\\s+/u', ' ', trim((string)$value));
        if ($value === '') {
            $value = 'Recherche';
        }
        if (function_exists('mb_substr')) {
            return mb_substr($value, 0, 48);
        }
        return substr($value, 0, 48);
    }

    private function markdownName($record) {
        $date = substr((string)($record['created_at'] ?? ''), 0, 10);
        if (!preg_match('/^\\d{4}-\\d{2}-\\d{2}$/', $date)) {
            $date = gmdate('Y-m-d');
        }
        $title = $this->cleanArchiveTitle($record['title'] ?? 'Recherche');
        $shortId = substr((string)$record['id'], 0, 8);
        return $date . ' - ' . $title . ' - ' . $shortId . '.md';
    }

    private function recoverMarkdownName($folder, $id) {
        $shortId = substr((string)$id, 0, 8);
        if ($shortId === '') {
            return '';
        }
        $matches = [];
        $pattern = '/^\\d{4}-\\d{2}-\\d{2} - .+ - ' . preg_quote($shortId, '/') . '\\.md$/u';
        foreach ($folder->getDirectoryListing() as $node) {
            $name = (string)$node->getName();
            if (preg_match($pattern, $name)) {
                $content = (string)$node->getContent();
                if (strpos($content, "\n- Chat-ID: " . $id . "\n") === false) {
                    continue;
                }
                $matches[] = $name;
                if (count($matches) > 1) {
                    return '';
                }
            }
        }
        return count($matches) === 1 ? $matches[0] : '';
    }

    private function writeFile($folder, $name, $content) {
        if ($folder->nodeExists($name)) {
            $file = $folder->get($name);
        } else {
            $file = $folder->newFile($name);
        }
        $file->putContent((string)$content);
        return $file;
    }

    private function resolveArchiveFile($folder, $id, $record) {
        $archiveFile = is_array($record) ? trim((string)($record['archive_file'] ?? '')) : '';
        if ($archiveFile !== '' && $folder->nodeExists($archiveFile)) {
            return $archiveFile;
        }
        $recovered = $this->recoverMarkdownName($folder, $id);
        if ($recovered !== '') {
            return $recovered;
        }
        $legacyHtml = $this->legacyHtmlName($id);
        if ($folder->nodeExists($legacyHtml)) {
            return $legacyHtml;
        }
        return '';
    }

    private function deleteMetadataFiles($folder, $id) {
        foreach ([$this->metaName($id), $this->legacyMetaName($id)] as $name) {
            if ($folder->nodeExists($name)) {
                $folder->get($name)->delete();
            }
        }
    }

    private function normalizeScopes($scopes) {
        $allowed = ['documents', 'mailarchive', 'chatarchive', 'web'];
        $out = [];
        if (!is_array($scopes)) {
            return $out;
        }
        foreach ($scopes as $scope) {
            $value = strtolower(trim((string)$scope));
            // Historic chat metadata may still say webarchive. Persisted Web
            // Evidence now belongs to the Chatarchive lifecycle/source class.
            if ($value === 'webarchive') {
                $value = 'chatarchive';
            }
            if (in_array($value, $allowed, true) && !in_array($value, $out, true)) {
                $out[] = $value;
            }
        }
        return $out;
    }

    private function normalizeMessages($messages) {
        $out = [];
        foreach (array_slice(is_array($messages) ? $messages : [], -self::MAX_MESSAGES) as $message) {
            if (!is_array($message)) {
                continue;
            }
            $role = isset($message['role']) ? (string)$message['role'] : '';
            if ($role !== 'user' && $role !== 'assistant') {
                continue;
            }
            $content = isset($message['content']) ? trim((string)$message['content']) : '';
            if ($content === '') {
                continue;
            }
            $item = ['role' => $role, 'content' => $content];
            $createdAt = isset($message['created_at']) ? trim((string)$message['created_at']) : '';
            if ($createdAt !== '' && strlen($createdAt) <= 64) {
                try {
                    $dt = new \DateTimeImmutable($createdAt);
                    $item['created_at'] = $dt->format(DATE_ATOM);
                } catch (\Exception $e) {
                    // Ignore malformed client timestamps; older chats have none.
                }
            }
            if ($role === 'assistant' && isset($message['source_scopes'])) {
                $sourceScopes = $this->normalizeScopes($message['source_scopes']);
                if ($sourceScopes) {
                    $item['source_scopes'] = $sourceScopes;
                }
            }
            if ($role === 'assistant' && isset($message['sources']) && is_array($message['sources'])) {
                $sources = [];
                foreach ($message['sources'] as $source) {
                    if (is_array($source)) {
                        $index = isset($source['index']) ? (int)$source['index'] : 0;
                        $reference = isset($source['reference']) ? trim((string)$source['reference']) : '';
                    } else {
                        $index = 0;
                        $reference = trim((string)$source);
                    }
                    if ($reference !== '') {
                        $sources[] = ['index' => $index, 'reference' => $reference];
                    }
                }
                if ($sources) {
                    $item['sources'] = $sources;
                }
            }
            $out[] = $item;
        }
        return $out;
    }

    private function defaultTitle($messages) {
        foreach ($messages as $message) {
            if (($message['role'] ?? '') === 'user') {
                $title = preg_replace('/\s+/', ' ', trim((string)$message['content']));
                if (function_exists('mb_substr')) {
                    return mb_substr($title, 0, 80);
                }
                return substr($title, 0, 80);
            }
        }
        return 'Neue Recherche';
    }

    private function renderMarkdown($record) {
        $title = preg_replace('/\\s+/u', ' ', trim((string)($record['title'] ?? 'Recherche')));
        $parts = [
            '# ' . $title,
            '',
            '**SunaQ Recherche**',
            '',
            '- Erstellt: ' . (string)($record['created_at'] ?? ''),
            '- Aktualisiert: ' . (string)($record['updated_at'] ?? ''),
            '- Chat-ID: ' . (string)($record['id'] ?? ''),
            '',
        ];
        $scopeLabels = [
            'documents' => 'Dokumente',
            'mailarchive' => 'Mailarchiv',
            'chatarchive' => 'Chatarchiv',
            'web' => 'Web',
        ];
        foreach ($record['messages'] as $message) {
            $role = ($message['role'] ?? '') === 'user' ? 'Benutzer' : 'SunaQ';
            $parts[] = '## ' . $role;
            $parts[] = '';

            $meta = [];
            $stamp = isset($message['created_at']) ? trim((string)$message['created_at']) : '';
            if ($stamp !== '') {
                $meta[] = $stamp;
            }
            $messageScopes = $this->normalizeScopes($message['source_scopes'] ?? []);
            if ($messageScopes) {
                $labels = array_map(function ($scope) use ($scopeLabels) {
                    return $scopeLabels[$scope] ?? $scope;
                }, $messageScopes);
                $meta[] = 'Quellen: ' . implode(', ', $labels);
            }
            if ($meta) {
                $parts[] = '*' . implode(' · ', $meta) . '*';
                $parts[] = '';
            }

            $parts[] = (string)($message['content'] ?? '');
            $parts[] = '';

            // Technical source handoff IDs stay in the hidden metadata sidecar.
            // The readable Markdown already contains the answer's user-facing
            // source list and must not expose internal files:<id> bookkeeping.
        }
        return rtrim(implode("\n", $parts)) . "\n";
    }

    public function save($id, $messages, $scopes = [], $title = '') {
        $folder = $this->archiveFolder(true);
        $id = $this->cleanId($id);
        $normalized = $this->normalizeMessages($messages);
        $now = gmdate('c');
        $metadataCorrupt = false;
        try {
            $existing = $this->load($id, false);
        } catch (ChatMetadataCorruptionException $e) {
            // Damaged metadata must not block saving a new message. Treat the
            // record as absent; the next save rewrites valid dossier metadata.
            $existing = null;
            $metadataCorrupt = true;
        }
        $record = [
            'id' => $id,
            'title' => trim((string)$title) !== '' ? trim((string)$title) : ($existing['title'] ?? $this->defaultTitle($normalized)),
            'created_at' => $existing['created_at'] ?? $now,
            'updated_at' => $now,
            'scopes' => $this->normalizeScopes($scopes),
            'messages' => $normalized,
            'source_origin' => 'chat_archive',
            'format' => 'markdown',
            'archive_layout' => 'research_dossier_v1',
        ];

        // New chats are one stable per-conversation research dossier. The
        // conversation id is the directory key so the server can address the
        // same dossier without relying on a client-generated title/path.
        $dossier = $this->dossierFolder($folder, $id, true);
        $this->ensureRunsFolder($dossier);
        $previousDossierFile = (
            is_array($existing) && !empty($existing['dossier_path'])
            ? trim((string)($existing['archive_file'] ?? ''))
            : ''
        );
        $chatFile = $this->markdownName($record);
        $markdown = $this->writeFile(
            $dossier,
            $chatFile,
            $this->renderMarkdown($record)
        );

        $record['archive_dir'] = $id;
        $record['archive_file'] = $chatFile;
        $record['dossier_path'] = $this->archivePath() . '/' . $id;
        $record['runs_path'] = $record['dossier_path'] . '/' . self::DOSSIER_RUNS_FOLDER;
        $record['archive_path'] = $record['dossier_path'] . '/' . $chatFile;
        $record['document_id'] = 'files:' . (string)$markdown->getId();

        // Update metadata before deleting a previous dossier filename. The delete
        // listener can then distinguish stale/renamed Markdown from the current
        // lifecycle anchor and will not remove runs during a chat rename.
        $this->writeFile(
            $dossier,
            self::DOSSIER_META_FILE,
            json_encode($record, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT)
        );
        if (
            $previousDossierFile !== ''
            && $previousDossierFile !== $chatFile
            && $dossier->nodeExists($previousDossierFile)
        ) {
            try {
                $dossier->get($previousDossierFile)->delete();
            } catch (\Exception $e) {
                // The new Markdown + metadata are authoritative; stale old name
                // is cleanup only and must not fail the completed chat save.
            }
        }

        // Successful dossier write migrates any legacy flat representation of
        // this conversation. Existing old chats remain readable until saved.
        $legacyArchiveFile = (
            is_array($existing) && empty($existing['dossier_path'])
            ? trim((string)($existing['archive_file'] ?? ''))
            : ''
        );
        foreach ([$this->metaName($id), $this->legacyMetaName($id), $this->legacyHtmlName($id)] as $legacyName) {
            try {
                if ($folder->nodeExists($legacyName)) {
                    $folder->get($legacyName)->delete();
                }
            } catch (\Exception $e) {
                // The dossier is already authoritative; stale/locked legacy
                // artifacts are cleanup only and must not fail the completed chat.
            }
        }
        if (
            $legacyArchiveFile !== ''
            && $legacyArchiveFile !== self::LEGACY_DOSSIER_CHAT_FILE
        ) {
            try {
                if ($folder->nodeExists($legacyArchiveFile)) {
                    $folder->get($legacyArchiveFile)->delete();
                }
            } catch (\Exception $e) {
                // Best-effort migration cleanup; list/load prefer the dossier.
            }
        } elseif ($metadataCorrupt) {
            try {
                $recovered = $this->recoverMarkdownName($folder, $id);
                if ($recovered !== '' && $folder->nodeExists($recovered)) {
                    $folder->get($recovered)->delete();
                }
            } catch (\Exception $e) {
                // Best-effort cleanup only.
            }
        }

        return $record;
    }


    public function load($id, $throwIfMissing = true) {
        $id = $this->cleanId($id);
        $folder = $this->archiveFolder(false);
        if ($folder === null) {
            if ($throwIfMissing) {
                throw new \InvalidArgumentException('Chat nicht gefunden.');
            }
            return null;
        }

        $dossierRecord = $this->loadDossierRecord($folder, $id);
        if (is_array($dossierRecord)) {
            return $dossierRecord;
        }

        // Legacy flat archive compatibility. Old chats are migrated to the dossier layout
        // on their next successful save.
        $metaName = $this->metaName($id);
        if (!$folder->nodeExists($metaName) && $folder->nodeExists($this->legacyMetaName($id))) {
            $metaName = $this->legacyMetaName($id);
        }
        if (!$folder->nodeExists($metaName)) {
            if ($throwIfMissing) {
                throw new \InvalidArgumentException('Chat nicht gefunden.');
            }
            return null;
        }
        $raw = $folder->get($metaName)->getContent();
        $record = json_decode((string)$raw, true);
        if (!is_array($record)) {
            throw new ChatMetadataCorruptionException('Gespeicherter Chat ist beschädigt.');
        }

        // The readable archive file is the lifecycle anchor.  If a user deletes
        // it manually in Nextcloud, stale hidden metadata must not keep the chat
        // alive as an invisible duplicate.
        $archiveFile = $this->resolveArchiveFile($folder, $id, $record);
        if ($archiveFile === '') {
            $this->deleteMetadataFiles($folder, $id);
            if ($throwIfMissing) {
                throw new \InvalidArgumentException('Chat nicht gefunden.');
            }
            return null;
        }
        if (empty($record['archive_file'])) {
            $record['archive_file'] = $archiveFile;
        }
        return $record;
    }


    public function listChats() {
        $folder = $this->archiveFolder(false);
        if ($folder === null) {
            return [];
        }
        $items = [];
        $seen = [];

        // Dossier v1 entries.
        foreach ($folder->getDirectoryListing() as $node) {
            $name = (string)$node->getName();
            if (!preg_match('/^[A-Za-z0-9_-]{8,64}$/', $name) || !method_exists($node, 'getDirectoryListing')) {
                continue;
            }
            try {
                $record = $this->loadDossierRecord($folder, $name);
                if (!is_array($record)) {
                    continue;
                }
                $id = (string)($record['id'] ?? $name);
                $seen[$id] = true;
                $items[] = [
                    'id' => $id,
                    'title' => (string)($record['title'] ?? 'Recherche'),
                    'updated_at' => (string)($record['updated_at'] ?? ''),
                    'created_at' => (string)($record['created_at'] ?? ''),
                    'scopes' => is_array($record['scopes'] ?? null) ? $record['scopes'] : [],
                ];
            } catch (\Exception $e) {
                // One damaged dossier must not hide the rest of the archive.
            }
        }

        // Legacy flat entries remain visible until they are saved/migrated.
        foreach ($folder->getDirectoryListing() as $node) {
            $name = $node->getName();
            if (!preg_match('/^\.([A-Za-z0-9_-]{8,64})\.(?:sunaq|akirag)\.json$/', $name, $match)) {
                continue;
            }
            try {
                $record = json_decode((string)$node->getContent(), true);
                if (is_array($record)) {
                    $id = (string)($record['id'] ?? $match[1]);
                    if (isset($seen[$id])) {
                        continue;
                    }
                    if ($this->resolveArchiveFile($folder, $id, $record) === '') {
                        $this->deleteMetadataFiles($folder, $id);
                        continue;
                    }
                    $items[] = [
                        'id' => $id,
                        'title' => (string)($record['title'] ?? 'Recherche'),
                        'updated_at' => (string)($record['updated_at'] ?? ''),
                        'created_at' => (string)($record['created_at'] ?? ''),
                        'scopes' => is_array($record['scopes'] ?? null) ? $record['scopes'] : [],
                    ];
                }
            } catch (\Exception $e) {
                // One damaged legacy chat must not hide the rest of the archive.
            }
        }
        usort($items, function ($a, $b) {
            return strcmp((string)$b['updated_at'], (string)$a['updated_at']);
        });
        return $items;
    }


    public function rename($id, $title) {
        try {
            $record = $this->load($id);
        } catch (ChatMetadataCorruptionException $e) {
            throw new \InvalidArgumentException('Gespeicherter Chat ist beschädigt und kann nicht umbenannt werden.');
        }
        $title = trim((string)$title);
        if ($title === '') {
            throw new \InvalidArgumentException('Titel darf nicht leer sein.');
        }
        return $this->save($record['id'], $record['messages'], $record['scopes'] ?? [], $title);
    }

    public function delete($id) {
        $id = $this->cleanId($id);
        $folder = $this->archiveFolder(false);
        if ($folder === null) {
            return;
        }

        // Dossier layout: deleting a chat removes chat.md, metadata, and all
        // research runs atomically at the Nextcloud folder boundary.
        $dossier = $this->dossierFolder($folder, $id, false);
        if ($dossier !== null) {
            $dossier->delete();
        }

        // Legacy cleanup for pre-dossier chats and partially migrated records.
        $metadataCorrupt = false;
        $record = null;
        $metaName = $this->metaName($id);
        if (!$folder->nodeExists($metaName) && $folder->nodeExists($this->legacyMetaName($id))) {
            $metaName = $this->legacyMetaName($id);
        }
        if ($folder->nodeExists($metaName)) {
            // Storage/read failures still propagate. Only malformed legacy
            // metadata is tolerated here so a damaged chat remains deletable.
            $raw = $folder->get($metaName)->getContent();
            $record = json_decode((string)$raw, true);
            if (!is_array($record)) {
                $metadataCorrupt = true;
                $record = null;
            }
        }

        $names = [$this->metaName($id), $this->legacyMetaName($id), $this->legacyHtmlName($id)];
        if (is_array($record) && !empty($record['archive_file'])) {
            $names[] = (string)$record['archive_file'];
        } elseif ($metadataCorrupt || is_array($record)) {
            $recoveredArchive = $this->recoverMarkdownName($folder, $id);
            if ($recoveredArchive !== '') {
                $names[] = $recoveredArchive;
            }
        }
        foreach (array_unique($names) as $name) {
            if ($folder->nodeExists($name)) {
                $folder->get($name)->delete();
            }
        }
    }

}
