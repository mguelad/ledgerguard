# Connector setup and operating contract

## Stripe App

Create a Stripe App using OAuth authentication. This is a Stripe App authorization, not a Connect platform. Request only `payment_intent_read`, `charge_read`, `checkout_session_read` and `event_read`. Refund reads are associated with the charge read permission; verify the actual installed grant in the sandbox. Never add payment, capture, refund or order write permissions.

Configure the exact callback `https://YOUR_ORIGIN/oauth/stripe/callback`. Keep separate developer keys/client IDs for each environment and test/live mode. These are operator-owned App credentials in Secrets Manager; merchants authorize through Stripe and never paste their account secret keys into LedgerGuard.

The authorization URL uses `https://marketplace.stripe.com/oauth/v2/authorize`; tokens use `POST https://api.stripe.com/v1/oauth/token` with App developer-key HTTP Basic authentication. Returned `scope`, `token_type`, account and mode are checked. Financial reads use approved GET paths only and the locked Stripe API version. One-hour access tokens are refreshed ahead of expiry while holding the connector lock. An uncertain rotating refresh is terminal for that authorization and requires renewed consent.

Create an organization and store in LedgerGuard. Choose the correct hostname and test/live mode, then Connect Stripe. In the store's connection controls, copy the opaque webhook destination and configure these payment/refund event families in the corresponding Stripe environment: PaymentIntent state changes, charge success/refund changes, refund status changes and Checkout Session completion/asynchronous result. Store the destination's `whsec_` signing secret using the authenticated control. The App must be configured to receive events from installed accounts; use the provider's current destination setup for App installs and test its actual account field behavior.

The webhook verifies raw-byte HMAC with a five-minute tolerance, destination namespace and event identity, then stores a minimal receipt. The worker retrieves the latest resource. Duplicate events do not duplicate findings. Configure a replacement secret before changing the destination; routine rotation accepts the preceding secret for twenty-four hours. For compromise, disconnect and revoke immediately instead of retaining overlap.

## WooCommerce plugin

Build with `python scripts/build_plugin.py --origin https://YOUR_ORIGIN`; install the emitted ZIP in WordPress. PHP 8.2+, 64-bit integers, Sodium, WooCommerce and Action Scheduler are required. Multisite is unsupported. Use the official Woo Stripe gateway versions included in the verified matrix; a custom gateway needs explicit compatibility evidence.

In WooCommerce → LedgerGuard, review the compiled destination and transmitted fields, select the gateway's current mode and enter a pairing code created by the agency's owner/admin. The code expires after ten minutes. Pairing requires `manage_woocommerce`, HTTPS, a WP nonce, merchant consent and proof of the private key. The service binds the key to the issued organization/store/mode. Local WordPress salts protect private signing material; salt changes require re-pairing.

The wire signature is Ed25519 over UTF-8 bytes:

```text
v1\ninstallation_id\nrequest_id\nsent_at\nsha256(raw_body)
```

The claim installation component is the literal `claim`. The request includes a UUID replay identifier and UTC timestamp. Bodies must be ≤1 MiB, depth ≤12 and ≤100 records. Unknown fields and schema versions fail closed. Redirects are disabled. There is no inbound command channel and no arbitrary destination setting.

A bounded ID snapshot prevents ordinary offset pagination skips during source changes. Scan pages carry stable semantic hashes; retries can re-sign the same page after the signature window. The server advances coverage only after contiguous acknowledged completion. `refund_ids` on each order declares its current refund inventory, including deletions. A source conflict or rejected record leaves coverage incomplete.

Hooks schedule a short-delay scan; fifteen-minute periodic scans use thirty-minute overlap, and daily repair covers the recent history window. The plugin limits one scan to 50,000 orders and leaves a visible error beyond that boundary. A maintenance-capable host must execute Action Scheduler reliably; low-traffic WP-Cron alone may miss freshness targets. Review a high-volume store before pilot admission.

## Before alerts

Both agency authorization and merchant pairing must be recorded. Complete all four source coverage ranges, verify an exact known order/payment pair and compare its currency/amount, run a dry reconciliation and review evidence. Only then enable store and organization alerts. Configure a verified recipient and test delivery plus feedback. Mode changes require a separate namespace; do not relabel historical data by changing a store's mode.

Disconnect stops intake and forgets stored credentials. Revoke/uninstall the App in Stripe to terminate provider authorization; disconnect on both WordPress and LedgerGuard for Woo. Keep financial findings unresolved until complete fresh source evidence supports resolution.

Sources: [Stripe App OAuth](https://docs.stripe.com/stripe-apps/api-authentication/oauth), [Stripe currencies](https://docs.stripe.com/currencies), [Woo order querying](https://developer.woocommerce.com/docs/features/orders/wc-get-orders/). Exact gateway metadata is restricted to verified identifiers such as `_stripe_intent_id`, `_stripe_checkout_session_id` and the CRUD transaction ID.
