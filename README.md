# CanarySLO Operator

An SLO-driven Kubernetes operator written in Python with Kopf, the Kubernetes client, and HTTPX. It creates an isolated canary Deployment, evaluates Prometheus RED metrics, progressively changes traffic, and rolls back or promotes a release.

This is a learning project I decided to work on; targeting Kubernetes, Prometheus, and the NGINX Ingress Controller.

## Behavior

For a `CanaryDeployment`, the operator:

1. Reads an existing stable Deployment.
2. Creates `<targetDeployment>-canary` with `canaryImage`.
3. Creates stable and canary Services with isolated selectors.
4. Creates an NGINX canary Ingress with the current weight.
5. Queries Prometheus for error rate and p99 latency.
6. Advances through `rolloutSteps` when metrics are healthy.
7. Routes traffic back to stable and scales the canary to zero after an SLO breach.
8. Promotes the canary image to stable and deletes temporary resources at 100%.

State is persisted in the custom resource status for restart recovery.

## Architecture

Canary Pods use isolated labels:

```yaml
devsecops.io/track: canary
devsecops.io/canarydeployment: <resource-name>
```

Traffic splitting uses NGINX annotations such as `nginx.ingress.kubernetes.io/canary-weight`.

## Repository Layout

```text
canary_operator.py          Controller
crds/canary-crd.yaml        CRD
examples/sample-canary.yaml Example resource
k8s/rbac.yaml               RBAC
tests/test_operator.py      Unit tests with mocked APIs
scripts/smoke-test.sh       Minikube/Kind smoke test
.github/workflows/ci.yml    CI checks
Dockerfile                  Container image
```

## Prerequisites

- Python 3.12+
- Docker
- `kubectl`
- Minikube or Kind
- Helm for live Prometheus and NGINX tests

## Install Development Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

## Automated Checks

```bash
python -m pytest -q
python -m mypy canary_operator.py
python -m py_compile canary_operator.py
bash -n scripts/smoke-test.sh
git diff --check
```

<!-- Validate manifests:

```bash
python - <<'PY'
from pathlib import Path
import yaml

for path in [Path("crds/canary-crd.yaml"), Path("examples/sample-canary.yaml"), Path("k8s/rbac.yaml")]:
		with path.open() as stream:
				list(yaml.safe_load_all(stream))
		print(f"valid YAML: {path}")
PY
``` -->

Build the image:

```bash
docker build --tag canary-operator:test .
```

