<?php
/** Observation jobs, pairing and administrator controls. */
final class LedgerGuard_Plugin {
	public static function activate(): void {
		// @phpstan-ignore smaller.alwaysFalse (Activation must reject hosts below the declared minimum.)
		if ( PHP_VERSION_ID < 80200 || ! extension_loaded( 'sodium' ) || is_multisite() || PHP_INT_SIZE < 8 ) {
			deactivate_plugins( plugin_basename( dirname( __DIR__ ) . '/ledgerguard-woocommerce.php' ) );
			wp_die( esc_html__( 'LedgerGuard requires PHP 8.2+, Sodium, a 64-bit host and a single-site installation.', 'ledgerguard' ) );
		}
	}

	public static function deactivate(): void {
		if ( function_exists( 'as_unschedule_all_actions' ) ) {
			foreach ( array( 'ledgerguard_delta', 'ledgerguard_repair', 'ledgerguard_page', 'ledgerguard_heartbeat' ) as $hook ) {
				as_unschedule_all_actions( $hook, null, 'ledgerguard' );
				as_unschedule_all_actions( $hook, null, 'ledgerguard-fast' ); }
		}
		delete_option( 'ledgerguard_scan_lock' );
	}

	public function register(): void {
		if ( ! class_exists( 'WooCommerce' ) || ! function_exists( 'as_enqueue_async_action' ) || ! extension_loaded( 'sodium' ) ) {
			return; }
		add_action( 'admin_menu', array( $this, 'menu' ) );
		add_action( 'admin_post_ledgerguard_pair', array( $this, 'pair' ) );
		add_action( 'admin_post_ledgerguard_disconnect', array( $this, 'disconnect' ) );
		add_action( 'admin_post_ledgerguard_rotate', array( $this, 'rotate' ) );
		add_action( 'admin_post_ledgerguard_retry', array( $this, 'retry' ) );
		add_action( 'action_scheduler_init', array( $this, 'schedule' ) );
		foreach ( array( 'woocommerce_payment_complete', 'woocommerce_order_status_changed', 'woocommerce_order_refunded', 'woocommerce_refund_deleted' ) as $hook ) {
			add_action( $hook, array( $this, 'order_changed' ), 20, 1 ); }
		add_action(
			'ledgerguard_delta',
			function (): void {
				$this->start_scan( false );
			}
		);
		add_action(
			'ledgerguard_repair',
			function (): void {
				$this->start_scan( true );
			}
		);
		add_action( 'ledgerguard_page', array( $this, 'page' ) );
		add_action( 'ledgerguard_heartbeat', array( $this, 'heartbeat' ) );
	}

	public function schedule(): void {
		if ( ! get_option( 'ledgerguard_identity' ) ) {
			return; }
		foreach ( array(
			'ledgerguard_delta'     => 900,
			'ledgerguard_repair'    => 86400,
			'ledgerguard_heartbeat' => 300,
		) as $hook => $interval ) {
			if ( ! as_has_scheduled_action( $hook, null, 'ledgerguard' ) ) {
				as_schedule_recurring_action( time() + 30, $interval, $hook, array(), 'ledgerguard' ); }
		}
	}

	public function order_changed( int $id ): void {
		if ( (int) $id < 1 ) {
			return; }
		if ( get_option( 'ledgerguard_identity' ) && ! as_has_scheduled_action( 'ledgerguard_delta', array(), 'ledgerguard-fast' ) ) {
			as_schedule_single_action( time() + 130, 'ledgerguard_delta', array(), 'ledgerguard-fast' );
		}
	}

	private function authorize( string $action ): void {
		if ( ! current_user_can( 'manage_woocommerce' ) || ! is_ssl() ) {
			wp_die( esc_html__( 'A secure administrator session is required.', 'ledgerguard' ), 403 ); }
		check_admin_referer( $action );
	}

	public function menu(): void {
		add_submenu_page( 'woocommerce', 'LedgerGuard', 'LedgerGuard', 'manage_woocommerce', 'ledgerguard', array( $this, 'screen' ) );
	}

