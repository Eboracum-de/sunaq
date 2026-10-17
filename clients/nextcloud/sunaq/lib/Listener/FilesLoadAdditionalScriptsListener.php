<?php
namespace OCA\Sunaq\Listener;

use OCA\Files\Event\LoadAdditionalScriptsEvent;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Util;

/**
 * Load the modern Files action as an init script so it registers before the
 * Files frontend initializes its action registry.
 */
class FilesLoadAdditionalScriptsListener implements IEventListener {
    public function handle(Event $event): void {
        if (!($event instanceof LoadAdditionalScriptsEvent)) {
            return;
        }
        if (method_exists(Util::class, 'addInitScript')) {
            // NC28+: register before the Files frontend initializes.
            Util::addInitScript('sunaq', 'files-actions-modern');
        } else {
            // NC26/27 expose the typed Files event but not addInitScript yet.
            Util::addScript('sunaq', 'files-actions-modern');
        }
    }
}
