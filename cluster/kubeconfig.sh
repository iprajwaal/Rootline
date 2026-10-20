#!/usr/bin/env bash
# Writes the kubeconfig Rootline's container uses: a reader and a fixer context, each with
# its own account's token, and the cluster's API as seen from inside Docker. It holds tokens,
# so it is not committed.
set -euo pipefail
k() { kubectl --context kind-shop -n shop "$@"; }
token() {
  until k get secret "$1" -o jsonpath='{.data.token}' 2>/dev/null | grep -q .; do sleep 1; done
  k get secret "$1" -o jsonpath='{.data.token}' | base64 -d
}
reader=$(token rootline-reader-token)
fixer=$(token rootline-fixer-token)
ca=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="kind-shop")].cluster.certificate-authority-data}')
mkdir -p cluster/.kube
umask 077
cat > cluster/.kube/config <<CONFIG
apiVersion: v1
kind: Config
clusters:
  - name: shop
    cluster: {server: "https://host.docker.internal:16443", certificate-authority-data: $ca}
users:
  - name: reader
    user: {token: $reader}
  - name: fixer
    user: {token: $fixer}
contexts:
  - name: reader
    context: {cluster: shop, user: reader, namespace: shop}
  - name: fixer
    context: {cluster: shop, user: fixer, namespace: shop}
current-context: reader
CONFIG
echo "wrote cluster/.kube/config with reader and fixer contexts"
