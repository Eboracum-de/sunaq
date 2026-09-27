<?php
namespace OCA\Sunaq\Service;

use OCP\Http\Client\IClientService;
use OCP\IConfig;
use OCP\IGroupManager;
use OCP\IUserSession;
use OCP\Security\ICrypto;

class RagProxy {
    const MODEL_ID = 'sunaq-standard';
    const MAX_MESSAGES = 30;
    const MAX_MESSAGE_CHARS = 30000;

    /** @var IClientService */
    private $clientService;

    /** @var IConfig */
    private $config;

    /** @var ICrypto */
    private $crypto;

    /** @var IUserSession */
    private $userSession;

    /** @var IGroupManager */
    private $groupManager;

    public function __construct(
        IClientService $clientService,
        IConfig $config,
        ICrypto $crypto,
        IUserSession $userSession,
        IGroupManager $groupManager
    ) {
        $this->clientService = $clientService;
        $this->config = $config;
        $this->crypto = $crypto;
        $this->userSession = $userSession;
        $this->groupManager = $groupManager;
    }

    private function appConfigValue($key, $default = '') {
        $value = $this->config->getAppValue('sunaq', (string)$key, '');
        if ($value !== '') {
            return $value;
        }
        // Compatibility with the 0.2.x app id.
        return $this->config->getAppValue('akirag', (string)$key, $default);
    }

    private function userAgent() {
        $version = trim((string)$this->config->getAppValue('sunaq', 'installed_version', ''));
        if ($version === '') {
            $version = 'unknown';
        }
        return 'SunaQ-Nextcloud/' . $version;
    }

    private function allowInsecureHttp() {
        $value = strtolower(trim((string)$this->appConfigValue('allow_insecure_http', '0')));
        return in_array($value, ['1', 'true', 'yes', 'on'], true);
    }

    private function assertCredentialTransport($baseUrl) {
        $scheme = strtolower((string)parse_url((string)$baseUrl, PHP_URL_SCHEME));
        if ($scheme === 'https') {
            return;
        }
        if ($scheme === 'http' && $this->allowInsecureHttp()) {
            return;
        }
        throw new \RuntimeException(
            'Credential-geschützte SunaQ-Anfragen erfordern HTTPS. Unsicheres HTTP kann nur explizit in den App-Einstellungen freigegeben werden.'
        );
    }

    private function userSettingsBackoffActive($uid) {
        $raw = $this->config->getUserValue((string)$uid, 'sunaq', 'user_settings_sync_failed_at', '0');
        $failedAt = (int)$raw;
        return $failedAt > 0 && (time() - $failedAt) < 15;
    }

    private function markUserSettingsSyncFailure($uid) {
        $this->config->setUserValue(
            (string)$uid,
            'sunaq',
            'user_settings_sync_failed_at',
            (string)time()
        );
    }

    private function clearUserSettingsSyncFailure($uid) {
        $this->config->deleteUserValue(
            (string)$uid,
            'sunaq',
            'user_settings_sync_failed_at'
        );
    }

