# Step 5 Transition Coordinator

`TransitionEngine` is the deterministic coordinator for the RFC-0003 transition
sequence:

1. observe
2. retrieve
3. infer
4. reflect
5. govern
6. commit
7. act

The coordinator produces a candidate state by deep-copying the source state,
then records both `metadata["last_trace"]` and `metadata["transition_log"]`.
The structured log is deterministic for a given event type and does not depend
on model-provider output.

`StateTransitionPipeline` keeps inference, reflection, governance, and commit
concerns separate. The transition engine prepares the candidate, the reflection
engine reviews it, the governance engine authorizes it, and the state manager
performs the versioned commit.
