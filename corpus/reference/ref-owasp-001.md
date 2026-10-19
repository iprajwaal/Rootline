---
id: REF-OWASP-001
type: ref
title: Rotating, expiring and revoking secrets
publisher: OWASP Cheat Sheet Series, Secrets Management Cheat Sheet
source_url: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
licence: CC BY-SA 4.0
attribution: OWASP Cheat Sheet Series contributors. This adaptation is shared under CC BY-SA 4.0.
changes: Shortened to rotation, expiry, revocation and leaks, and reworded.
retrieved: "2026-10-19"
topics: [api key, secret rotation, expired key, revocation, credentials]
---

## Rotation

Rotate secrets regularly, so that stolen credentials only work for a short time. Regular rotation
also discourages reusing credentials. Automate rotation of static secrets where you can. User
passwords are the exception: rotate them only when there is reason to think they were compromised.

## Expiry

Create secrets that expire after a set time where possible. Applications should check that a secret
is still active before they trust it. For dynamic secrets, use a short lease and revoke them when
they are no longer needed.

## Revocation

Revoke secrets that are no longer needed or may be compromised. Revoke exposed keys immediately.
Restarting an application does not revoke stolen credentials: they keep working until the backing
service expires or revokes them. Log attempts to use a revoked secret.

## Do not leak secrets

- Never write secrets to logs. Mask them, and remove any that end up in logs.
- Pipeline output must not show secrets.
- Avoid passing secrets in environment variables where possible, because they can end up in logs
  or system dumps.
