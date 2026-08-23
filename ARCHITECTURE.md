# Architecture note

## The problem I was actually solving

The data pack is deliberately imperfect. A deprecated policy sits alongside the
current one, two customer agreements override the general policy on some topics
but not others, one known issue is already resolved, and two closed tickets
contain advice that was wrong when it was given.

So the hard part is not retrieval. It is deciding which source governs a
particular question, and being able to show why. I built around that.

## Where decisions live

The rule I held to: **the model routes and explains, it never decides.**

Anything involving money, a threshold or a deadline goes through a deterministic
engine in `app/domain/`. `cancellation.py` decides whether a shipment can be
cancelled and for how much. `credit.py` decides whether a failed pickup earns a
credit. `sla.py` decides severity, target and breach. Each returns a `Decision`
carrying the outcome, the reasoning steps, the citations and any assumptions it
had to make.

The model receives that object and writes two sentences about it. It is told not
to recompute, and it has no arithmetic to do because the answer is already in
the result.

This is why the numbers in the demo are the numbers in the test suite. 42
ground-truth assertions pin them.

## Source precedence

Support Policy v3 §1 sets the order itself: signed agreement, then current
policy or SOP, then current product documentation. Deprecated documents are not
policy and historical tickets are context only. I did not invent that ranking, I
implemented the one the documents mandate.

The part that needed thought is that an agreement overrides only the topics it
addresses. LumenWorks' agreement replaces the failed-pickup credit but
explicitly defers to the SOP on cancellation fees. So precedence is resolved
**per topic, not per document**, `precedence.resolve(topic, account, at)`
returns which clause governs and why, including the case where a contract exists
but stays silent.

The visible consequence: "can I cancel without a fee?" gets three different
correct answers for three accounts, each cited to a different source.

One case I chose to surface rather than smooth over. LumenWorks' 4-hour credit
threshold *replaces* the SOP's 2-hour one, which makes them worse off between 2
and 4 hours than an account with no agreement at all. The system says so instead
of quietly applying whichever rule is kinder.

## Documents and structured data

Two separate paths, because they have different failure modes.

**Prose** is extracted from the PDFs at boot into 24 chunks, each tagged with
status, effective date, supersession and account scope, all parsed from the
document text, not assigned by hand. Retrieval is BM25, not embeddings. That was
a deliberate choice: two of the traps in the pack are cases where the
semantically nearest passage is the wrong one, and exact keyword overlap on
clause language beats fuzzy similarity here. Three filters run before ranking:
deprecated documents are excluded, contract chunks are scoped to their account,
and ticket history is not in the index at all.

**Numbers** never come from retrieval. Every threshold, fee and cap lives in
`app/sources/*.json` alongside the verbatim sentence it came from. At startup all
40 quotes are asserted against the text extracted from the PDFs and the app
refuses to start if one does not match. Change a number without changing the
document and it fails immediately. This is what lets me claim the system did not
invent a rule, the claim is checkable, not just stated.

## Access control

Enforced in the data layer, never in the prompt.

Authorisation attaches to the `Principal`, not to any tool argument. There is no
parameter a model can set to widen its own scope. `ScopedStore` filters before
data leaves it, and a cross-account read returns not-found, indistinguishable
from a record that does not exist, so absence leaks nothing either. For a
customer, `resolve_account()` returns their own account regardless of what
reference was passed, so a customer asking about another company by name gets
their own data or nothing.

The same check is applied again at the HTTP layer, so the dashboard endpoint
returns 403 for a customer even if the frontend were modified.

## Confirmation before state changes

Structural, not instructed. Telling a model to ask first is a suggestion, one
confused turn and it asks and then acts anyway.

So the model has exactly one action capability: `prepare_action`, which writes a
pending record and returns a preview. Committing is a separate HTTP endpoint that
only the interface calls, carrying a token that is never included in any tool
result and therefore never enters the model's context. The bypass is not
forbidden, it is absent from the model's vocabulary. A test asserts that no
model-callable tool contains "commit", "confirm", "execute" or "apply".

`prepare_action` also runs the engine that governs the action, so a draft carries
its own evidence: whether the amount exceeds the ₹1,000 approval limit, and
whether the sources entitle the customer to it at all. A manager approving a
₹2,000 credit sees that the records support ₹300, before approving.

## The agent loop

Eight iterations maximum, with a per-tool-name call budget. The budget is keyed
on the tool name rather than exact arguments, because a model defeats an
argument-based guard simply by rephrasing, which is what happened: five
searches, same passage, full context cost each time.

Two things reduce work rather than cap it. Before the model runs, the loop
resolves which sources govern the account in question and fetches any record the
message names by id, then hands both over as ordinary tool results. That
replaced a prompt rule telling the model to look things up first, which cost an
iteration and could be ignored. And once tool results exist, the full ruleset is
swapped for a 139-token continuation prompt, because the routing rules only
matter while the model is choosing tools.

The interface receives the full tool result; the model receives a compacted view.
An agent loop resends its whole context every iteration, so a fat result is paid
for repeatedly. Internal identifiers are dropped from the model's view entirely,
which makes leaking one into customer-facing prose impossible rather than
discouraged.

## Degrading rather than failing

Every answer's facts come from an engine, so a failed model call costs the
wording, not the answer. If the provider is unavailable at the point where the
model would write prose, the loop emits the engines' own summaries and marks the
turn degraded. On a free tier this is the difference between a rate limit being a
hiccup and being a lost answer.

The client is a thin wrapper over the OpenAI chat-completions shape rather than a
vendor SDK, so switching provider is two environment variables. Two provider
quirks are handled there: schema type unions are collapsed for providers whose
validator takes a single type, and the provider's own assistant message is
replayed verbatim rather than rebuilt, because some providers attach opaque
fields they require echoed back.

## Trade-offs I accepted

**BM25 over embeddings.** Better on this corpus and inspectable, but it would not
survive paraphrased questions over a much larger document set. At that size I
would add embeddings as a second retriever and keep BM25 for clause lookup.

**Rules over a model for proactive detection.** Every signal on the Today view is
a deterministic rule, so each one is explainable and testable. It will not spot a
pattern nobody anticipated.

**Actions in memory.** They reset with the process. The dataset is a read-only
snapshot and the assignment permits a mocked action tool, so persistence would
have been effort spent away from the parts that are graded.

**Assumptions stated, not invented.** Business hours are not defined anywhere in
the pack, so I picked 09:00–18:00 Monday to Friday, recorded it as an assumption
in `config.py`, and made the system say so when an answer depends on it. Same for
the `premium_support` flag, which appears in the data but is defined in no
document, it grants nothing, and the system says that when asked.

**A small model doing narration.** Because the engines own every number, a cheap
model is enough. That was a cost decision, and it holds only as long as the
engines keep the judgement.
