# Security policy

## Supported versions

While the project is on 0.x, only the **latest minor release** receives security fixes.
The policy for 1.0 and later will be published here before 1.0 ships.

| Version | Supported |
|---|---|
| latest 0.x minor | yes |
| older 0.x | no, please upgrade |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for security problems.**

Report privately through GitHub Security Advisories:
<https://github.com/Uttsavv/even-simpler-salesforce/security/advisories/new>

Please include:

- the affected version(s) and the client in use (`AsyncSalesforce` / `Salesforce`);
- a description of the issue and its impact (e.g. token leak, log redaction bypass,
  SOQL injection through a helper, SSRF via a crafted `nextRecordsUrl`/cursor);
- a minimal reproduction. **Never send real tokens, session IDs, client secrets,
  private keys or customer data.** Use placeholders or a throwaway Developer Edition org.

## What to expect

This project has a single maintainer (Utsav Singh), so timelines are best effort:

- acknowledgement within **7 days**;
- an initial assessment and, if confirmed, a fix plan within **30 days**;
- coordinated disclosure: a GitHub Security Advisory (with a CVE where appropriate) is
  published together with the fixed release, and the reporter is credited unless they
  prefer otherwise.

## Scope

In scope: the `even-simpler-salesforce` package and this repository's release tooling
(CI workflows, release scripts).

Out of scope: vulnerabilities in Salesforce itself (report those to
[Salesforce Security](https://security.salesforce.com/)), and in third-party
dependencies unless this library uses them in an unsafe way.

## Handling secrets in this project

- Access tokens, refresh tokens and client secrets are redacted from every log event and
  from `ESF_DEBUG=1` output.
- Recorded test fixtures are redacted by the recorder; CI and a pre-commit hook reject
  anything matching the Salesforce session-ID pattern `00D…!…`.
