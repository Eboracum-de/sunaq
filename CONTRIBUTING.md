# Contributing

Thanks for taking an interest in SunaQ.

Bug reports, reproducible test cases, documentation corrections, operational feedback and feature discussions are especially useful during the public-beta phase.

## Before opening an issue

Please remove or redact:

- customer and personal data,
- document contents that are not public,
- API keys, passwords and provider tokens,
- private certificates and keys,
- internal hostnames/IPs where they are sensitive,
- `runtime.env`, `provider.env` and runtime database contents.

For security vulnerabilities, follow `SECURITY.md` rather than opening a public issue with exploit details.

## Code and documentation contributions

The public project is licensed under **AGPL-3.0-only**. Eboracum GmbH also preserves the option to offer separately negotiated commercial/proprietary licenses.

Contributors keep ownership of their work. The project does **not** require copyright assignment.

A CLA is required only when a contribution is substantial/copyrightable enough that the dual-licensing rights chain matters. The CLA grants Eboracum GmbH additional **non-exclusive** rights, including the ability to sublicense the accepted contribution under separate commercial terms. In return, the CLA expressly commits that accepted contributions remain available in the public upstream project under AGPL-3.0-only.

Practical rule:

1. Issues, bug reports, operational feedback and feature discussions need no CLA.
2. Tiny typo/formatting corrections normally need no CLA.
3. Before investing in a substantial contribution, open an issue so design and licensing expectations are clear.
4. Non-trivial code, tests, prompts or documentation require acceptance of `CLA.md` before merge.
5. If an employer or organization owns the work, an authorized representative must approve the contribution rights.

The acceptance process is documented in `docs/CLA-PROCESS.md`. Do not submit material you do not have the right to contribute.

## Development baseline

Create a clean development environment and run the full test suite:

```bash
python -m pytest
```

See `docs/DEVELOPMENT.md` for repository layout, supported deployment mappings and release checks.

For changes affecting Dockerized deployments, including legacy Super-Light compatibility paths, also check installer/Compose/configuration behavior because deployment regression tests inspect these files directly.

## Design invariants

Changes should preserve these project-level rules unless an architectural change is explicitly discussed:

- Nextcloud is the final document authorization authority.
- Retrieval indexes may propose candidates but may not grant document access.
- ACL-rejected documents do not trigger adaptive fetching/backfill simply to fill context.
- shared identity/alias curation may improve retrieval across users, but it never grants access to the protected source document.
- document evidence shown to a user remains subject to the current Nextcloud authorization boundary.
- local and remote processing boundaries remain administrator-controlled.
- source scope and retrieval-engine selection remain separate concepts.
- graph observations are not silently promoted into global facts.
- archive copies have their own explicit lifecycle; chat/web retention must not be mistaken for source-document revocation.
- runtime secrets and deployment state stay outside source control.

Security-sensitive changes should include or update an adversarial regression case and remain consistent with `docs/THREAT-MODEL.md`.

## Pull requests

Keep functional changes focused where practical. A useful pull request includes:

- what problem is being solved,
- deployment/profile impact,
- tests added or updated,
- any configuration or migration impact,
- confirmation that no secrets or private corpus data are included.

See `.github/pull_request_template.md`.