	public function screen(): void {
		if ( ! current_user_can( 'manage_woocommerce' ) ) {
			return; }
		$identity = get_option( 'ledgerguard_identity', array() );
		$health   = get_option( 'ledgerguard_health', array() );
		echo '<div class="wrap"><h1>LedgerGuard</h1><p>Read-only payment integrity monitoring.</p><p>Connection: ' . esc_html( $identity ? 'Paired (' . $identity['mode'] . ')' : 'Not paired' ) . '</p>';
		echo '<p>Destination: <code>' . esc_html( LedgerGuard_Client::API_ORIGIN ) . '</code></p><p>Sent fields: order/refund identifiers, amounts, currency, payment status, exact Stripe identifiers, timestamps and connector versions. Customer details and order items are excluded.</p>';
		echo '<p>Coverage through: ' . esc_html( get_option( 'ledgerguard_cursor', 'Initial scan pending' ) ) . '</p><p>Last status: ' . esc_html( $health['code'] ?? 'No connection activity' ) . '</p>';
		if ( ! $identity ) {
			echo '<form method="post" action="' . esc_url( admin_url( 'admin-post.php' ) ) . '"><input type="hidden" name="action" value="ledgerguard_pair">';
			wp_nonce_field( 'ledgerguard_pair' );
			echo '<p><label for="lg-code">Pairing code</label> <input id="lg-code" name="pairing_code" required autocomplete="off" maxlength="128"></p><p><label for="lg-mode">Mode</label> <select id="lg-mode" name="mode"><option value="test">Test</option><option value="live">Live</option></select></p><p><label><input type="checkbox" name="consent" value="yes" required> I confirm the destination and the data fields above.</label></p>';
			submit_button( 'Pair store' );
			echo '</form>';
		} else {
			foreach ( array(
				'retry'      => 'Retry a full repair',
				'rotate'     => 'Rotate signing key',
				'disconnect' => 'Disconnect this store',
			) as $action => $label ) {
				echo '<form method="post" action="' . esc_url( admin_url( 'admin-post.php' ) ) . '"><input type="hidden" name="action" value="ledgerguard_' . esc_attr( $action ) . '">';
				wp_nonce_field( 'ledgerguard_' . $action );
				submit_button( $label, 'secondary' );
				echo '</form>';
			}
		}
		echo '</div>';
	}

