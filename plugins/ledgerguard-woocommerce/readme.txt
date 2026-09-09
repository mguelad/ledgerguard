=== LedgerGuard ===
Requires at least: 7.0
Tested up to: 7.1
Requires PHP: 8.2
Stable tag: 0.1.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Read-only payment integrity observations for LedgerGuard. Requires WooCommerce, the official Stripe gateway, Sodium, 64-bit PHP and a single-site installation.

== Installation ==
Build the plugin for your verified HTTPS LedgerGuard origin using scripts/build_plugin.py. Install the resulting ZIP. In WooCommerce > LedgerGuard, review the destination and transmitted fields, confirm the gateway mode and enter the one-time pairing code issued by your organization.

== Data handling ==
Only allowlisted order/refund identifiers, status, money values, Stripe references and timestamps leave the store. Customer names, emails, addresses, order items, arbitrary metadata and credentials are excluded. Private signing keys are protected by Sodium secretbox with a key derived from WordPress salts. Rotating WordPress salts requires re-pairing. The plugin has no inbound cloud command endpoint.

== Recovery ==
Correct the health error first, then use Retry a full repair. Rotating a signing key retains its pending identity across uncertain responses. Disconnecting removes local credentials and stops jobs; disconnect the installation in LedgerGuard as well. After a gateway mode change, use separate test/live installations and review historic data before enabling alerts.

== Changelog ==
= 0.1.0 =
Initial implementation of signed pairing, contiguous scan batches, overlapping repair, key rotation and connection health.
