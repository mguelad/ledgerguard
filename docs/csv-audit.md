# Offline CSV audit

The CLI accepts normalized UTF-8 CSVs, not arbitrary provider exports. It rejects duplicate/missing headers, malformed row widths, files over 20 MiB and more than 100,000 rows per input. It never copies raw inputs. Remove originals under your import retention policy; generated evidence has its own restricted retention.

All timestamps end in `Z`. Amounts must be nonnegative, exact decimal or integer strings as specified. No floats, thousands separators, scientific notation or implicit currency conversion are accepted. Test/live account and store namespace must agree with command arguments.

| Input | Required columns | Units |
| --- | --- | --- |
| Stripe | `id,status,amount,amount_received,currency,created_at,changed_at,account_id,mode` | Provider integer minor units |
| Woo | `id,status,amount,total_refunded,currency,created_at,changed_at,store_id,mode,transaction_id` | Woo decimal major units |
| Refunds | `id,source,parent_id,amount,currency,status,changed_at` | Stripe minor units; Woo decimal major units |

Optional Stripe columns: `succeeded_at,method,capture_method,charge_id,session_id,store_id,order_id`. Multiple alias IDs are semicolon-separated. Optional Woo columns: `paid_at,payment_method,payment_intent_id,charge_id,session_id`. Extra CSV columns are not retained in normalized facts, but do not include customer data in the first place.

Refund `source` is `stripe` or `woo`. Parent ID is a PaymentIntent for Stripe and an order for Woo. Provide succeeded, pending and failed statuses accurately. For deleted Woo refunds remove the row and update the order's total; a stale export cannot establish correctness.

Coverage arguments must describe a proven complete range shared by the inputs. The range cannot exceed thirty-five days. A stale end produces PI-009 rather than absent-record conclusions. Unattributed payments remain warnings. No provider credentials are required.

Outputs are `findings.csv`, `report.html`, `report.pdf` and `evidence.json`. CSV cells beginning with spreadsheet formula characters are neutralized. HTML/PDF text is escaped. Evidence includes explicit versions, identities, source observations and the input digest. A report is an investigation artifact, not a financial ledger or a full statutory tenant data export.
