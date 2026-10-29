# I30 retrieval gold set and labelling guide

Status: **frozen** on 2026-10-28 for ticket I30 (branch `laneb`, base
commit `6639b16`). This guide is the labelling rubric for the corpus in
`data/gold/retrieval/`.

## Freeze record

The set is frozen as a file list plus line counts. No hashing scheme is used.
Line counts were measured with `Get-Content <file> | Measure-Object -Line`.

| File | Lines | Contents |
|---|---:|---|
| `data/gold/retrieval/corpus.jsonl` | 32 | 8 documents per topic |
| `data/gold/retrieval/queries.jsonl` | 40 | 10 queries per topic, 20 English and 20 Chinese |
| **Total** | **72** | |

Any change to a test query or its labels requires a new freeze record. The test
split is sealed: this batch does not call a retriever or `evaluate()` on it.

## Corpus provenance

The corpus is local and hand-built from the existing collector fixtures. No
live third-party API was called. The fixture files are the source-shape
reference, and every corpus row names one in `origin_fixture`:

- `tests/fixtures/arxiv/page1.xml`, `tests/fixtures/arxiv/page2.xml`
- `tests/fixtures/openalex/page1.json`
- `tests/fixtures/crossref/page1.json`
- `tests/fixtures/github/repository.json`, `tests/fixtures/github/releases.json`
- `tests/fixtures/hn/item.json`
- `tests/fixtures/rss/feed.xml`

Some titles and abstracts are copied from those fixtures. Rows whose
`source_record_id` starts with `i30-` are local additions written in the same
collector record shape. They cover the AI Agents and vector database topics,
which the small fixtures do not contain, and keep every query answerable inside
this local corpus.

Every document is under 100 KB. The corpus contains no live data, no secrets,
and no personal contact details from the fixtures.

## Corpus schema

Each JSONL row has:

| Field | Meaning |
|---|---|
| `doc_id` | Stable id used by qrels, e.g. `ag-01` |
| `source` | `arxiv`, `openalex`, `crossref`, `github`, `hn`, or `rss` |
| `source_record_id` | Fixture record id, or an `i30-` local id |
| `title` | Short title |
| `text` | Short abstract, description, or community blurb |
| `language` | `en` or `zh` for the document text |
| `topic` | `ai_agents`, `rag`, `vector_databases`, or `developer_tools` |
| `url` | Local or fixture-shaped URL; never fetched |
| `origin_fixture` | Fixture file that supplied the record shape |

## Query schema

Each JSONL row has:

| Field | Meaning |
|---|---|
| `query_id` | Stable id, e.g. `rag-zh-train-02` |
| `query` | Query text in English or Chinese |
| `language` | Query language: `en` or `zh` |
| `topic` | One of the four MVP topics |
| `split` | `train`, `dev`, or `test` |
| `relevance` | Object mapping `doc_id` to a grade from 0 to 3 |

The qrels include explicit grade-0 hard negatives so that off-topic but
lexically confusable documents are visible in the labels. A document omitted
from `relevance` is treated as grade 0.

## Relevance scale

| Grade | Label | Rule |
|---:|---|---|
| 3 | Highly relevant | Directly answers the query and would be a correct top result. |
| 2 | Relevant | Covers a key aspect of the query but is not the primary source. |
| 1 | Marginal | Adjacent background or context; useful only after better results. |
| 0 | Not relevant | Off-topic, or a hard negative that shares vocabulary but not intent. |

Grade 2 is not a synonym for "some word overlap". A document must still help
answer the query. Grade 1 is reserved for genuine background, not for a
near-miss that would mislead a reader.

## Coverage and construction rules

| Topic | Documents | Queries (en/zh) | Train / dev / test |
|---|---:|---:|---|
| AI Agents (`ai_agents`) | 8 | 10 (5/5) | 6 / 2 / 2 |
| RAG (`rag`) | 8 | 10 (5/5) | 6 / 2 / 2 |
| Vector databases (`vector_databases`) | 8 | 10 (5/5) | 6 / 2 / 2 |
| Developer tools (`developer_tools`) | 8 | 10 (5/5) | 6 / 2 / 2 |
| **Total** | **32** | **40 (20/20)** | **24 / 8 / 8** |

Rules applied to every query:

