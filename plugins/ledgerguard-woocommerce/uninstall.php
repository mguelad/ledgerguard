<?php
if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
	exit; }
foreach ( array( 'ledgerguard_identity', 'ledgerguard_rotation', 'ledgerguard_claim', 'ledgerguard_scan', 'ledgerguard_pending', 'ledgerguard_cursor', 'ledgerguard_health', 'ledgerguard_scan_lock' ) as $option ) {
	delete_option( $option ); }
if ( function_exists( 'as_unschedule_all_actions' ) ) {
	foreach ( array( 'ledgerguard_delta', 'ledgerguard_repair', 'ledgerguard_page', 'ledgerguard_heartbeat' ) as $hook ) {
		as_unschedule_all_actions( $hook, null, 'ledgerguard' ); }
	as_unschedule_all_actions( 'ledgerguard_delta', null, 'ledgerguard-fast' );
}