    public function chat(array $messages, array $sourceScopes = [], $modelId = '', $requestId = '') {
        $baseUrl = rtrim($this->appConfigValue('middleware_url', ''), '/');
        $encryptedKey = $this->appConfigValue('api_key_encrypted', '');
        if ($baseUrl === '' || $encryptedKey === '') {
            throw new \RuntimeException('SunaQ Recherche ist noch nicht konfiguriert.');
        }
        $this->assertCredentialTransport($baseUrl);

        try {
            $apiKey = $this->crypto->decrypt($encryptedKey);
        } catch (\Exception $e) {
            throw new \RuntimeException('Der gespeicherte Middleware-API-Key kann nicht gelesen werden.');
        }

        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $uid = $user->getUID();
        $groupIds = [];
        foreach ($this->groupManager->getUserGroups($user) as $group) {
            $gid = trim((string)$group->getGID());
            if ($gid !== '') {
                $groupIds[] = $gid;
            }
        }
        $groupIds = array_values(array_unique($groupIds));

        $cleanMessages = $this->sanitizeMessages($messages);
        if (count($cleanMessages) === 0) {
            throw new \InvalidArgumentException('Keine Anfrage übergeben.');
        }

        $effectiveSourceScopes = $this->applySourceScopes($cleanMessages, $sourceScopes);

        $payload = [
            'messages' => $cleanMessages,
            'stream' => false,
            'user' => $uid,
        ];
        $modelId = trim((string)$modelId);
        if ($modelId !== '') {
            $payload['model'] = $modelId;
        }

        $headers = [
            'Accept' => 'application/json',
            'User-Agent' => $this->userAgent(),
            'Content-Type' => 'application/json',
            'Authorization' => 'Bearer ' . $apiKey,
            'X-RAG-User-ID' => $uid,
            'X-RAG-User-Groups' => json_encode($groupIds),
            'X-RAG-Web-Allowed' => 'true',
        ];
        $requestId = trim((string)$requestId);
        if ($requestId !== '' && preg_match('/^[A-Za-z0-9._-]{8,128}$/', $requestId)) {
            $headers['X-RAG-Request-ID'] = $requestId;
        }

        $client = $this->clientService->newClient();
        try {
            $response = $client->post($baseUrl . '/v1/chat/completions', [
                'headers' => $headers,
                'allow_redirects' => false,
                'body' => json_encode($payload),
                'timeout' => 240,
                'connect_timeout' => 15,
            ]);
        } catch (\Exception $e) {
            throw new \RuntimeException('Middleware-Anfrage fehlgeschlagen: ' . $this->safeError($e->getMessage()));
        }

        $status = (int)$response->getStatusCode();
        $decoded = json_decode((string)$response->getBody(), true);
        if ($status < 200 || $status >= 300) {
            $detail = is_array($decoded) && isset($decoded['detail']) ? (string)$decoded['detail'] : 'HTTP ' . $status;
            throw new \RuntimeException('Middleware-Fehler: ' . $detail);
        }

        if (!is_array($decoded) || !isset($decoded['choices'][0]['message']['content'])) {
            throw new \RuntimeException('Middleware hat keine lesbare Chat-Antwort geliefert.');
        }

        $rawContent = (string)$decoded['choices'][0]['message']['content'];
        $sources = $this->extractSourceMarkers($rawContent);
        $cleanContent = preg_replace('/<!--rag-source:\d+:[^>]+-->/', '', $rawContent);
        $cleanContent = trim(preg_replace('/[ \t]+\n/', "\n", (string)$cleanContent));

        $suggestions = [];
        if (isset($decoded['suggestions']) && is_array($decoded['suggestions'])) {
            foreach (array_slice($decoded['suggestions'], 0, 4) as $suggestion) {
                if (!is_array($suggestion)) {
                    continue;
                }
                $label = trim((string)($suggestion['label'] ?? ''));
                $action = strtolower(trim((string)($suggestion['action'] ?? '')));
                $query = trim((string)($suggestion['query'] ?? ''));
                $suggestionModel = trim((string)($suggestion['model'] ?? ''));
                if ($label === '' || !in_array($action, ['query', 'rerun', 'focus'], true)) {
                    continue;
                }
                if (($action === 'query' || $action === 'rerun') && $query === '') {
                    continue;
                }
                $item = ['label' => $label, 'action' => $action];
                if ($query !== '') {
                    $item['query'] = $query;
                }
                if ($suggestionModel !== '') {
                    $item['model'] = $suggestionModel;
                }
                $suggestions[] = $item;
            }
        }

        return [
            'content' => $cleanContent,
            'sources' => $sources,
            'suggestions' => $suggestions,
            'id' => isset($decoded['id']) ? (string)$decoded['id'] : '',
            'model' => isset($decoded['model']) ? (string)$decoded['model'] : $modelId,
            'source_scopes' => $effectiveSourceScopes,
        ];
    }

