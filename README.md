# Meeting Intelligence

Most meeting tools tell you what was said. This one tracks what was decided — and what keeps getting left unresolved.

---

## What It Does

Paste in or upload a meeting transcript. The tool extracts:

- **Decisions Made** — what was actually resolved, with owner and confidence level
- **Open Items** — tasks assigned or implied, with owner and deadline extracted or inferred
- **Blockers and Risks** — things that surfaced that could slow execution, with severity
- **Unresolved Questions** — things that came up but didn't get answered, with why they matter
- **Draft Follow-up** — a ready-to-send email summarizing the above

Name a recurring meeting series and run each session against it. The tool tracks patterns across sessions and surfaces what keeps getting left unresolved — turning a one-shot summarizer into an accountability layer.

**Cross-session output (after 2+ sessions on the same series):**
- Organizational Friction Report — recurring blockers, persistently unresolved questions, overdue open items, execution debt score
- Decision Log — all decisions across all sessions, with date and owner

---

## What Makes It Different

Most meeting tools answer "what happened?" This one answers "what keeps happening?"

Gong, Granola, Gemini meeting notes, and Chorus all do single-session extraction. None of them surface organizational friction patterns across sessions — recurring blockers, persistently unresolved questions, overdue commitments, execution debt accumulating week over week.

The stronger differentiation is the reasoning quality baked into the extraction layer: the confidence scoring, the distinction between a real decision and a discussion, the "why it matters" framing on unresolved questions, and explicit detection of ownership vacuums and circular dependency patterns. That's operator judgment, not just feature parity.

---

## Architecture

```
Layer 1 — Deterministic Preprocessing (preprocessing.py)
  Timestamp stripping, speaker normalization, commitment detection,
  decision signal detection, question extraction, quality scoring.
  Runs before Claude sees anything.

Layer 2 — Reasoning Layer (prompt.py)
  Two-pass Claude API call using structured tool use.
  Pass 1: Extraction (decisions, open items, blockers, questions)
  Pass 2: Follow-up draft (forced tool call from structured Pass 1 output)
  Raw transcripts never passed to the follow-up pass.

Layer 3 — Persistence and Cross-Session Analysis (storage.py)
  Supabase (Postgres). Structured output only — raw transcripts never written.
  Fuzzy token-overlap matching for recurring pattern detection.
  Weighted execution debt scoring across sessions.
```

**Stack:**

| Layer | Technology |
|---|---|
| Frontend | Streamlit |
| Backend | Python |
| API | Anthropic Claude Sonnet (tool use, two-pass) |
| Persistence | Supabase (Postgres, free tier) |
| PDF parsing | pypdf |
| Hosting | Streamlit Community Cloud |

---

## Running It

### Prerequisites

- Python 3.11+
- An Anthropic API key
- A Supabase project (optional — cross-session tracking requires it; single-session runs work without it)

### Installation

```bash
git clone https://github.com/tommymeer/meeting-intelligence
cd meeting-intelligence
pip install -r requirements.txt
```

### Environment Variables

```bash
ANTHROPIC_API_KEY=your_key_here
SUPABASE_URL=your_project_url        # optional
SUPABASE_KEY=your_anon_key           # optional
```

For Streamlit Cloud deployment, add these under **App Settings → Secrets**.

### Supabase Setup (one-time, if using cross-session tracking)

1. Create a project at [supabase.com](https://supabase.com)
2. Run `supabase_schema.sql` in the SQL Editor
3. Run: `grant all on meeting_series to anon; grant all on session_results to anon;`
4. Copy Project URL and anon key to your environment variables

### Running Locally

```bash
streamlit run app.py
```

---

## Input

**Supported formats:** `.txt`, `.pdf`, direct paste
Optimized for Otter.ai, Fireflies, Zoom AI, and Gong exports.

**Limits:**
- Minimum: 200 words (below this, run is blocked)
- Soft cap: 15,000 words (~60 min meeting) — warning shown, run not blocked

---

## Cost and Spend Controls

- ~$0.03–0.05 per run at Claude Sonnet pricing (two API calls per run)
- Monthly spend limit: $50 (set in Anthropic Console → Billing → Limits)
- At this rate: ~1,000–1,500 runs before hitting the spend ceiling

---

## Scale and Limitations

This is a well-built demo and proof of concept. Current infrastructure is intentionally sized for individual use and low-volume distribution.

| Constraint | Detail |
|---|---|
| Concurrency | Streamlit Community Cloud is single-threaded per session — not designed for high concurrency. Handles light concurrent use (3–5 users); degrades beyond that. |
| Storage | Supabase free tier supports ~100,000+ rows before storage limits — sufficient for demo scale. |
| Context window | Soft cap at 15,000 words. Very long transcripts may produce less precise results. |
| Auth | RLS disabled in v1 — series names are globally unique; two users with the same series name share a series. Auth layer deferred to production version. |
| Integrations | No native Slack, Notion, or email send. Copy-paste covers downstream use cases without auth complexity. |
| Decision Log dates | Reflect analysis date, not meeting date — catch-up analysis on old transcripts will show the run date. |

At hundreds of concurrent users, this would require a proper backend with queuing, auth, and a paid infrastructure tier. That's a known limitation, documented intentionally.

---

## Privacy and Security

- Raw transcripts are never written to the database
- Only structured output (decisions, items, blockers, questions) is persisted in Supabase
- No API keys stored in the repository
- Transcripts are processed by Anthropic's API — review [Anthropic's privacy policy](https://www.anthropic.com/privacy) for data handling details
- For sensitive meetings, remove names and figures before pasting

---

## Known Limitations (v1)

- **Session UUID loss** — clearing cookies loses access to prior history (data remains in DB, accessible by series name)
- **No auth** — series names are globally unique; no user isolation
- **Single-threaded** — not designed for high concurrency
- **No integrations** — copy-paste only
- **Decision Log dates** — reflect run date, not meeting date
- **Incomplete category extraction** — Certain transcript structures can occasionally produce incomplete extraction — for example, decisions surfaced correctly while blockers or action items are underrepresented. A coverage detection layer flags these cases to the user rather than presenting incomplete output silently. The root cause is not fully characterized; contributing factors may include transcript structure, model attention allocation, or prompt design.


---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Transcript input, preprocessing, single-pass extraction, five-section output | ✅ Complete |
| 2 | Structured tool use, validated output schema | ✅ Complete |
| 3 | Confidence scoring, quality gate, input limits, UI polish | ✅ Complete |
| 4 | Supabase persistence, Meeting Series selector, Decision Log, Friction Report | ✅ Complete |
| 5 | Cross-session export, series management, fuzzy pattern matching, weighted debt model | ✅ Complete |
| 6 | Anonymization mode, first-time empty state, auth layer | 🔲 Planned |

---

## What This Demonstrates

Anyone can summarize a meeting. The judgment is in knowing what a decision actually looks like versus a discussion, what an implicit owner assignment sounds like, and what questions are dangerous to leave unresolved. That's the reasoning layer earning its keep.
