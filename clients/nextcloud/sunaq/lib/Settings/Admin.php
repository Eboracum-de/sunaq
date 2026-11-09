<?php
namespace OCA\Sunaq\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\IConfig;
use OCP\Settings\ISettings;

class Admin implements ISettings {
    /** @var IConfig */
    private $config;

    public function __construct(IConfig $config) {
        $this->config = $config;
    }

    private function appConfigValue($key, $default = '') {
        $value = $this->config->getAppValue('sunaq', (string)$key, '');
        if ($value !== '') {
            return $value;
        }
        return $this->config->getAppValue('akirag', (string)$key, $default);
    }

    public function getForm() {
        return new TemplateResponse('sunaq', 'admin', [
            'middleware_url' => $this->appConfigValue('middleware_url', ''),
            'has_api_key' => $this->appConfigValue('api_key_encrypted', '') !== '',
            'allow_insecure_http' => in_array(
                strtolower($this->appConfigValue('allow_insecure_http', '0')),
                ['1', 'true', 'yes', 'on'],
                true
            ),
            'analysis_mode' => in_array(
                strtolower($this->appConfigValue('analysis_mode', '0')),
                ['1', 'true', 'yes', 'on'],
                true
            ),
            'show_search_strategy' => in_array(
                strtolower($this->appConfigValue('show_search_strategy', '1')),
                ['1', 'true', 'yes', 'on'],
                true
            ),
        ]);
    }

    public function getSection() {
        return 'sunaq';
    }

    public function getPriority() {
        return 50;
    }
}
