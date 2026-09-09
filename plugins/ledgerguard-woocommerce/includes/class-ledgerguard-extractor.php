<?php
/** HPOS-safe, allowlisted extraction. No order writes or direct order SQL. */
final class LedgerGuard_Extractor {
	public static function timestamp( ?DateTimeInterface $date ): ?string {
		return $date ? gmdate( 'Y-m-d\TH:i:s\Z', $date->getTimestamp() ) : null;
	}

	public static function decimal( mixed $value ): string {
		// Read the unfiltered CRUD decimal. A float has already lost its source precision.
		if ( ! is_string( $value ) && ! is_int( $value ) ) {
			throw new RuntimeException( 'AMOUNT_FORMAT_UNSUPPORTED' ); }
		$text = (string) $value;
		if ( ! preg_match( '/^(0|[1-9][0-9]*)(\.[0-9]+)?$/D', $text ) ) {
			throw new RuntimeException( 'AMOUNT_FORMAT_UNSUPPORTED' ); }
		$text = str_contains( $text, '.' ) ? rtrim( rtrim( $text, '0' ), '.' ) : $text;
		return $text;
	}

	public static function exact_id( mixed $value, string $prefix ): string {
		return is_string( $value ) && preg_match( '/^' . preg_quote( $prefix, '/' ) . '[A-Za-z0-9_]{1,240}$/D', $value ) ? $value : '';
	}

	/** @return list<array<string, mixed>> */
	public static function order( int $id ): array {
		$order = wc_get_order( $id );
		if ( ! $order || $order instanceof WC_Order_Refund ) {
			return array(); }
		$method = $order->get_payment_method();
		if ( ! preg_match( '/^stripe(?:_[a-z0-9_]+)?$/D', $method ) ) {
			return array(); }
		if ( ( function_exists( 'wcs_is_subscription' ) && wcs_is_subscription( $id ) ) || ( function_exists( 'wcs_order_contains_subscription' ) && wcs_order_contains_subscription( $id ) ) || ( function_exists( 'wcs_order_contains_renewal' ) && wcs_order_contains_renewal( $id ) ) ) {
			return array(); }
		$created  = self::timestamp( $order->get_date_created() );
		$revision = self::timestamp( $order->get_date_modified() ) ?? $created;
		if ( ! $created || ! $revision ) {
			throw new RuntimeException( 'ORDER_TIMESTAMP_MISSING' ); }
		$ids = array();
		foreach ( array(
			'payment_intent'   => array( '_stripe_intent_id', 'pi_' ),
			'checkout_session' => array( '_stripe_checkout_session_id', 'cs_' ),
		) as $name => $source ) {
			$value = self::exact_id( $order->get_meta( $source[0], true ), $source[1] );
			if ( $value ) {
				$ids[ $name ] = $value; }
		}
		$transaction = (string) $order->get_transaction_id();
		if ( ! preg_match( '/^(pi|ch|cs)_[A-Za-z0-9_]{1,240}$/D', $transaction ) ) {
			$transaction = ''; }
		if ( str_starts_with( $transaction, 'ch_' ) ) {
			$ids['charge'] = $transaction; }
		$refunds        = $order->get_refunds();
		$total_refunded = self::sum_refunds( $refunds );
		$records        = array(
			array(
				'kind'           => 'order',
				'source_id'      => (string) $order->get_id(),
				'revision'       => $revision,
				'created_at'     => $created,
				'status'         => $order->get_status(),
				'amount'         => self::decimal( $order->get_total( 'edit' ) ),
				'total_refunded' => $total_refunded,
				'refund_ids'     => array_map( static fn( $refund ): string => (string) $refund->get_id(), $refunds ),
				'currency'       => strtoupper( $order->get_currency() ),
				'payment_method' => $method,
				'transaction_id' => $transaction,
				'stripe_ids'     => (object) $ids,
				'paid_at'        => self::timestamp( $order->get_date_paid() ),
				'completed_at'   => self::timestamp( $order->get_date_completed() ),
			),
		);
		foreach ( $refunds as $refund ) {
			$date = self::timestamp( $refund->get_date_created() );
			if ( ! $date ) {
				throw new RuntimeException( 'REFUND_TIMESTAMP_MISSING' ); }
			$records[] = array(
				'kind'       => 'refund',
				'source_id'  => (string) $refund->get_id(),
				'parent_id'  => (string) $order->get_id(),
				'revision'   => self::timestamp( $refund->get_date_modified() ) ?? $date,
				'created_at' => $date,
				'amount'     => self::decimal( $refund->get_amount( 'edit' ) ),
				'currency'   => strtoupper( $refund->get_currency() ),
				'status'     => 'recorded',
			);
		}
		return $records;
	}

	/** @param array<WC_Order_Refund> $refunds */
	public static function sum_refunds( array $refunds ): string {
		// Fixed three-digit intermediate scale, all additions on integers.
		$sum = 0;
		foreach ( $refunds as $refund ) {
			$text     = self::decimal( $refund->get_amount( 'edit' ) );
			$parts    = explode( '.', $text );
			$fraction = $parts[1] ?? '';
			if ( strlen( $fraction ) > 3 || strlen( $parts[0] ) > 12 ) {
				throw new RuntimeException( 'REFUND_PRECISION_UNSUPPORTED' ); }
			$minor = (int) $parts[0] * 1000 + (int) str_pad( $fraction, 3, '0' );
			if ( $sum > PHP_INT_MAX - $minor ) {
				throw new RuntimeException( 'REFUND_TOTAL_OVERFLOW' ); }
			$sum += $minor;
		}
		return self::decimal( (string) intdiv( $sum, 1000 ) . '.' . str_pad( (string) ( $sum % 1000 ), 3, '0', STR_PAD_LEFT ) );
	}

	/** @return array<string, string> */
	public static function versions(): array {
		global $wp_version;
		return array(
			'plugin'      => LEDGERGUARD_VERSION,
			'wordpress'   => (string) $wp_version,
			'woocommerce' => WC_VERSION,
			'gateway'     => defined( 'WC_STRIPE_VERSION' ) ? WC_STRIPE_VERSION : '0.0.0',
			'php'         => PHP_VERSION,
		);
	}
}
