# Evaluation Model

## 1. Purpose

SPST-UEA evaluates persistent system behavior through observable metrics rather than philosophical claims.

## 2. Primary Evaluation Dimensions

### Identity Continuity

Measures consistency of identity-relevant state across transitions.

### Goal Persistence

Measures retention and appropriate updating of long-term goals.

### State Reconstruction

Measures recovery of relevant operational state after interruption.

### Semantic Stability

Measures preservation of meaning under paraphrase, delay, model substitution, or context reconstruction.

### Governance Effectiveness

Measures detection, prevention, correction, and recovery from invalid transitions or actions.

### Reflective Accuracy

Measures whether self-evaluation identifies real errors and improves outcomes.

### Cognitive Metabolism

Measures useful information integration, retention, correction, and discard.

## 3. Composite Indices

The initial evaluation framework uses normalized component scores in the range `[0, 1]`.

### Existential Stability Index

```text
ESI = w1*IC + w2*GP + w3*SR + w4*CR + w5*SS
```

Where:

- `IC` = identity consistency,
- `GP` = goal persistence,
- `SR` = state reconstruction,
- `CR` = contradiction resistance,
- `SS` = semantic stability.

### Recursive Self-Reconstruction Index

```text
RSRI = mean(MRA, CRA, SMF, RCS)
```

Where:

- `MRA` = memory recall accuracy,
- `CRA` = context reconstruction accuracy,
- `SMF` = self-model fidelity,
- `RCS` = reconstruction completion success.

### Governance Index

```text
GI = mean(DDR, CSR, FPR_inv, RTR)
```

Where:

- `DDR` = deviation detection rate,
- `CSR` = correction success rate,
- `FPR_inv` = inverse false-positive rate,
- `RTR` = recovery-to-stability rate.

## 4. Unified Score

A unified score MAY be reported, but component scores MUST also be published.

```text
SUSI = Σ wi * Xi
```

A unified score MUST NOT hide component-level failure.

## 5. Required Experimental Controls

Comparisons SHOULD include:

- base model only,
- base model plus memory,
- base model plus generic agent framework,
- and SPST-UEA implementation.

## 6. Reproducibility

Every benchmark result SHOULD include:

- model identifier,
- model version,
- prompt configuration,
- tool configuration,
- memory backend,
- runtime version,
- random seed where applicable,
- task dataset,
- metric implementation,
- and raw result artifacts.

## 7. Evidence Levels

- `E0`: idea
- `E1`: implementation feasibility demonstrated
- `E2`: reproduced in one controlled environment
- `E3`: reproduced across multiple environments
- `E4`: independently reproduced by a third party
