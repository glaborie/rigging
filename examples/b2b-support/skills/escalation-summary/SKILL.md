---
name: escalation-summary
description: How to write the summary a human colleague receives when a conversation is escalated.
---

# Escalation summaries

The summary is read by a colleague who has 30 seconds and has not seen the
conversation. It answers four questions, in this order, in at most four sentences:

1. **Who and what** — customer, account tier if known, what they want.
2. **What was established** — the facts you confirmed (SKU, quantity, order id,
   amounts), each with its source.
3. **Why it stopped here** — the exact blocker: out of policy, above approval
   threshold, ambiguous request, or explicit ask for a human.
4. **What the human should do** — one concrete next step.

Then append the structured facts block:

```
facts:
  order_id: ...
  sku: ...
  amount: ...
  policy_ids: [p1, p3]
```

Do not apologise, do not restate the whole conversation, do not promise anything
on the human's behalf. The `reason` argument is one of: `out_of_policy`,
`needs_approval`, `ambiguous`, `customer_requested`.
