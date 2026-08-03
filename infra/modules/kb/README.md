# module: kb (Phase 2)

Bedrock Knowledge Base over health.studio's public, evidence-graded corpus:

- Source S3 bucket — markdown synced from the health.studio repo
  (docs/anatomy, articles with references)
- Bedrock Knowledge Base: Titan embeddings, chunking defaults tuned for
  markdown sections
- Vector store: **S3 Vectors** — serverless and near-zero cost at rest
  (deliberately not OpenSearch Serverless, whose OCU minimums cost more per
  month than the rest of the chat path combined)
- Ingestion job triggered by the content-sync script (Phase 3)