    public function models() {
        $baseUrl = rtrim($this->appConfigValue('middleware_url', ''), '/');
        $encryptedKey = $this->appConfigValue('api_key_encrypted', '');
        if ($baseUrl === '' || $encryptedKey === '') {
            throw new \RuntimeException('SunaQ Recherche ist noch nicht konfiguriert.');
        }
        $this->assertCredentialTransport($baseUrl);

        try {
            $apiKey = $this->crypto->decrypt($encryptedKey);
        } catch (\Exception $e) {
            throw new \RuntimeException('Der gespeicherte Middleware-API-Key kann nicht gelesen werden.');
        }

        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $uid = trim((string)$user->getUID());

        $client = $this->clientService->newClient();
        try {
            $response = $client->get($baseUrl . '/v1/models', [
                'headers' => [
                    'Accept' => 'application/json',
                    'User-Agent' => $this->userAgent(),
                    'Authorization' => 'Bearer ' . $apiKey,
                    'X-RAG-User-ID' => $uid,
                ],
                'allow_redirects' => false,
                'timeout' => 15,
                'connect_timeout' => 5,
            ]);
        } catch (\Exception $e) {
            throw new \RuntimeException('Modellliste konnte nicht geladen werden: ' . $this->safeError($e->getMessage()));
        }

        $status = (int)$response->getStatusCode();
        $decoded = json_decode((string)$response->getBody(), true);
        if ($status < 200 || $status >= 300) {
            $detail = is_array($decoded) && isset($decoded['detail']) ? (string)$decoded['detail'] : 'HTTP ' . $status;
            throw new \RuntimeException('Middleware-Fehler: ' . $detail);
        }

        $models = is_array($decoded) && isset($decoded['data']) && is_array($decoded['data'])
            ? array_values($decoded['data'])
            : [];
        $defaultModel = '';
        foreach ($models as $model) {
            if (is_array($model) && !empty($model['default']) && !empty($model['id'])) {
                $defaultModel = (string)$model['id'];
                break;
            }
        }
        if ($defaultModel === '' && isset($models[0]['id'])) {
            $defaultModel = (string)$models[0]['id'];
        }
        return ['models' => $models, 'default_model' => $defaultModel];
    }

    public function userSettings() {
        $baseUrl = rtrim($this->appConfigValue('middleware_url', ''), '/');
        $encryptedKey = $this->appConfigValue('api_key_encrypted', '');
        if ($baseUrl === '' || $encryptedKey === '') {
            throw new \RuntimeException('SunaQ Recherche ist noch nicht konfiguriert.');
        }
        $this->assertCredentialTransport($baseUrl);

        try {
            $apiKey = $this->crypto->decrypt($encryptedKey);
        } catch (\Exception $e) {
            throw new \RuntimeException('Der gespeicherte Middleware-API-Key kann nicht gelesen werden.');
        }
        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $uid = trim((string)$user->getUID());
        if ($this->userSettingsBackoffActive($uid)) {
            throw new \RuntimeException('Benutzereinstellungen werden nach einem Verbindungsfehler kurzzeitig lokal weiterverwendet.');
        }

        try {
            $client = $this->clientService->newClient();
            $response = $client->get($baseUrl . '/v1/user-settings', [
                'headers' => [
                    'Accept' => 'application/json',
                    'User-Agent' => $this->userAgent(),
                    'Authorization' => 'Bearer ' . $apiKey,
                    'X-RAG-User-ID' => $uid,
                ],
                'allow_redirects' => false,
                'timeout' => 10,
                'connect_timeout' => 5,
            ]);
        } catch (\Exception $e) {
            $this->markUserSettingsSyncFailure($uid);
            throw new \RuntimeException('Benutzereinstellungen konnten nicht geladen werden: ' . $this->safeError($e->getMessage()));
        }
        $status = (int)$response->getStatusCode();
        $decoded = json_decode((string)$response->getBody(), true);
        if ($status < 200 || $status >= 300) {
            $this->markUserSettingsSyncFailure($uid);
            $detail = is_array($decoded) && isset($decoded['detail']) ? (string)$decoded['detail'] : 'HTTP ' . $status;
            throw new \RuntimeException('Middleware-Fehler: ' . $detail);
        }
        $this->clearUserSettingsSyncFailure($uid);
        return is_array($decoded) ? $decoded : [];
    }

