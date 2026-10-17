<?php
namespace OCA\Sunaq\Listener;

use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Files\Events\Node\BeforeNodeDeletedEvent;
use OCP\Files\File;

class ChatArchiveDeleteListener implements IEventListener {
    public function handle(Event $event): void {
        if (!($event instanceof BeforeNodeDeletedEvent)) {
            return;
        }

        if (method_exists($event, 'getNode')) {
            $node = $event->getNode();
        } elseif (method_exists($event, 'getTarget')) {
            $node = $event->getTarget();
        } else {
            return;
        }
        if (!($node instanceof File)) {
            return;
        }

        $name = (string)$node->getName();
        if (substr($name, -3) !== '.md') {
            return;
        }

        try {
            $content = (string)$node->getContent();
        } catch (\Exception $e) {
            return;
        }

        if (!preg_match('/(?:^|\r?\n)- Chat-ID: ([A-Za-z0-9_-]{8,64})(?:\r?\n|$)/', $content, $match)) {
            return;
        }
        $id = (string)$match[1];

        try {
            $parent = $node->getParent();

            // Dossier layout: <archive>/<conversation-id>/<descriptive-chat>.md.
            // Metadata identifies the current lifecycle anchor. This matters on a
            // chat rename: ChatStore writes the new Markdown and metadata first,
            // then removes the stale old filename without deleting research runs.
            if ((string)$parent->getName() === $id && $parent->nodeExists('.conversation.json')) {
                try {
                    $meta = json_decode((string)$parent->get('.conversation.json')->getContent(), true);
                } catch (\Exception $e) {
                    $meta = null;
                }
                $archiveFile = is_array($meta) ? trim((string)($meta['archive_file'] ?? '')) : '';
                if ($archiveFile === $name) {
                    foreach (['.conversation.json', 'runs'] as $managed) {
                        if ($parent->nodeExists($managed)) {
                            $parent->get($managed)->delete();
                        }
                    }
                    return;
                }
            }

            // Legacy flat layout remains supported for existing archives.
            $shortId = substr($id, 0, 8);
            if ($shortId === '' || !preg_match('/ - ' . preg_quote($shortId, '/') . '\\.md$/u', $name)) {
                return;
            }
            foreach (['.' . $id . '.sunaq.json', '.' . $id . '.akirag.json'] as $sidecar) {
                if ($parent->nodeExists($sidecar)) {
                    $parent->get($sidecar)->delete();
                }
            }
        } catch (\Exception $e) {
            // Never block the user's requested file deletion. ChatStore also
            // prunes orphan metadata/dossiers on its next list/load operation.
        }
    }
}
