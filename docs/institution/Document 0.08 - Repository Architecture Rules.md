# Document 0.08: Repository Architecture Rules

These rules define the absolute boundaries of the Scientific Institution and its subsystems. They are frozen and must be strictly adhered to by all future engineering iterations.

## Rule 1 — The Institution Is the Root
The repository is organized around the Scientific Institution. Everything else, including Project Brain, is a subsystem.
Hierarchy:
- Scientific Institution
  - Constitutional Documents (`docs/institution/`)
  - Scientific Architecture (`docs/architecture/`)
  - Research Corpus (`docs/research/`)
  - Canonical Observation Memory (`data/`)
  - Engineering (`src/`)
    - Project Brain Runtime (`project-brain/`)
  - QuantOS Runtime (`start_all.py`, etc.)

## Rule 2 — Three Different Types of Memory
- **Institutional Memory:** Constitution, Governance, Capability Ontology.
- **Scientific Memory:** Canonical Observation Dataset, Replay Data, Experimental Results.
- **Runtime Memory:** AI context, Task history, Active engineering decisions (`project-brain/memory/`).
These must never be mixed.

## Rule 3 — Engineering Is a Consumer
Project Brain consumes Institutional Documents, Architecture Documents, and Scientific Memory. It does NOT own them. The engineering runtime reads institutional knowledge, it does not store it in its own memory banks.

## Rule 4 — Research Has Its Own Lifecycle
The research workflow is an explicit institutional pipeline:
Hypothesis → Experiment → Replication → Discovery → Institutional Knowledge → Engineering Adoption (optional)
Engineering should only adopt discoveries after successful scientific validation.

## Rule 5 — Canonical Observation Dataset Is the Source of Scientific Truth
For market reality, the Canonical Observation Dataset is the primary source of truth. Project Brain graphs, summaries, and caches are secondary representations. If a graph conflicts with the dataset, the dataset always wins.

## Rule 6 — Multiple Knowledge Graphs
Maintain independent graphs for different domains in `project-brain/graph/`:
- **Code Graph:** Repository structure and dependencies.
- **Capability Graph:** Institutional capabilities and their engineering implementation.
- **Knowledge Graph:** Documents, hypotheses, experiments, discoveries.
- **Observation Graph (future):** Relationships between market observations across time.
Do not merge these into one graph.

## Rule 7 — Repository as a Scientific Laboratory
The repository should not evolve into an AI coding workspace. It should evolve into a Scientific Laboratory. Every artifact must belong to: Institution, Architecture, Observation, Research, Engineering, or Runtime.

## Rule 8 — Preserve Existing Engineering
Do not functionally modify the QuantOS runtime, Trading engine, Canonical Observation implementation, Replay specification, or Data Lake purely for architectural redesign. The objective is architectural clarity, not additional functionality.
