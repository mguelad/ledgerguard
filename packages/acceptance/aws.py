"""Read-only checks of an initialized AWS deployment, not a release approval."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

QUEUES = ("ingestion", "reconciliation", "notification", "reporting", "repair")
REGION = "eu-west-1"


def validate_config(config: dict[str, Any], account: str, environment: str, image: str) -> None:
    if not isinstance(config, dict) or any(not isinstance(value, str) for value in config.values()):
        raise ValueError("Expected an object containing only deployment identifiers")
    name = f"ledgerguard-{environment}"
    if (
        not re.fullmatch(r"\d{12}", account)
        or environment not in {"dev", "staging", "production"}
        or config.get("account_id") != account
        or config.get("environment") != environment
        or config.get("region") != REGION
        or config.get("database_identifier") != name
        or config.get("cluster_name") != name
        or not re.fullmatch(rf"{account}\.dkr\.ecr\.{REGION}\.amazonaws\.com/{name}@sha256:[a-f0-9]{{64}}", image)
        or not re.fullmatch(rf"arn:aws:kms:{REGION}:{account}:key/[a-f0-9-]{{36}}", config.get("kms_key_arn", ""))
        or not re.fullmatch(rf"{REGION}_[A-Za-z0-9]+", config.get("cognito_user_pool_id", ""))
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", config.get("email_domain", ""))
    ):
        raise ValueError("Expected account, environment and immutable deployment resources must agree")
    bucket = config.get("report_bucket", "")
    if (
        not re.fullmatch(rf"{name}-reports-[a-z0-9-]+", bucket)
        or len(bucket) > 63
        or config.get("deletion_ledger_bucket") != f"{name}-{account}-audit"
    ):
        raise ValueError("Expected environment-scoped report and erasure buckets")


def inspect(
    session: Any, config: dict[str, Any], account: str, environment: str, image: str, *, now: datetime | None = None
) -> dict[str, Any]:
    validate_config(config, account, environment, image)
    now = now or datetime.now(UTC)
    checks: list[dict[str, Any]] = []

    def check(name: str, operation: Callable[[], bool]) -> bool:
        try:
            passed = operation() is True
            checks.append({"name": name, "status": "pass" if passed else "fail"})
            return passed
        except (BotoCoreError, ClientError, KeyError, ValueError, TypeError, IndexError):
            # AWS exception messages can include configuration or account data.
            # Never serialize credentials, raw API results or exception strings.
            checks.append({"name": name, "status": "error"})
            return False

    def client(service: str) -> Any:
        return session.client(service, region_name=REGION)

    report = {
        "schema_version": 1,
        "kind": "aws-configuration-preflight",
        "checked_at": now.isoformat(),
        "environment": environment,
        "image": image,
        "checks": checks,
        "passed": False,
        "production_accepted": False,
    }
    if not check("caller.expected_account", lambda: client("sts").get_caller_identity()["Account"] == account):
        return report  # No resource inspection in an unexpected account.
    name, kms = config["cluster_name"], config["kms_key_arn"]
    rds, s3, sqs, ecs = (client(service) for service in ("rds", "s3", "sqs", "ecs"))

    def database() -> bool:
        rows = rds.describe_db_instances(DBInstanceIdentifier=config["database_identifier"])["DBInstances"]
        if len(rows) != 1:
            return False
        db = rows[0]
        latest = db.get("LatestRestorableTime")
        checks.append(
            {
                "name": "rds.pitr_recency_not_measured_rpo",
                "status": "pass"
                if isinstance(latest, datetime) and 0 <= (now - latest).total_seconds() <= 300
                else "fail",
            }
        )
        parameters = db["DBParameterGroups"]
        ssl = len(parameters) == 1 and parameters[0]["ParameterApplyStatus"] == "in-sync"
        if ssl:
            ssl = any(
                parameter["ParameterName"] == "rds.force_ssl" and parameter.get("ParameterValue") == "1"
                for page in rds.get_paginator("describe_db_parameters").paginate(
                    DBParameterGroupName=parameters[0]["DBParameterGroupName"]
                )
                for parameter in page["Parameters"]
            )
        return (
            db["DBInstanceStatus"] == "available"
            and db["Engine"] == "postgres"
            and db["EngineVersion"].split(".")[0] == "17"
            and db["PubliclyAccessible"] is False
            and db["StorageEncrypted"] is True
            and db["KmsKeyId"] == kms
            and db["BackupRetentionPeriod"] >= 35
            and db["MultiAZ"] is True
            and db["DeletionProtection"] is True
            and ssl
        )

    check("rds.private_encrypted_multi_az_tls_backups", database)
    key_client = client("kms")
    check("kms.enabled", lambda: key_client.describe_key(KeyId=kms)["KeyMetadata"]["KeyState"] == "Enabled")
    check("kms.rotation", lambda: key_client.get_key_rotation_status(KeyId=kms)["KeyRotationEnabled"] is True)

    def bucket(key: str) -> bool:
        args = {"Bucket": config[key], "ExpectedBucketOwner": account}
        block = s3.get_public_access_block(**args)["PublicAccessBlockConfiguration"]
        rules = s3.get_bucket_encryption(**args)["ServerSideEncryptionConfiguration"]["Rules"]
        return (
            all(
                block.get(field) is True
                for field in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
            )
            and len(rules) == 1
            and rules[0]["ApplyServerSideEncryptionByDefault"] == {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": kms}
            and s3.get_bucket_versioning(**args).get("Status") == "Enabled"
        )

    for key in ("report_bucket", "deletion_ledger_bucket"):
        check(f"s3.{key}.private_encrypted_versioned", partial(bucket, key))

    def mfa() -> bool:
        value = client("cognito-idp").get_user_pool_mfa_config(UserPoolId=config["cognito_user_pool_id"])
        return value["MfaConfiguration"] == "ON" and value["SoftwareTokenMfaConfiguration"]["Enabled"] is True

    check("cognito.required_totp_configuration", mfa)

    def queue(kind: str) -> bool:
        prefix = f"https://sqs.{REGION}.amazonaws.com/{account}/{name}-{kind}"
        work = sqs.get_queue_attributes(QueueUrl=prefix, AttributeNames=["All"])["Attributes"]
        dead = sqs.get_queue_attributes(QueueUrl=prefix + "-dlq", AttributeNames=["All"])["Attributes"]
        redrive = json.loads(work["RedrivePolicy"])
        return (
            work["KmsMasterKeyId"] == dead["KmsMasterKeyId"] == kms
            and redrive["deadLetterTargetArn"] == f"arn:aws:sqs:{REGION}:{account}:{name}-{kind}-dlq"
            and int(redrive["maxReceiveCount"]) == 8
            and int(work["VisibilityTimeout"]) >= 900
            and int(dead["MessageRetentionPeriod"]) >= 1209600
            and all(
                int(dead[field]) == 0
                for field in (
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                )
            )
        )

    for kind in QUEUES:
        check(f"sqs.{kind}.encryption_redrive_empty_dlq", partial(queue, kind))

    def service(kind: str) -> bool:
        value = ecs.describe_services(cluster=name, services=[f"{name}-{kind}"])
        if value.get("failures") or len(value["services"]) != 1:
            return False
        current = value["services"][0]
        definition = ecs.describe_task_definition(taskDefinition=current["taskDefinition"])["taskDefinition"]
        containers = definition["containerDefinitions"]
        return (
            current["status"] == "ACTIVE"
            and current["desiredCount"] >= (2 if kind == "api" and environment == "production" else 1)
            and current["runningCount"] == current["desiredCount"]
            and current["pendingCount"] == 0
            and len(current["deployments"]) == 1
            and current["deployments"][0]["rolloutState"] == "COMPLETED"
            and current["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"
            and len(containers) == 1
            and containers[0]["image"] == image
            and containers[0]["readonlyRootFilesystem"] is True
        )

    for kind in ("api", *QUEUES):
        check(f"ecs.{kind}.stable_private_expected_image", partial(service, kind))

    def schedule(kind: str) -> bool:
        value = client("scheduler").get_schedule(Name=f"{name}-{kind}")
        target = value["Target"]["EcsParameters"]
        definition = ecs.describe_task_definition(taskDefinition=target["TaskDefinitionArn"])["taskDefinition"]
        containers = definition["containerDefinitions"]
        return (
            value["State"] == "ENABLED"
            and target["NetworkConfiguration"]["awsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"
            and len(containers) == 1
            and containers[0]["image"] == image
        )

    for kind in ("scheduler", "maintenance"):
        check(f"scheduler.{kind}.enabled_expected_image", partial(schedule, kind))

    mail = client("sesv2")
    check("ses.sending_enabled", lambda: mail.get_account()["SendingEnabled"] is True)
    check("ses.outside_sandbox", lambda: mail.get_account()["ProductionAccessEnabled"] is True)
    check(
        "ses.domain_verified",
        lambda: mail.get_email_identity(EmailIdentity=config["email_domain"])["VerifiedForSendingStatus"] is True,
    )
    report["passed"] = all(item["status"] == "pass" for item in checks)
    return report
