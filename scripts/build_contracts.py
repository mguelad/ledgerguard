"""Maintain the versioned HTTP contract with explicit request and response shapes."""

import json
import os
import re
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.control_plane.settings")
django.setup()
from apps.control_plane.urls import urlpatterns  # noqa: E402

STRING = {"type": "string"}
UUID_VALUE = {"type": "string", "format": "uuid"}
TIMESTAMP = {"type": "string", "format": "date-time"}
BOOLEAN = {"type": "boolean"}
INTEGER = {"type": "integer", "minimum": 0}
MODE = {"enum": ["test", "live"]}


def obj(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields) if required is None else required,
        "additionalProperties": False,
    }


def arr(schema):
    return {"type": "array", "items": schema}


def enum(*v):
    return {"enum": list(v)}


status = obj({"status": STRING})
location = obj({"id": UUID_VALUE, "location": STRING})
version = obj({"lock_version": INTEGER})
requests = {
    "create_organization": obj({"name": STRING | {"minLength": 1, "maxLength": 100}}),
    "create_store": obj({"name": STRING | {"maxLength": 100}, "hostname": STRING | {"maxLength": 253}, "mode": MODE}),
    "pairing_code": obj({}),
    "connector_disconnect": obj({}),
    "verify_link": obj({"order_id": STRING | {"pattern": "^[1-9][0-9]*$"}}),
    "webhook_secret": obj({"signing_secret": STRING | {"pattern": "^whsec_[A-Za-z0-9]{16,200}$"}}),
    "finding_action": obj({"note": STRING | {"maxLength": 1000}, "until": TIMESTAMP}, []),
    "reports": obj({"format": enum("csv", "html", "pdf"), "store_id": UUID_VALUE}, ["format"]),
    "policy": obj(
        {
            "instant_grace_minutes": INTEGER | {"minimum": 5, "maximum": 60},
            "async_grace_minutes": INTEGER | {"minimum": 10, "maximum": 120},
            "refund_grace_minutes": INTEGER | {"minimum": 30, "maximum": 120},
            "freshness_minutes": INTEGER | {"minimum": 5, "maximum": 30},
            "disabled_rules": arr(enum(*[f"PI-{n:03}" for n in range(1, 11)])),
            "alerts_enabled": BOOLEAN,
            "shadow_rules": BOOLEAN,
            "review_dry_run": BOOLEAN,
        },
        [],
    ),
    "organization_policy": obj({"retention_days": enum(90, 180, 400), "alerts_enabled": BOOLEAN}, []),
    "invite_member": obj(
        {
            "email": STRING | {"format": "email"},
            "role": enum("admin", "analyst", "viewer", "merchant"),
            "store_id": UUID_VALUE,
        },
        ["email", "role"],
    ),
    "accept_invitation": obj({"token": STRING}),
    "membership_change": obj(
        {
            "membership_id": UUID_VALUE,
            "role": enum("admin", "analyst", "viewer", "merchant"),
            "active": BOOLEAN,
            "store_id": UUID_VALUE,
        },
        ["membership_id"],
    ),
    "support_grant": obj(
        {
            "support_user_id": INTEGER | {"minimum": 1},
            "reason": STRING | {"minLength": 10, "maxLength": 500},
            "minutes": INTEGER | {"minimum": 1, "maximum": 120},
            "revoke_id": UUID_VALUE,
        },
        [],
    ),
    "alert_route": obj({"membership_id": UUID_VALUE, "active": BOOLEAN}),
    "organization_delete": obj({"confirm_name": STRING}),
    "claim": {"$ref": "./schemas/woo-claim-1.0.json"},
    "batch": {"$ref": "./schemas/woo-batch-1.0.json"},
    "heartbeat": {"$ref": "./schemas/woo-heartbeat-1.0.json"},
    "rotate": {"$ref": "./schemas/woo-rotation-1.0.json"},
}
finding = obj(
    {
        "id": UUID_VALUE,
        "finding_key": STRING,
        "rule": STRING,
        "severity": STRING,
        "state": STRING,
        "store": STRING,
        "mode": MODE,
        "amount_minor": {"type": ["integer", "null"]},
        "currency": STRING,
        "exponent": INTEGER,
        "confidence": INTEGER,
        "first_seen": TIMESTAMP,
        "last_seen": TIMESTAMP,
        "identities": STRING,
        "next_step": STRING,
        "lock_version": INTEGER,
    }
)
coverage = obj(
    {
        "source": STRING,
        "covered_from": TIMESTAMP,
        "covered_through": TIMESTAMP,
        "observed_at": TIMESTAMP,
        "complete": BOOLEAN,
        "clock_offset_seconds": {"type": "integer"},
    }
)
run = obj(
    {
        "id": UUID_VALUE,
        "store_id": UUID_VALUE,
        "rule_version": STRING,
        "policy_version": INTEGER,
        "input_digest": STRING,
        "evaluated_at": TIMESTAMP,
        "coverage": arr(coverage),
        "warnings": arr(STRING),
        "shadow": BOOLEAN,
    }
)
responses = {
    "create_organization": location,
    "create_store": obj({"id": UUID_VALUE, "location": STRING, "lock_version": INTEGER}),
    "pairing_code": obj({"pairing_code": STRING, "expires_at": TIMESTAMP, "mode": MODE}),
    "verify_link": obj({"status": enum("verified", "conflict"), "payment_id": STRING, "detail": STRING}, ["status"]),
    "webhook_secret": obj({"destination_url": STRING, "lock_version": INTEGER}),
    "findings": obj({"results": arr(finding), "next_cursor": {"type": ["string", "null"]}}),
    "finding_action": obj({"id": UUID_VALUE, "state": STRING, "lock_version": INTEGER, "location": STRING}),
    "runs": obj({"results": arr(run)}),
    "reports": obj({"id": UUID_VALUE, "status": STRING, "location": STRING}),
    "policy": obj({"policy_version": INTEGER, "lock_version": INTEGER}),
    "organization_policy": obj(
        {"retention_days": enum(90, 180, 400), "alerts_enabled": BOOLEAN, "lock_version": INTEGER}
    ),
    "membership_change": status,
    "invite_member": obj({"invitation_token": STRING, "expires_at": TIMESTAMP}),
    "accept_invitation": obj({"location": STRING}),
    "support_grant": obj({"id": UUID_VALUE, "expires_at": TIMESTAMP, "status": STRING}, []),
    "alert_route": obj({"id": UUID_VALUE, "active": BOOLEAN}),
    "connector_disconnect": obj({"status": STRING}),
    "organization_delete": obj({"status": STRING}),
    "claim": obj(
        {
            "installation_id": UUID_VALUE,
            "key_id": UUID_VALUE,
            "mode": MODE,
            "server_time": TIMESTAMP,
            "schema_version": enum("1.0"),
        }
    ),
    "heartbeat": obj({"request_id": UUID_VALUE, "server_time": TIMESTAMP, "status": STRING}),
    "rotate": obj({"request_id": UUID_VALUE, "server_time": TIMESTAMP, "status": STRING}),
    "batch": obj(
        {
            "request_id": UUID_VALUE,
            "server_time": TIMESTAMP,
            "coverage_advanced": BOOLEAN,
            "results": arr(
                obj(
                    {"source_id": STRING, "status": STRING, "kind": STRING, "code": STRING},
                    ["source_id", "kind", "status"],
                )
            ),
        }
    ),
    "stripe_webhook": obj({"receipt_id": UUID_VALUE, "status": STRING}),
}
error = obj(
    {"type": STRING, "title": STRING, "status": INTEGER, "detail": STRING, "code": STRING, "correlation_id": STRING},
    ["type", "title", "status", "detail", "code", "correlation_id"],
)
doc = {
    "openapi": "3.1.0",
    "info": {
        "title": "LedgerGuard API",
        "version": "1.0.0",
        "description": "Read-only payment integrity control plane. Browser mutations require a CSRF token and idempotency key; versioned mutations also require If-Match.",
    },
    "servers": [{"url": "http://localhost:8000", "description": "Local development"}],
    "security": [{"Session": []}],
    "components": {
        "securitySchemes": {
            "Session": {"type": "apiKey", "in": "cookie", "name": "sessionid"},
            "WooSignature": {"type": "apiKey", "in": "header", "name": "X-LedgerGuard-Signature"},
            "StripeSignature": {"type": "apiKey", "in": "header", "name": "Stripe-Signature"},
        },
        "schemas": {"Problem": error},
    },
    "paths": {},
}
for route in urlpatterns:
    raw = str(route.pattern)
    if not raw.startswith(("api/", "webhooks/", "oauth/")):
        continue
    name = route.callback.__name__
    path = "/" + re.sub(r"<(?:uuid|str):(\w+)>", r"{\1}", raw)
    method = "get" if name in {"oauth_start", "oauth_callback", "findings", "runs", "report_download"} else "post"
    parameters = []
    for kind, key in re.findall(r"<(uuid|str):(\w+)>", raw):
        parameters.append(
            {
                "name": key,
                "in": "path",
                "required": True,
                "schema": enum("acknowledge", "suppress", "resolve", "note")
                if key == "action_name"
                else UUID_VALUE
                if kind == "uuid"
                else STRING,
            }
        )
    if method == "post" and not raw.startswith(("api/v1/plugin", "webhooks/")):
        for key in ["X-CSRFToken"] + ([] if name == "accept_invitation" else ["Idempotency-Key"]):
            parameters.append({"name": key, "in": "header", "required": True, "schema": STRING})
    if name in {"finding_action", "policy", "organization_policy", "webhook_secret", "connector_disconnect"}:
        parameters.append(
            {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "schema": STRING,
                "description": "Current resource lock_version, optionally quoted",
            }
        )
    if name == "oauth_start":
        parameters.append({"name": "store_id", "in": "query", "required": True, "schema": UUID_VALUE})
    if name == "oauth_callback":
        parameters += [{"name": key, "in": "query", "required": True, "schema": STRING} for key in ["state", "code"]]
    if name == "findings":
        parameters += [{"name": key, "in": "query", "schema": STRING} for key in ["state", "rule", "cursor"]]
    success = (
        "201"
        if name in {"create_organization", "create_store", "pairing_code", "invite_member", "support_grant", "claim"}
        else "202"
        if name in {"reports", "organization_delete"}
        else "302"
        if name in {"oauth_start", "oauth_callback"}
        else "200"
    )
    response = {"description": "Successful request"}
    if success == "302":
        response["headers"] = {"Location": {"schema": STRING}}
    elif name == "report_download":
        response["content"] = {
            mime: {"schema": {"type": "string", "format": "binary"}}
            for mime in ["text/csv", "text/html", "application/pdf"]
        }
    else:
        response["content"] = {"application/json": {"schema": responses[name]}}
    operation = {
        "operationId": name,
        "summary": name.replace("_", " ").capitalize(),
        "parameters": parameters,
        "responses": {
            success: response,
            "default": {
                "description": "Rejected request; no source financial mutation",
                "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
            },
        },
    }
    if name in {"claim", "support_grant"}:
        operation["responses"]["200"] = response
    if name == "report_download":
        operation["responses"]["302"] = {
            "description": "Short-lived private object download",
            "headers": {"Location": {"schema": STRING}},
        }
    if name in requests:
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": requests[name]}}}
    if raw.startswith("api/v1/plugin"):
        operation["security"] = [{"WooSignature": []}]
        parameters.append({"name": "X-LedgerGuard-Key-ID", "in": "header", "required": True, "schema": UUID_VALUE})
    if raw.startswith("webhooks/"):
        operation["security"] = [{"StripeSignature": []}]
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": obj(
                        {
                            "id": STRING,
                            "type": STRING,
                            "created": INTEGER,
                            "livemode": BOOLEAN,
                            "account": STRING,
                            "data": {"type": "object"},
                        },
                        ["id", "type", "created", "livemode", "data"],
                    )
                }
            },
        }
    doc["paths"][path] = {method: operation}
Path("contracts/openapi.json").write_text(json.dumps(doc, indent=2) + "\n")
print(f"Contract written: {len(doc['paths'])} paths")