These checks are also run by [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Start Kubernetes

### Minikube

```bash
minikube start --driver=docker --wait=all --wait-timeout=120s
kubectl --request-timeout=5s get nodes
```

If the profile has stopped control-plane components, recreate the disposable profile:

```bash
minikube delete -p minikube
minikube start -p minikube --driver=docker --wait=all --wait-timeout=120s
```

### Kind

```bash
kind create cluster --name canary-slo
kubectl config use-context kind-canary-slo
kubectl --request-timeout=5s get nodes
```

## Install Prometheus

Unit tests mock Prometheus. The smoke test checks Kubernetes reconciliation and does not require Prometheus. For live SLO tests:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
	--namespace monitoring --create-namespace \
	--set grafana.enabled=false --wait --timeout 5m
```

Verify the server:

```bash
kubectl -n monitoring get pods
kubectl -n monitoring get svc | grep prometheus
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
curl -fsS http://127.0.0.1:9090/-/ready
curl -G -fsS http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=up'
```

Use the actual service name shown by `kubectl -n monitoring get svc` in the `prometheusUrl` field.

## Run the Operator

```bash
kubectl apply -f crds/canary-crd.yaml
kubectl apply -f k8s/rbac.yaml
kopf run --standalone --all-namespaces --verbose canary_operator.py
```

Local Kopf uses the active kubeconfig. In-cluster deployment uses the ServiceAccount from `k8s/rbac.yaml`.

## Apply a Canary Manually

Create a stable workload and Service:

```bash
kubectl create namespace canary-demo
kubectl -n canary-demo create deployment payments --image=nginx:1.25-alpine --replicas=2
kubectl -n canary-demo label deployment payments app.kubernetes.io/name=payments --overwrite
kubectl -n canary-demo expose deployment payments --name=payments --port=80
```

Apply the sample with local substitutions:

```bash
sed \
	-e 's/namespace: default/namespace: canary-demo/' \
	-e 's/targetDeployment: auth-service/targetDeployment: payments/' \
	-e 's/serviceName: auth-service/serviceName: payments/' \
	-e 's#prometheusUrl:.*#prometheusUrl: "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090"#' \
	examples/sample-canary.yaml | kubectl apply -f -
```

Inspect reconciliation:

```bash
kubectl -n canary-demo get canarydeployment
kubectl -n canary-demo get deployment,service,ingress
kubectl -n canary-demo get canarydeployment auth-service-canary -o yaml
kubectl -n canary-demo get pods -l devsecops.io/track=canary -o wide
```

Expected resources:

```text
payments
payments-canary
payments Service
payments-canary Service
payments-canary-routing Ingress
```

## Run the Kubernetes Smoke Test

The script creates a temporary namespace, stable Deployment, Services, CanaryDeployment, and local operator process. It verifies canary workload reconciliation and cleans up asynchronously.

```bash
./scripts/smoke-test.sh
```

Override the namespace or Prometheus URL:

```bash
NAMESPACE=canary-demo PROMETHEUS_URL=http://prometheus.example.svc:9090 ./scripts/smoke-test.sh
```

This test does not prove real metrics or weighted traffic. Those require an instrumented application and an installed Ingress controller.

## Test Prometheus and Progressive Rollout

The sample uses:

```yaml
rolloutSteps: [5, 10, 25, 50, 100]
```

The application must expose:

- `http_requests_total` with `deployment` and `status` labels;
- `http_request_duration_seconds_bucket` with a `deployment` label.

Query the same PromQL directly:

```bash
PROM=http://127.0.0.1:9090
curl -G -fsS "$PROM/api/v1/query" --data-urlencode \
	'query=sum(rate(http_requests_total{deployment="payments",status=~"5.."}[1m])) / sum(rate(http_requests_total{deployment="payments"}[1m])) * 100'
curl -G -fsS "$PROM/api/v1/query" --data-urlencode \
	'query=histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{deployment="payments"}[1m])) by (le))'
```

Watch rollout state and routing:

```bash
watch kubectl -n canary-demo get canarydeployment auth-service-canary -o yaml
kubectl -n canary-demo get ingress payments-canary-routing -o yaml
```

Expected status includes `phase`, `trafficWeight`, `currentStepIndex`, `lastTransitionTime`, `lastAnalysisTime`, and `observedMetrics`.

## Test Rollback

With live metrics available, force thresholds below the observed values:

```bash
kubectl -n canary-demo patch canarydeployment auth-service-canary --type=merge -p \
	'{"spec":{"thresholds":{"maxErrorRatePercent":0,"maxP99LatencySeconds":0}}}'
```

Verify:

```bash
kubectl -n canary-demo get canarydeployment auth-service-canary -o yaml
kubectl -n canary-demo get deployment payments-canary
kubectl -n canary-demo get ingress payments-canary-routing -o yaml
```

Expected result: phase `Failed`, traffic weight `0`, canary scaled to zero, and observed breach metrics in status.

## Test Promotion

With healthy live metrics, allow the rollout to reach `100`:

```bash
kubectl -n canary-demo get deployment payments -o jsonpath='{.spec.template.spec.containers[*].image}'; echo
kubectl -n canary-demo get deployment payments-canary service payments-canary ingress payments-canary-routing
kubectl -n canary-demo get canarydeployment auth-service-canary -o yaml
```

Expected result: stable uses the canary image, canary Deployment/Service/Ingress are removed, and phase is `Promoted`.

## Test Restart Safety

1. Apply a CanaryDeployment and wait for a status step.
2. Stop Kopf with `Ctrl+C`.
3. Start it again.
4. Confirm the saved status and Ingress weight are retained.

```bash
kubectl -n canary-demo get canarydeployment auth-service-canary -o jsonpath='{.status}'; echo
kubectl -n canary-demo get ingress payments-canary-routing -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/canary-weight}'; echo
```

## Test Invalid Configuration

```bash
kubectl apply --dry-run=server -f crds/canary-crd.yaml
kubectl apply --dry-run=server -f examples/sample-canary.yaml
```

Try a copied sample with `[25, 10, 100]` or an interval of `0`. The API schema rejects invalid ranges/types; the controller rejects non-increasing rollout steps.

## Cleanup

```bash
kubectl delete namespace canary-demo --ignore-not-found --wait=false
kubectl delete -f k8s/rbac.yaml --ignore-not-found
kubectl delete -f crds/canary-crd.yaml --ignore-not-found
```

If a disposable namespace is stuck after stopping Kopf, remove only the test resource finalizer:

```bash
kubectl -n canary-demo patch canarydeployment auth-service-canary --type=merge -p '{"metadata":{"finalizers":[]}}'
```

## Verification Status

Verified locally:

- 10 unit tests pass;
- mypy and Python compilation pass;
- YAML and shell validation pass;
- Docker image builds;
- Minikube reaches `Ready`;
- smoke test creates the canary Deployment and both Services.

Requires a live Prometheus/Ingress environment:

- real RED metric collection;
- weighted traffic observation;
- automatic rollback from a real SLO breach;
- automatic promotion after healthy analysis.

## Known Limitations

- PromQL uses a hard-coded `[1m]` analysis window.
- Metric names and labels are application-specific.
- Weighted routing requires NGINX Ingress.
- The smoke test verifies reconciliation, not traffic percentages.
- Promotion updates container images but does not provide a release history.
- `datetime.utcnow()` currently emits a Python 3.13 deprecation warning.
