# Canary Operator Implementation Plan

This plan tracks the path from the current skeleton to a working SLO-driven canary controller.

## Current State

- [x] Repository skeleton with Python, CRD, sample resource, RBAC, and Dockerfile.
- [x] Prometheus instant-query helper for scalar results.
- [x] RED metric queries for error rate and p99 latency.
- [x] Threshold evaluation and Kubernetes status updates.
- [x] Controller module renamed to `canary_operator.py` so it does not shadow Python's standard `operator` module.
- [x] Local kubeconfig fallback added for development; in-cluster service-account configuration remains supported.
- [x] Real canary Deployment creation and image reconciliation.
- [x] Delivery contract for canary image, replicas, rollout steps, and SLO thresholds.
- [x] Isolated canary selectors so stable Services cannot select canary Pods.
- [x] Controller progression follows declared rollout steps.
- [x] Real traffic management (dual Services + NGINX Ingress canary annotations).
- [ ] Reliable rollout state machine and restart-safe timing.
- [ ] Real rollback to the stable version.
- [ ] Automated tests and a repeatable local end-to-end test.
- [ ] Final README based on verified behavior.

## Remaining Work, In Order

### 1. Define the delivery contract

- Decide whether the first traffic implementation uses two Deployments plus a weighted ingress/service, or a supported progressive-delivery mechanism such as Argo Rollouts.
- Extend the CRD with the stable workload, canary image, service/route, rollout steps, and analysis timing.
- Add schema validation for percentages, positive intervals, and required fields.

Acceptance check: applying an invalid `CanaryDeployment` is rejected by the API server, and a valid sample clearly describes both stable and canary versions.

### 2. Reconcile the canary workload

- Watch `CanaryDeployment` creation and changes.
- Create or update the canary Deployment from the stable workload's pod template and the requested image.
- Preserve labels, selectors, ports, and namespace boundaries.
- Record observed canary and stable revisions in status.

Acceptance check: applying the sample creates a separate canary Deployment with the requested image and matching service labels.

### 3. Implement real progressive traffic

- Start at the first configured step instead of only changing `status.trafficWeight`.
- Apply each weight to the chosen traffic router.
- Persist the current step and last transition time in status.
- Make reconciliation idempotent so retries do not skip steps.

Acceptance check: the actual route/backend split matches status at 5%, 10%, 25%, and later configured steps.

### 4. Make SLO analysis production-safe

- Use configurable analysis windows instead of hard-coded one-minute windows.
- Handle missing series, zero request volume, malformed Prometheus responses, and query timeouts explicitly.
- Store observed metrics and the last analysis timestamp in status.
- Add a burn-rate/error-budget calculation after the basic thresholds work.

Acceptance check: no traffic promotion occurs when analysis is unavailable or there is insufficient traffic, and the status explains why.

### 5. Implement rollback and promotion

- On breach, route 100% back to stable and scale down or remove the canary.
- On success, promote the canary image to stable and clean up temporary resources.
- Record a reason, condition, and completion time.
- Ensure a controller restart resumes the correct phase.

Acceptance check: a deliberately failing canary returns traffic to stable without manual intervention; a healthy canary completes promotion.

### 6. Add tests and packaging checks

- Unit-test Prometheus parsing, threshold evaluation, step progression, and rollback decisions without a cluster.
- Add manifest validation and a Docker build check.
- Add a local Kind or Minikube smoke test with a documented Prometheus setup.
- Add CI for syntax, tests, YAML validation, and image build.

Acceptance check: the checks run from a clean checkout and fail on a known bad canary scenario.

### 7. Rewrite the README

Do this after the controller contract and local smoke test are stable. Replace the current README's aspirational claims with:

- supported architecture and traffic mechanism;
- exact install and cleanup commands;
- CRD and sample configuration reference;
- status lifecycle and failure behavior;
- local test procedure and expected output;
- known limitations and production considerations.

## Next Slice

Start with the delivery contract and workload reconciliation. The current code cannot perform a canary rollout because `targetDeployment` points at one existing Deployment and the timer only changes a status field; the next implementation must create a distinct canary workload before traffic shifting can be meaningful.