	public function pair(): void {
		$this->authorize( 'ledgerguard_pair' );
		check_admin_referer( 'ledgerguard_pair' );
		$code    = isset( $_POST['pairing_code'] ) ? sanitize_text_field( wp_unslash( $_POST['pairing_code'] ) ) : '';
		$mode    = isset( $_POST['mode'] ) ? sanitize_key( wp_unslash( $_POST['mode'] ) ) : '';
		$consent = isset( $_POST['consent'] ) ? sanitize_key( wp_unslash( $_POST['consent'] ) ) : '';
		if ( ! preg_match( '/^[A-Za-z0-9_-]{40,128}$/D', $code ) || ! in_array( $mode, array( 'test', 'live' ), true ) || 'yes' !== $consent ) {
			wp_die( 'Invalid pairing request.' ); }
		$settings     = get_option( 'woocommerce_stripe_settings', array() );
		$gateway_mode = ( $settings['testmode'] ?? 'no' ) === 'yes' ? 'test' : 'live';
		if ( $gateway_mode !== $mode ) {
			wp_die( 'The selected mode must match the Stripe gateway setting.' ); }
		// Retain a failed claim's identity so a lost successful response can be retried safely.
		$pending = get_option( 'ledgerguard_claim', array() );
		if ( ! $pending || hash( 'sha256', $code ) !== $pending['code_hash'] ) {
			$identity = LedgerGuard_Crypto::generate();
			$pending  = array(
				'identity'   => $identity,
				'code_hash'  => hash( 'sha256', $code ),
				'request_id' => wp_generate_uuid4(),
				'sent_at'    => gmdate( 'Y-m-d\TH:i:s\Z' ),
			);
			update_option( 'ledgerguard_claim', $pending, false );
		}
		if ( strtotime( $pending['sent_at'] ) < time() - 240 ) {
			$pending['request_id'] = wp_generate_uuid4();
			$pending['sent_at']    = gmdate( 'Y-m-d\TH:i:s\Z' );
			update_option( 'ledgerguard_claim', $pending, false ); }
		$identity = $pending['identity'];
		$envelope = array(
			'schema_version' => '1.0',
			'request_id'     => $pending['request_id'],
			'pairing_code'   => $code,
			'public_key'     => $identity['public_key'],
			'key_id'         => $identity['key_id'],
			'sent_at'        => $pending['sent_at'],
			'mode'           => $mode,
			'versions'       => LedgerGuard_Extractor::versions(),
			'capabilities'   => array( 'orders', 'refunds', 'hpos', 'delta', 'repair' ),
		);
		try {
			$result = LedgerGuard_Client::send( '/api/v1/plugin/installations/claim', $envelope, $identity );
			if (
				! is_string( $result['installation_id'] ?? null )
				|| ! preg_match( '/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/D', $result['installation_id'] )
				|| ( $result['key_id'] ?? '' ) !== $identity['key_id']
				|| ( $result['mode'] ?? '' ) !== $mode
				|| ( $result['schema_version'] ?? '' ) !== '1.0'
			) {
				throw new RuntimeException( 'CLAIM_RESPONSE_INVALID' ); }
			$identity['installation_id'] = $result['installation_id'];
			$identity['mode']            = $mode;
			update_option( 'ledgerguard_identity', $identity, false );
			delete_option( 'ledgerguard_claim' );
			$this->schedule();
			$this->start_scan( true );
		} catch ( Throwable $error ) {
			$this->record_error( $error ); }
		wp_safe_redirect( admin_url( 'admin.php?page=ledgerguard' ) );
		exit;
	}

	public function disconnect(): void {
		$this->authorize( 'ledgerguard_disconnect' );
		self::deactivate();
		foreach ( array( 'ledgerguard_identity', 'ledgerguard_claim', 'ledgerguard_rotation', 'ledgerguard_scan', 'ledgerguard_pending', 'ledgerguard_cursor' ) as $option ) {
			delete_option( $option ); }
		wp_safe_redirect( admin_url( 'admin.php?page=ledgerguard' ) );
		exit;
	}

	public function rotate(): void {
		$this->authorize( 'ledgerguard_rotate' );
		$identity = get_option( 'ledgerguard_identity', array() );
		if ( ! $identity ) {
			wp_die( 'Store is not paired.' ); }
		$next = get_option( 'ledgerguard_rotation', array() );
		if ( ! $next ) {
			$next = LedgerGuard_Crypto::generate();
			update_option( 'ledgerguard_rotation', $next, false ); }
		$envelope = $this->envelope( $identity ) + array(
			'new_key_id'     => $next['key_id'],
			'new_public_key' => $next['public_key'],
		);
		try {
			$result = LedgerGuard_Client::send( '/api/v1/plugin/keys/rotate', $envelope, $identity );
			$this->require_ack( $result, $envelope['request_id'] );
			update_option( 'ledgerguard_identity', array_merge( $identity, $next ), false );
			delete_option( 'ledgerguard_rotation' );
		} catch ( Throwable $error ) {
			$this->record_error( $error ); }
		wp_safe_redirect( admin_url( 'admin.php?page=ledgerguard' ) );
		exit;
	}

	public function retry(): void {
		$this->authorize( 'ledgerguard_retry' );
		delete_option( 'ledgerguard_scan' );
		delete_option( 'ledgerguard_pending' );
		delete_option( 'ledgerguard_scan_lock' );
		$this->start_scan( true );
		wp_safe_redirect( admin_url( 'admin.php?page=ledgerguard' ) );
		exit;
	}

