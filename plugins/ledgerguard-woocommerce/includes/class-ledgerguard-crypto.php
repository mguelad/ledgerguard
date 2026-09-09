<?php
/** Local key protection and protocol signing. */
final class LedgerGuard_Crypto {
	public static function key(): string {
		return hash_hkdf( 'sha256', wp_salt( 'auth' ) . wp_salt( 'secure_auth' ), SODIUM_CRYPTO_SECRETBOX_KEYBYTES, 'ledgerguard-private-key-v1' );
	}

	public static function seal( string $secret ): string {
		$nonce = random_bytes( SODIUM_CRYPTO_SECRETBOX_NONCEBYTES );
		// phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Binary protocol encoding.
		return base64_encode( $nonce . sodium_crypto_secretbox( $secret, $nonce, self::key() ) );
	}

	public static function open( string $sealed ): string {
		// phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode -- Decode the sealed key envelope.
		$raw = base64_decode( $sealed, true );
		if ( false === $raw || strlen( $raw ) < SODIUM_CRYPTO_SECRETBOX_NONCEBYTES + SODIUM_CRYPTO_SECRETBOX_MACBYTES ) {
			throw new RuntimeException( 'KEY_UNAVAILABLE' );
		}
		$secret = sodium_crypto_secretbox_open( substr( $raw, SODIUM_CRYPTO_SECRETBOX_NONCEBYTES ), substr( $raw, 0, SODIUM_CRYPTO_SECRETBOX_NONCEBYTES ), self::key() );
		if ( false === $secret ) {
			throw new RuntimeException( 'KEY_UNAVAILABLE' ); }
		return $secret;
	}

	public static function base( string $installation, string $request, string $sent_at, string $body ): string {
		return "v1\n" . $installation . "\n" . $request . "\n" . $sent_at . "\n" . hash( 'sha256', $body );
	}

	public static function sign( string $private_key, string $installation, string $request, string $sent_at, string $body ): string {
		// phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Binary protocol encoding.
		return base64_encode( sodium_crypto_sign_detached( self::base( $installation, $request, $sent_at, $body ), $private_key ) );
	}

	/** @return array{key_id: string, public_key: string, sealed_key: string} */
	public static function generate(): array {
		$pair = sodium_crypto_sign_keypair();
		return array(
			'key_id'     => wp_generate_uuid4(),
			// phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Public key transport encoding.
			'public_key' => base64_encode( sodium_crypto_sign_publickey( $pair ) ),
			'sealed_key' => self::seal( sodium_crypto_sign_secretkey( $pair ) ),
		);
	}
}
