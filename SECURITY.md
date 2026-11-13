# Security Policy

Report a suspected vulnerability in HarborRAG privately, and learn which releases are covered
and which security controls a deployment must supply itself.

## Supported releases

HarborRAG 2.x is the current release line. The project is in alpha: maintainers review
security reports on a best-effort basis, and the repository does not yet promise a response
or remediation service-level agreement. Users are responsible for assessing each release and
hardening their own deployment.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability, and do not put sensitive
reproduction data in public discussions. Use GitHub's
[private vulnerability reporting](https://github.com/cbtw-apac/HarborRAG/security/advisories/new)
to contact the maintainers.

Include the affected version, component, impact, prerequisites, a minimal reproduction and
any suggested mitigation. Use synthetic data, and remove credentials, customer content,
internal hostnames and production logs.

Maintainers use the private advisory to validate the report, coordinate a fix and publish an
advisory when appropriate. Please let that process finish before any public disclosure.

## Deployment boundary

The repository provides application components, local development compositions and security
contracts such as a tenant-aware access context. It does not provide a complete production
security perimeter. Operators must supply and validate internet-facing identity,
authorization, TLS, network policy, secret delivery, backups, monitoring, resource controls
and incident response; see
[What a production deployment still needs](docs/developers/deployment/README.md#what-a-production-deployment-still-needs).

General bugs and feature requests belong in the public
[issue tracker](https://github.com/cbtw-apac/HarborRAG/issues).
