# neuralOSd

An offline, on-device data-answering runtime that **refuses what it cannot
verify**, **accounts for what it excludes**, and **ships with its own tests**.

## Quick Start

```bash
pip install 'neuralosd[all]'
neuralosd init --source your_data.csv --name mydata --out ./mydata
neuralosd ask --instance-dir ./mydata "how many rows"
```

## Why it's different

Every other AI data tool optimises for *answering*. neuralOSd optimises for
*not answering when it can't verify*. That's the opposite design goal, and
it's what makes it safe for regulated environments.

| Every other tool | neuralOSd |
|---|---|
| The model generates an answer | The model **selects from a closed list** |
| If the answer looks wrong, add a prompt | If the match is weak, **refuse** |
| Verification = ask the model if it's sure | Verification = **accounting, invariants, oracle** |
| Errors are silent | Discards are **visible** |
| Deployment = "it should work" | Deployment = **verified end to end** |

## What's new in 1.10.0

- **Multi-instance serving**: one process, many instances, routed by path
- **Parallel probe sweep**: the reason loop's probe scan is now concurrent
- **Mapper verdict cache**: repeat reason runs skip the model entirely
- **Template fork**: deploy from a baked sandbox snapshot in seconds
- **Synapse mesh integration**: register, serve, discover, delegate
- **REST-preferred boxlite door**: no runtime lock when boxlite serve is available
- **Plural-normalized scoring**: "clinics" matches "clinic" everywhere
- **Exact-phrase bonus**: a fully-covered trigger beats a partial match
- **action_intent_reason shared function**: the gate and the floor-miss path
  use the same check
- **Whole-word qualifier matching**: "resolved" doesn't fire inside "unresolved"
- **Conformance suite**: 16 cases that all pass
- **Shipped behavioural banks**: golden.json + traps.json with every instance