    public function status($requestId) {
        $requestId = trim((string)$requestId);
        if (!preg_match('/^[A-Za-z0-9._-]{8,128}$/', $requestId)) {
            throw new \InvalidArgumentException('Ungültige Request-ID.');
        }

        $baseUrl = rtrim($this->appConfigValue('middleware_url', ''), '/');
        $encryptedKey = $this->appConfigValue('api_key_encrypted', '');
        if ($baseUrl === '' || $encryptedKey === '') {
            throw new \RuntimeException('SunaQ Recherche ist noch nicht konfiguriert.');
        }
        $this->assertCredentialTransport($baseUrl);

        try {
            $apiKey = $this->crypto->decrypt($encryptedKey);
        } catch (\Exception $e) {
            throw new \RuntimeException('Der gespeicherte Middleware-API-Key kann nicht gelesen werden.');
        }
        $user = $this->userSession->getUser();
        if ($user === null) {
            throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
        }
        $uid = trim((string)$user->getUID());

        try {
            $client = $this->clientService->newClient();
            $response = $client->get($baseUrl . '/v1/status/' . rawurlencode($requestId), [
                'headers' => [
                    'Accept' => 'application/json',
                    'User-Agent' => $this->userAgent(),
                    'Authorization' => 'Bearer ' . $apiKey,
                    'X-RAG-User-ID' => $uid,
                ],
                'allow_redirects' => false,
                'timeout' => 5,
                'connect_timeout' => 2,
            ]);
        } catch (\Exception $e) {
            throw new \RuntimeException('Status konnte nicht geladen werden: ' . $this->safeError($e->getMessage()));
        }

        $status = (int)$response->getStatusCode();
        $decoded = json_decode((string)$response->getBody(), true);
        if ($status === 404) {
            return ['stage' => 'pending', 'label' => 'Anfrage wird vorbereitet …'];
        }
        if ($status < 200 || $status >= 300) {
            throw new \RuntimeException('Statusabfrage fehlgeschlagen: HTTP ' . $status);
        }
        return is_array($decoded) ? $decoded : [];
    }

    public function registerChatArchive($documentId, $path) {
        $documentId = trim((string)$documentId);
        $path = trim((string)$path);
        if (!preg_match('/^files:\\d+$/', $documentId) || $path === '') {
            return false;
        }

        $baseUrl = rtrim($this->appConfigValue('middleware_url', ''), '/');
        $encryptedKey = $this->appConfigValue('api_key_encrypted', '');
        if ($baseUrl === '' || $encryptedKey === '') {
            return false;
        }
        try {
            $this->assertCredentialTransport($baseUrl);
        } catch (\RuntimeException $e) {
            return false;
        }

        try {
            $user = $this->userSession->getUser();
            if ($user === null) {
                return false;
            }
            $uid = trim((string)$user->getUID());
            if ($uid === '') {
                return false;
            }

            $apiKey = $this->crypto->decrypt($encryptedKey);
            $client = $this->clientService->newClient();
            $response = $client->post($baseUrl . '/v1/archive/chat/register', [
                'headers' => [
                    'Accept' => 'application/json',
                    'User-Agent' => $this->userAgent(),
                    'Content-Type' => 'application/json',
                    'Authorization' => 'Bearer ' . $apiKey,
                    'X-RAG-User-ID' => $uid,
                ],
                'allow_redirects' => false,
                'body' => json_encode([
                    'document_id' => $documentId,
                    'path' => $path,
                ]),
                'timeout' => 3,
                'connect_timeout' => 2,
            ]);
            $status = (int)$response->getStatusCode();
            return $status >= 200 && $status < 300;
        } catch (\Exception $e) {
            // Provenance registration is an optimization. The archive itself
            // remains valid and path-based self-healing can repair it later.
            return false;
        }
    }

