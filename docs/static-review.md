# Static review decisions

Review owner: repository maintainer. Review again before the next connected release, no later than 7 December 2026.

- PHPStan runs at level 6 against pinned WordPress 7.1 and WooCommerce 11.1 declarations. The small Action Scheduler declaration file mirrors the public functions used by the connector and is not bundled in the plugin.
- The PHP minimum-version activation guard is intentionally retained although the analyser assumes supported PHP. Its one narrow `smaller.alwaysFalse` annotation does not disable other checks.
- WPCS recognizes WooCommerce's registered `manage_woocommerce` capability. Base64 calls encode binary protocol material; local annotations identify those uses. A bounded page loop uses changing array counts; counting a PHP array is constant time.
- Documentation-comment layout checks are excluded from WPCS; executable code formatting and WordPress security checks remain active. Public PHP interfaces carry native types and iterable value annotations.
- Runtime logs use an explicit field allowlist. ALB raw request access logs are disabled to avoid persisting OAuth codes or source query strings. CloudTrail integrity validation and application audit history remain enabled.

No dependency vulnerability, scanner finding or permission grant is waived by this document. A changed dependency, protocol or release environment requires renewed review.
