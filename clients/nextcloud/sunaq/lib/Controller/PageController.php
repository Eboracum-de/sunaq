<?php
namespace OCA\Sunaq\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\IConfig;
use OCP\IRequest;

class PageController extends Controller {
    /** @var IConfig */
    private $config;

    public function __construct($AppName, IRequest $request, IConfig $config) {
        parent::__construct($AppName, $request);
        $this->config = $config;
    }

    private function appBool($key, $default = false) {
        $fallback = $default ? '1' : '0';
        $value = $this->config->getAppValue('sunaq', (string)$key, '');
        if ($value === '') {
            $value = $this->config->getAppValue('akirag', (string)$key, $fallback);
        }
        return in_array(strtolower((string)$value), ['1', 'true', 'yes', 'on'], true);
    }

    /**
     * @NoAdminRequired
     * @NoCSRFRequired
     */
    public function index() {
        return new TemplateResponse('sunaq', 'main', [
            'analysis_mode' => $this->appBool('analysis_mode', false),
            'show_search_strategy' => $this->appBool('show_search_strategy', true),
        ]);
    }
}
