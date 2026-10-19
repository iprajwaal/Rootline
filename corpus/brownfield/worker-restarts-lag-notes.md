ShopLite orders platform

# Consumer lag on the orders queue after worker restarts

Detailed root cause analysis

ShopLite Commerce

## Summary

| Field | Value |
| --- | --- |
| RCA ID | TBD |
| Date | 2026-04-10 |
| Short description | A memory leak in orders-worker 2.7 made workers restart every 20 to 30 minutes. Each restart forced a consumer group rebalance, and lag on the orders topic grew to 18,000 messages. |
| Authors |  |
| Stakeholders |  |
| Status | Final |
| Issue environment | GCP, production, europe-west1, GKE cluster shop-prod |
| Incident start time | 2026-04-08 12:14 UTC |
| Incident duration | 48 minutes |
| Affected services or applications | orders-worker, order-queue |
| Severity level | Medium |
| Trigger | Alert OrderQueueConsumerLagHigh |
| Related SOPs | None |

## Problem statement

Consumer lag on the orders topic grew to 18,000 messages. orders-worker pods were killed with "OOMKilled (exit code 137)" every 20 to 30 minutes. Scaling workers did not help, because the new workers also ran out of memory and every restart paused all consumers.

## Impact

### Description of impact

TBD

### Key metrics affected

TBD

### Customer feedback

TBD

## Immediate resolution

Rolled orders-worker back from 2.7 to 2.6.

## Event timeline

| Timestamp | Event description | Action taken | Responsibility |
| --- | --- | --- | --- |
| 2026-04-08 10:30 | orders-worker 2.7 released. | TBD | Orders team |
| 2026-04-08 11:05 | First worker restart with OOMKilled (exit code 137). | TBD There was no alert on worker restarts. | SRE on-call |
| 2026-04-08 12:14 | Alert OrderQueueConsumerLagHigh fired with lag at 12,500. | On-call followed SOP-QUEUE-002 and scaled workers to 4. | SRE on-call |
| 2026-04-08 12:41 | Lag still growing. On-call saw the restart count. | Rolled orders-worker back to 2.6. | SRE on-call |
| 2026-04-08 13:02 | Lag back under 1,000. | Closed the incident. | SRE on-call |

## Analysis

### 5-whys analysis

### Fishbone analysis

## Root causes

Based on the analysis above, the root causes of the incident are:

1. orders-worker 2.7 leaked memory through an order cache that was never cleared.
2. The lag procedure scaled workers without first checking that they were healthy.

## Action items

The following action items address the root causes above.

TBD
