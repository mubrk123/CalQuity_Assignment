# AI tool usage

**Tool:** Claude (Anthropic), via Claude Code.

**How I used it:** as an implementer. I read the data pack, worked out that the
hard part was source precedence rather than retrieval, and decided the design.
Deterministic engines own every number. Precedence is resolved per topic rather
than per document. Access control sits on the principal. Confirmation is enforced
by removing the capability rather than instructing against it. Retrieval is
keyword, not embeddings. Claude wrote the code for those decisions, and the
tests.

I reviewed each change before it went in and rejected several, mostly where a
fix was proposed as a prompt instruction when the assignment calls for
enforcement in the data and tool layer.
