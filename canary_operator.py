import logging
from copy import deepcopy
from datetime import datetime

import httpx
import kopf
import kubernetes  # type: ignore[import-untyped]


def load_kubernetes_config():
    """Use service-account credentials in Kubernetes and kubeconfig locally."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


load_kubernetes_config()
apps_v1 = kubernetes.client.AppsV1Api()
core_v1 = kubernetes.client.CoreV1Api()
networking_v1 = kubernetes.client.NetworkingV1Api()


def ensure_canary_deployment(
    namespace: str,
    target_name: str,
    canary_image: str,
    resource_name: str,
    canary_replicas: int,
    logger,
) -> str:
    """Create or update a canary Deployment from the stable Deployment template."""
    target = apps_v1.read_namespaced_deployment(name=target_name, namespace=namespace)
    if not target.spec or not target.spec.template or not target.spec.template.spec:
        raise ValueError(f"Deployment {target_name!r} has no usable pod template")
    if not target.spec.template.spec.containers:
        raise ValueError(f"Deployment {target_name!r} has no containers")

    canary_name = f"{target_name}-canary"
    pod_template = deepcopy(target.spec.template)
    pod_template.metadata = pod_template.metadata or kubernetes.client.V1ObjectMeta()
    pod_template.metadata.labels = dict(pod_template.metadata.labels or {})
    pod_template.metadata.labels["devsecops.io/canary"] = "true"
    pod_template.metadata.labels["devsecops.io/canarydeployment"] = resource_name
    pod_template.metadata.labels["devsecops.io/track"] = "canary"
    selector_labels = dict(target.spec.selector.match_labels or {})
    selector_labels["devsecops.io/canarydeployment"] = resource_name
    selector_labels["devsecops.io/track"] = "canary"
    pod_template.metadata.labels.update(selector_labels)
    pod_template.spec.containers[0].image = canary_image

    deployment_body = kubernetes.client.V1Deployment(
        metadata=kubernetes.client.V1ObjectMeta(
            name=canary_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "canary-operator",
                "devsecops.io/canarydeployment": resource_name,
            },
        ),
        spec=kubernetes.client.V1DeploymentSpec(
            replicas=canary_replicas,
            selector=kubernetes.client.V1LabelSelector(match_labels=selector_labels),
            template=pod_template,
        ),
    )

    try:
        existing = apps_v1.read_namespaced_deployment(
            name=canary_name, namespace=namespace
        )
        patch_body = {
            "spec": {
                "replicas": canary_replicas,
                "template": {
                    "metadata": {"labels": pod_template.metadata.labels},
                    "spec": {
                        "containers": [
                            {"name": container.name, "image": container.image}
                            for container in pod_template.spec.containers
                        ]
                    },
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=existing.metadata.name,
            namespace=namespace,
            body=patch_body,
        )
        logger.info(
            "Updated canary Deployment %s to image %s", canary_name, canary_image
        )
    except kubernetes.client.exceptions.ApiException as error:
        if error.status != 404:
            raise
        apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment_body)
        logger.info(
            "Created canary Deployment %s with image %s", canary_name, canary_image
        )

    return canary_name


def ensure_services(
    namespace: str,
    target_name: str,
    canary_name: str,
    service_name: str,
    resource_name: str,
    logger,
) -> dict:
    """
    Ensure stable and canary Services exist with isolated selectors.
    Returns dict with stable_service and canary_service names.
    """
    stable_port = 80
    canary_port = 80

    try:
        existing_service = core_v1.read_namespaced_service(
            name=service_name, namespace=namespace
        )
        if existing_service.spec and existing_service.spec.ports:
            stable_port = existing_service.spec.ports[0].port or 80
            canary_port = stable_port
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.info("Original service %s not found; using defaults", service_name)

    canary_service_name = f"{service_name}-canary"

    stable_selector = {"app.kubernetes.io/name": target_name}
    canary_selector = {
        "devsecops.io/track": "canary",
        "devsecops.io/canarydeployment": resource_name,
    }

    for svc_name, selector, port in [
        (service_name, stable_selector, stable_port),
        (canary_service_name, canary_selector, canary_port),
    ]:
        service_body = kubernetes.client.V1Service(
            metadata=kubernetes.client.V1ObjectMeta(
                name=svc_name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/managed-by": "canary-operator",
                    "devsecops.io/canarydeployment": resource_name,
                },
            ),
            spec=kubernetes.client.V1ServiceSpec(
                ports=[kubernetes.client.V1ServicePort(port=port, target_port=port)],
                selector=selector,
                type="ClusterIP",
            ),
        )

        try:
            core_v1.read_namespaced_service(name=svc_name, namespace=namespace)
            core_v1.patch_namespaced_service(
                name=svc_name,
                namespace=namespace,
                body=service_body,
            )
            logger.info("Updated Service %s in namespace %s", svc_name, namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            core_v1.create_namespaced_service(namespace=namespace, body=service_body)
            logger.info(
                "Created Service %s in namespace %s with selector %s",
                svc_name,
                namespace,
                selector,
            )

    return {"stable_service": service_name, "canary_service": canary_service_name}


def reconcile_ingress_routing(
    namespace: str,
    service_name: str,
    traffic_weight: int,
    resource_name: str,
    logger,
) -> None:
    """
    Reconcile Ingress with canary-weight annotations for traffic splitting.
    Uses NGINX ingress controller canary annotations.
    """
    if traffic_weight == 0 or traffic_weight == 100:
        logger.info(
            "Traffic at %d%%; skipping ingress canary annotation (binary state)",
            traffic_weight,
        )
        return

    canary_service_name = f"{service_name}-canary"
    canary_weight_percent = traffic_weight

    ingress_name = f"{service_name}-canary-routing"
    ingress_body = kubernetes.client.V1Ingress(
        metadata=kubernetes.client.V1ObjectMeta(
            name=ingress_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "canary-operator",
                "devsecops.io/canarydeployment": resource_name,
            },
            annotations={
                "nginx.ingress.kubernetes.io/canary": "true",
                "nginx.ingress.kubernetes.io/canary-by-header": "X-Canary",
                "nginx.ingress.kubernetes.io/canary-weight": str(canary_weight_percent),
                "nginx.ingress.kubernetes.io/canary-by-header-value": "always",
            },
        ),
        spec=kubernetes.client.V1IngressSpec(
            ingress_class_name="nginx",
            rules=[
                kubernetes.client.V1IngressRule(
                    host=None,
                    http=kubernetes.client.V1HTTPIngressRuleValue(
                        paths=[
                            kubernetes.client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=kubernetes.client.V1IngressBackend(
                                    service=kubernetes.client.V1IngressServiceBackend(
                                        name=canary_service_name, port=kubernetes.client.V1ServiceBackendPort(number=80)
                                    )
                                ),
                            )
                        ]
                    ),
                )
            ],
        ),
    )

    try:
        existing = networking_v1.read_namespaced_ingress(
            name=ingress_name, namespace=namespace
        )
        networking_v1.patch_namespaced_ingress(
            name=ingress_name,
            namespace=namespace,
            body=ingress_body,
        )
        logger.info(
            "Updated Ingress %s canary weight to %d%%", ingress_name, canary_weight_percent
        )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        networking_v1.create_namespaced_ingress(namespace=namespace, body=ingress_body)
        logger.info(
            "Created Ingress %s with canary weight %d%%", ingress_name, canary_weight_percent
        )


def get_rollout_steps(spec: dict) -> list:
    """Extract and validate rollout steps from spec."""
    rollout_steps = spec.get("rolloutSteps")
    if rollout_steps:
        steps = [int(step) for step in rollout_steps]
        if steps != sorted(set(steps)) or steps[-1] != 100:
            raise ValueError("rolloutSteps must be strictly increasing and end at 100")
        return steps

    step_weight = int(spec.get("stepWeight", 20))
    steps = []
    weight = step_weight
    while weight < 100:
        steps.append(weight)
        weight += step_weight
    steps.append(100)
    return steps


def get_step_index_for_weight(target_weight: int, steps: list) -> int:
    """Return the index in steps list where the weight is, or -1 if not found."""
    try:
        return steps.index(target_weight)
    except ValueError:
        return -1


def initialize_rollout_status(patch, logger):
    """Initialize status fields for a new rollout."""
    now = datetime.utcnow().isoformat()
    patch.status["phase"] = "Initializing"
    patch.status["trafficWeight"] = 0
    patch.status["currentStepIndex"] = -1
    patch.status["lastTransitionTime"] = now
    patch.status["lastAnalysisTime"] = None
    patch.status["reason"] = "Rollout initialized"
    logger.info("Initialized rollout status at %s", now)


async def query_prometheus(prometheus_url: str, query: str) -> float:
    """Execute an instant PromQL query and return the first scalar value."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                f"{prometheus_url.rstrip('/')}/api/v1/query",
                params={"query": query},
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("data", {}).get("result", [])
            if not results:
                return 0.0
            return float(results[0]["value"][1])
        except Exception as error:
            logging.error("Prometheus query failed for %r: %s", query, error)
            raise


