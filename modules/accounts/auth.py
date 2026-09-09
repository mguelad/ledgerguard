import base64
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.control_plane.errors import Problem, problem_response
from modules.accounts.models import Membership


class LocalAuthenticationForm(AuthenticationForm):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Preserve document-order keyboard navigation and the skip link.
        self.fields["username"].widget.attrs.pop("autofocus", None)


class LocalLoginView(LoginView):
    template_name = "login.html"
    form_class = LocalAuthenticationForm
    next_page = "/"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if settings.PRODUCTION:
            return redirect("/auth/start")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: Any) -> HttpResponse:
        response = super().form_valid(form)
        self.request.session["authenticated_at"] = time.time()
        self.request.session["last_active_at"] = time.time()
        return response


def oidc_start(request: HttpRequest) -> HttpResponse:
    if not settings.COGNITO_DOMAIN or not settings.COGNITO_CLIENT_ID:
        return problem_response(request, Problem("AUTH_PROVIDER_UNAVAILABLE", 503))
    request.session.cycle_key()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    request.session["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier, "created_at": time.time()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": settings.COGNITO_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": settings.PUBLIC_URL + "/auth/callback",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    return redirect(settings.COGNITO_DOMAIN + "/oauth2/authorize?" + urlencode(params))


def oidc_callback(request: HttpRequest) -> HttpResponse:
    state = request.session.pop("oidc", None)
    if (
        not isinstance(state, dict)
        or type(state.get("created_at")) not in {int, float}
        or not all(isinstance(state.get(name), str) for name in ["state", "nonce", "verifier"])
        or time.time() - state["created_at"] > 600
        or state["created_at"] > time.time() + 60
        or not hmac.compare_digest(state["state"], request.GET.get("state", ""))
    ):
        return problem_response(request, Problem("INVALID_LOGIN_STATE", 400))
    try:
        response = httpx.post(
            settings.COGNITO_DOMAIN + "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.COGNITO_CLIENT_ID,
                "code": request.GET.get("code", ""),
                "redirect_uri": settings.PUBLIC_URL + "/auth/callback",
                "code_verifier": state["verifier"],
            },
            auth=(settings.COGNITO_CLIENT_ID, settings.COGNITO_CLIENT_SECRET)
            if settings.COGNITO_CLIENT_SECRET
            else None,
            timeout=15,
            follow_redirects=False,
        )
        response.raise_for_status()
        token = response.json()["id_token"]
        jwks = jwt.PyJWKClient(settings.COGNITO_ISSUER + "/.well-known/jwks.json", timeout=10)
        claims = jwt.decode(
            token,
            jwks.get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            audience=settings.COGNITO_CLIENT_ID,
            issuer=settings.COGNITO_ISSUER,
            options={"require": ["exp", "iat", "sub", "nonce", "auth_time"]},
        )
        authentication_time = claims.get("auth_time")
        email = claims.get("email")
        if (
            claims.get("token_use") != "id"
            or not isinstance(claims.get("nonce"), str)
            or not hmac.compare_digest(claims["nonce"], state["nonce"])
            or claims.get("email_verified") is not True
            or type(authentication_time) is not int
            or not authentication_time <= time.time() + 60
            or time.time() - authentication_time > 600
            or not isinstance(email, str)
            or not 3 <= len(email) <= 254
            or "@" not in email
        ):
            raise ValueError("Invalid authentication context")
        # Subject, not mutable email, is the stable identity. Cognito pool MFA is required by deployment.
        user, _ = get_user_model().objects.get_or_create(username="cognito:" + claims["sub"], defaults={"email": email})
        if not user.is_active:
            raise ValueError("Disabled identity")
        user.email = email
        user.set_unusable_password()
        user.save()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["authenticated_at"] = authentication_time
        request.session["last_active_at"] = time.time()
        return redirect("/")
    except (httpx.HTTPError, jwt.PyJWTError, TypeError, ValueError, KeyError):
        return problem_response(request, Problem("LOGIN_FAILED", 401))


@require_POST
def sign_out(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("/login/")


def home(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/login/")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.user_id',%s,true)", [str(request.user.pk)])
        memberships = list(Membership.objects.filter(user=request.user, active=True))
    return render(request, "organizations.html", {"memberships": memberships})
