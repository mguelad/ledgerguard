import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.test import Client

from modules.accounts import auth
from modules.accounts.models import AuditLog, Membership, Organization
from modules.accounts.tenancy import tenant_scope
from modules.connectors.models import Store
from modules.findings.models import AlertRoute
from tests.test_connector_journey import operator as operator
from tests.test_connector_journey import post

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


def signed_in(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    session = client.session
    session["authenticated_at"] = time.time()
    session.save()
    client.get("/")
    return client


def test_invitation_role_scope_revocation_and_owner_protection(operator):
    _, owner = operator
    org = post(owner, "/api/v1/organizations", {"name": "Agency"}).json()["id"]
    stores = [
        post(
            owner,
            f"/api/v1/organizations/{org}/stores",
            {"name": name, "hostname": name + ".example.com", "mode": "test"},
        ).json()["id"]
        for name in ["shop", "second"]
    ]
    merchant = get_user_model().objects.create_user(username="merchant", email="merchant@example.com")
    viewer = get_user_model().objects.create_user(username="viewer", email="viewer@example.com")
    client = signed_in(merchant)
    invitation = post(
        owner,
        f"/api/v1/organizations/{org}/members/invite",
        {"email": merchant.email, "role": "merchant", "store_id": stores[0]},
    ).json()
    assert (
        post(signed_in(viewer), "/api/v1/invitations/accept", {"token": invitation["invitation_token"]}).status_code
        == 404
    )
    accepted = post(client, "/api/v1/invitations/accept", {"token": invitation["invitation_token"]})
    assert accepted.json()["location"] == f"/stores/{stores[0]}/"
    assert post(client, "/api/v1/invitations/accept", {"token": invitation["invitation_token"]}).status_code == 404
    assert client.get(f"/stores/{stores[0]}/").status_code == 200
    assert client.get(f"/stores/{stores[1]}/").status_code == 404
    assert client.get(f"/o/{org}/").status_code == 404
    assert post(client, f"/api/v1/stores/{stores[0]}/pairing-codes", {}).status_code == 403
    with tenant_scope(org):
        member = Membership.objects.get(user=merchant)
        original_owner = Membership.objects.get(role="owner")
    assert (
        post(
            owner, f"/api/v1/organizations/{org}/members", {"membership_id": str(original_owner.id), "active": False}
        ).status_code
        == 409
    )
    assert (
        post(
            owner, f"/api/v1/organizations/{org}/members", {"membership_id": str(member.id), "role": "viewer"}
        ).status_code
        == 200
    )
    assert client.get(f"/o/{org}/").status_code == 200
    assert post(client, f"/api/v1/organizations/{org}/policy", {"alerts_enabled": True}, version=0).status_code == 403
    assert (
        post(
            owner, f"/api/v1/organizations/{org}/alert-routes", {"membership_id": str(member.id), "active": True}
        ).status_code
        == 200
    )
    assert (
        post(
            owner, f"/api/v1/organizations/{org}/members", {"membership_id": str(member.id), "active": False}
        ).status_code
        == 200
    )
    assert client.get(f"/o/{org}/").status_code == 404
    with tenant_scope(org):
        assert not AlertRoute.objects.get(email=merchant.email).active


def test_support_requires_explicit_grant_is_read_only_and_revocable(operator):
    _, owner = operator
    org = post(owner, "/api/v1/organizations", {"name": "Agency"}).json()["id"]
    support = get_user_model().objects.create_user(username="support", is_staff=True, email="support@example.com")
    client = signed_in(support)
    assert client.get(f"/o/{org}/").status_code == 404
    base = f"/api/v1/organizations/{org}"
    assert (
        post(
            owner,
            base + "/support-grants",
            {"support_user_id": "bad", "reason": "Review connector coverage", "minutes": 30},
        ).status_code
        == 422
    )
    grant = post(
        owner,
        base + "/support-grants",
        {"support_user_id": support.id, "reason": "Review connector coverage", "minutes": 30},
    )
    assert grant.status_code == 201
    assert client.get(f"/o/{org}/").status_code == 200
    assert (
        post(
            client, base + "/stores", {"name": "Forbidden", "hostname": "shop.example.com", "mode": "test"}
        ).status_code
        == 403
    )
    with tenant_scope(org):
        assert AuditLog.objects.filter(action="support.request", actor_id=str(support.id)).count() == 1
        assert not Store.objects.exists()
    assert post(owner, base + "/support-grants", {"revoke_id": grant.json()["id"]}).status_code == 200
    assert client.get(f"/o/{org}/").status_code == 404


def test_organization_policy_versions_reauthentication_and_erasure(operator):
    _, owner = operator
    org = post(owner, "/api/v1/organizations", {"name": "Agency"}).json()["id"]
    with tenant_scope(org):
        version = Organization.objects.get(id=org).lock_version
    base = f"/api/v1/organizations/{org}"
    assert post(owner, base + "/policy", {"retention_days": 91}, version).status_code == 422
    changed = post(owner, base + "/policy", {"retention_days": 90, "alerts_enabled": True}, version)
    assert changed.json() == {"retention_days": 90, "alerts_enabled": True, "lock_version": version + 1}
    assert post(owner, base + "/policy", {"alerts_enabled": False}, version).status_code == 409
    assert post(owner, base + "/policy", {"alerts_enabled": False}, version + 1).status_code == 200
    session = owner.session
    session["authenticated_at"] = time.time() - 601
    session.save()
    assert post(owner, base + "/delete", {"confirm_name": "Agency"}).status_code == 403
    session = owner.session
    session["authenticated_at"] = time.time()
    session.save()
    assert post(owner, base + "/delete", {"confirm_name": "Wrong"}).status_code == 422
    assert post(owner, base + "/delete", {"confirm_name": "Agency"}).status_code == 202
    assert owner.get(f"/o/{org}/").status_code == 403


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "nonce",
        "issuer",
        "audience",
        "email_verified",
        "token_use",
        "auth_time",
        "future_auth_time",
        "typed_auth_time",
        "oversize_email",
    ],
)
def test_oidc_signed_claims_pkce_and_single_use_state(monkeypatch, settings, failure):
    settings.COGNITO_DOMAIN = "https://identity.example.com"
    settings.COGNITO_CLIENT_ID = "fixture-client"
    settings.COGNITO_ISSUER = "https://identity.example.com/pool"
    client = Client()
    start = client.get("/auth/start")
    params = parse_qs(urlparse(start["Location"]).query)
    assert params["code_challenge_method"] == ["S256"] and params["prompt"] == ["login"]
    state = client.session["oidc"]
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "sub": "subject-123",
        "iss": settings.COGNITO_ISSUER,
        "aud": settings.COGNITO_CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "auth_time": int(time.time()),
        "nonce": state["nonce"],
        "email": "verified@example.com",
        "email_verified": True,
        "token_use": "id",
    }
    changes = {
        "nonce": {"nonce": "wrong"},
        "issuer": {"iss": "https://wrong.example.com"},
        "audience": {"aud": "wrong"},
        "email_verified": {"email_verified": False},
        "token_use": {"token_use": "access"},
        "auth_time": {"auth_time": int(time.time()) - 601},
        "future_auth_time": {"auth_time": int(time.time()) + 61},
        "typed_auth_time": {"auth_time": "recent"},
        "oversize_email": {"email": "x" * 250 + "@example.com"},
    }
    claims.update(changes.get(failure, {}))
    token = jwt.encode(claims, private, algorithm="RS256")
    post_token = Mock(
        return_value=httpx.Response(
            200, request=httpx.Request("POST", settings.COGNITO_DOMAIN), json={"id_token": token}
        )
    )
    monkeypatch.setattr(auth.httpx, "post", post_token)
    monkeypatch.setattr(
        auth.jwt,
        "PyJWKClient",
        lambda *a, **k: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key())),
    )
    result = client.get("/auth/callback", {"state": state["state"], "code": "synthetic-code"})
    assert result.status_code == (401 if failure else 302)
    assert post_token.call_args.kwargs["data"]["code_verifier"] == state["verifier"]
    assert client.get("/auth/callback", {"state": state["state"], "code": "synthetic-code"}).status_code == 400
    if not failure:
        user = get_user_model().objects.get(username="cognito:subject-123")
        assert not user.has_usable_password()
        assert client.post("/logout/").status_code == 302


def test_idle_session_is_revoked(operator, settings):
    _, client = operator
    settings.SESSION_IDLE_SECONDS = 60
    session = client.session
    session["last_active_at"] = time.time() - 61
    session.save()
    assert client.get("/")["Location"] == "/login/"
