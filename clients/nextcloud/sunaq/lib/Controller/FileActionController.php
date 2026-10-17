<?php
namespace OCA\Sunaq\Controller;

use OCA\Sunaq\Service\RagProxy;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\DataResponse;
use OCP\Files\Folder;
use OCP\Files\IRootFolder;
use OCP\IRequest;
use OCP\IUserSession;

class FileActionController extends Controller {
    /** @var RagProxy */
    private $proxy;

    /** @var IRootFolder */
    private $rootFolder;

    /** @var IUserSession */
    private $userSession;

    public function __construct(
        $AppName,
        IRequest $request,
        RagProxy $proxy,
        IRootFolder $rootFolder,
        IUserSession $userSession
    ) {
        parent::__construct($AppName, $request);
        $this->proxy = $proxy;
        $this->rootFolder = $rootFolder;
        $this->userSession = $userSession;
    }

    /**
     * Suggest a human-readable file name for one explicitly selected document.
     *
     * Prototype contract:
     * - no rename is performed;
     * - no chat/conversation is created;
     * - the existing /use document-resolution path remains authoritative,
     *   including its live Nextcloud ACL check.
     *
     * @NoAdminRequired
     *
     * @param string $path
     * @param int|string $fileId
     * @return DataResponse
     */
    public function suggestName($path = '', $fileId = '') {
        try {
            $path = $this->normalizeSelectedPath($path);
            $fileId = $this->normalizeFileId($fileId);
            $requestId = $this->requestId();
            $reference = $fileId !== ''
                ? 'files:' . $fileId
                : $this->quoteUseReference($path);

            $result = $this->requestSuggestion($reference, $requestId);
            $rawSuggestion = isset($result['content']) ? (string)$result['content'] : '';
            $fallbackUsed = false;

            // A Nextcloud file can exist before/while its FullTextSearch row is
            // available under the expected files:<id> Elasticsearch document id.
            // Retry once with the exact user-relative path.  This remains a
            // deterministic /use selection and still passes the normal live ACL.
            if ($fileId !== '' && $this->isSuggestionResolutionError($rawSuggestion)) {
                $reference = $this->quoteUseReference($path);
                $result = $this->requestSuggestion($reference, $requestId . '-path');
                $rawSuggestion = isset($result['content']) ? (string)$result['content'] : '';
                $fallbackUsed = true;
            }

            $this->assertSuggestionResponse($rawSuggestion);
            $suggestion = $this->normalizeSuggestion(
                $rawSuggestion,
                basename($path)
            );

            return new DataResponse([
                'ok' => true,
                'suggested_name' => $suggestion,
                'source_path' => $path,
                'file_id' => $fileId,
                'reference' => $reference,
                'fallback_used' => $fallbackUsed,
                'model' => isset($result['model']) ? (string)$result['model'] : '',
            ]);
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 400);
        } catch (\RuntimeException $e) {
            return new DataResponse(['error' => $e->getMessage()], 502);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Dateinamenvorschlag konnte nicht erstellt werden.'], 500);
        }
    }

    /**
     * Rename one selected Nextcloud file after explicit user confirmation.
     *
     * The Node API remains authoritative for permissions, storage semantics,
     * shares and file identity. The caller supplies both path and id so a stale
     * Files view cannot accidentally rename a different node.
     *
     * @NoAdminRequired
     *
     * @param string $path
     * @param int|string $fileId
     * @param string $newName
     * @return DataResponse
     */
    public function rename($path = '', $fileId = '', $newName = '') {
        try {
            $path = $this->normalizeSelectedPath($path);
            $fileId = $this->normalizeFileId($fileId);
            $user = $this->userSession->getUser();
            if ($user === null) {
                throw new \RuntimeException('Keine angemeldete Nextcloud-Sitzung gefunden.');
            }

            $userFolder = $this->rootFolder->getUserFolder($user->getUID());
            $node = $userFolder->get(ltrim($path, '/'));
            if ($node instanceof Folder) {
                throw new \InvalidArgumentException('Bitte eine Datei auswählen.');
            }

            if ($fileId !== '' && (string)$node->getId() !== $fileId) {
                throw new \RuntimeException(
                    'Die ausgewählte Datei hat sich zwischenzeitlich geändert oder wurde verschoben.'
                );
            }
            $oldName = (string)$node->getName();
            $candidate = $this->normalizeRenameCandidate($newName, $oldName);
            if ($candidate === $oldName) {
                return new DataResponse([
                    'ok' => true,
                    'changed' => false,
                    'new_name' => $oldName,
                    'new_path' => $path,
                    'file_id' => (string)$node->getId(),
                ]);
            }

            $parent = $node->getParent();
            if ($parent->nodeExists($candidate)) {
                throw new \InvalidArgumentException(
                    'Im Zielordner existiert bereits eine Datei mit diesem Namen.'
                );
            }

            $targetPath = rtrim((string)$parent->getPath(), '/') . '/' . $candidate;
            $moved = $node->move($targetPath);
            $parentPath = dirname($path);
            $publicPath = ($parentPath === '/' || $parentPath === '.')
                ? '/' . $candidate
                : rtrim($parentPath, '/') . '/' . $candidate;

            return new DataResponse([
                'ok' => true,
                'changed' => true,
                'new_name' => (string)$moved->getName(),
                'new_path' => $publicPath,
                'file_id' => (string)$moved->getId(),
            ]);
        } catch (\OCP\Files\NotFoundException $e) {
            return new DataResponse(['error' => 'Die ausgewählte Datei wurde nicht gefunden.'], 404);
        } catch (\InvalidArgumentException $e) {
            return new DataResponse(['error' => $e->getMessage()], 400);
        } catch (\OCP\Files\NotPermittedException $e) {
            return new DataResponse(['error' => 'Die Datei darf nicht umbenannt werden.'], 403);
        } catch (\OCP\Lock\LockedException $e) {
            return new DataResponse(['error' => 'Die Datei ist derzeit gesperrt.'], 423);
        } catch (\RuntimeException $e) {
            return new DataResponse(['error' => $e->getMessage()], 409);
        } catch (\Exception $e) {
            return new DataResponse(['error' => 'Umbenennen fehlgeschlagen.'], 500);
        }
    }
    private function normalizeSelectedPath($path) {
        $path = trim((string)$path);
        if ($path === '' || strlen($path) > 2048 || strpos($path, "\0") !== false) {
            throw new \InvalidArgumentException('Ungültiger Dateipfad.');
        }
        $path = '/' . ltrim($path, '/');
        if (preg_match('#(?:^|/)\.\.(?:/|$)#', $path)) {
            throw new \InvalidArgumentException('Ungültiger Dateipfad.');
        }
        if (substr($path, -1) === '/') {
            throw new \InvalidArgumentException('Bitte eine Datei auswählen.');
        }
        return $path;
    }

    private function normalizeFileId($fileId) {
        $value = trim((string)$fileId);
        if ($value === '') {
            return '';
        }
        if (!preg_match('/^[0-9]{1,24}$/', $value)) {
            throw new \InvalidArgumentException('Ungültige Datei-ID.');
        }
        return $value;
    }
    private function quoteUseReference($path) {
        $escaped = str_replace(['\\', '"'], ['\\\\', '\\"'], (string)$path);
        return '"' . $escaped . '"';
    }

    private function requestSuggestion($reference, $requestId) {
        $prompt = '/use:' . $reference . ' '
            . 'Analysiere ausschließlich dieses Dokument und schlage einen prägnanten, '
            . 'sachlichen Dateinamen vor. Antworte ausschließlich mit dem Dateinamen, '
            . 'ohne Markdown, Anführungszeichen oder Erklärung.';

        return $this->proxy->chat(
            [['role' => 'user', 'content' => $prompt]],
            [],
            '',
            '',
            $requestId
        );
    }

    private function isSuggestionIndexMissingError($value) {
        $text = trim((string)$value);
        return (
            preg_match('/FullTextSearch-Index/ui', $text)
            && (
                preg_match('/in Nextcloud vorhanden/ui', $text)
                || preg_match('/noch nicht im FullTextSearch-Index vorhanden/ui', $text)
                || preg_match('/FullTextSearch-Index muss .* aktualisiert werden/ui', $text)
            )
        );
    }

    private function isSuggestionResolutionError($value) {
        $lines = preg_split('/\r?\n/', trim((string)$value));
        $firstLine = trim((string)($lines[0] ?? ''));
        return (
            $this->isSuggestionIndexMissingError($value)
            || preg_match('/^(?:Die\s+)?Datei(?:\s+`[^`]+`)?\s+wurde nicht gefunden\.?$/ui', $firstLine)
            || preg_match('/^Über \/use konnten keine Dokumente aufgelöst werden\.?$/u', $firstLine)
            || preg_match('/^Die Quellen-Nummer/u', $firstLine)
            || preg_match('/ ist nicht eindeutig\.?$/u', $firstLine)
        );
    }

    private function assertSuggestionResponse($value) {
        if ($this->isSuggestionIndexMissingError($value)) {
            throw new \RuntimeException(
                'Die ausgewählte Datei ist in Nextcloud vorhanden, aber ihre aktuelle Version '
                . 'ist noch nicht im FullTextSearch-Index verfügbar.'
            );
        }
        if ($this->isSuggestionResolutionError($value)) {
            throw new \RuntimeException(
                'Die ausgewählte Datei konnte von SunaQ nicht eindeutig geladen werden.'
            );
        }
    }

    private function normalizeSuggestion($value, $originalName) {
        $value = trim((string)$value);
        if ($value === '') {
            throw new \RuntimeException('SunaQ hat keinen Dateinamen vorgeschlagen.');
        }

        $lines = preg_split('/\r?\n/', $value);
        $candidate = trim((string)($lines[0] ?? ''));
        $candidate = preg_replace('/^\s*(?:dateiname|filename|vorschlag)\s*:\s*/iu', '', $candidate);
        $candidate = trim($candidate, " \t\n\r\0\x0B\"'");
        $candidate = preg_replace('/[<>:"\\\\\/|?*\x00-\x1F]+/u', '-', $candidate);
        $candidate = trim((string)$candidate, " .-");

        $extension = (string)pathinfo((string)$originalName, PATHINFO_EXTENSION);
        if ($extension !== '') {
            $candidate = preg_replace(
                '/\.' . preg_quote($extension, '/') . '$/iu',
                '',
                $candidate
            );
            $candidate = rtrim((string)$candidate, " .-");
            if ($candidate !== '') {
                $candidate .= '.' . $extension;
            }
        }

        if ($candidate === '') {
            throw new \RuntimeException('SunaQ hat keinen verwertbaren Dateinamen vorgeschlagen.');
        }

        if (function_exists('mb_substr')) {
            $candidate = mb_substr($candidate, 0, 220, 'UTF-8');
        } else {
            $candidate = substr($candidate, 0, 220);
        }
        return $candidate;
    }

    private function normalizeRenameCandidate($value, $originalName) {
        $candidate = trim((string)$value);
        if ($candidate === '' || $candidate === '.' || $candidate === '..') {
            throw new \InvalidArgumentException('Der Dateiname darf nicht leer sein.');
        }
        if (preg_match('/[\\\\\/\x00-\x1F]/u', $candidate)) {
            throw new \InvalidArgumentException('Der Dateiname enthält unzulässige Zeichen.');
        }

        $extension = (string)pathinfo((string)$originalName, PATHINFO_EXTENSION);
        if ($extension !== '') {
            $candidate = preg_replace(
                '/\.' . preg_quote($extension, '/') . '$/iu',
                '',
                $candidate
            );
            $candidate = rtrim((string)$candidate, " .");
            if ($candidate === '') {
                throw new \InvalidArgumentException('Der Dateiname darf nicht leer sein.');
            }
            $candidate .= '.' . $extension;
        }

        if (function_exists('mb_strlen')) {
            if (mb_strlen($candidate, 'UTF-8') > 220) {
                throw new \InvalidArgumentException('Der Dateiname ist zu lang.');
            }
        } elseif (strlen($candidate) > 220) {
            throw new \InvalidArgumentException('Der Dateiname ist zu lang.');
        }
        return $candidate;
    }
    private function requestId() {
        try {
            return 'fileaction-' . bin2hex(random_bytes(8));
        } catch (\Exception $e) {
            return 'fileaction-' . str_replace('.', '', uniqid('', true));
        }
    }
}
