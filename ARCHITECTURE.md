# CanarySLO Operator Architecture

## Overview

The CanarySLO Operator is an autonomous, SLO-driven Kubernetes controller that orchestrates progressive canary deployments. It watches `CanaryDeployment` custom resources, gradually shifts traffic to a new version, and automatically rolls back if reliability targets are violated.

---

## Core Components

### 1. Workload Management (Deployments)

The operator manages two Deployments with **isolated selectors**:

- **Stable Deployment**: The existing production workload. Selector labels remain unchanged.
- **Canary Deployment**: A new version created by the operator with:
  - Pod template copied from stable (to maintain compatibility)
  - Container image replaced with `canaryImage`
  - Additional labels for routing:
    - `devsecops.io/track=canary`
    - `devsecops.io/canarydeployment=<resource-name>`

**Selector Isolation**: The canary Deployment's selector includes `devsecops.io/track=canary`, ensuring canary Pods **never** match the stable Service. This prevents accidental cross-traffic.

```yaml
# Stable Deployment selector (unchanged)
selector:
  matchLabels:
    app: my-service

# Canary Deployment selector (isolated)
selector:
  matchLabels:
    app: my-service
    devsecops.io/track: canary
    devsecops.io/canarydeployment: my-canary-resource
```

---

### 2. Traffic Routing (Services + Ingress)

The operator implements traffic shifting via **dual Services + NGINX Ingress canary annotations**, following the pattern used by Argo Rollouts:

#### Stable Service

- **Name**: `<serviceName>` (e.g., `auth-service`)
- **Selector**: Points to stable Deployment pods only
- **Port**: 80 (configurable)

#### Canary Service

- **Name**: `<serviceName>-canary` (e.g., `auth-service-canary`)
- **Selector**: Points to canary Deployment pods only
- **Port**: 80 (same as stable)

#### Canary Ingress

- **Name**: `<serviceName>-canary-routing`
- **Backend**: Routes to `canary-service`
- **Canary Annotations** (NGINX controller):
  - `nginx.ingress.kubernetes.io/canary: true`
  - `nginx.ingress.kubernetes.io/canary-weight: <traffic_weight>`
  - `nginx.ingress.kubernetes.io/canary-by-header: X-Canary`

The NGINX ingress controller interprets these annotations and automatically splits traffic proportionally:

```
traffic_weight = 10%

90% of requests → stable-service → stable Deployment
10% of requests → canary-service → canary Deployment
```

**Why this approach?**

- Industry-standard (Argo Rollouts uses identical pattern)
- No custom proxying logic needed
- Works with any NGINX-compatible controller
- Observable via `kubectl get ingress`
- Graceful fallback if ingress controller unavailable

---

### 3. SLO Evaluation (Prometheus)

On each reconciliation cycle (default: 30s), the operator:

1. **Queries Prometheus** for RED metrics on the stable Deployment:
   - **Rate**: `sum(rate(http_requests_total[1m]))`
   - **Errors**: `sum(rate(http_requests_total{status=~"5.."}[1m]))`
   - **Duration**: `histogram_quantile(0.99, ...)`

2. **Calculates error rate**: `(errors / total) * 100`

3. **Compares thresholds**:
   ```
   if error_rate > maxErrorRatePercent OR p99_latency > maxP99LatencySeconds:
       → SLO breach detected
   else:
       → Healthy, proceed to next step
   ```

4. **Advances traffic** per declared `rolloutSteps`:
   ```yaml
   rolloutSteps: [5, 10, 25, 50, 100]
   
   # Progression
   0% → 5% (after first healthy window)
   5% → 10% (after second healthy window)
   10% → 25% (after third window)
   ...
   100% → promotion
   ```

---

### 4. State Machine

The operator maintains strict phase transitions in the CRD status:

```
Initializing
    ↓
Progressing (traffic: 0% → 5% → 10% → ... → 100%)
    ├→ [SLO breach] → Failed (rollback to 0%)
    └→ [100% + healthy] → Promoted
```

**Status fields** track progress:
```yaml
status:
  phase: Progressing
  trafficWeight: 25
  currentStep: 2  # index in rolloutSteps
  canaryDeployment: auth-service-canary
  stableService: auth-service
  canaryService: auth-service-canary
  observedMetrics:
    errorRate: 0.3
    p99Latency: 0.245
  lastTransitionTime: "2026-09-08T12:34:56Z"
  reason: "progressing to next step"
```

---

## Rollout Lifecycle

