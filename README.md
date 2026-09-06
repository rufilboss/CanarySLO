# Autonomous SLO-Driven Canary Operator (`canary-operator`)

A cloud-native Kubernetes Operator written in Python using `kopf` and `httpx` that automates progressive canary deployments and instant rollbacks based on real-time Prometheus RED metrics (Rate, Errors, Duration) and SLO budget consumption.

---

## 1. Project Overview & Architecture

### The Problem

Traditional deployment strategies (e.g., rolling updates) swap replicas blindly without verifying service-level health. When an unhandled edge case or performance regression hits production, bad code reaches 100% of users before engineers notice alerts.

### The Solution

The **SLO-Driven Canary Operator** acts as an autonomous reliability gatekeeper:

1. Watches a custom resource definition (`CanaryDeployment`).
2. Gradually shifts traffic in incremental steps (e.g., `25% -> 50% -> 75% -> 100%`).
3. Periodically queries live Prometheus metrics via PromQL for HTTP 5xx error rate percentages and $p99$ response latencies.
4. **Auto-Promotes** if metrics stay healthy within defined SLO budgets.
5. **Auto-Rolls Back** instantly if any threshold is violated, cutting off traffic to the canary and logging the failure reason to Kubernetes status subresources.

+------------------------------------+
                |        Kubernetes Cluster          |
                |                                    |
                |   +----------------------------+   |
                |   |   CanaryDeployment CRD     |   |
                |   +-------------+--------------+   |
                |                 |                  |
                |                 | watches          |
                |                 v                  |
+-------------+     |   +----------------------------+   |     +----------------+
|  Prometheus | <-------+    Python Kopf Operator    +-------> | Target Workload|
| (RED metrics|<--------+  (PromQL Evaluation Loop)  | patch   |  (Deployments) |
+-------------+     |   +----------------------------+   |     +----------------+
+------------------------------------+

## 2. Step-by-Step Local Run Guide

You can run and test this operator locally on Minikube or Kind (Kubernetes in Docker).

Prerequisites:

- docker
- kubectl
- minikube or kind
- python 3.10+

Step 1: Start a Local Cluster & Install Prometheus

If using Minikube:

```bash
minikube start --driver=docker
```

Install Prometheus using Helm:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace \
  --set server.service.type=ClusterIP
```

Step 2: Apply the Custom Resource Definition (CRD) & RBAC

Apply the CRD schema:

```bash
kubectl apply -f crds/canary-crd.yaml
```

Set up RBAC permissions (`k8s/rbac.yaml`):

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: canary-operator-sa
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: canary-operator-role
rules:
  - apiGroups: ["devsecops.io"]
    resources: ["canarydeployments", "canarydeployments/status"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: canary-operator-binding
subjects:
  - kind: ServiceAccount
    name: canary-operator-sa
    namespace: default
roleRef:
  kind: ClusterRole
  name: canary-operator-role
  apiGroup: rbac.authorization.k8s.io
```

```bash
kubectl apply -f k8s/rbac.yaml
```

Step 3: Run the Operator

Option A: Running locally against the cluster (Fastest for Development)
Ensure your ~/.kube/config points to your active cluster:

```bash
# Set up a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install requirements
pip install -r requirements.txt

# Run operator via Kopf (uses local kubeconfig automatically)
kopf run --standalone -A operator.py
```

Option B: Running inside the cluster as a Deployment
Build and load the Docker image:

```bash
docker build -t canary-operator:latest .

# If using Kind:
# kind load docker-image canary-operator:latest

# If using Minikube:
# minikube image load canary-operator:latest
```

Deploy the operator:

```bash
kubectl create deployment canary-operator --image=canary-operator:latest
kubectl set serviceaccount deployment/canary-operator canary-operator-sa
```

Step 4: Test Canary Progression and Rollback
Deploy a Sample Service:

```bash
kubectl create deployment auth-service --image=nginx:alpine --replicas=2
```

Trigger Canary Analysis:

```bash
kubectl apply -f examples/sample-canary.yaml
```

Observe Operator Decisions & CRD Status:

```bash
# Stream operator logs
kopf run -A operator.py --verbose

# Inspect Custom Resource status
kubectl get canarydeployment auth-service-canary -o yaml
```

The operator transitions through lifecycle states:

Initializing -> Progressing (Traffic Weight: 25%, 50%, 75%) -> Promoted (Traffic Weight: 100%)

Or Failed (Traffic Weight: 0%) if an error budget or latency threshold is breached.

<!-- Engineering Trade-offs & Production Considerations

PromQL Evaluation Window: Uses an instant query over a 1m rate window. In high-throughput clusters, a 3m to 5m exponential smoothing window prevents transient anomalies from triggering false-positive rollbacks.

Controller Concurrency: kopf.timer executes asynchronously using httpx, preventing thread starvation while polling external metrics endpoints.

Idempotency & State Recovery: State is stored in the Kubernetes Custom Resource status subresource, ensuring the controller recovers state across restarts without split-brain conflicts. -->
