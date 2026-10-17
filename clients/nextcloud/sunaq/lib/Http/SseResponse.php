<?php
namespace OCA\Sunaq\Http;

use OCP\AppFramework\Http\ICallbackResponse;
use OCP\AppFramework\Http\IOutput;
use OCP\AppFramework\Http\Response;

/**
 * Lazily emits an SSE response without letting the AppFramework buffer it.
 *
 * Compatible with Nextcloud 23's ICallbackResponse API.
 */
class SseResponse extends Response implements ICallbackResponse {
    /** @var callable */
    private $producer;

    public function __construct(callable $producer) {
        parent::__construct();
        $this->producer = $producer;
        $this->addHeader('Content-Type', 'text/event-stream; charset=utf-8');
        $this->addHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
        $this->addHeader('X-Accel-Buffering', 'no');
    }

    public function callback(IOutput $output) {
        $emit = function ($chunk) use ($output) {
            $value = (string)$chunk;
            if ($value === '') {
                return;
            }
            $output->setOutput($value);
            if (function_exists('ob_flush')) {
                @ob_flush();
            }
            if (function_exists('flush')) {
                @flush();
            }
        };

        call_user_func($this->producer, $emit);
    }
}
