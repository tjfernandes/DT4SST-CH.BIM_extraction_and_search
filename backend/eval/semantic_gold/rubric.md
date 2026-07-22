# HBIM-005B relevance rubric — `hbim-semantic-gold` 1.0.0

Normative. This file is hashed in `dataset.json` and frozen by the
preregistration commit: a silent edit here breaks the baseline gate exactly like
a corpus edit, because the grades are *derived from* these rules.

No embedding model had been executed, in any form, when this rubric and the data
it governs were authored.

## 1. Why grades are derived and not hand-assigned

Free-hand grading is unauditable and is the classic leakage vector: a judgment
can drift toward whatever a model happens to return. Here every grade is the
output of a pure, total function — `eval.semantic_gold_dataset.derive_grade` —
of the corpus and each query's declared facets. `qrels.jsonl` is that function's
materialised output; the dataset test regenerates it and compares byte for byte,
so a hand-edited judgment fails the suite.

The query **text** is authored independently of the facets, as paraphrase,
synonym or cross-lingual phrasing. The facets fix the ground truth; the text
defines the retrieval task. Section 5 keeps those two layers from collapsing
into a lexical identity.

## 2. Facets

Each query declares mandatory facets (`must`) and optional secondary facets
(`should`) as structured predicates.

Fields are restricted to the closed allowlist in
`eval.text_projection.PROJECTED_FIELDS` — exactly the surface the embedding
receives. A query can therefore never be graded on information the model cannot
read: `metrics`, `global_id`, `project_id` and `source` are neither projected
nor gradeable.

Operators are typed by field arity:

| Arity | Fields | Operators |
|---|---|---|
| scalar | `ifc_class`, `name`, `description`, `object_type`, `predefined_type`, `semantic_label`, `location.{site,building,storey,space}.name` | `in`, `not_in`, `eq`, `contains_ci`, `is_null`, `not_null` |
| list | `materials.name` | `any_in`, `all_in`, `not_in`, `is_null`, `not_null` |

Only `contains_ci` is case-insensitive (NFC + casefold). Everything else is
exact, so a predicate value must match the authored text character for
character.

## 3. Grade scale

Let `m` be the number of mandatory facets, `f` the number that fail on an
element, and `g` the number of secondary facets that fail.

| Condition | Grade | Meaning |
|---|---|---|
| `f == 0` and `g == 0` | **3** | satisfies every mandatory and secondary facet |
| `f == 0` and `g >= 1` | **2** | satisfies the main intent; a secondary facet is missing |
| `f == 1` and `m >= 2` | **1** | related but incomplete — one facet away |
| otherwise | **0** | not relevant |

Exactly one branch applies to every (query, element) pair, so contradictions and
ties are impossible by construction; a test asserts this over the full
62 × 122 cross product.

Only grades `>= 1` are stored. Grade 0 is the default, so a hidden judgment is
detectable by regeneration.

## 4. Metric semantics

- **Relevant** for Recall@10 and MRR@10: `grade >= 2`
  (`relevance_threshold` in `dataset.json`).
- **nDCG@10**: graded, gain `2**grade - 1`, discount `1 / log2(rank + 1)`,
  ideal ranking = judged grades sorted descending, truncated at `k = 10`.
- A query is **rank-evaluated** when at least one element reaches `grade >= 2`.
  All three macro metrics average over that one shared set — nDCG is not given a
  wider set merely because grade-1 near misses give it a non-zero IDCG.
- **Zero-relevant** queries declare `expects_zero_relevant: true` and are
  excluded from every macro average. `eval.metrics.recall_at_k` and `mrr_at_k`
  return `1.0` vacuously on an empty relevant set, so averaging them in would
  silently inflate every reported number. They may still carry grade-1 near
  misses.
- Every rank-evaluated query satisfies `1 <= |{grade >= 2}| <= 10`. More
  relevant documents than the cutoff would cap Recall@10 below 1.0 for *every*
  model and compress exactly the differences this baseline exists to expose.

## 5. Anti-leakage rules

