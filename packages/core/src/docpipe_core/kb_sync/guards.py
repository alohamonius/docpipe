"""The refusals: every way a sync says no *before* the first mutation.

Both guards share one design rule — refuse with the whole picture (every
offender, the exact ratio) and change nothing, so a refused sync leaves the
bucket byte-for-byte as it was.
"""

from __future__ import annotations

# Blast-radius limit: the fraction of the keys under the prefix that one sync may
# delete before it refuses and asks to be told the number explicitly.
#
# 10% is measured, not picked. Against the 2026-08-14 corpus (383 documents +
# 383 sidecars) the largest family a routine edit can retire is the 31
# `body-graph-structure` chunks — 8.1%; every prose source-doc family is ≤ 15
# chunks (3.9%). So every ordinary heading rewrite passes untouched. The
# 132-chunk `body-graph-connection` family is 34% and is *meant* to trip this:
# retiring a third of the corpus is a decision, not a side effect. A half-built
# `build/kb` or a --source one directory too deep lands far above it.
DEFAULT_MAX_DELETE_RATIO = 0.10

# Titan v2 refuses embedding inputs past 8,192 tokens. The guard works in bytes
# because the true tokenizer lives server-side; 4 B/token is deliberately
# generous for this corpus — its largest chunk measures 24.7 KB ≈ 5k tokens
# (~4.9 B/token) — so every real chunk clears the 32 KiB limit with ~30%
# headroom, while a file that trips it is plausibly past the model's cap and
# worth a human look either way. The proxy assumes Latin-script UTF-8:
# CJK/Greek-heavy text runs ~3 B per character at ~1 token per character, so a
# 30 KB file could clear the byte gate yet exceed the token cap — restate this
# before pointing the guard at a non-English corpus.
TITAN_V2_MAX_TOKENS = 8192
_BYTES_PER_TOKEN = 4
DEFAULT_MAX_DOC_BYTES = TITAN_V2_MAX_TOKENS * _BYTES_PER_TOKEN  # 32 KiB


class BlastRadiusRefused(RuntimeError):
    """A prune would remove more of the bucket than the caller allowed.

    Raised *before* any mutation — no upload, no delete, no ingestion job — so a
    refused sync leaves the bucket exactly as it was.
    """

    def __init__(self, *, deletions: int, remote_total: int, ratio: float, limit: float) -> None:
        self.deletions = deletions
        self.remote_total = remote_total
        self.ratio = ratio
        self.limit = limit
        super().__init__(
            f"refusing to prune {deletions} of {remote_total} keys under the prefix "
            f"({ratio:.0%} > the {limit:.0%} blast-radius limit); nothing was changed.\n"
            "  The usual cause is a local corpus that is not the one you think it is: an\n"
            "  unbuilt or half-built build/kb, or a --source pointing one level too deep.\n"
            "  Rebuild with `pnpm kb:build` and re-run.\n"
            "  If this many chunks really are retiring, say the number out loud:\n"
            "  --max-delete-ratio 1.0 (or any value above the ratio printed above)."
        )


class OversizedDocRefused(ValueError):
    """A document likely exceeds the embedding model's token cap.

    Raised at plan time — before any upload — because with one-file-one-vector
    the alternative is a FAILED document surfacing mid-ingestion. Sidecars are
    exempt: they produce no vector, and their 2 KB filterable-metadata cap is
    Bedrock's own constraint, enforced at ingestion.
    """

    def __init__(self, docs: list[tuple[str, int]], limit: int) -> None:
        self.docs = docs
        self.limit = limit
        listing = "\n".join(f"  {path} — {size:,} B" for path, size in docs)
        super().__init__(
            f"{len(docs)} document(s) exceed {limit:,} B (≈{limit // _BYTES_PER_TOKEN:,} tokens "
            f"at {_BYTES_PER_TOKEN} B/token; Titan v2 caps at {TITAN_V2_MAX_TOKENS:,}); "
            "nothing was uploaded.\n"
            f"{listing}\n"
            "  Split the source document upstream (`pnpm kb:build`) so each chunk embeds\n"
            "  whole, or — if the file is measured to fit anyway — raise max_doc_bytes\n"
            "  (--max-doc-bytes) above the size printed above."
        )