	/**
	 * @param array<string, string> $identity
	 * @return array<string, string>
	 */
	private function envelope( array $identity ): array {
		return array(
			'schema_version'  => '1.0',
			'request_id'      => wp_generate_uuid4(),
			'installation_id' => $identity['installation_id'],
			'sent_at'         => gmdate( 'Y-m-d\TH:i:s\Z' ),
		);
	}

	public function start_scan( bool $repair ): void {
		if ( ! get_option( 'ledgerguard_identity' ) ) {
			return; }
		$existing = get_option( 'ledgerguard_scan', array() );
		if ( $existing ) {
			if ( ( $existing['attempt'] ?? 0 ) < 8 ) {
				as_enqueue_async_action( 'ledgerguard_page', array(), 'ledgerguard' );
			} return; }
		if ( ! add_option( 'ledgerguard_scan_lock', time(), '', false ) ) {
			return; }
		try {
			$through = time() - 120;
			$cursor  = get_option( 'ledgerguard_cursor' );
			$from    = $repair || ! $cursor ? $through - 35 * DAY_IN_SECONDS : strtotime( $cursor ) - 1800;
			// One bounded ID snapshot avoids offset pagination skipping records as orders change.
			$ids = wc_get_orders(
				array(
					'date_modified' => $from . '...' . $through,
					'limit'         => 50001,
					'orderby'       => 'ID',
					'order'         => 'ASC',
					'return'        => 'ids',
					'status'        => array_keys( wc_get_order_statuses() ),
				)
			);
			if ( count( $ids ) > 50000 ) {
				throw new RuntimeException( 'SCAN_LIMIT_EXCEEDED' ); }
			$scan = array(
				'scan_id'         => wp_generate_uuid4(),
				'ids'             => array_values( $ids ),
				'offset'          => 0,
				'page'            => 1,
				'covered_from'    => gmdate( 'Y-m-d\TH:i:s\Z', $from ),
				'covered_through' => gmdate( 'Y-m-d\TH:i:s\Z', $through ),
				'carry'           => array(),
				'attempt'         => 0,
			);
			update_option( 'ledgerguard_scan', $scan, false );
			as_enqueue_async_action( 'ledgerguard_page', array(), 'ledgerguard' );
		} catch ( Throwable $error ) {
			$this->record_error( $error ); } finally {
			delete_option( 'ledgerguard_scan_lock' ); }
	}

