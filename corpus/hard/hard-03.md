# Runbook: payment API key

On the first of May at midnight the key we use for the payment provider simply expired. Every card payment came back with a 401 for almost an hour. The renewal email had gone to Diego's old mailbox and he left in March, so nobody saw it. Ana got paged at half past midnight, found the expiry date with shopctl, and rotated the key once the payments on-call gave her the permission. About 1,900 checkouts failed in that window.
