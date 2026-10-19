---
id: REF-K8S-003
type: ref
title: Debug a pod that keeps crashing
publisher: Kubernetes documentation
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/
licence: CC BY 4.0
attribution: Copyright The Kubernetes Authors.
changes: Shortened to logs, describe and debug copies, and reworded.
retrieved: "2026-10-19"
topics: [crash loop, CrashLoopBackOff, pod logs, kubectl debug, restarts]
---

## Read the logs

Look at the logs of the affected container:

```
kubectl logs <pod-name> -c <container-name>
```

If the container crashed before, read the log of the previous run:

```
kubectl logs <pod-name> -c <container-name> --previous
```

## Describe the pod

`kubectl describe pod <pod-name>` shows each container's state (Waiting, Running or Terminated),
whether it is ready, its restart count and recent events. A rising restart count points to a crash
loop when the restart policy is `Always`. A crashing container looks like this:

```
State:          Waiting
  Reason:       CrashLoopBackOff
Last State:     Terminated
  Reason:       Error
  Exit Code:    1
```

The events at the end show which component reported what, with a reason and a message.

## Debug with a copy of the pod

If the image has no shell, or the app crashes on start, you cannot use `kubectl exec`. Make a copy
of the pod with a different command instead:

```
kubectl debug <pod-name> -it --copy-to=<pod-name>-debug --container=<container-name> -- sh
```

Name the container with `--container`, otherwise `kubectl debug` adds a new container. Delete the
copy when you are done.
