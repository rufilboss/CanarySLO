#!/usr/bin/env bash
set -euo pipefail

namespace="${NAMESPACE:-canary-smoke}"
prometheus_url="${PROMETHEUS_URL:-http://prometheus.monitoring.svc.cluster.local:9090}"
operator_pid=""

cleanup() {
    if [[ -n "$operator_pid" ]]; then
        kill "$operator_pid" 2>/dev/null || true
        wait "$operator_pid" 2>/dev/null || true
    fi
    kubectl delete namespace "$namespace" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

for command_name in kubectl kopf; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing required command: $command_name" >&2
        exit 1
    fi
done

kubectl apply -f crds/canary-crd.yaml
kubectl create namespace "$namespace" 2>/dev/null || true
kubectl -n "$namespace" create deployment payments \
    --image=nginx:1.25-alpine \
    --replicas=2 \
    --dry-run=client -o yaml \
    | kubectl apply -f -
kubectl -n "$namespace" label deployment payments app.kubernetes.io/name=payments --overwrite
kubectl -n "$namespace" expose deployment payments --port=80 --name=payments

kopf run --standalone --all-namespaces canary_operator.py >"${TMPDIR:-/tmp}/canary-operator-smoke.log" 2>&1 &
operator_pid=$!

sed "s/namespace: default/namespace: ${namespace}/; s#prometheusUrl:.*#prometheusUrl: \"${prometheus_url}\"#" \
    examples/sample-canary.yaml \
    | kubectl apply -f -

for attempt in {1..30}; do
    if kubectl -n "$namespace" get deployment payments-canary >/dev/null 2>&1; then
        kubectl -n "$namespace" get deployment payments-canary
        kubectl -n "$namespace" get service payments payments-canary
        echo "Smoke test passed: canary workload and Services were reconciled."
        exit 0
    fi
    sleep 2
done

echo "Smoke test failed: canary Deployment was not created." >&2
cat "${TMPDIR:-/tmp}/canary-operator-smoke.log" >&2
exit 1
