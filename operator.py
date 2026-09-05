import kopf
import kubernetes
import httpx
import logging
from datetime import datetime

# Initialize K8s Client
kubernetes.config.load_incluster_config() # or load_kube_config() for local dev
apps_v1 = kubernetes.client.AppsV1Api()
custom_objects = kubernetes.client.CustomObjectsApi()

async def query_prometheus(prometheus_url: str, query: str) -> float:
    """Executes an instant PromQL query and returns the scalar value."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                f"{prometheus_url.rstrip('/')}/api/v1/query",
                params={"query": query}
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("data", {}).get("result", [])
            if not results:
                return 0.0
            return float(results[0]["value"][1])
        except Exception as e:
            logging.error(f"Prometheus query failed for '{query}': {e}")
            raise

def rollback_deployment(namespace: str, deployment_name: str, logger):
    """Rolls back the target deployment revision in case of SLO breach."""
    logger.warning(f"ROLLBACK TRIGGERED: Restoring previous revision of {deployment_name}")
    # In practice, you can trigger a rollout undo or patch the deployment's image tag
    # For demonstration, we scale target canary replicas to 0 or update annotation
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"devsecops.io/rollback-timestamp": str(datetime.utcnow())}
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)

@kopf.timer('devsecops.io', 'v1alpha1', 'canarydeployments', interval=30.0)
async def evaluate_canary_slo(spec, status, name, namespace, patch, logger, **_):
    target = spec.get('targetDeployment')
    prom_url = spec.get('prometheusUrl')
    step_weight = spec.get('stepWeight', 20)
    thresholds = spec.get('thresholds', {})
    
    current_weight = status.get('trafficWeight', 0)
    phase = status.get('phase', 'Initializing')

    if phase in ['Promoted', 'Failed']:
        return  # Terminal states, stop reconciliation

    # 1. Evaluate RED Metrics from Prometheus
    error_rate_query = (
        f'sum(rate(http_requests_total{{deployment="{target}",status=~"5.."}}[1m])) / '
        f'sum(rate(http_requests_total{{deployment="{target}"}}[1m])) * 100'
    )
    p99_latency_query = (
        f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{deployment="{target}"}}[1m])) by (le))'
    )

    try:
        error_rate = await query_prometheus(prom_url, error_rate_query)
        p99_latency = await query_prometheus(prom_url, p99_latency_query)
        logger.info(f"[{target}] Current Metrics -> Error Rate: {error_rate:.2f}%, p99: {p99_latency:.3f}s")
    except Exception:
        logger.warning("Could not reach Prometheus. Pausing step progression.")
        return

    # 2. Check SLO Thresholds
    max_error = thresholds.get('maxErrorRatePercent', 1.0)
    max_latency = thresholds.get('maxP99LatencySeconds', 0.5)

    if error_rate > max_error or p99_latency > max_latency:
        logger.error(f"SLO Breach detected! Error Rate: {error_rate}% (Max: {max_error}%), p99: {p99_latency}s (Max: {max_latency}s)")
        rollback_deployment(namespace, target, logger)
        patch.status['phase'] = 'Failed'
        patch.status['trafficWeight'] = 0
        patch.status['reason'] = f"SLO breached at {datetime.utcnow()}"
        return

    # 3. Advance Traffic if Healthy
    next_weight = min(current_weight + step_weight, 100)
    patch.status['trafficWeight'] = next_weight

    if next_weight == 100:
        patch.status['phase'] = 'Promoted'
        logger.info(f"Canary {target} successfully reached 100% traffic and promoted.")
    else:
        patch.status['phase'] = 'Progressing'
        logger.info(f"Canary healthy. Increasing traffic weight to {next_weight}%")