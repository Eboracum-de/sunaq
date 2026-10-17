<?php
namespace OCA\Sunaq\Controller;

use OCA\Sunaq\Http\SseResponse;
use OCA\Sunaq\Service\ChatStore;
use OCA\Sunaq\Service\RagProxy;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\DataResponse;
use OCP\IRequest;

class ChatController extends Controller {
    /** @var RagProxy */
    private $proxy;
    /** @var ChatStore */
    private $store;

    public function __construct($AppName, IRequest $request, RagProxy $proxy, ChatStore $store) {
        parent::__construct($AppName, $request);
        $this->proxy = $proxy;
        $this->store = $store;
    }

    private function syncArchiveSettings() {
        try {
            $settings = $this->proxy->userSettings();
            $path = isset($settings['chat_archive_path']) ? trim((string)$settings['chat_archive_path']) : '';
            if ($path !== '') {
                $this->store->setArchivePath($path);
            }
            return [
                'enabled' => !empty($settings['chat_archive_enabled']),
                'path' => $path,
                'chat_archive_enabled' => !empty($settings['chat_archive_enabled']),
                'chat_archive_path' => $path,
                'source_capabilities' => isset($settings['source_capabilities']) && is_array($settings['source_capabilities'])
                    ? $settings['source_capabilities']
                    : [],
            ];
        } catch (\Exception $e) {
            // Persistence is privacy-sensitive policy.  If the provider policy
            // cannot be read, fail closed for new archive writes rather than
            // silently persisting a chat contrary to administrator intent.
            return [
                'enabled' => false,
                'path' => '',
                'chat_archive_enabled' => false,
                'chat_archive_path' => '',
                'source_capabilities' => [],
            ];
        }
    }

    private function syncArchivePath() {
        $this->syncArchiveSettings();
    }

