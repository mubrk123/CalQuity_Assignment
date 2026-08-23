# Product note

## Which additional problems I took, and why

I built both.

**Proactive issue detection** was not really optional once I decided to support
the internal context. A support agent opening a tool at the start of a shift
wants to know what needs attention, not to think of the right question first. The
Today view runs rule-based detectors of seven kinds over the whole dataset and
surfaces ten signals, two of them critical: response targets already exceeded,
complaints a known issue already explains, customers who were given wrong advice
in a past ticket, and entitlements nobody has claimed.

Every signal is deterministic and traceable to a record, which matters because a
dashboard that cannot be audited gets ignored after the first wrong alert.

**Trust and reliability** is the whole system rather than a feature. Three
things carry it. Numbers come from engines pinned by tests, not from the model.
Every numeric rule is stored with the verbatim sentence it came from and verified
against the PDFs at startup, so the system cannot drift from the documents
without failing to boot. And every answer shows what it leaned on, the clause,
its authority tier, and the quote, so a reviewer can disagree with the answer
rather than having to trust it.

The visible test of that is asking three accounts the same cancellation question
and getting three different correct answers, each pointing at a different source.

## What I would build next

**Make escalations workable, not just visible.** Right now an escalation is
created and can be found, but nothing progresses it, no queue, no assignment, no
resolution. That is the obvious next increment and it is what would move this
from a demo to something a 20-person operations team could actually use.

**Close the loop on wrong past advice.** The system already detects that a
customer was told the wrong thing in a closed ticket. It should be able to draft
the correction to that customer, not just flag it internally.

**Coverage-aware answers on timing.** The business calendar works, but a customer
asking "when will someone reply?" on a Sunday deserves a date, not an
explanation of why the clock has not started.

## What I deliberately left out

**Persistence for actions.** They live in memory and reset with the process. The
supplied dataset is read-only and the assignment allows the action tool to be
mocked, so building a database would have spent time away from the parts that
matter.

**Embeddings.** Keyword retrieval is better on this corpus and I could test it. I
would revisit that at a document count where paraphrase matters more than exact
clause language.

**An LLM judging severity or eligibility.** Tempting, and it would handle edge
cases the rules miss. I chose rules because I can test them and explain them, and
because the cost of a confidently wrong credit decision is higher than the cost of
escalating an unusual case to a human.

**Multi-turn memory across sessions.** Each conversation is self-contained.

## One metric I would watch

**The share of answers where a human afterwards did something different from
what was proposed.**

Not accuracy against a labelled set, that measures whether the engines are
right, and the tests already do that. This measures whether the system is
*useful*: an agent who reads the answer and then quietly does something else has
been given a correct answer to the wrong question. It is measurable from the
action log, since every proposal is recorded and every commit or discard is too.

The related number worth tracking underneath it: how often the system escalates
rather than answering. That should not be zero, because refusing to guess is the
point. But if it climbs, the sources have a gap worth filling.

## What testing changed

Four defects found by using the thing, all of them design faults rather than
polish:

- A customer with two shipments was told they had none, because I had removed the
  tool that lists orders and the model filled the gap instead of admitting it
  could not look.
- "Record not found" was being read as "you have nothing", because the message
  was written for access control and said nothing about its own scope.
- Whether a credit was allowed was being decided by the model reading a clause,
  when the tool already knew the approval limit.
- A customer saw internal issue vocabulary in their own escalation, because the
  justification field is written for an approver and the same card is shown to
  both.

The pattern connecting the first two is worth stating: a missing capability does
not degrade into "I can't check that", it degrades into confabulation. Any tool
that can return nothing has to say what its emptiness means.
