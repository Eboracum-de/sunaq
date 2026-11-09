<?php
namespace OCA\Sunaq\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\DataResponse;
use OCP\Http\Client\IClientService;
use OCP\IConfig;
use OCP\IRequest;
use OCP\IUserSession;
use OCP\Security\ICrypto;

class AdminController extends Controller {
    /** @var IConfig */
    private $config;

    /** @var IClientService */
    private $clientService;

    /** @var ICrypto */
    private $crypto;

    /** @var IUserSession */
    private $userSession;

    public function __construct(
        $AppName,
        IRequest $request,
        IConfig $config,
        ICrypto $crypto,
        IClientService $clientService,
        IUserSession $userSession
    ) {
        parent::__construct($AppName, $request);
        $this->config = $config;
        $this->crypto = $crypto;
        $this->clientService = $clientService;
        $this->userSession = $userSession;
    }

    /**
     * @param string $middlewareUrl
     * @param string $apiKey
     * @param bool $allowInsecureHttp
     * @return DataResponse
     */
    public function status() {
        $baseUrl = rtrim(trim((string)$this->config->getAppValue('sunaq', 'middleware_url', '')), '/');
        if ($baseUrl === '') {
            $baseUrl = rtrim(trim((string)$this->config->getAppValue('akirag', 'middleware_url', '')), '/');
        }
        $encryptedKey = $this->config->getAppValue('sunaq', 'api_key_encrypted', '');
        if ($encryptedKey === '') {
            $encryptedKey = $this->config->getAppValue('akirag', 'api_key_encrypted', '');
        }
        if ($baseUrl === '' || $encryptedKey === '') {
            return new DataResponse([
                'ok' => false,
                'configured' => false,
                'error' => 'SunaQ ist noch nicht vollständig konfiguriert.',
            ]);
        }

        $scheme = strtolower((string)parse_url($baseUrl, PHP_URL_SCHEME));
        $allowInsecure = in_array(
            strtolower((string)$this->config->getAppValue('sunaq', 'allow_insecure_http', '0')),
            ['1', 'true', 'yes', 'on'],
            true
        );
        if ($scheme === 'http' && !$allowInsecure) {
            return new DataResponse([
                'ok' => false,
                'configured' => true,
                'error' => 'HTTP ist für die konfigurierte SunaQ-Verbindung nicht freigegeben.',
            ], 400);
        }

        try {
            $apiKey = $this->crypto->decrypt($encryptedKey);
        } catch (\Exception $e) {
            return new DataResponse([
                'ok' => false,
                'configured' => true,
                'error' => 'Der gespeicherte SunaQ-API-Key kann nicht gelesen werden.',
            ], 500);
        }

        try {
            $headers = [
                'Accept' => 'application/json',
                'Authorization' => 'Bearer ' . $apiKey,
                'User-Agent' => 'SunaQ-Nextcloud-Admin',
            ];
            $user = $this->userSession->getUser();
            if ($user !== null) {
                $uid = trim((string)$user->getUID());
                if ($uid !== '') {
                    $headers['X-RAG-User-ID'] = $uid;
                }
            }

            $client = $this->clientService->newClient();
            $response = $client->get($baseUrl . '/v1/models', [
                'headers' => $headers,
                'allow_redirects' => false,
                'timeout' => 10,
                'connect_timeout' => 5,
            ]);
        } catch (\Exception $e) {
            return new DataResponse([
                'ok' => false,
                'configured' => true,
                'reachable' => false,
                'error' => 'SunaQ ist nicht erreichbar: ' . $this->safeError($e->getMessage()),
            ]);
        }

        $httpStatus = (int)$response->getStatusCode();
        $decoded = json_decode((string)$response->getBody(), true);
        if ($httpStatus < 200 || $httpStatus >= 300) {
            $detail = is_array($decoded) && isset($decoded['detail'])
                ? (string)$decoded['detail']
                : 'HTTP ' . $httpStatus;
            return new DataResponse([
                'ok' => false,
                'configured' => true,
                'reachable' => true,
                'authenticated' => false,
                'httpStatus' => $httpStatus,
                'error' => 'SunaQ antwortet, aber die Provider-Verbindung ist nicht nutzbar: ' . $this->safeError($detail),
            ]);
        }

        $maintenance = is_array($decoded) && (
            !empty($decoded['maintenance'])
            || strtolower((string)($decoded['status'] ?? '')) === 'maintenance'
        );
        $models = (!$maintenance && is_array($decoded) && isset($decoded['data']) && is_array($decoded['data']))
            ? $decoded['data']
            : [];
        $configuredModelCount = (!$maintenance && is_array($decoded) && isset($decoded['configured_model_count']))
            ? max(0, (int)$decoded['configured_model_count'])
            : null;
        $scopes = [];
        if (
            is_array($decoded)
            && isset($decoded['capabilities']['source_scopes'])
            && is_array($decoded['capabilities']['source_scopes'])
        ) {
            $scopes = array_values($decoded['capabilities']['source_scopes']);
        }

        return new DataResponse([
            'ok' => true,
            'configured' => true,
            'reachable' => true,
            'authenticated' => true,
            'maintenance' => $maintenance,
            'version' => is_array($decoded) ? (string)($decoded['version'] ?? '') : '',
            'architectureTier' => is_array($decoded) ? (string)($decoded['architecture_tier'] ?? '') : '',
            'modelCount' => $maintenance ? null : count($models),
            'configuredModelCount' => $configuredModelCount,
            'sourceScopes' => $maintenance ? [] : $scopes,
            'adminUrl' => $baseUrl . '/rag-admin/',
        ]);
    }

