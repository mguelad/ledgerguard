import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from packages.acceptance import aws
from packages.acceptance.stripe_app import PERMISSIONS, https_origin, manifest, validate_manifest

ACCOUNT = "123456789012"
NAME = "ledgerguard-staging"
IMAGE = f"{ACCOUNT}.dkr.ecr.eu-west-1.amazonaws.com/{NAME}@sha256:" + "a" * 64
KMS = f"arn:aws:kms:eu-west-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"
NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
CONFIG = {
    "account_id": ACCOUNT,
    "environment": "staging",
    "region": "eu-west-1",
    "cluster_name": NAME,
    "database_identifier": NAME,
    "kms_key_arn": KMS,
    "cognito_user_pool_id": "eu-west-1_Example",
    "report_bucket": NAME + "-reports-fixture",
    "deletion_ledger_bucket": f"{NAME}-{ACCOUNT}-audit",
    "email_domain": "mail.example.com",
}


def test_stripe_manifest_is_exactly_read_only_oauth_and_reproducible():
    value = manifest("com.ledgerguard.staging", "https://staging.example.com", "0.1.0")
    assert {item["permission"] for item in value["permissions"]} == set(PERMISSIONS)
    assert all(item["permission"].endswith("_read") for item in value["permissions"])
    assert value["allowed_redirect_uris"] == ["https://staging.example.com/oauth/stripe/callback"]
    assert value["distribution_type"] == "public" and value["stripe_api_access_type"] == "oauth"
    assert value["sandbox_install_compatible"] is False
    validate_manifest(value, "com.ledgerguard.staging", "https://staging.example.com", "0.1.0")
    for field, replacement in [
        ("permissions", [{"permission": "charge_write", "purpose": "bad"}]),
        ("permissions", value["permissions"] + [{"permission": "customer_read", "purpose": "unneeded"}]),
        ("stripe_api_access_type", "platform"),
        ("allowed_redirect_uris", ["https://other.example.com/oauth/stripe/callback"]),
        ("sandbox_install_compatible", True),
    ]:
        with pytest.raises(ValueError):
            validate_manifest(
                value | {field: replacement}, "com.ledgerguard.staging", "https://staging.example.com", "0.1.0"
            )


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/",
        "https://example.com?q=secret",
        "https://example.com#fragment",
        "https://example.com:8443",
        "https://api.ledgerguard.invalid",
        "https://127.0.0.1",
        "https://localhost",
        "https://host.local",
        "https://example.com\n",
    ],
)
def test_manifest_rejects_unsafe_or_placeholder_origins(origin):
    with pytest.raises(ValueError):
        https_origin(origin)


def test_manifest_rejects_unselected_identity_and_version():
    with pytest.raises(ValueError):
        manifest("com.example.app", "https://staging.example.com", "0.1.0")
    with pytest.raises(ValueError):
        manifest("com.ledgerguard.staging", "https://staging.example.com", "latest")


@pytest.fixture
def cloud():
    services = {name: Mock() for name in ["sts", "rds", "kms", "s3", "sqs", "ecs", "scheduler", "cognito-idp", "sesv2"]}
    session = Mock()
    session.client.side_effect = lambda name, **kwargs: services[name]
    services["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    db = {
        "DBInstanceStatus": "available",
        "Engine": "postgres",
        "EngineVersion": "17.6",
        "PubliclyAccessible": False,
        "StorageEncrypted": True,
        "KmsKeyId": KMS,
        "BackupRetentionPeriod": 35,
        "MultiAZ": True,
        "DeletionProtection": True,
        "LatestRestorableTime": NOW - timedelta(seconds=60),
        "DBParameterGroups": [{"DBParameterGroupName": "fixture", "ParameterApplyStatus": "in-sync"}],
    }
    services["rds"].describe_db_instances.return_value = {"DBInstances": [db]}
    services["rds"].get_paginator.return_value.paginate.return_value = [
        {"Parameters": [{"ParameterName": "other"}]},
        {"Parameters": [{"ParameterName": "rds.force_ssl", "ParameterValue": "1"}]},
    ]
    services["kms"].describe_key.return_value = {"KeyMetadata": {"KeyState": "Enabled"}}
    services["kms"].get_key_rotation_status.return_value = {"KeyRotationEnabled": True}
    services["s3"].get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
    }
    services["s3"].get_bucket_encryption.return_value = {
        "ServerSideEncryptionConfiguration": {
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": KMS}},
            ]
        }
    }
    services["s3"].get_bucket_versioning.return_value = {"Status": "Enabled"}
    services["cognito-idp"].get_user_pool_mfa_config.return_value = {
        "MfaConfiguration": "ON",
        "SoftwareTokenMfaConfiguration": {"Enabled": True},
    }

    def queue(QueueUrl, AttributeNames):
        kind = QueueUrl.rsplit("/", 1)[1]
        return {
            "Attributes": {
                "KmsMasterKeyId": KMS,
                "VisibilityTimeout": "900",
                "MessageRetentionPeriod": "1209600",
                "ApproximateNumberOfMessages": "0",
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": f"arn:aws:sqs:eu-west-1:{ACCOUNT}:{kind}-dlq", "maxReceiveCount": 8}
                ),
            }
        }

    services["sqs"].get_queue_attributes.side_effect = queue
    services["ecs"].describe_services.return_value = {
        "services": [
            {
                "status": "ACTIVE",
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
                "deployments": [{"rolloutState": "COMPLETED"}],
                "taskDefinition": "fixture-task",
                "networkConfiguration": {"awsvpcConfiguration": {"assignPublicIp": "DISABLED"}},
            }
        ]
    }
    services["ecs"].describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"image": IMAGE, "readonlyRootFilesystem": True}],
        }
    }
    services["scheduler"].get_schedule.return_value = {
        "State": "ENABLED",
        "Target": {
            "EcsParameters": {
                "TaskDefinitionArn": "fixture-task",
                "NetworkConfiguration": {"awsvpcConfiguration": {"AssignPublicIp": "DISABLED"}},
            }
        },
    }
    services["sesv2"].get_account.return_value = {"SendingEnabled": True, "ProductionAccessEnabled": True}
    services["sesv2"].get_email_identity.return_value = {"VerifiedForSendingStatus": True}
    return session, services


