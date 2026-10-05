# Evaluation data

`demo/dataset.json` contains **invented software fixtures**, not a model benchmark:

- Four short Markdown documents about fictional Orchard orders and fictional Acorn retention guides.
- Seven inline-evidence questions: four answerable, two missing-evidence, and one conflict case.
- Eight supported originals and eight altered claims, one pair in each planned mutation category.
- All source families use the `demo` split. Review state is `synthetic_fixture`, with no fabricated reviewer identities.

The evaluation questions use the frozen inline excerpts in the manifest. They make no retrieval-quality measurement. The ordinary `task app:seed` command ingests the separate Markdown files through the same leased upload/indexing path as user documents, so the interactive retrieval walkthrough still exercises PostgreSQL and the configured embedding interface.

The marker strings `[fixture:unsupported]` and `[fixture:conflict]` exercise deterministic mock branches. They are not naturally occurring model mistakes. The mock verifier uses exact text rules, not learned semantic reasoning. In particular, a plausible excerpt can pass its fixture check while failing to answer the question completely. Natural correctness and completeness remain unmeasured until independent annotation.

`templates/` contains JSON schemas and one clearly unfilled authoring scaffold. The scaffold has no reviewed labels and cannot meet the frozen study's sample requirements. It exists to show the manifest shape; replace its placeholders before running an operator study.

Data remains at the repository root; run tasks there after `task setup` so relative fixture paths resolve consistently. See [docs/evaluation.md](../docs/evaluation.md) for the rubric, split protocol, commands, budget accounting, and qualification requirements. Do not promote fixture labels or generated mutation proposals to independently reviewed gold.