1. At least one grade-3 positive.
2. Most queries also have a grade-2 and a grade-1 document, so nDCG rewards
   ordering rather than a single binary hit.
3. At least one grade-0 hard negative where a confusable document exists.
4. Academic, code, and community source families are represented across the
   set; no query depends on a single source family.
5. Chinese queries include English documents and English queries include
   Chinese documents, so the set exercises cross-lingual retrieval.
6. Train, dev, and test query ids and query strings do not overlap.

The per-topic split is: English and Chinese queries 1-3 are train, query 4 is
dev, and query 5 is test. Test is held out for I20 evaluation and is not used
to tune thresholds or prompts.

## Annotation process

Labels were assigned by reading each document's title and text against the
query intent, then applying the scale above. Ambiguous cases were resolved
toward the lower grade unless the document clearly helped answer the query.

This pass is a single-annotator baseline. The rubric is fixed so a second
independent annotator can label the same file; disagreements go to the review
queue and are resolved before the set is used as a release gate. Until that
second pass exists, results on this set are diagnostic.

## Metrics

`src/techpulse/evaluation/retrieval_metrics.py` implements:

- **nDCG@10** with gain `2**grade - 1` and discount `log2(rank + 1)`.
  The ideal DCG is computed from the gold grades. Missing documents have grade
  0. Duplicate ranked ids are ignored after their first occurrence.
- **Recall@20** as the fraction of grade > 0 documents found in the first 20
  results.

`evaluate(retriever, gold)` accepts any callable `query -> ranked doc ids` and
returns per-query results plus macro averages overall, by language, and by
topic. Section 10 thresholds are `nDCG@10 >= 0.75` and `Recall@20 >= 0.85`;
section 7.2 requires English and Chinese to pass independently.

## GQ extension protocol (2026-10-29)

Ticket GQ extends the evaluation set with a new file
`data/gold/retrieval/queries_ext.jsonl` (24 queries) without touching the
frozen I30 freeze record above. `queries.jsonl` is unchanged and keeps its
`train` / `dev` / `test` layout; the sealed `test` split stays sealed.

- **New splits.** The extension introduces `dev2` and `heldout`. They are
  additional tuning-signal-free measurement splits, not a continuation of the
  I30 free/consumer/test design.
- **Assignment.** Each query's split is derived deterministically from its
  text: `split = "dev2" if int(sha256(query.encode("utf-8")).hexdigest(), 16) % 2 == 0 else "heldout"`.
  No manual choice was made per query.
- **Scale.** Grades in the extension run 0-2 (2 = directly answers the query,
  1 = marginal background, 0 = not relevant / hard negative). Grades are not
  calibrated against the 0-3 I30 scale, so do not compare absolute numbers
  across files.
- **Provenance.** Each row carries a one-line `justification` quoting the
  labelled document text and is answerable from `data/gold/retrieval/`
  `corpus.jsonl` only. No documents were invented and no retrieval run was
  used to choose labels.
- **Single annotator.** The extension is a single-annotator pass by
  `glm-5.3-flash-free (opencode)` recorded in each row's `labeller` field;
  `crosscheck` is `"pending"` until an independent second pass resolves
  disagreements. Until crosschecked, results on `dev2`/`heldout` are
  diagnostic only.
- **Held-out is never used for tuning.** The `heldout` split (and the sealed
  I30 `test` split) is never used to tune thresholds, prompts, lexicons, or
  retrievers. It is reported only alongside `dev`/`dev2`, after all tuning
  decisions are frozen. `dev2` may be used for tuning.
- **Sealed split safety.** `src/techpulse/evaluation/split_eval.py` is the
  read-only per-split reporting entry point. It contains no threshold
  constants of its own; it drops split values outside `dev` / `dev2` /
  `heldout` from the raw JSON before labels are validated (so sealed `test`
  rows are never parsed as judgements), and it raises if a sealed judgement
  is ever passed to its evaluator.

The real index is not merged in this batch, so no retrieval run was executed
against it. To evaluate later, filter out the sealed test split first:

```python
from pathlib import Path

from techpulse.evaluation.retrieval_metrics import evaluate, load_gold_queries

gold = load_gold_queries(Path("data/gold/retrieval/queries.jsonl"))
train_and_dev = tuple(query for query in gold if query.split != "test")
result = evaluate(retriever, train_and_dev)
```