def inspect(cloud):
    return aws.inspect(cloud[0], CONFIG, ACCOUNT, "staging", IMAGE, now=NOW)


def test_aws_preflight_is_read_only_bounded_and_not_production_acceptance(cloud):
    report = inspect(cloud)
    assert report["passed"] is True and report["production_accepted"] is False
    assert len(report["checks"]) == 24
    for service in cloud[1].values():
        assert all(str(call[0]).split(".")[0].startswith(("get_", "describe_")) for call in service.mock_calls)
    for call in cloud[1]["s3"].get_bucket_encryption.call_args_list:
        assert call.kwargs["ExpectedBucketOwner"] == ACCOUNT
    assert "fixture-task" not in json.dumps(report)


def test_wrong_aws_account_stops_before_resource_access(cloud):
    cloud[1]["sts"].get_caller_identity.return_value = {"Account": "999999999999"}
    assert inspect(cloud)["passed"] is False
    assert cloud[0].client.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("region", "us-east-1"),
        ("account_id", "999999999999"),
        ("environment", "production"),
        ("cluster_name", "unrelated"),
        ("database_identifier", "unrelated"),
        ("kms_key_arn", "alias/unrelated"),
        ("cognito_user_pool_id", "us-east-1_Other"),
        ("report_bucket", "unrelated"),
        ("deletion_ledger_bucket", "unrelated"),
    ],
)
def test_mismatched_aws_targets_fail_before_api_access(cloud, field, value):
    with pytest.raises(ValueError):
        aws.inspect(cloud[0], CONFIG | {field: value}, ACCOUNT, "staging", IMAGE)
    cloud[0].client.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("PubliclyAccessible", True),
        ("StorageEncrypted", False),
        ("MultiAZ", False),
        ("DeletionProtection", False),
        ("BackupRetentionPeriod", 7),
        ("EngineVersion", "16.4"),
        ("LatestRestorableTime", NOW - timedelta(minutes=6)),
        ("LatestRestorableTime", None),
        ("LatestRestorableTime", NOW + timedelta(seconds=1)),
    ],
)
def test_database_drift_or_missing_backup_evidence_fails(cloud, field, value):
    cloud[1]["rds"].describe_db_instances.return_value["DBInstances"][0][field] = value
    assert inspect(cloud)["passed"] is False


@pytest.mark.parametrize(
    "service,method,response",
    [
        ("rds", "describe_db_instances", {"DBInstances": []}),
        ("kms", "get_key_rotation_status", {"KeyRotationEnabled": False}),
        ("s3", "get_bucket_versioning", {"Status": "Suspended"}),
        ("cognito-idp", "get_user_pool_mfa_config", {"MfaConfiguration": "OPTIONAL"}),
        ("ecs", "describe_services", {"services": [], "failures": [{"reason": "MISSING"}]}),
        ("scheduler", "get_schedule", {}),
        ("sesv2", "get_account", {"SendingEnabled": True, "ProductionAccessEnabled": False}),
    ],
)
def test_missing_or_unsafe_aws_configuration_never_passes(cloud, service, method, response):
    getattr(cloud[1][service], method).return_value = response
    assert inspect(cloud)["passed"] is False


def test_preflight_errors_are_redacted_and_other_checks_continue(cloud):
    cloud[1]["rds"].describe_db_instances.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-value-do-not-emit"}}, "DescribeDBInstances"
    )
    report = inspect(cloud)
    assert report["passed"] is False and "secret-value" not in json.dumps(report)
    assert report["checks"][1]["status"] == "error"
    cloud[1]["sesv2"].get_email_identity.assert_called_once()


def test_nonempty_dlq_and_wrong_running_image_fail(cloud):
    original = cloud[1]["sqs"].get_queue_attributes.side_effect

    def queue(**kwargs):
        result = original(**kwargs)
        result["Attributes"]["ApproximateNumberOfMessages"] = "1"
        return result

    cloud[1]["sqs"].get_queue_attributes.side_effect = queue
    cloud[1]["ecs"].describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"image": IMAGE[:-1] + "b", "readonlyRootFilesystem": True}],
        }
    }
    report = inspect(cloud)
    assert not report["passed"]
    assert all(item["status"] == "fail" for item in report["checks"] if item["name"].startswith(("ecs.", "sqs.")))


def test_rds_parameter_pagination_must_find_forced_tls(cloud):
    cloud[1]["rds"].get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
    assert not inspect(cloud)["passed"]


def test_invalid_image_never_contacts_aws(cloud):
    with pytest.raises(ValueError):
        aws.inspect(cloud[0], copy.deepcopy(CONFIG), ACCOUNT, "staging", IMAGE.split("@")[0] + ":latest")
    cloud[0].client.assert_not_called()


@pytest.mark.parametrize("config", [[], {"account_id": None}])
def test_malformed_configuration_is_rejected_without_api_calls(cloud, config):
    with pytest.raises(ValueError):
        aws.inspect(cloud[0], config, ACCOUNT, "staging", IMAGE)
    cloud[0].client.assert_not_called()
