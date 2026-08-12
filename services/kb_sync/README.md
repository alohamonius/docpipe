# services/kb_sync

One-shot tool that loads health.studio's public corpus into the Bedrock
Knowledge Base. Logic lives in `docpipe_core.kb_sync` (unit-tested with moto);
this is just the CLI.

**Pipeline:** local markdown → KB *source* S3 bucket → `StartIngestionJob` →
Bedrock chunks + embeds each file (Titan v2, 1024-dim) → vectors land in the S3
Vectors index the chat path queries.

Both layers of incrementality keep re-runs cheap:

- **upload** skips files whose bytes already match S3 (ETag == content MD5),
- **ingestion** re-embeds only added/changed/deleted docs on Bedrock's side.

## Usage

Config (bucket / KB id / data-source id) resolves from flags → env vars →
`terraform output` in `infra/envs/dev`, so after `terraform apply` you only
need `--source`:

```bash
# dry run — show the upload plan, write nothing
uv run python services/kb_sync/sync.py --source ../health.studio/docs --dry-run

# full sync: upload changed docs, start ingestion, wait for completion
uv run python services/kb_sync/sync.py --source ../health.studio/docs

# upload only, no embedding spend (e.g. staging content before ingesting)
uv run python services/kb_sync/sync.py --source ../health.studio/docs --no-ingest
```

Exit code is non-zero if a waited-on ingestion job ends FAILED or times out.

AWS auth comes from your profile/SSO (the `docpipe` profile). The tool needs
`s3:PutObject`/`HeadObject` on the source bucket and
`bedrock:StartIngestionJob`/`GetIngestionJob` on the KB.
