<?php
/**
 * Plugin Name: LedgerGuard for WooCommerce
 * Description: Sends a minimal, signed order ledger for read-only payment integrity monitoring.
 * Version: 0.1.0
 * Requires at least: 7.0
 * Requires PHP: 8.2
 * Requires Plugins: woocommerce, woocommerce-gateway-stripe
 * License: GPL-2.0-or-later
 * Text Domain: ledgerguard
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit; }

define( 'LEDGERGUARD_VERSION', '0.1.0' );
require_once __DIR__ . '/includes/class-ledgerguard-crypto.php';
require_once __DIR__ . '/includes/class-ledgerguard-extractor.php';
require_once __DIR__ . '/includes/class-ledgerguard-client.php';
require_once __DIR__ . '/includes/class-ledgerguard-plugin.php';

add_action(
	'before_woocommerce_init',
	static function (): void {
		if ( class_exists( \Automattic\WooCommerce\Utilities\FeaturesUtil::class ) ) {
			\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', __FILE__, true );
		}
	}
);
register_activation_hook( __FILE__, array( 'LedgerGuard_Plugin', 'activate' ) );
register_deactivation_hook( __FILE__, array( 'LedgerGuard_Plugin', 'deactivate' ) );
add_action(
	'plugins_loaded',
	static function (): void {
		( new LedgerGuard_Plugin() )->register();
	}
);
