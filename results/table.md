Suite `examples/b2b-support/eval.yaml`, 5 repeats per model. 95% Wilson intervals.

| Model | Safety invariants | Task success | Crashed runs | Median latency (s) | Median steps | Median cost (USD) | Flaky cases |
|---|---|---|---|---|---|---|---|
| `qwen3:8b` | n/a | 21/40 (52%, 37%-67%) | 0 | n/a | n/a | n/a | commitment-refused, human-requested, refund-outside-window-blocked, unknown-sku-honest |
| `gemma4:31b-cloud` | n/a | 35/40 (88%, 74%-95%) | 0 | n/a | n/a | n/a | - |
| `opencode-go/glm-5.2` | n/a | n/a | 5 | n/a | n/a | n/a | - |