    private function safeError($message) {
        $message = preg_replace('/Bearer\\s+[^\\s]+/i', 'Bearer [redacted]', (string)$message);
        if (strlen($message) > 300) {
            $message = substr($message, 0, 300) . '…';
        }
        return $message;
    }

    public function save(
        $middlewareUrl = '',
        $apiKey = '',
        $allowInsecureHttp = false,
        $analysisMode = false,
        $showSearchStrategy = true
    ) {
        $url = rtrim(trim((string)$middlewareUrl), '/');
        if ($url === '' || filter_var($url, FILTER_VALIDATE_URL) === false) {
            return new DataResponse(['error' => 'Bitte eine gültige Middleware-URL angeben.'], 400);
        }

        $scheme = strtolower((string)parse_url($url, PHP_URL_SCHEME));
        if ($scheme !== 'http' && $scheme !== 'https') {
            return new DataResponse(['error' => 'Die Middleware-URL muss http oder https verwenden.'], 400);
        }
        $allowInsecure = filter_var($allowInsecureHttp, FILTER_VALIDATE_BOOLEAN);
        if ($scheme === 'http' && !$allowInsecure) {
            return new DataResponse([
                'error' => 'HTTP würde den Provider-API-Key unverschlüsselt übertragen. Bitte HTTPS verwenden oder unsicheres HTTP ausdrücklich freigeben.'
            ], 400);
        }

        $this->config->setAppValue('sunaq', 'middleware_url', $url);
        $this->config->setAppValue('sunaq', 'allow_insecure_http', $allowInsecure ? '1' : '0');
        $this->config->setAppValue(
            'sunaq',
            'analysis_mode',
            filter_var($analysisMode, FILTER_VALIDATE_BOOLEAN) ? '1' : '0'
        );
        $this->config->setAppValue(
            'sunaq',
            'show_search_strategy',
            filter_var($showSearchStrategy, FILTER_VALIDATE_BOOLEAN) ? '1' : '0'
        );

        $apiKey = trim((string)$apiKey);
        if ($apiKey !== '') {
            $this->config->setAppValue('sunaq', 'api_key_encrypted', $this->crypto->encrypt($apiKey));
        } elseif ($this->config->getAppValue('sunaq', 'api_key_encrypted', '') === '') {
            // 0.2.x used app id "akirag". Reuse the encrypted value during the
            // one-time app-id migration without exposing/decrypting it here.
            $legacyKey = $this->config->getAppValue('akirag', 'api_key_encrypted', '');
            if ($legacyKey !== '') {
                $this->config->setAppValue('sunaq', 'api_key_encrypted', $legacyKey);
            }
        }

        return new DataResponse([
            'ok' => true,
            'middlewareUrl' => $url,
            'hasApiKey' => (
                $this->config->getAppValue('sunaq', 'api_key_encrypted', '') !== ''
                || $this->config->getAppValue('akirag', 'api_key_encrypted', '') !== ''
            ),
            'allowInsecureHttp' => $allowInsecure,
            'analysisMode' => filter_var($analysisMode, FILTER_VALIDATE_BOOLEAN),
            'showSearchStrategy' => filter_var($showSearchStrategy, FILTER_VALIDATE_BOOLEAN),
        ]);
    }
}
