---
id: REF-K8S-001
type: ref
title: Node-pressure eviction when a node runs low on disk
publisher: Kubernetes documentation
source_url: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
licence: CC BY 4.0
attribution: Copyright The Kubernetes Authors.
changes: Shortened to the parts about disk pressure and reworded.
retrieved: "2026-10-19"
topics: [disk full, disk pressure, eviction, kubelet, inodes]
---

## Summary

The kubelet watches memory, disk space and filesystem inodes on each node. When one of them
crosses a threshold, the kubelet first tries to free space on the node. If that is not enough, it
fails and terminates pods to reclaim the resource. Pods managed by a Deployment or StatefulSet are
replaced by the control plane.

## Disk signals

- `nodefs.available`: free space on the node's root filesystem.
- `nodefs.inodesFree`: free inodes on the root filesystem (Linux only).
- `imagefs.available`: free space on the filesystem that holds container images.

## Default hard thresholds

- `memory.available<100Mi` on Linux nodes
- `nodefs.available<10%`
- `imagefs.available<15%`
- `nodefs.inodesFree<5%` on Linux nodes
- `imagefs.inodesFree<5%` on Linux nodes

Hard thresholds have no grace period, so the pod is stopped at once. If you change one threshold,
the others are not kept at their defaults unless `MergeDefaultEvictionSettings` is set to true.

## DiskPressure condition

The kubelet reports the `DiskPressure` node condition when free disk space or inodes on the root,
image or container filesystem cross an eviction threshold. Conditions update every 10 seconds by
default. `eviction-pressure-transition-period` (default 5 minutes) stops the condition from
flapping.

## What the kubelet does before evicting

When `DiskPressure` is reported, the kubelet reclaims node-level disk first. Without a separate
image filesystem it garbage collects dead pods and containers, then deletes unused images. It only
evicts pods if this does not bring the signal back below the threshold.

## Which pods are evicted first

When the root filesystem triggers eviction, the kubelet ranks pods by total disk use: local
volumes, logs and the writable layer of all their containers. QoS class does not apply to
ephemeral storage.

## Useful commands

- `kubectl describe node <node-name>` shows the node conditions, including `DiskPressure`.
- `kubectl get events --field-selector reason=Evicted` lists recent evictions.
