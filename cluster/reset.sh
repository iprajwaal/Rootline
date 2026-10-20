#!/usr/bin/env bash
# Puts the real shop back to its healthy state, whatever fault was injected.
set -euo pipefail
k() { kubectl --context kind-shop -n shop "$@"; }
k scale deploy/reports --replicas=0
k set env deploy/traffic EVERY_SECONDS-
k scale deploy/traffic --replicas=1
k scale deploy/orders-worker --replicas=1
# The faults and their fixes set these on the deployment; without them the config map applies.
k set env deploy/orders-api DB_POOL_MAX- PAYMENT_GATEWAY_URL-
k rollout status deploy/orders-api --timeout=180s
k exec deploy/orders-api -- sh -c 'rm -f /var/lib/shoplite/tmp/junk-*'
k exec statefulset/orders-db -- psql -U shop -d orders \
  -c "alter system reset default_transaction_read_only" -c "select pg_reload_conf()" >/dev/null
k rollout restart statefulset/cache
k rollout status statefulset/cache --timeout=120s
# Done only when health says so, so every rehearsal starts from the same state.
for _ in $(seq 1 30); do
  curl -sf localhost:30080/health >/dev/null && exit 0
  sleep 2
done
echo "the shop is not healthy after the reset" >&2
exit 1
