"""Deploy a verified ECR digest after successful migrations; restore rollout targets on failure."""

import argparse
import copy
import json
import re
import time
from pathlib import Path

import boto3

TASK_FIELDS = {
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
}
SCHEDULE_FIELDS = {
    "Name",
    "GroupName",
    "ScheduleExpression",
    "StartDate",
    "EndDate",
    "Description",
    "ScheduleExpressionTimezone",
    "State",
    "KmsKeyArn",
    "Target",
    "FlexibleTimeWindow",
    "ActionAfterCompletion",
}
QUEUES = ["ingestion", "reconciliation", "notification", "reporting", "repair"]


def revision(ecs, reference, family, image):
    current = ecs.describe_task_definition(taskDefinition=reference)["taskDefinition"]
    if current["family"] != family or len(current["containerDefinitions"]) != 1:
        raise ValueError("Unexpected task family or container topology")
    value = copy.deepcopy({k: v for k, v in current.items() if k in TASK_FIELDS})
    value["containerDefinitions"][0]["image"] = image
    return ecs.register_task_definition(**value)["taskDefinition"]["taskDefinitionArn"]


def run_operation(ecs, cluster, definition, network):
    response = ecs.run_task(
        cluster=cluster, taskDefinition=definition, launchType="FARGATE", networkConfiguration=network, count=1
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("Operation task could not start")
    task = response["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=cluster, tasks=[task], WaiterConfig={"Delay": 6, "MaxAttempts": 150})
    result = ecs.describe_tasks(cluster=cluster, tasks=[task])
    containers = result["tasks"][0].get("containers", []) if result.get("tasks") else []
    if len(containers) != 1 or containers[0].get("exitCode") != 0:
        raise RuntimeError("Operation task failed; inspect the private task log before retrying")


def stable(ecs, cluster, targets):
    names = list(targets)
    ecs.get_waiter("services_stable").wait(
        cluster=cluster, services=names, WaiterConfig={"Delay": 10, "MaxAttempts": 90}
    )
    result = ecs.describe_services(cluster=cluster, services=names)
    if result.get("failures") or len(result["services"]) != len(names):
        raise RuntimeError("Could not verify every service")
    for service in result["services"]:
        expected = targets[service["serviceName"]]
        if service["taskDefinition"] != expected or service["runningCount"] < service["desiredCount"]:
            raise RuntimeError("A service rolled back or did not reach its requested revision")


def schedules_current(scheduler, name, targets, states, attempts=15, delay=2):
    for attempt in range(attempts):
        current = {suffix: scheduler.get_schedule(Name=name + "-" + suffix) for suffix in targets}
        if all(
            current[suffix].get("State") == states[suffix]
            and current[suffix].get("Target", {}).get("EcsParameters", {}).get("TaskDefinitionArn") == target
            for suffix, target in targets.items()
        ):
            return
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError("Scheduled operations did not reach their requested revisions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["dev", "staging", "production"], required=True)
    parser.add_argument(
        "--image", required=True, help="Image signature and provenance must be verified before this command"
    )
    parser.add_argument(
        "--initialize", action="store_true", help="First deployment only; requires a bootstrap administrator role"
    )
    parser.add_argument("--receipt", type=Path, default=Path("deployment.json"))
    args = parser.parse_args()
    name = "ledgerguard-" + args.environment
    account = boto3.client("sts", region_name="eu-west-1").get_caller_identity()["Account"]
    prefix = f"{account}.dkr.ecr.eu-west-1.amazonaws.com/{name}@sha256:"
    if not args.image.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{64}", args.image.removeprefix(prefix)):
        parser.error("Image must be an immutable digest from this environment's ECR repository")
    ecs = boto3.client("ecs", region_name="eu-west-1")
    scheduler = boto3.client("scheduler", region_name="eu-west-1")
    service_names = [name + "-" + suffix for suffix in ["api", *QUEUES]]
    response = ecs.describe_services(cluster=name, services=service_names)
    if response.get("failures") or len(response["services"]) != len(service_names):
        raise RuntimeError("Terraform must provision all six services before deployment")
    old = {s["serviceName"]: s for s in response["services"]}
    if not args.initialize and any(s["desiredCount"] < 1 for s in old.values()):
        raise RuntimeError("Use the documented first-deployment sequence for disabled services")
    schedules = {}
    for suffix in ["scheduler", "maintenance"]:
        value = scheduler.get_schedule(Name=name + "-" + suffix)
        schedules[suffix] = {k: v for k, v in value.items() if k in SCHEDULE_FIELDS}
    network = old[name + "-api"]["networkConfiguration"]
    if network["awsvpcConfiguration"].get("assignPublicIp") != "DISABLED":
        raise RuntimeError("Operation tasks must use private networking")
    if args.initialize:
        bootstrap = revision(ecs, name + "-bootstrap", name + "-bootstrap", args.image)
        run_operation(ecs, name, bootstrap, network)
    migrator = revision(ecs, name + "-migrator", name + "-migrator", args.image)
    run_operation(ecs, name, migrator, network)
    changed, scheduled, targets, schedule_targets, schedule_states = [], [], {}, {}, {}
    try:
        for service in service_names:
            target = revision(ecs, old[service]["taskDefinition"], service, args.image)
            values = {"cluster": name, "service": service, "taskDefinition": target}
            if args.initialize:
                values["desiredCount"] = max(
                    old[service]["desiredCount"],
                    2 if service.endswith("-api") and args.environment == "production" else 1,
                )
            # Record first: a lost UpdateService response may already have changed the service.
            changed.append(service)
            ecs.update_service(**values)
            targets[service] = target
        stable(ecs, name, targets)
        for suffix, original in schedules.items():
            value = copy.deepcopy(original)
            task = revision(ecs, value["Target"]["EcsParameters"]["TaskDefinitionArn"], name + "-" + suffix, args.image)
            value["Target"]["EcsParameters"]["TaskDefinitionArn"] = task
            if args.initialize:
                value["State"] = "ENABLED"
            scheduled.append(suffix)
            scheduler.update_schedule(**value)
            schedule_targets[suffix] = task
            schedule_states[suffix] = value["State"]
        schedules_current(scheduler, name, schedule_targets, schedule_states)
    except Exception as failure:
        errors = []
        for suffix in scheduled:
            try:
                scheduler.update_schedule(**schedules[suffix])
            except Exception:
                errors.append("schedule:" + suffix)
        if scheduled and not errors:
            try:
                schedules_current(
                    scheduler,
                    name,
                    {suffix: schedules[suffix]["Target"]["EcsParameters"]["TaskDefinitionArn"] for suffix in scheduled},
                    {suffix: schedules[suffix]["State"] for suffix in scheduled},
                )
            except Exception:
                errors.append("schedule-verification")
        for service in changed:
            try:
                ecs.update_service(
                    cluster=name,
                    service=service,
                    taskDefinition=old[service]["taskDefinition"],
                    desiredCount=old[service]["desiredCount"],
                )
            except Exception:
                errors.append("service:" + service)
        if errors:
            raise RuntimeError("Rollback requires operator attention for " + ", ".join(errors)) from failure
        if changed:
            stable(ecs, name, {service: old[service]["taskDefinition"] for service in changed})
        raise RuntimeError(
            "Rollout reverted. Migrations remain applied; use the documented expand-contract recovery procedure."
        ) from failure
    args.receipt.write_text(
        json.dumps(
            {
                "environment": args.environment,
                "image": args.image,
                "services": targets,
                "scheduled_operations": schedule_targets,
                "migrator": migrator,
            },
            indent=2,
        )
        + "\n"
    )
    print("Requested service revisions are healthy; both scheduled jobs use the same image digest.")


if __name__ == "__main__":
    main()
