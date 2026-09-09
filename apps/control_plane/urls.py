from django.urls import path

from modules.accounts import auth
from modules.connectors import views

from . import api, dashboard
from .health import live, ready

urlpatterns = [
    path("", auth.home, name="home"),
    path("login/", auth.LocalLoginView.as_view(), name="login"),
    path("logout/", auth.sign_out),
    path("auth/start", auth.oidc_start),
    path("auth/callback", auth.oidc_callback),
    path("o/<uuid:organization_id>/", dashboard.overview),
    path("o/<uuid:organization_id>/settings/", dashboard.organization_settings),
    path("stores/<uuid:store_id>/", dashboard.store_detail),
    path("findings/<uuid:finding_id>/", dashboard.finding_detail),
    path("reports/<uuid:report_id>/", dashboard.report_detail),
    path("api/v1/organizations", api.create_organization),
    path("api/v1/organizations/<uuid:organization_id>/stores", api.create_store),
    path("api/v1/organizations/<uuid:organization_id>/members/invite", api.invite_member),
    path("api/v1/organizations/<uuid:organization_id>/members", api.membership_change),
    path("api/v1/invitations/accept", api.accept_invitation),
    path("api/v1/organizations/<uuid:organization_id>/support-grants", api.support_grant),
    path("api/v1/organizations/<uuid:organization_id>/alert-routes", api.alert_route),
    path("api/v1/organizations/<uuid:organization_id>/delete", api.organization_delete),
    path("api/v1/organizations/<uuid:organization_id>/policy", api.organization_policy),
    path("api/v1/stores/<uuid:store_id>/pairing-codes", api.pairing_code),
    path("api/v1/stores/<uuid:store_id>/verify-link", api.verify_link),
    path("api/v1/stores/<uuid:store_id>/policy", api.policy),
    path("api/v1/stores/<uuid:store_id>/findings", api.findings),
    path("api/v1/plugin/installations/claim", views.claim),
    path("api/v1/plugin/facts/batch", views.batch),
    path("api/v1/plugin/heartbeat", views.heartbeat),
    path("api/v1/plugin/keys/rotate", views.rotate),
    path("api/v1/stripe/oauth/start", api.oauth_start),
    path("oauth/stripe/callback", api.oauth_callback),
    path("webhooks/stripe/<uuid:destination_id>", views.stripe_webhook),
    path("api/v1/connectors/<uuid:connector_id>/disconnect", api.connector_disconnect),
    path("api/v1/connectors/<uuid:connector_id>/webhook-secret", api.webhook_secret),
    path("api/v1/findings/<uuid:finding_id>/<str:action_name>", api.finding_action),
    path("api/v1/reconciliation-runs", api.runs),
    path("api/v1/reports", api.reports),
    path("api/v1/reports/<uuid:report_id>/download", api.report_download),
    path("health/live", live),
    path("health/ready", ready),
]
