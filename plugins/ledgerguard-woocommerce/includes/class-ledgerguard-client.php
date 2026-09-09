<?php
/** A release binds this constant to one verified HTTPS origin. */
final class LedgerGuard_Client {
	public const API_ORIGIN = 'https://api.ledgerguard.invalid';

	/**
	 * @param array<string, mixed> $envelope
	 * @param array<string, string> $identity
	 * @return array<string, mixed>
	 */
	public static function send( string $route, array $envelope, array $identity ): array {
		if ( ! in_array( $route, array( '/api/v1/plugin/installations/claim', '/api/v1/plugin/facts/batch', '/api/v1/plugin/heartbeat', '/api/v1/plugin/keys/rotate' ), true ) ) {
			throw new RuntimeException( 'ROUTE_NOT_ALLOWED' ); }
		$body = wp_json_encode( $envelope, JSON_UNESCAPED_SLASHES );
		if ( false === $body || strlen( $body ) > 1048576 ) {
			throw new RuntimeException( 'BATCH_TOO_LARGE' ); }
		$key          = LedgerGuard_Crypto::open( $identity['sealed_key'] );
		$installation = $envelope['installation_id'] ?? 'claim';
		$signature    = LedgerGuard_Crypto::sign( $key, $installation, $envelope['request_id'], $envelope['sent_at'], $body );
		sodium_memzero( $key );
		$response = wp_remote_post(
			self::API_ORIGIN . $route,
			array(
				'timeout'             => 20,
				'redirection'         => 0,
				'sslverify'           => true,
				'limit_response_size' => 131072,
				'headers'             => array(
					'Content-Type'            => 'application/json',
					'X-LedgerGuard-Key-ID'    => $identity['key_id'],
					'X-LedgerGuard-Signature' => $signature,
				),
				'body'                => $body,
				'data_format'         => 'body',
			)
		);
		if ( is_wp_error( $response ) ) {
			throw new RuntimeException( 'NETWORK_UNAVAILABLE' ); }
		$status = wp_remote_retrieve_response_code( $response );
		$result = json_decode( wp_remote_retrieve_body( $response ), true, 12, JSON_THROW_ON_ERROR );
		if ( $status < 200 || $status > 299 || ! is_array( $result ) ) {
			$code = is_array( $result ) ? ( $result['code'] ?? 'REMOTE_REJECTED' ) : 'REMOTE_REJECTED';
			throw new RuntimeException( preg_match( '/^[A-Z_]{1,64}$/D', $code ) ? esc_html( $code ) : 'REMOTE_REJECTED' );
		}
		return $result;
	}
}
