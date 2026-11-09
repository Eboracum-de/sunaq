<?php
namespace OCA\Sunaq\Sections;

use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

class AdminSection implements IIconSection {
    /** @var IURLGenerator */
    private $urlGenerator;

    public function __construct(IURLGenerator $urlGenerator) {
        $this->urlGenerator = $urlGenerator;
    }

    public function getID() {
        return 'sunaq';
    }

    public function getName() {
        return 'SunaQ';
    }

    public function getPriority() {
        return 50;
    }

    public function getIcon() {
        return $this->urlGenerator->imagePath('sunaq', 'app-dark.svg');
    }
}
