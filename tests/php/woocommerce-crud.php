<?php
/** Run with wp eval-file in a disposable WordPress/WooCommerce installation. */
add_filter('pre_http_request', static fn() => new WP_Error('test_network_disabled', 'Network disabled during CRUD assertions'), 1);
add_filter('pre_wp_mail', '__return_true');
function ledgerguard_expect(bool $condition, string $message): void { if (!$condition) { throw new RuntimeException($message); } }
$expected_hpos = getenv('HPOS') === 'yes';
ledgerguard_expect(\Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled() === $expected_hpos, 'Requested order datastore is not active');
$order = new WC_Order();
$order->set_currency('EUR');
$order->set_total('129.95');
$order->set_payment_method('stripe');
$order->set_transaction_id('ch_fixture');
$order->set_status('processing');
$order->set_billing_email('excluded-customer@example.invalid');
$order->set_billing_first_name('ExcludedCustomerName');
$order->update_meta_data('_stripe_intent_id', 'pi_fixture');
$order->update_meta_data('_unapproved_metadata', 'ExcludedArbitraryMetadata');
$order->save();
$refund = new WC_Order_Refund();
$refund->set_parent_id($order->get_id());
$refund->set_currency('EUR');
$refund->set_amount('12.34');
$refund->set_reason('ExcludedRefundReason');
$refund->save();
$before = wc_get_order($order->get_id())->get_data();
$facts = LedgerGuard_Extractor::order($order->get_id());
ledgerguard_expect(count($facts) === 2, 'Order and refund were not both extracted');
ledgerguard_expect($facts[0]['amount'] === '129.95' && $facts[0]['total_refunded'] === '12.34', 'Money was changed');
ledgerguard_expect($facts[0]['stripe_ids']->payment_intent === 'pi_fixture', 'Exact identity is missing');
ledgerguard_expect($facts[0]['refund_ids'] === [(string)$refund->get_id()], 'Refund inventory is incomplete');
ledgerguard_expect($facts[1]['amount'] === '12.34' && $facts[1]['parent_id'] === (string)$order->get_id(), 'Refund parent or amount is incorrect');
$encoded = wp_json_encode($facts);
foreach (['excluded-customer', 'ExcludedCustomerName', 'ExcludedArbitraryMetadata', 'ExcludedRefundReason', 'billing', 'shipping'] as $blocked) {
    ledgerguard_expect(strpos($encoded, $blocked) === false, 'Unapproved field left the extractor');
}
ledgerguard_expect(wc_get_order($order->get_id())->get_data() == $before, 'Extraction modified the order');
$refund->delete(true);
$remaining = LedgerGuard_Extractor::order($order->get_id());
ledgerguard_expect($remaining[0]['refund_ids'] === [] && $remaining[0]['total_refunded'] === '0', 'Deleted refund remains in the inventory');
$order->set_payment_method('cod');
$order->save();
ledgerguard_expect(LedgerGuard_Extractor::order($order->get_id()) === [], 'An unsupported gateway was extracted');
$order->delete(true);
echo 'CRUD, privacy, refund inventory and no-write assertions passed; HPOS=' . ($expected_hpos ? 'yes' : 'no') . "\n";