    private function sanitizeMessages(array $messages) {
        $messages = array_slice($messages, -self::MAX_MESSAGES);
        $clean = [];

        foreach ($messages as $message) {
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
            if (strlen($content) > self::MAX_MESSAGE_CHARS) {
                throw new \InvalidArgumentException('Eine Nachricht ist zu lang.');
            }
            if ($role === 'assistant' && isset($message['sources']) && is_array($message['sources'])) {
                $markers = [];
                foreach ($message['sources'] as $source) {
                    if (!is_array($source)) {
                        continue;
                    }
                    $index = isset($source['index']) ? (int)$source['index'] : 0;
                    $reference = isset($source['reference']) ? trim((string)$source['reference']) : '';
                    if ($index > 0 && $reference !== '' && strpos($reference, '-->') === false) {
                        $markers[] = '<!--rag-source:' . $index . ':' . $reference . '-->';
                    }
                }
                if ($markers) {
                    $content .= "\n\n" . implode('', $markers);
                }
            }
            $clean[] = ['role' => $role, 'content' => $content];
        }

        if (count($clean) === 0 || $clean[count($clean) - 1]['role'] !== 'user') {
            throw new \InvalidArgumentException('Die letzte Nachricht muss eine Benutzeranfrage sein.');
        }

        return $clean;
    }

    private function applySourceScopes(array &$messages, array $sourceScopes) {
        if (!$messages) {
            return [];
        }
        $last = count($messages) - 1;
        if (($messages[$last]['role'] ?? '') !== 'user') {
            return [];
        }
        $content = (string)($messages[$last]['content'] ?? '');
        if (preg_match('/^\s*(?:(?:\/(?:list:raw|documents|mailarchive|webarchive|chatarchive|files|vector|graph|elastic|web|list|new|force|health|help|use:[^\s]+))\s*)+/i', $content, $prefixMatch)) {
            $prefix = $prefixMatch[0];
            if (preg_match_all('/\/(documents|mailarchive|webarchive|chatarchive|web)\b/i', $prefix, $scopeMatches)) {
                $explicit = [];
                foreach ($scopeMatches[1] as $scope) {
                    $value = strtolower(trim((string)$scope));
                    if ($value !== '' && !in_array($value, $explicit, true)) {
                        $explicit[] = $value;
                    }
                }
                if ($explicit) {
                    return $explicit; // explicit user source selection wins over UI state
                }
            }
            if (preg_match('/\/(?:use:|health\b|help\b)/i', $prefix)) {
                return []; // direct/special commands are not source-scoped
            }
        }

        $allowed = ['documents', 'mailarchive', 'webarchive', 'chatarchive', 'web'];
        $selected = [];
        foreach ($sourceScopes as $scope) {
            $value = strtolower(trim((string)$scope));
            if (in_array($value, $allowed, true) && !in_array($value, $selected, true)) {
                $selected[] = $value;
            }
        }
        if (!$selected) {
            return [];
        }
        $directives = array_map(function ($scope) { return '/' . $scope; }, $selected);
        $messages[$last]['content'] = implode(' ', $directives) . ' ' . ltrim($content);
        return $selected;
    }
    private function extractSourceMarkers($content) {
        $sources = [];
        if (preg_match_all('/<!--rag-source:(\d+):([^>]+)-->/', (string)$content, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $match) {
                $reference = trim((string)$match[2]);
                if ($reference !== '') {
                    $sources[] = ['index' => (int)$match[1], 'reference' => $reference];
                }
            }
        }
        return $sources;
    }

    private function safeError($message) {
        $message = preg_replace('/Bearer\\s+[^\\s]+/i', 'Bearer [redacted]', (string)$message);
        if (strlen($message) > 300) {
            $message = substr($message, 0, 300) . '…';
        }
        return $message;
    }
}
