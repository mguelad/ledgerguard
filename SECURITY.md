# Security

Do not post secrets, customer records or exploitable details in a public issue. [Report a vulnerability privately through GitHub](https://github.com/mguelad/ledgerguard/security/advisories/new). Report affected versions, a minimal synthetic reproduction and the suspected boundary. Use opaque tenant/resource identifiers only where essential.

Triage suspected cross-tenant access, credential exposure or unauthorized source mutation as SEV-1. Follow [incident response](docs/runbooks/security-incident.md). Disable affected intake and alerts, preserve safe evidence, rotate impacted credentials and establish scope before restoring access.

Supported application line: 0.1.x during pilot verification. This source has not received an independent penetration test. ASVS Level 2 and penetration testing are release gates, not certifications implied by this repository.
