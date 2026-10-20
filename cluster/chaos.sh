#!/usr/bin/env bash
# Breaks the real shop the way production breaks. Each fault is fixed by its SOP's command.
set -euo pipefail
k() { kubectl --context kind-shop -n shop "$@"; }
case "${1:-}" in
  db_pool_exhausted)
    # A small pool and six slow sales reports holding connections: checkout waits 30 s for
    # a connection, then fails with PoolTimeout.
    k set env deploy/orders-api DB_POOL_MAX=4
    k rollout status deploy/orders-api --timeout=120s
    k scale deploy/reports --replicas=6
    ;;
  disk_full)
    # Old junk files fill the receipts volume to the last byte, so new receipts cannot be
    # written. They are dated two days back, as forgotten temp files would be.
    k exec deploy/orders-api -- sh -c '
      i=0
      while head -c 1048576 /dev/zero > /var/lib/shoplite/tmp/junk-$i.pdf 2>/dev/null; do
        i=$((i + 1))
      done
      touch -d "2 days ago" /var/lib/shoplite/tmp/junk-*'
    ;;
  cache_down)
    # Redis stops listening where the service expects it and drops every client. They get
    # "Connection refused" until a restart brings it back on its normal port.
    k exec statefulset/cache -- sh -c 'printf "CONFIG SET port 6380\nCLIENT KILL TYPE normal\n" | redis-cli'

    ;;
  queue_consumer_lag)
    # A flash sale: four times the shoppers, each ordering faster. One worker cannot keep up,
    # so orders wait in the queue.
    k set env deploy/traffic EVERY_SECONDS=0.1
    k scale deploy/traffic --replicas=4
    ;;
  bad_config_deploy)
    # A deploy that loses the payment gateway URL: the new pod exits at start, again and again.
    k set env deploy/orders-api PAYMENT_GATEWAY_URL=
    ;;
  *)
    echo "usage: cluster/chaos.sh db_pool_exhausted|disk_full|cache_down|queue_consumer_lag|bad_config_deploy" >&2
    exit 2
    ;;
esac
