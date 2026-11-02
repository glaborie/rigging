# TODO: MLoPS Skills Development

## Current Status
- v0.1 through v0.4: COMPLETE (all 27 tests passing, ruff+mypy --strict clean)
- v0.5: In progress (60-second terminal recording pending)
- v0.6: Planned (SubagentRunner, MLflow exporter)

---

## MLOps Skills Gap Closure

### Week 1-2: Feature Store Parity (Gap from Q24)
Goal: Debug offline/online feature disagreement in rag-llmops-platform

- [ ] Read feast.dev/docs/concepts/online_vs_offline - understand the core problem
- [ ] Add logging to features.py to detect value divergence
- [ ] Create test case that reproduces a parity bug
- [ ] PR: Fix parity issue + root cause analysis

Deliverable: One debuggable feature store issue

### Week 3-4: Eval-Driven Routing (Gap from Q25)
Goal: Design provider routing architecture using DeepEval

- [ ] Study SvcMoloch (Anthropic) - focus on request flow, not implementation
- [ ] Design router flow in docs: request -> benchmark cache -> pick provider -> log
- [ ] Write architecture diagram + pseudocode
- [ ] Create examples/router/ with interface definition

Deliverable: Architecture documentation + route interface

### Week 5-6: CD Pipeline (Gap from Q27)
Goal: Git push -> automated model deployment

- [ ] Extend GitHub Actions to include deployment stage
- [ ] Build minikube deployment script: kubectl apply from registry
- [ ] Add rollback mechanism (previous version tag)
- [ ] Verify green pipeline button = model live

Deliverable: Working CD pipeline with manual verification

---

## v0.5 Completion Tasks

- [ ] 60-second terminal recording in README
- [ ] FastAPI + SSE web widget (stretch goal)

---

## Reference Resources

1. Feature Stores: "Feature Engineering for Machine Learning" - Ch. 4
2. Eval Routing: "SvcMoloch: Intelligent Model Routing" (Anthropic)
3. CD for ML: "Continuous Delivery for Machine Learning"

---

## Verification Milestones

After completing TODO items, you should:
- [ ] Debug a feature parity issue in under 30 minutes
- [ ] Sketch eval-routing flow from memory
- [ ] Name 3 deployment stages (build/test/deploy)
- [ ] Explain why manual model promotion fails at scale
