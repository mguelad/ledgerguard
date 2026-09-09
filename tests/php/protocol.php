<?php
/** Cross-language protocol checks, using public synthetic test vectors. */
$ledgerguard_test_salt = 'public-synthetic-salt';
function wp_salt(string $scheme): string { global $ledgerguard_test_salt; return $ledgerguard_test_salt . $scheme; }
require __DIR__ . '/../../plugins/ledgerguard-woocommerce/includes/class-ledgerguard-crypto.php';
require __DIR__ . '/../../plugins/ledgerguard-woocommerce/includes/class-ledgerguard-extractor.php';
require __DIR__ . '/../../plugins/ledgerguard-woocommerce/includes/class-ledgerguard-plugin.php';
function expect(bool $condition, string $message): void { if (!$condition) { throw new RuntimeException($message); } }
$vector = json_decode(file_get_contents(__DIR__ . '/../../fixtures/contracts/woo-signature.json'), true, 12, JSON_THROW_ON_ERROR);
$pair = sodium_crypto_sign_seed_keypair(hex2bin($vector['seed_hex']));
$private = sodium_crypto_sign_secretkey($pair);
expect(base64_encode(sodium_crypto_sign_publickey($pair)) === $vector['public_key'], 'Ed25519 public key differs');
$base = LedgerGuard_Crypto::base($vector['installation_id'], $vector['request_id'], $vector['sent_at'], $vector['body']);
expect($base === $vector['signature_base'], 'Signature bytes differ');
expect(LedgerGuard_Crypto::sign($private, $vector['installation_id'], $vector['request_id'], $vector['sent_at'], $vector['body']) === $vector['signature'], 'PHP/Python signatures differ');
expect(!sodium_crypto_sign_verify_detached(base64_decode($vector['signature']), $base . ' ', sodium_crypto_sign_publickey($pair)), 'Changed bytes accepted');
$sealed = LedgerGuard_Crypto::seal($private);
expect(LedgerGuard_Crypto::open($sealed) === $private, 'Sealed key did not round-trip');
$ledgerguard_test_salt = 'changed-public-synthetic-salt';
try { LedgerGuard_Crypto::open($sealed); throw new RuntimeException('Changed salts were accepted'); }
catch (RuntimeException $error) { expect($error->getMessage() === 'KEY_UNAVAILABLE', 'Wrong salt failure'); }
foreach (['129.9500' => '129.95', '100' => '100', '0.000' => '0', '1.234' => '1.234'] as $input => $expected) {
    expect(LedgerGuard_Extractor::decimal($input) === $expected, 'Decimal precision lost');
}
foreach ([1.25, '1e2', '-1', '1,00', ' 1', 'NaN'] as $invalid) {
    try { LedgerGuard_Extractor::decimal($invalid); throw new RuntimeException('Invalid decimal accepted'); }
    catch (RuntimeException $error) { expect($error->getMessage() === 'AMOUNT_FORMAT_UNSUPPORTED', 'Wrong decimal failure'); }
}
$plugin = new LedgerGuard_Plugin();
$ack = new ReflectionMethod(LedgerGuard_Plugin::class, 'require_ack');
$ack->invoke($plugin, ['request_id' => 'request-one', 'status' => 'accepted'], 'request-one');
foreach ([['request_id' => 'request-two', 'status' => 'accepted'], ['request_id' => 'request-one', 'status' => 'rejected'], []] as $invalid_ack) {
    try { $ack->invoke($plugin, $invalid_ack, 'request-one'); throw new RuntimeException('Invalid acknowledgement accepted'); }
    catch (RuntimeException $error) { expect($error->getMessage() === 'ACKNOWLEDGEMENT_INVALID', 'Wrong acknowledgement failure'); }
}
echo "PHP/Python Ed25519 protocol, salt rotation, decimal and acknowledgement checks passed.\n";
