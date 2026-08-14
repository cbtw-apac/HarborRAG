# Security Policy

## Supported releases

HarborRAG 2.x is the current release line. The project is in Alpha: maintainers review security
reports on a best-effort basis, and the repository does not yet promise a response or remediation
service-level agreement. Users are responsible for assessing each release and hardening their own
deployment.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability or include sensitive reproduction data in
public discussions. Use GitHub's
[private vulnerability reporting](https://github.com/cbtw-apac/HarborRAG/security/advisories/new)
to contact the maintainers.

Include the affected version, component, impact, prerequisites, a minimal reproduction, and any
suggested mitigation. Use synthetic data and remove credentials, customer content, internal
hostnames, and production logs.

Maintainers will use the private advisory to validate the report, coordinate a fix, and publish an
advisory when appropriate. Please allow that process to complete before public disclosure.

## Deployment boundary

The repository provides application components, local development compositions, and security
contracts such as tenant-aware access context. It does not provide a complete production security
perimeter. Operators must supply and validate internet-facing identity, authorization, TLS, network
policy, secret delivery, backups, monitoring, resource controls, and incident response.

General bugs and feature requests belong in the public
[issue tracker](https://github.com/cbtw-apac/HarborRAG/issues).
