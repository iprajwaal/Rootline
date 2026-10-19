---
id: REF-K8S-002
type: ref
title: Local ephemeral storage limits for containers
publisher: Kubernetes documentation
source_url: https://kubernetes.io/docs/concepts/storage/ephemeral-storage/
licence: CC BY 4.0
attribution: Copyright The Kubernetes Authors.
changes: Shortened, reworded, and the example reduced to one container.
retrieved: "2026-10-19"
topics: [disk full, ephemeral storage, emptyDir, eviction, limits]
---

## Summary

Nodes have local ephemeral storage on attached disks or sometimes in RAM. Pods use it for scratch
space, caching and logs. There is no long-term durability guarantee: if a node fails, data in its
ephemeral storage can be lost.

## What counts as ephemeral storage

- The container's writable layer, and container images.
- Local `emptyDir` volumes, except tmpfs `emptyDir` volumes, which count as memory.
- The pod's own logs, usually under `/var/log/pods`.
- System files mapped into the pod, such as `/etc/hosts`.

## Requests and limits

Each container can set `spec.containers[].resources.requests.ephemeral-storage` and
`spec.containers[].resources.limits.ephemeral-storage`. Values are byte quantities such as `2Gi`
or `500Mi`. Watch the case of the suffix: `400m` means 0.4 bytes.

```yaml
resources:
  requests:
    ephemeral-storage: "2Gi"
  limits:
    ephemeral-storage: "4Gi"
volumes:
  - name: scratch
    emptyDir:
      sizeLimit: 500Mi
```

## When a pod uses too much

If a pod uses more ephemeral storage than allowed, the kubelet sets an eviction signal that
triggers pod eviction. With container-level isolation, a container whose writable layer and logs go
over its limit gets its pod marked for eviction. With pod-level isolation, the kubelet compares
usage against the sum of the container limits.

## Notes

- A namespace quota on ephemeral storage only applies to pods that set a limit.
- The scheduler only places a pod where the sum of its containers' ephemeral storage requests is
  less than the node's ephemeral storage capacity.
