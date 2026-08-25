# Open-Source Publication Guidelines

HarborRAG documentation should let the community understand, run, evaluate, extend, and safely
operate the code that is actually in the repository. It should not expose private operational
context or turn internal plans into public promises.

## Publish for the community

- Implemented capabilities, stable contracts, architecture invariants, and known limitations.
- Reproducible installation, local development, configuration, testing, and troubleshooting steps.
- Sanitized examples using fictional tenants, sources, users, URLs, and document content.
- Package ownership, supported extension points, compatibility expectations, and migration notes.
- Security boundaries, required operator controls, and coordinated disclosure instructions.
- Contribution standards, quality gates, issue templates, and review expectations.
- Benchmarks only when the workload, hardware, configuration, dataset origin, and method are
  reproducible and safe to redistribute.

## Do not publish

- Credentials, tokens, cookies, private keys, connection strings, secret names that reveal private
  infrastructure, or production environment files.
- Customer, employee, or partner data; real document excerpts; private prompts; support logs; or
  screenshots containing identifying content.
- Internal hostnames, IP addresses, account identifiers, network diagrams, asset inventories,
  incident records, or production topology details that are not required to use the project.
- Unreleased vulnerability details or exploit instructions before a coordinated fix and advisory.
- Proprietary source material, licensed assets without redistribution rights, internal research,
  commercial strategy, fundraising information, or confidential roadmaps.
- Unsupported performance, security, adoption, compatibility, or production-readiness claims.

## Review carefully

Some information can be useful publicly but needs deliberate review:

- Security architecture should explain trust boundaries and defensive contracts without exposing
  live targets or secrets.
- Deployment examples should use placeholders and least privilege, not a copy of a company
  environment.
- Roadmaps should describe direction, not guaranteed dates or commitments.
- Third-party names and marks should be factual, attributed, and used within their licenses.
- Generated output, fixtures, and telemetry samples should be treated as data, not harmless code.

## Pull-request checklist

Before publishing documentation, examples, fixtures, or website content:

- [ ] Every claim matches code or is clearly labeled as a limitation or future direction.
- [ ] Commands work from a clean checkout with documented prerequisites.
- [ ] Examples use synthetic names and data.
- [ ] No secret, personal, customer, partner, internal-host, or incident data is present.
- [ ] Assets and quoted material have redistribution permission.
- [ ] Security-sensitive changes follow [SECURITY.md](../../SECURITY.md).
- [ ] New pages are linked from the public table of contents and pass the website link check.
- [ ] Temporary research and private reference material are not part of the commit.

When uncertain, leave the material out of the public change and ask a maintainer or security owner
for review. Removing unnecessary detail is safer than attempting to redact a sensitive artifact in
place.
