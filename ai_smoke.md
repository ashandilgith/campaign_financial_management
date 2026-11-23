# AI document reading – live smoke test

Model: `gpt-6-luna` (provider `openai`)

| Case | Status | Fields correct | Expected flag | Time |
|---|---|---|---|---|
| Supplier tax invoice (PDF) | done | 7/7 | ✅ | 6.2s |
| Thermal receipt photo (JPEG, rotated) | done | 6/6 | n/a | 4.6s |

## Supplier tax invoice (PDF)

- document_type: ✅ (got `invoice`)
- vendor: ✅ (got `Lanka Stage & Sound (Pvt) Ltd`)
- document_number: ✅ (got `LSS/INV/4471`)
- document_date: ✅ (got `2026-11-17`)
- currency: ✅ (got `LKR`)
- total_amount: ✅ (got `354000`)
- tax_amount: ✅ (got `54000`)

Checks raised:
- [warn] Bill total LKR 354,000.00 ≠ amount paid LKR 320,000.00.
- [info] lss-invoice-4471.pdf: VAT 18% is LKR 54,000; subtotal LKR 300,000 and grand total LKR 354,000 add up. Authorised signature line is blank.

## Thermal receipt photo (JPEG, rotated)

- document_type: ✅ (got `receipt`)
- vendor: ✅ (got `CEYPETCO FUEL STATION KIRIBATHGODA`)
- document_number: ✅ (got `R-008812`)
- document_date: ✅ (got `2026-11-21`)
- currency: ✅ (got `LKR`)
- total_amount: ✅ (got `18400`)

Checks raised:
- [warn] Receipts add up to LKR 18,400.00 (1 receipt not read), but the settlement claims LKR 63,400.00 spent.
- [info] fuel-receipt.jpg: No tax breakdown shown. The line items add up to the stated total.
