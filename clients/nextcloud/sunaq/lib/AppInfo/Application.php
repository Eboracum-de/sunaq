<?php
namespace OCA\Sunaq\AppInfo;

use OCA\Sunaq\Listener\ChatArchiveDeleteListener;
use OCA\Sunaq\Listener\FilesLoadAdditionalScriptsListener;
use OCA\Files\Event\LoadAdditionalScriptsEvent;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;
use OCP\Files\Events\Node\BeforeNodeDeletedEvent;
use OCP\Util;

class Application extends App implements IBootstrap {
    const APP_ID = 'sunaq';

    public function __construct() {
        parent::__construct(self::APP_ID);
    }

    public function register(IRegistrationContext $context): void {
        $context->registerEventListener(
            BeforeNodeDeletedEvent::class,
            ChatArchiveDeleteListener::class
        );
        $context->registerEventListener(
            LoadAdditionalScriptsEvent::class,
            FilesLoadAdditionalScriptsListener::class
        );
    }

    public function boot(IBootContext $context): void {
        // NC23-25 uses the legacy FileList plugin surface. Keep that adapter
        // isolated from current Files generations, which use the typed event
        // plus the @nextcloud/files action registry.
        $version = \OC_Util::getVersion();
        $major = isset($version[0]) ? (int)$version[0] : 0;
        if ($major > 0 && $major < 26) {
            $dispatcher = \OC::$server->getEventDispatcher();
            $dispatcher->addListener('OCA\\Files::loadAdditionalScripts', function () {
                Util::addScript(self::APP_ID, 'files-actions');
            });
        }
    }
}
