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

Step 1: Start a Local Cluster & Install Prometheus

If using Minikube:

```bash
minikube start --driver=docker
Install Prometheus using Helm:
```

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace \
  --set server.service.type=ClusterIP
```
