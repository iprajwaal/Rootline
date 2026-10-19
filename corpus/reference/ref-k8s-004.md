---
id: REF-K8S-004
type: ref
title: Roll back a Kubernetes Deployment
publisher: Kubernetes documentation
source_url: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment
licence: CC BY 4.0
attribution: Copyright The Kubernetes Authors.
changes: Shortened to stuck rollouts, rollout history and rollback, and reworded.
retrieved: "2026-10-19"
topics: [rollback, deployment, rollout history, crash loop, bad deploy]
---

## When to roll back

Roll back when a Deployment is not stable, for example when it is crash looping. Rollout history is
kept so you can roll back at any time. `.spec.revisionHistoryLimit` sets how many old revisions are
kept (default 10). Setting it to 0 removes the history, and then the Deployment cannot be rolled
back.

A new revision is only created when the pod template (`.spec.template`) changes, such as a new
image or labels. Scaling does not create a revision. A rollback only restores the pod template.

## Spot a stuck rollout

```
kubectl rollout status deployment/<name>
```

A stuck rollout keeps printing `Waiting for rollout to finish: 1 out of 3 new replicas have been
updated...`. `kubectl get pods` shows the new pods failing, for example in `ImagePullBackOff`. The
Deployment controller stops a bad rollout by itself based on `maxUnavailable` (25% by default).

## Check the rollout history

```
kubectl rollout history deployment/<name>
kubectl rollout history deployment/<name> --revision=2
```

The `CHANGE-CAUSE` column comes from the `kubernetes.io/change-cause` annotation.

## Roll back

Roll back to the previous revision:

```
kubectl rollout undo deployment/<name>
```

Or roll back to a specific revision:

```
kubectl rollout undo deployment/<name> --to-revision=2
```

The Deployment controller records a `DeploymentRollback` event.

## Confirm

```
kubectl get deployment <name>
kubectl describe deployment <name>
```