	public function page(): void {
		$identity = get_option( 'ledgerguard_identity', array() );
		$scan     = get_option( 'ledgerguard_scan', array() );
		if ( ! $identity || ! $scan ) {
			return; }
		$locked = get_option( 'ledgerguard_scan_lock' );
		if ( $locked && (int) $locked < time() - 120 ) {
			delete_option( 'ledgerguard_scan_lock' ); }
		if ( ! add_option( 'ledgerguard_scan_lock', time(), '', false ) ) {
			return; }
		try {
			$settings = get_option( 'woocommerce_stripe_settings', array() );
			if ( ( ( $settings['testmode'] ?? 'no' ) === 'yes' ? 'test' : 'live' ) !== $identity['mode'] ) {
				throw new RuntimeException( 'GATEWAY_MODE_CHANGED' ); }
			$pending = $scan['pending'] ?? array();
			if ( ! $pending ) {
				$records       = $scan['carry'];
				$scan['carry'] = array();
				// phpcs:ignore Squiz.PHP.DisallowSizeFunctionsInLoops.Found -- Array counts change as refunds join a bounded page.
				while ( count( $records ) < 100 && $scan['offset'] < count( $scan['ids'] ) ) {
					$facts = LedgerGuard_Extractor::order( (int) $scan['ids'][ $scan['offset'] ] );
					++$scan['offset'];
					$records = array_merge( $records, $facts );
				}
				if ( count( $records ) > 100 ) {
					$scan['carry'] = array_slice( $records, 100 );
					$records       = array_slice( $records, 0, 100 ); }
				$final           = $scan['offset'] >= count( $scan['ids'] ) && ! $scan['carry'];
				$pending         = $this->envelope( $identity ) + array(
					'covered_from'    => $scan['covered_from'],
					'covered_through' => $scan['covered_through'],
					'scan_id'         => $scan['scan_id'],
					'page'            => $scan['page'],
					'final_page'      => $final,
					'records'         => $records,
				);
				$scan['pending'] = $pending;
				update_option( 'ledgerguard_scan', $scan, false );
			}
			if ( strtotime( $pending['sent_at'] ) < time() - 240 ) {
				$pending['request_id'] = wp_generate_uuid4();
				$pending['sent_at']    = gmdate( 'Y-m-d\TH:i:s\Z' );
				$scan['pending']       = $pending;
				update_option( 'ledgerguard_scan', $scan, false );
			}
			$result = LedgerGuard_Client::send( '/api/v1/plugin/facts/batch', $pending, $identity );
			if ( ( $result['request_id'] ?? '' ) !== $pending['request_id'] || count( $result['results'] ?? array() ) !== count( $pending['records'] ) ) {
				throw new RuntimeException( 'BATCH_ACK_INVALID' ); }
			foreach ( $result['results'] as $record ) {
				if ( ! in_array( $record['status'] ?? '', array( 'accepted', 'duplicate' ), true ) ) {
					throw new RuntimeException( 'SOURCE_FACT_REJECTED' ); }
			}
			$scan['attempt'] = 0;
			if ( $pending['final_page'] ) {
				if ( empty( $result['coverage_advanced'] ) ) {
					throw new RuntimeException( 'COVERAGE_NOT_ADVANCED' ); }
				update_option( 'ledgerguard_cursor', $scan['covered_through'], false );
				delete_option( 'ledgerguard_scan' );
			} else {
				unset( $scan['pending'] );
				++$scan['page'];
				update_option( 'ledgerguard_scan', $scan, false );
				as_enqueue_async_action( 'ledgerguard_page', array(), 'ledgerguard' );
			}
			update_option(
				'ledgerguard_health',
				array(
					'code' => 'ACCEPTED',
					'at'   => gmdate( 'c' ),
				),
				false
			);
		} catch ( Throwable $error ) {
			$this->record_error( $error );
			++$scan['attempt'];
			update_option( 'ledgerguard_scan', $scan, false );
			if ( $scan['attempt'] < 8 ) {
				as_schedule_single_action( time() + min( 900, 15 * 2 ** $scan['attempt'] ) + random_int( 0, 30 ), 'ledgerguard_page', array(), 'ledgerguard' ); }
		} finally {
			delete_option( 'ledgerguard_scan_lock' ); }
	}

	public function heartbeat(): void {
		$identity = get_option( 'ledgerguard_identity', array() );
		if ( ! $identity ) {
			return; }
		$hpos     = class_exists( \Automattic\WooCommerce\Utilities\OrderUtil::class ) && \Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled();
		$envelope = $this->envelope( $identity ) + array(
			'queue_depth'          => get_option( 'ledgerguard_scan' ) ? 1 : 0,
			'versions'             => LedgerGuard_Extractor::versions(),
			'hpos'                 => $hpos,
			'clock_offset_seconds' => 0,
		);
		try {
			$result = LedgerGuard_Client::send( '/api/v1/plugin/heartbeat', $envelope, $identity );
			$this->require_ack( $result, $envelope['request_id'] ); } catch ( Throwable $error ) {
			$this->record_error( $error ); }
	}

	/** @param array<string, mixed> $result */
	private function require_ack( array $result, string $request_id ): void {
		if ( ( $result['request_id'] ?? '' ) !== $request_id || ( $result['status'] ?? '' ) !== 'accepted' ) {
			throw new RuntimeException( 'ACKNOWLEDGEMENT_INVALID' ); }
	}

	private function record_error( Throwable $error ): void {
		$code = $error->getMessage();
		update_option(
			'ledgerguard_health',
			array(
				'code' => preg_match( '/^[A-Z_]{1,64}$/D', $code ) ? $code : 'CONNECTOR_ERROR',
				'at'   => gmdate( 'c' ),
			),
			false
		);
	}
}
