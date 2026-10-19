---
id: REF-GCP-001
type: ref
title: Troubleshoot permission denied errors in Google Cloud IAM
publisher: Google Cloud documentation
source_url: https://docs.cloud.google.com/iam/docs/troubleshooting-access
licence: CC BY 4.0
attribution: Google Cloud documentation. Code samples on the source page are Apache 2.0.
changes: Shortened to the troubleshooting steps and reworded.
retrieved: "2026-10-19"
topics: [permission denied, 403, IAM, service account, access]
---

## Steps

1. Find the principal, the resource and the permission. A permission error message with an error
   ID gives all three. Otherwise, collect the principal (user, service account or agent identity),
   the full resource name and the permission yourself.
2. Make sure you can view the policies that apply. Without that access, results for those policies
   show as `Unknown`.
3. Check the allow policies on the resource and on its parent project, folder and organization.
   Look for a role binding that includes both the permission and the principal, and check that any
   conditions on it are met.
4. Check the deny policies on the resource and its ancestors. Look for a deny rule that covers the
   permission and the principal, and whether an exception applies.
5. Check any Principal Access Boundary policy bound to the principal, in case it excludes the
   resource.
6. Run Policy Troubleshooter from the console, the gcloud CLI or the REST API. For conditional
   bindings or deny rules, give it the request context, or start from an audit log entry.

Access is only granted when every policy type allows it.
