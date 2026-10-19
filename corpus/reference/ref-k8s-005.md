---
id: REF-K8S-005
type: ref
title: Container killed for going over its memory limit (OOMKilled)
publisher: Kubernetes documentation
source_url: https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/
licence: CC BY 4.0
attribution: Copyright The Kubernetes Authors.
changes: Shortened to memory limits and OOM kills, and reworded.
retrieved: "2026-10-19"
topics: [out of memory, OOMKilled, exit code 137, memory limit, restarts]
---

## Summary

A container can use more than its memory request if the node has memory free, but not more than
its memory limit. If it allocates more than its limit, it becomes a candidate for termination. If it
keeps using memory beyond the limit, it is terminated. If the container can be restarted, the
kubelet restarts it like any other failure, so a leaking container is killed and restarted again
and again.

Memory limits are enforced by the kernel's out of memory (OOM) killer, and only when the kernel
detects memory pressure. A container may briefly use more than its limit before it is killed.

## What it looks like

`kubectl get pod` shows the status and the restart count going up:

```
NAME          READY   STATUS      RESTARTS   AGE
memory-demo   0/1     OOMKilled   1          24s
```

`kubectl get pod <pod-name> --output=yaml` shows why the last run ended:

```yaml
lastState:
  terminated:
    exitCode: 137
    reason: OOMKilled
```

## Set requests and limits

```yaml
resources:
  requests:
    memory: "256Mi"
  limits:
    memory: "512Mi"
```

If a container sets a limit but no request, Kubernetes uses the limit as the request.
