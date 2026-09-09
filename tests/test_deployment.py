import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("deployment", Path(__file__).parents[1] / "scripts/deploy.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_task_revision_keeps_roles_network_and_command():
    current = {
        "family": "ledgerguard-staging-api",
        "taskRoleArn": "role:api",
        "executionRoleArn": "role:execution",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "revision": 1,
        "taskDefinitionArn": "old",
        "status": "ACTIVE",
        "containerDefinitions": [
            {"name": "app", "image": "old", "command": ["gunicorn"], "readonlyRootFilesystem": True}
        ],
    }
    ecs = Mock()
    ecs.describe_task_definition.return_value = {"taskDefinition": current}
    ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "new"}}
    assert deployment.revision(ecs, "old", current["family"], "verified-digest") == "new"
    registered = ecs.register_task_definition.call_args.kwargs
    assert registered["taskRoleArn"] == "role:api"
    assert registered["containerDefinitions"][0] == {
        "name": "app",
        "image": "verified-digest",
        "command": ["gunicorn"],
        "readonlyRootFilesystem": True,
    }
    assert "revision" not in registered and current["containerDefinitions"][0]["image"] == "old"


def test_automatic_ecs_rollback_is_not_reported_as_a_success():
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [{"serviceName": "api", "taskDefinition": "old", "runningCount": 2, "desiredCount": 2}]
    }
    with pytest.raises(RuntimeError, match="rolled back"):
        deployment.stable(ecs, "cluster", {"api": "new"})


def test_schedule_update_is_read_back_before_success():
    scheduler = Mock()
    scheduler.get_schedule.side_effect = [
        {"State": "DISABLED", "Target": {"EcsParameters": {"TaskDefinitionArn": "old"}}},
        {"State": "ENABLED", "Target": {"EcsParameters": {"TaskDefinitionArn": "new"}}},
    ]
    deployment.schedules_current(
        scheduler,
        "ledgerguard-staging",
        {"scheduler": "new"},
        {"scheduler": "ENABLED"},
        attempts=2,
        delay=0,
    )
    assert scheduler.get_schedule.call_count == 2


def test_schedule_mismatch_fails_the_rollout():
    scheduler = Mock()
    scheduler.get_schedule.return_value = {
        "State": "ENABLED",
        "Target": {"EcsParameters": {"TaskDefinitionArn": "old"}},
    }
    with pytest.raises(RuntimeError, match="Scheduled operations"):
        deployment.schedules_current(
            scheduler,
            "ledgerguard-staging",
            {"scheduler": "new"},
            {"scheduler": "ENABLED"},
            attempts=1,
            delay=0,
        )


@pytest.mark.parametrize("exit_code", [None, 1, 137])
def test_migration_must_exit_successfully_before_rollout(exit_code):
    ecs = Mock()
    ecs.run_task.return_value = {"tasks": [{"taskArn": "migration"}]}
    ecs.describe_tasks.return_value = {"tasks": [{"containers": [{"exitCode": exit_code}]}]}
    with pytest.raises(RuntimeError, match="Operation task failed"):
        deployment.run_operation(ecs, "cluster", "migrator", {})