def rollback_deployment(namespace: str, deployment_name: str, logger):
    """Record a rollback request on the target deployment."""
    logger.warning(
        "ROLLBACK TRIGGERED: restoring previous revision of %s", deployment_name
    )
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "devsecops.io/rollback-timestamp": datetime.utcnow().isoformat()
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(
        name=deployment_name,
        namespace=namespace,
        body=patch_body,
    )


@kopf.timer("devsecops.io", "v1alpha1", "canarydeployments", interval=30.0)
async def evaluate_canary_slo(spec, status, namespace, name, patch, logger, **_):
    target = spec.get("targetDeployment")
    service_name = spec.get("serviceName", target)
    canary_image = spec.get("canaryImage")
    prometheus_url = spec.get("prometheusUrl")
    canary_replicas = spec.get("canaryReplicas", 1)
    thresholds = spec.get("thresholds", {})

    current_weight = status.get("trafficWeight", 0)
    current_step_index = status.get("currentStepIndex", -1)
    phase = status.get("phase", "Initializing")
    last_transition_time = status.get("lastTransitionTime")

    if phase in ["Promoted", "Failed"]:
        return

    try:
        steps = get_rollout_steps(spec)
    except ValueError as error:
        patch.status["phase"] = "Blocked"
        patch.status["reason"] = str(error)
        logger.error("Invalid rollout contract: %s", error)
        return

    if phase == "Initializing":
        if current_step_index == -1:
            initialize_rollout_status(patch, logger)
            current_step_index = -1
            phase = "Initializing"
        if current_weight == 0:
            phase = "Initializing"
        else:
            current_step_index = get_step_index_for_weight(current_weight, steps)
            if current_step_index == -1:
                patch.status["phase"] = "Blocked"
                patch.status["reason"] = f"Current weight {current_weight}% not in rolloutSteps"
                logger.error("Weight %d not in rolloutSteps %s", current_weight, steps)
                return
            phase = "Progressing"

    try:
        canary_name = ensure_canary_deployment(
            namespace=namespace,
            target_name=target,
            canary_image=canary_image,
            resource_name=name,
            canary_replicas=canary_replicas,
            logger=logger,
        )
        patch.status["canaryDeployment"] = canary_name
        patch.status["observedStableDeployment"] = target
        patch.status["observedCanaryImage"] = canary_image
    except Exception as error:
        logger.error("Could not reconcile canary Deployment: %s", error)
        patch.status["phase"] = "Blocked"
        patch.status["reason"] = str(error)
        return

    try:
        services = ensure_services(
            namespace=namespace,
            target_name=target,
            canary_name=canary_name,
            service_name=service_name,
            resource_name=name,
            logger=logger,
        )
        patch.status["stableService"] = services["stable_service"]
        patch.status["canaryService"] = services["canary_service"]
    except Exception as error:
        logger.error("Could not reconcile Services: %s", error)
        patch.status["phase"] = "Blocked"
        patch.status["reason"] = str(error)
        return

    try:
        reconcile_ingress_routing(
            namespace=namespace,
            service_name=service_name,
            traffic_weight=current_weight,
            resource_name=name,
            logger=logger,
        )
    except Exception as error:
        logger.warning(
            "Could not reconcile ingress routing (check if NGINX ingress is installed): %s",
            error,
        )

    error_rate_query = (
        f'sum(rate(http_requests_total{{deployment="{target}",status=~"5.."}}[1m])) / '
        f'sum(rate(http_requests_total{{deployment="{target}"}}[1m])) * 100'
    )
    p99_latency_query = (
        f"histogram_quantile(0.99, "
        f'sum(rate(http_request_duration_seconds_bucket{{deployment="{target}"}}[1m])) by (le))'
    )

    try:
        error_rate = await query_prometheus(prometheus_url, error_rate_query)
        p99_latency = await query_prometheus(prometheus_url, p99_latency_query)
        logger.info(
            "[%s] Current metrics -> error rate: %.2f%%, p99: %.3fs",
            target,
            error_rate,
            p99_latency,
        )
    except Exception:
        logger.warning("Could not reach Prometheus. Pausing step progression.")
        return

    max_error = thresholds.get("maxErrorRatePercent", 1.0)
    max_latency = thresholds.get("maxP99LatencySeconds", 0.5)

    now_iso = datetime.utcnow().isoformat()
    patch.status["lastAnalysisTime"] = now_iso

    if error_rate > max_error or p99_latency > max_latency:
        logger.error(
            "SLO breach detected: error rate %.2f%% (max %.2f%%), p99 %.3fs (max %.3fs)",
            error_rate,
            max_error,
            p99_latency,
            max_latency,
        )
        rollback_deployment(namespace, target, logger)
        patch.status["phase"] = "Failed"
        patch.status["trafficWeight"] = 0
        patch.status["currentStepIndex"] = -1
        patch.status["lastTransitionTime"] = now_iso
        patch.status["reason"] = f"SLO breached: error_rate {error_rate:.2f}% (max {max_error}%), p99 {p99_latency:.3f}s (max {max_latency}s)"
        return

    next_step_index = current_step_index + 1
    if next_step_index < len(steps):
        next_weight = steps[next_step_index]
    else:
        next_weight = 100

    now_iso = datetime.utcnow().isoformat()
    patch.status["trafficWeight"] = next_weight
    patch.status["currentStepIndex"] = next_step_index if next_step_index < len(steps) else len(steps) - 1
    patch.status["lastTransitionTime"] = now_iso
    patch.status["lastAnalysisTime"] = now_iso

    if next_weight == 100:
        patch.status["phase"] = "Promoted"
        patch.status["reason"] = f"Canary promoted to stable (100%% traffic, SLO healthy)"
        logger.info(
            "Canary %s successfully reached 100%% traffic and promoted.", target
        )
    else:
        patch.status["phase"] = "Progressing"
        patch.status["reason"] = f"Healthy; advancing from step {current_step_index + 1} to {next_step_index + 1} ({next_weight}%% traffic)"
        logger.info("Canary healthy. Advancing from step %d to %d (%d%% traffic)", current_step_index + 1, next_step_index + 1, next_weight)
