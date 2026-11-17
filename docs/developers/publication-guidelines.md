# Open-Source Publication Guidelines

This page tells contributors what may go into HarborRAG's public documentation, examples,
fixtures and website, and what must stay private. Everything under `docs/`, the package
READMEs, and the root `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` and `SECURITY.md` are
public: they publish to the documentation site, and each package README is also its PyPI page.

Public documentation should let the community understand, run, evaluate, extend and safely
operate the code that is actually in the repository. It must not expose private operational
context or turn internal plans into public promises.

## Publish for the community

- Implemented capabilities, stable contracts, architecture invariants and known limitations.
- Reproducible installation, local development, configuration, testing and troubleshooting
  steps.
- Sanitized examples that use fictional tenants, sources, users, URLs and document content.
- Package ownership, supported extension points, compatibility expectations and migration
  notes.
- Security boundaries, required operator controls and coordinated disclosure instructions.
- Contribution standards, quality gates, issue templates and review expectations.
- Benchmarks, but only when the workload, hardware, configuration, dataset origin and method
  are reproducible and safe to redistribute.

## Do not publish

- Credentials, tokens, cookies, private keys, connection strings, secret names that reveal
  private infrastructure, or production environment files.
- Customer, employee or partner data, real document excerpts, private prompts, support logs,
  or screenshots that contain identifying content.
- Internal hostnames, IP addresses, account identifiers, network diagrams, asset inventories,
  incident records, or production topology details that are not needed to use the project.
- Unreleased vulnerability details or exploit instructions before a coordinated fix and
  advisory.
- Proprietary source material, licensed assets without redistribution rights, internal
  research, commercial strategy, fundraising information or confidential roadmaps.
- Unsupported claims about performance, security, adoption, compatibility or production
  readiness.

## Review carefully

Some information is useful in public but needs a deliberate review:

- Security architecture should explain trust boundaries and defensive contracts without
  exposing live targets or secrets.
- Deployment examples should use placeholders and least privilege, not a copy of a company
  environment.
- Roadmaps should describe direction, not guaranteed dates or commitments.
- Third-party names and marks should be factual, attributed and used within their licenses.
- Generated output, fixtures and telemetry samples are data, not harmless code: review them
  like any other content.

## Automated checks

Two scripts catch a narrow set of problems. `website/check_publication.py` rejects references
to private architecture material and runs in the quality-gates workflow on every pull request.
`website/check_branding.py` rejects stale predecessor branding and runs when the documentation
site is built. Neither one looks for personal data, credentials or internal names; those
depend on review. How to run the documentation checks locally is in
[Documentation changes](../../CONTRIBUTING.md#documentation-changes).

## Pull-request checklist

Before you publish documentation, examples, fixtures or website content:

- [ ] Every claim matches the code, or is clearly labelled as a limitation or future direction.
- [ ] Commands work from a clean checkout with the documented prerequisites.
- [ ] Examples use synthetic names and data.
- [ ] No secret, personal, customer, partner, internal-host or incident data is present.
- [ ] Assets and quoted material have redistribution permission.
- [ ] Security-sensitive changes follow [SECURITY.md](../../SECURITY.md).
- [ ] New pages are listed in `docs/TOC.md` and pass the website link check.
- [ ] Temporary research and private reference material are not part of the commit.

When in doubt, leave the material out of the public change and ask a maintainer or security
owner to review it. Removing unnecessary detail is safer than trying to redact a sensitive
artifact in place.