### Example: Successful Promotion

```
[Day 1, 12:00:00] User applies CanaryDeployment CRD
  status.phase = Initializing

[12:00:30] Operator reconciles:
  1. Reads stable Deployment (auth-service)
  2. Creates canary Deployment (auth-service-canary) with new image
  3. Creates stable Service (auth-service) → stable pods
  4. Creates canary Service (auth-service-canary) → canary pods
  5. Creates Ingress with canary-weight: 0 (no traffic yet)
  6. Queries Prometheus (no metrics yet)
  status.phase = Initializing

[12:01:00] Operator reconciles:
  - Prometheus shows: error_rate = 0.2%, p99 = 245ms (healthy)
  - Thresholds: error_rate < 0.5%, p99 < 250ms ✓
  - Advance to first step: 5%
  status.phase = Progressing
  status.trafficWeight = 5

[12:01:30] Operator reconciles:
  - Update Ingress canary-weight: 5
  - (NGINX now sends 5% traffic to canary)

[12:02:00] Operator reconciles:
  - Prometheus shows: error_rate = 0.25%, p99 = 248ms (healthy)
  - Advance to next step: 10%
  status.trafficWeight = 10

[12:02:30] Ingress updated → 10% traffic to canary

... (repeat for 25%, 50%, ...)

[12:05:00] Operator reconciles:
  - traffic_weight is 100
  - Prometheus shows: error_rate = 0.3%, p99 = 242ms (healthy)
  - Promotion decision: Update stable Deployment image to canary image
  - Delete canary Deployment and Services
  - status.phase = Promoted
  - status.reason = "Canary promoted to stable"
```

### Example: Rollback on SLO Breach

```
[12:02:30] Operator reconciles:
  - Canary at 50% traffic
  - Prometheus shows: error_rate = 5.2%, p99 = 1.8s (BREACH)
  - Threshold: error_rate < 0.5%, p99 < 250ms ✗
  
  ROLLBACK TRIGGERED:
  1. Update Ingress canary-weight: 0 (return all traffic to stable)
  2. Scale canary Deployment → 0 replicas
  3. status.phase = Failed
  4. status.reason = "SLO breach: error_rate 5.2% (max 0.5%), p99 1.8s (max 0.25s)"
  5. Record timestamp and metrics for investigation
```

---

## Production Considerations

### Restart Safety
- All state stored in CRD status (Kubernetes etcd)
- On operator restart, resumes from last recorded phase/weight
- No split-brain risk (single controller, etcd is source of truth)

### Observable
- Status conditions logged to CRD status
- Each step change logged with metrics
- Ingress rules visible via `kubectl get ingress`
- Services observable via `kubectl get svc`

### Failure Modes
1. **Prometheus unavailable**: Operator logs warning, pauses progression (safe)
2. **Invalid rolloutSteps**: Operator blocks with explanation (safe)
3. **Canary Deployment fails to create**: Operator blocks, user debugs CRD (safe)
4. **NGINX ingress controller missing**: Operator warns, continues with Services only (degraded but safe)

### Scalability
- One CanaryDeployment CRD = one rollout (can run multiple in parallel)
- No shared state beyond Kubernetes API
- Horizontal scaling: run multiple operator replicas with leader election (kopf built-in)

---

## Why This Design?

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| **Two Deployments** | Isolated stable + canary | No risk of cross-traffic; clean rollback |
| **Dual Services** | Separate service per workload | Standard Kubernetes pattern; RBAC-compatible |
| **NGINX Ingress annotations** | Industry standard canary routing | Same as Argo Rollouts; supports Traefik, Kong |
| **CRD status as state** | Single source of truth | No external etcd/database; restart-safe |
| **30s reconciliation** | Reasonable SLO check cadence | Fast enough for user experience; not spammy on Prometheus |
| **Rollout steps explicit** | Contract in CRD | Clear intent; prevents accidental step jumps |
| **Type hints + validation** | Production safety | Catches bugs early; self-documenting |

---

## Next: Improvements for Production

1. **Error budget burn-rate** (SRE advanced): Calculate how quickly the budget is consumed, not just if thresholds are breached
2. **Custom analysis windows** (currently hard-coded 1m): Allow configurable Prometheus query windows
3. **Automatic promotion** (currently manual): Option to promote without reaching 100%
4. **Canary abort policy** (currently immediate rollback): Graceful drain with timeout
5. **Metrics export** (operator observability): Prometheus metrics on operator itself (rollouts completed, failures, duration)