1. Document text is projected from the typed `ElementRecord` alone. The
   projection function cannot see a query, a facet or a grade — that is enforced
   by its signature, not by review.
2. No corpus record may carry a non-canonical key, and no projected text may
   contain evaluation vocabulary (`query`, `qrel`, `grade`, `relevance`,
   `facet`, `embedding`) as a whole token. The check is token-exact, because
   heritage Portuguese legitimately contains `gradeamento` (railing) and
   `relevo` (relief).
3. Queries tagged `low_lexical_overlap` share **no content token** with **any**
   of their relevant documents, compared after NFC, casefold and accent
   stripping, ignoring `stopwords.json`. Eighteen of the sixty-two queries carry
   this tag, so a purely lexical matcher cannot score them at all.
4. Query ids, grades and facets appear nowhere in the corpus; element ids appear
   nowhere in a query text.
5. Nothing in this dataset may be revised because a model scored unexpectedly.
   A demonstrated *defect* — a mis-derived grade, a validator-detectable
   inconsistency — requires a `dataset_version` bump and a new superseding
   preregistration commit that re-runs every model from scratch.

## 6. Relevance rationales

Concise, human-reviewable reasons. Each query also carries a one-line `notes`
field; neither is ever projected, embedded or used by any metric.

**Cross-lingual needs (`sg-0001`–`sg-0018`).** The three sites use genuinely
different materials, not translations of one another: the Portuguese convent and
castle record `madeira de castanheiro`, `calcário`, `azulejo`, `ferro forjado`,
`ardósia` and `tijolo`, while the English manor records `oak`, `limestone`,
`glazed tile`, `wrought iron` and `slate`. A Portuguese need for oak joinery
therefore *correctly* excludes the chestnut doors — chestnut is a different
timber, not a translation — and the resulting judgment is right for domain
reasons rather than convenient for lexical ones. This is what makes the
low-overlap requirement satisfiable without distorting the ground truth.

**Condition and heritage needs (`sg-0019`, `sg-0033`, `sg-0035`, `sg-0044`,
`sg-0049`).** Graded only on conditions actually represented in a canonical
field — `semantic_label` values such as `parede com humidade ascendente` or
`decayed timber beam`. No condition sub-schema is invented.

**Type-synonym needs (`sg-0005`, `sg-0014`, `sg-0026`, `sg-0031`, `sg-0042`,
`sg-0047`, `sg-0061`).** The need names a functional type (*guard*, *portal*,
*partition*) while the corpus names an object type (`Gradeamento`, `Corrimão`,
`Balustrade`, `Alçapão`). Relevance rests on the type relation, not the word.

**Location needs (`sg-0021`, `sg-0023`, `sg-0025`, `sg-0030`, `sg-0037`,
`sg-0039`, `sg-0041`, `sg-0056`, `sg-0057`).** Scoped by building or storey. The
remaining elements of the same gallery, hall or crypt share the location
vocabulary without satisfying the element-type facet, so they are near misses or
distractors rather than answers.

**Material-named needs (`sg-0060`, `sg-0061`, `sg-0062`).** Single-facet needs
that name a material in the text. Everything sharing that material without being
the requested element type falls to grade 0: a matcher keying on
`madeira de castanheiro` alone returns beams, ceilings and floors instead of the
doors asked for. These carry most of the corpus's lexical-overlap distractors.

**Zero-relevant needs (`sg-0051`–`sg-0055`).** Reinforced concrete, aluminium
curtain walling, transport elements, solar devices and MEP networks are absent
from a pre-industrial heritage corpus by construction. They are genuine needs an
operator might type, and nothing may be retrieved for them.

## 7. Provenance

All data is synthetic. The three sites — `Convento de São Bento`,
`Ashcombe Manor Estate` and `Castelo de Vale Escuro` — are invented, as are
every name, description, `global_id` and identifier. No real museum, heritage,
project, IFC or document record was copied, in whole or in part. There are no
hosts, credentials, paths or personal names anywhere in the dataset.