    /**
     * @NoAdminRequired
     *
     * @param array $messages
     * @param array $sourceScopes
     * @param string $conversationId
     * @param string $model
     * @param string $requestId
     * @return DataResponse
     */
    public function send($messages = [], $sourceScopes = [], $conversationId = '', $model = '', $requestId = '') {
        if (!is_array($messages)) {
            return new DataResponse(['error' => 'Ungültiger Gesprächsverlauf.'], 400);
        }
        if (!is_array($sourceScopes)) {
            $sourceScopes = [];
        }

        try {
            // The provider must receive the same stable conversation id that
            // will later become the Nextcloud dossier directory. Do this
            // server-side as well as in JavaScript so a stale browser asset
            // cannot lose first-turn ResearchRun provenance.
            $conversationId = $this->store->ensureConversationId($conversationId);
            $result = $this->proxy->chat($messages, $sourceScopes, $conversationId, $model, $requestId);
            $stored = $messages;
            $assistantCreatedAt = gmdate('c');
            $assistant = [
                'role' => 'assistant',
                'content' => isset($result['content']) ? (string)$result['content'] : '',
                'created_at' => $assistantCreatedAt,
            ];
            if (!empty($result['sources']) && is_array($result['sources'])) {
                $assistant['sources'] = $result['sources'];
            }
            if (!empty($result['source_scopes']) && is_array($result['source_scopes'])) {
                $assistant['source_scopes'] = $result['source_scopes'];
            }
            $stored[] = $assistant;
            $archiveSettings = $this->syncArchiveSettings();
            $result['message_created_at'] = $assistantCreatedAt;
            $result['chat_archive_enabled'] = !empty($archiveSettings['enabled']);
            if (!empty($archiveSettings['enabled'])) {
                try {
                    $chat = $this->store->save($conversationId, $stored, $sourceScopes);
                    $this->proxy->registerChatArchive(
                        $chat['document_id'] ?? '',
                        $chat['archive_path'] ?? ''
                    );
                    $result['conversation'] = [
                        'id' => $chat['id'],
                        'title' => $chat['title'],
                        'updated_at' => $chat['updated_at'],
                        'scopes' => $chat['scopes'],
                    ];
                } catch (\Exception $archiveException) {
                    $result['archive_warning'] = 'Antwort erstellt, Chatarchiv konnte jedoch nicht aktualisiert werden.';
                }
            }
            return new DataResponse($result);
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 400);
        } catch (\RuntimeException $e) {
            return new DataResponse(['error' => $e->getMessage()], 502);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Middleware nicht erreichbar.'], 502);
        }
    }


    /**
     * Stream the provider response to the browser. The legacy send() endpoint
     * remains unchanged for compatibility/fallback.
     *
     * @NoAdminRequired
     *
     * @param array $messages
     * @param array $sourceScopes
     * @param string $conversationId
     * @param string $model
     * @param string $requestId
     * @return SseResponse|DataResponse
     */
    public function stream($messages = [], $sourceScopes = [], $conversationId = '', $model = '', $requestId = '') {
        if (!is_array($messages)) {
            return new DataResponse(['error' => 'Ungültiger Gesprächsverlauf.'], 400);
        }
        if (!is_array($sourceScopes)) {
            $sourceScopes = [];
        }

        try {
            $conversationId = $this->store->ensureConversationId($conversationId);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Ungültige Conversation-ID.'], 400);
        }

        return new SseResponse(function ($emit) use (
            $messages,
            $sourceScopes,
            $conversationId,
            $model,
            $requestId
        ) {
            try {
                $result = $this->proxy->streamChat(
                    $messages,
                    $sourceScopes,
                    $conversationId,
                    $model,
                    $requestId,
                    $emit
                );

                $stored = $messages;
                $assistantCreatedAt = gmdate('c');
                $assistant = [
                    'role' => 'assistant',
                    'content' => isset($result['content']) ? (string)$result['content'] : '',
                    'created_at' => $assistantCreatedAt,
                ];
                if (!empty($result['sources']) && is_array($result['sources'])) {
                    $assistant['sources'] = $result['sources'];
                }
                if (!empty($result['source_scopes']) && is_array($result['source_scopes'])) {
                    $assistant['source_scopes'] = $result['source_scopes'];
                }
                $stored[] = $assistant;

                $archiveSettings = $this->syncArchiveSettings();
                $result['message_created_at'] = $assistantCreatedAt;
                $result['chat_archive_enabled'] = !empty($archiveSettings['enabled']);

                if (!empty($archiveSettings['enabled'])) {
                    try {
                        $chat = $this->store->save($conversationId, $stored, $sourceScopes);
                        $this->proxy->registerChatArchive(
                            $chat['document_id'] ?? '',
                            $chat['archive_path'] ?? ''
                        );
                        $result['conversation'] = [
                            'id' => $chat['id'],
                            'title' => $chat['title'],
                            'updated_at' => $chat['updated_at'],
                            'scopes' => $chat['scopes'],
                        ];
                    } catch (\Exception $archiveException) {
                        $result['archive_warning'] = 'Antwort erstellt, Chatarchiv konnte jedoch nicht aktualisiert werden.';
                    }
                }

                call_user_func(
                    $emit,
                    "event: sunaq_meta\n"
                    . 'data: ' . json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)
                    . "\n\n"
                );
                call_user_func($emit, "event: sunaq_done\ndata: {}\n\n");
            } catch (\InvalidArgumentException $e) {
                call_user_func(
                    $emit,
                    "event: sunaq_error\ndata: "
                    . json_encode(['error' => $e->getMessage()], JSON_UNESCAPED_UNICODE)
                    . "\n\n"
                );
            } catch (\RuntimeException $e) {
                call_user_func(
                    $emit,
                    "event: sunaq_error\ndata: "
                    . json_encode(['error' => $e->getMessage()], JSON_UNESCAPED_UNICODE)
                    . "\n\n"
                );
            } catch (\Exception $e) {
                call_user_func(
                    $emit,
                    "event: sunaq_error\ndata: "
                    . json_encode(['error' => 'Middleware nicht erreichbar.'], JSON_UNESCAPED_UNICODE)
                    . "\n\n"
                );
            }
        });
    }


    /**
     * @NoAdminRequired
     */
    public function models() {
        try {
            $settings = $this->syncArchiveSettings();
            $models = $this->proxy->models();
            $models['user_settings'] = $settings;
            return new DataResponse($models);
        } catch (\RuntimeException $e) {
            return new DataResponse(['error' => $e->getMessage()], 502);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Modellliste konnte nicht geladen werden.'], 502);
        }
    }

    /**
     * @NoAdminRequired
     */
    public function status($requestId = '') {
        try {
            return new DataResponse($this->proxy->status($requestId));
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 400);
        } catch (\RuntimeException $e) {
            return new DataResponse(['error' => $e->getMessage()], 502);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Status konnte nicht geladen werden.'], 502);
        }
    }

    /**
     * @NoAdminRequired
     */
    public function listChats() {
        try {
            $this->syncArchivePath();
            return new DataResponse(['chats' => $this->store->listChats()]);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Chatarchiv konnte nicht gelesen werden.'], 500);
        }
    }

    /**
     * @NoAdminRequired
     */
    public function load($id = '') {
        try {
            $this->syncArchivePath();
            return new DataResponse(['chat' => $this->store->load($id)]);
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 404);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Chat konnte nicht gelesen werden.'], 500);
        }
    }

    /**
     * @NoAdminRequired
     */
    public function rename($id = '', $title = '') {
        try {
            $settings = $this->syncArchiveSettings();
            if (empty($settings['enabled'])) {
                return new DataResponse(['error' => 'Das Chatarchiv ist deaktiviert.'], 403);
            }
            $chat = $this->store->rename($id, $title);
            $this->proxy->registerChatArchive(
                $chat['document_id'] ?? '',
                $chat['archive_path'] ?? ''
            );
            return new DataResponse(['ok' => true, 'chat' => $chat]);
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 400);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Chat konnte nicht umbenannt werden.'], 500);
        }
    }

    /**
     * @NoAdminRequired
     */
    public function delete($id = '') {
        try {
            $this->syncArchivePath();
            $this->store->delete($id);
            return new DataResponse(['ok' => true]);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Chat konnte nicht gelöscht werden.'], 500);
        }
    }
}
