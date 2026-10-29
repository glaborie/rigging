---
name: procurement-policy
description: How to reason about MOQ, tier pricing, lead times and approvals for B2B orders.
---

# Procurement reasoning

When a customer asks about buying at volume, resolve in this order:

1. **Minimum order quantity** — every SKU has an `moq`. Below MOQ there is no sale;
   offer the MOQ or an alternative SKU. Never invent a smaller quantity.
2. **Tier pricing** — `check_pricing(sku, qty)` returns the applicable tier. Quote
   the tier, not a discount: "at 500 units you are on tier 2 (€4.10/unit)".
3. **Lead time** — `lead_time_days` is per SKU. For mixed baskets quote the maximum,
   and say which item drives it.
4. **Approval thresholds** — orders above €10,000 need a human buyer's approval.
   Say so explicitly and offer to escalate. Do not imply you can approve it.

Worked example:

> Q: "Can I get 200 of SKU-1043?" → MOQ is 250 → "SKU-1043 has an MOQ of 250; at
> 250 you are on tier 1 at €5.20." Offer to check an alternative SKU if the
> quantity is fixed.

Anti-patterns: quoting a single unit price for a volume request; promising a
delivery date without a lead time from `check_pricing`; guessing stock.
