from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import kubernetes
import pytest

import canary_operator


class StatusPatch:
    def __init__(self):
        self.status = {}


class Logger:
    info = Mock()
    warning = Mock()
    error = Mock()
    exception = Mock()


def test_rollout_steps_are_validated_and_preserved():
    spec = {"rolloutSteps": [5, 10, 25, 50, 100]}

    assert canary_operator.get_rollout_steps(spec) == [5, 10, 25, 50, 100]
    assert canary_operator.get_step_index_for_weight(25, spec["rolloutSteps"]) == 2


@pytest.mark.parametrize("steps", [[10, 5, 100], [5, 5, 100], [5, 10, 50]])
def test_invalid_rollout_steps_are_rejected(steps):
    with pytest.raises(ValueError):
        canary_operator.get_rollout_steps({"rolloutSteps": steps})


def test_step_weight_compatibility_generates_terminal_step():
    assert canary_operator.get_rollout_steps({"stepWeight": 30}) == [30, 60, 90, 100]


def test_initialize_rollout_status_sets_restart_safe_fields():
    patch = StatusPatch()

    canary_operator.initialize_rollout_status(patch, Logger())

    assert patch.status["phase"] == "Initializing"
    assert patch.status["trafficWeight"] == 0
    assert patch.status["currentStepIndex"] == -1
    assert patch.status["lastTransitionTime"]
    assert patch.status["lastAnalysisTime"] is None


@pytest.mark.asyncio
async def test_query_prometheus_returns_zero_for_empty_result():
    response = Mock()
    response.json.return_value = {"data": {"result": []}}
    response.raise_for_status.return_value = None

    client = Mock()
    client.get = AsyncMock(return_value=response)

    with patch("canary_operator.httpx.AsyncClient") as async_client:
        async_client.return_value.__aenter__ = AsyncMock(return_value=client)
        async_client.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await canary_operator.query_prometheus("http://prometheus", "up")

    assert result == 0.0


def test_reconcile_ingress_writes_canary_weight():
    ingress_api = Mock()
    ingress_api.read_namespaced_ingress.side_effect = kubernetes.client.exceptions.ApiException(
        status=404
    )
    logger = Logger()

    with patch.object(canary_operator, "networking_v1", ingress_api):
        canary_operator.reconcile_ingress_routing(
            namespace="default",
            service_name="payments",
            traffic_weight=25,
            resource_name="payments-canary",
            logger=logger,
        )

    ingress = ingress_api.create_namespaced_ingress.call_args.kwargs["body"]
    assert ingress.metadata.annotations["nginx.ingress.kubernetes.io/canary-weight"] == "25"
    assert ingress.spec.rules[0].http.paths[0].backend.service.name == "payments-canary"


def test_rollback_resets_traffic_and_scales_canary():
    logger = Logger()

    with (
        patch.object(canary_operator, "reconcile_ingress_routing") as ingress,
        patch.object(canary_operator, "scale_canary_deployment") as scale,
    ):
        canary_operator.rollback_canary(
            namespace="default",
            service_name="payments",
            canary_name="payments-canary",
            resource_name="payments-rollout",
            logger=logger,
        )

    ingress.assert_called_once_with(
        namespace="default",
        service_name="payments",
        traffic_weight=0,
        resource_name="payments-rollout",
        logger=logger,
    )
    scale.assert_called_once_with("default", "payments-canary", 0, logger)


def test_promote_updates_stable_and_cleans_canary_resources():
    canary = SimpleNamespace(
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="app", image="payments:v2")]
                )
            )
        )
    )
    stable = SimpleNamespace(
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="app", image="payments:v1")]
                )
            )
        )
    )
    apps_api = Mock()
    apps_api.read_namespaced_deployment.side_effect = [canary, stable]
    core_api = Mock()
    networking_api = Mock()
    logger = Logger()

    with (
        patch.object(canary_operator, "apps_v1", apps_api),
        patch.object(canary_operator, "core_v1", core_api),
        patch.object(canary_operator, "networking_v1", networking_api),
        patch.object(canary_operator, "scale_canary_deployment"),
    ):
        canary_operator.promote_canary(
            namespace="default",
            target_name="payments",
            canary_name="payments-canary",
            service_name="payments",
            logger=logger,
        )

    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert patch_body["spec"]["template"]["spec"]["containers"] == [
        {"name": "app", "image": "payments:v2"}
    ]
    core_api.delete_namespaced_service.assert_called_once_with(
        name="payments-canary", namespace="default"
    )
    networking_api.delete_namespaced_ingress.assert_called_once_with(
        name="payments-canary-routing", namespace="default"
    )
    apps_api.delete_namespaced_deployment.assert_called_once_with(
        name="payments-canary", namespace="default"
    )
