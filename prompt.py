"""
prompt.py — Reasoning layer for Meeting Intelligence.
Constructs the Claude prompt from preprocessed transcript data and calls
the Anthropic API using structured tool use. Claude calls tools to build
output incrementally rather than returning a single prose response.
No file I/O. No Streamlit. Pure API logic.
Tool schema:
  create_decision(description, owner, confidence)
  create_action_item(task, owner, deadline, confidence)
  create_blocker(description, severity)
  create_open_question(question, why_it_matters)
  draft_followup(email_text)
"""
import anthropic
# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "create_decision",
        "description": (
            "Record a decision that was made in the meeting. Call this once per "
            "distinct decision. A decision is something that was resolved, agreed upon, "
            "or ratified — not merely discussed or proposed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Clear, standalone description of the decision made.",
                },
                "owner": {
                    "type": "string",
                    "description": "Person or role responsible, if named or clearly implied. Use null if unknown.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": (
                        "High: explicitly stated in the transcript. "
                        "Medium: strongly implied by context. "
                        "Low: inferred — requires human review."
                    ),
                },
            },
            "required": ["description", "confidence"],
        },
    },
    {
        "name": "create_action_item",
        "description": (
            "Record an action item, commitment, or open task assigned or implied in the meeting. "
            "Call this once per distinct task. Captures both explicit assignments ('John will send') "
            "and implicit ones ('someone needs to follow up on'). "
            "If no owner is named or clearly implied, record the task with owner: null and "
            "confidence: Low — do not omit it. An unowned task is an organizational gap that must be surfaced."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear, actionable description of the task.",
                },
                "owner": {
                    "type": "string",
                    "description": "Person or role assigned, if named or implied. Use null if unassigned.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Deadline if stated or implied (e.g. 'Friday', 'EOW', 'June 15'). Use null if none.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": (
                        "High: explicitly assigned with clear ownership. "
                        "Medium: strongly implied ownership or deadline. "
                        "Low: inferred — ownership or task scope is ambiguous."
                    ),
                },
            },
            "required": ["task", "confidence"],
        },
    },
    {
        "name": "create_blocker",
        "description": (
            "Record a blocker, risk, or dependency that surfaced in the meeting and could "
            "slow or prevent execution. Call this once per distinct blocker."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Clear description of the blocker or risk.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": (
                        "High: actively blocking progress or has a hard deadline impact. "
                        "Medium: significant risk if unaddressed soon. "
                        "Low: worth monitoring but not immediately urgent."
                    ),
                },
            },
            "required": ["description", "severity"],
        },
    },
    {
        "name": "create_open_question",
        "description": (
            "Record a question that was raised in the meeting but not answered or resolved. "
            "Flag questions that could slow execution or cause misalignment if left open. "
            "Do not include rhetorical questions or questions that were answered in the transcript."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The unresolved question, stated clearly.",
                },
                "why_it_matters": {
                    "type": "string",
                    "description": "One sentence on why this question matters for execution or alignment.",
                },
            },
            "required": ["question", "why_it_matters"],
        },
    },
    {
        "name": "draft_followup",
        "description": (
            "Draft a follow-up email summarizing the meeting. Call this exactly once, "
            "after all decisions, action items, blockers, and open questions have been recorded. "
            "The email should be ready to send with minimal editing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_text": {
                    "type": "string",
                    "description": (
                        "Plain text follow-up email. Include: subject line, brief context sentence, "
                        "decisions made, open items with owners and deadlines, unresolved questions, "
                        "and a clear next step or call to action. Professional but not stiff."
                    ),
                },
            },
            "required": ["email_text"],
        },
    },
]
# ── Prompt construction ────────────────────────────────────────────────────────
def build_system_prompt() -> str:
    return """\
You are an expert Chief of Staff and operational analyst. Your job is to extract \
structured intelligence from meeting transcripts with precision and judgment.
You have been given a preprocessed meeting transcript along with pre-extracted signals: \
commitment phrases, decision signals, and questions flagged by a deterministic preprocessing layer. \
Use these signals as starting points — do not treat them as exhaustive. \
The preprocessing layer catches explicit language; you catch meaning.
Your core judgment calls:
- A DECISION is something resolved, not just discussed. "We should probably..." is not a decision. \
"We're going with Vendor A" is.
- An ACTION ITEM requires a doer. If ownership is genuinely ambiguous, record it as Low confidence \
with the task clearly described — do not fabricate an owner.
- A BLOCKER is something that could slow or prevent execution, even if no one called it that explicitly.
- An OPEN QUESTION was raised and not answered. Rhetorical questions and answered questions don't count.
Confidence scoring discipline:
- High: the transcript says it explicitly and unambiguously. The person stated it directly with no hedging.
- Medium: strongly implied by context, role, or conversational flow. Includes hesitant acceptances ("I guess," "I can do that"), role-implied ownership, and deadlines inferred from context.
- Low: you are inferring — ownership or commitment is genuinely ambiguous. Flag it honestly.
Use the tools to record each item as you identify it. \
Do not summarize or editorialize beyond what the transcript supports.

Critical: deferrals and unresolved items are not the same as resolution.
- If a topic is deferred ("we'll revisit next week", "same decision: discuss later"), \
record the deferral as a decision AND separately record the underlying issue as a blocker \
or open question. A deferral decision does not close out the blocker or question \
— it means the problem persists.
- If an action item has no named owner, record it anyway with owner: null and confidence: Low. \
A task that belongs to nobody is more dangerous than a task with an ambiguous owner \
— omitting it hides an organizational failure that the tool exists to surface.
- Dysfunctional meeting patterns — repeated deferrals, ownership vacuums, unresolved blockers \
carried week over week — are signal, not noise. Extract them explicitly.
Extraction invariants — these are independent obligations, not optional:
- If the transcript contains any indication of risk, delay, dependency, stalled work, or \
unresolved uncertainty, you must call create_blocker at least once. Extracting decisions \
does not satisfy this requirement.
- If any task was assigned or implied — including tasks with no named owner — you must call \
create_action_item at least once. A meeting where decisions were made but nothing was assigned \
to anyone is almost never accurate.
- If any question was raised but deflected, deferred, or left unanswered, you must call \
create_open_question at least once.
These invariants exist because the model tends to terminate after extracting decisions. \
Do not terminate early. Coverage completeness across all four categories is the completion condition.
"""

def build_user_prompt(preprocessed: dict, prior_open_items: list) -> str:
    meta = preprocessed.get("metadata", {})
    commitments = preprocessed.get("explicit_commitments", [])
    decision_signals = preprocessed.get("decision_signals", [])
    questions = preprocessed.get("questions", [])
    normalized_text = preprocessed.get("normalized_text", "")
    lines = []
    # Extraction checklist — injected at the top of every prompt so Claude
    # works through all four categories before finishing, regardless of signal density.
    lines.append("## EXTRACTION INSTRUCTIONS")
    lines.append(
        "Work through the transcript and call tools for every item you identify. "
        "Before you finish, confirm you have actively called each of these tools as appropriate:\n"
        "- create_decision: for every decision made, including deferrals\n"
        "- create_action_item: for every task assigned or implied, including unowned ones (owner: null, confidence: Low)\n"
        "- create_blocker: for everything slowing or preventing execution, even if not named as a blocker explicitly\n"
        "- create_open_question: for every question raised but not answered, including deflected ones\n"
        "An empty category is only correct if you actively looked and found nothing — not if you stopped early. "
        "Do not call draft_followup until you have worked through all four tool types."
    )
    lines.append("")
    # Meeting metadata
    lines.append("## MEETING METADATA")
    lines.append(f"Title: {meta.get('title') or 'Not detected'}")
    lines.append(f"Date: {meta.get('date') or 'Not detected'}")
    lines.append(f"Attendees: {meta.get('attendees_raw') or 'Not detected'}")
    lines.append("")
    # Preprocessed signals
    if decision_signals:
        lines.append("## DECISION SIGNALS (flagged by preprocessing)")
        lines.append("These sentences contain language associated with decisions. Evaluate each carefully.")
        for i, s in enumerate(decision_signals[:20], 1):
            lines.append(f"{i}. {s['text']}")
        lines.append("")
    if commitments:
        lines.append("## COMMITMENT SIGNALS (flagged by preprocessing)")
        lines.append("These sentences contain language associated with assignments or commitments.")
        for i, s in enumerate(commitments[:30], 1):
            lines.append(f"{i}. {s['text']}")
        lines.append("")
    if questions:
        lines.append("## QUESTIONS FLAGGED BY PREPROCESSING")
        lines.append("Evaluate which of these remain unresolved in the transcript.")
        for i, q in enumerate(questions[:20], 1):
            lines.append(f"{i}. {q}")
        lines.append("")
    # Prior open items for recurring mode
    if prior_open_items:
        lines.append("## OPEN ITEMS FROM LAST SESSION (recurring meeting mode)")
        lines.append(
            "For each item below, assess whether it was resolved, still open, or escalated "
            "based on the current transcript. Surface still-open items in your analysis."
        )
        for i, item in enumerate(prior_open_items, 1):
            owner = item.get("owner", "Unassigned")
            deadline = item.get("deadline", "No deadline")
            lines.append(f"{i}. {item.get('task', '')} — {owner} — {deadline}")
        lines.append("")
    # Low-signal warning — injected when preprocessing found few explicit signals.
    # A sparse signal list means the transcript lacks explicit commitment/decision
    # language, not that it lacks content. Claude must read the full transcript
    # directly rather than anchoring on the short signal list.
    total_signals = len(commitments) + len(decision_signals) + len(questions)
    if total_signals < 4:
        lines.append("## ⚠ LOW-SIGNAL TRANSCRIPT NOTICE")
        lines.append(
            "The preprocessing layer found very few explicit commitment or decision phrases in this transcript. "
            "This does NOT mean the meeting was uneventful — it means the language was implicit, "
            "indirect, or structured around deferrals rather than explicit assignments. "
            "You must read the full transcript carefully and extract blockers, open items, and "
            "unresolved questions directly from context. "
            "Common patterns to look for in low-signal transcripts: "
            "(1) topics that end with 'we'll discuss next week' with no owner — these are deferrals AND blockers; "
            "(2) tasks nobody claimed — record with owner: null, confidence: Low; "
            "(3) questions raised but deflected rather than answered — these are open questions; "
            "(4) repeated circular disagreements with no resolution — these are both blockers and open questions. "
            "Do not let a short signal list constrain your extraction. Extract from the transcript itself."
        )
        lines.append("")
    # Full transcript
    lines.append("## TRANSCRIPT")
    lines.append(normalized_text)
    return '\n'.join(lines)
# ── Response parsing ───────────────────────────────────────────────────────────
def parse_tool_calls(response) -> dict:
    """
    Walk the response content blocks and assemble structured output
    from tool use calls.
    Returns the five-section dict that app.py renders.
    """
    result = {
        "decisions": [],
        "open_items": [],
        "blockers": [],
        "open_questions": [],
        "followup_email": "",
        "still_open": [],
    }
    for block in response.content:
        if block.type != "tool_use":
            continue
        name = block.name
        inp = block.input
        if name == "create_decision":
            result["decisions"].append({
                "description": inp.get("description", ""),
                "owner": inp.get("owner"),
                "confidence": inp.get("confidence", "Low"),
            })
        elif name == "create_action_item":
            result["open_items"].append({
                "task": inp.get("task", ""),
                "owner": inp.get("owner"),
                "deadline": inp.get("deadline"),
                "confidence": inp.get("confidence", "Low"),
            })
        elif name == "create_blocker":
            result["blockers"].append({
                "description": inp.get("description", ""),
                "severity": inp.get("severity", "Medium"),
            })
        elif name == "create_open_question":
            result["open_questions"].append({
                "question": inp.get("question", ""),
                "why_it_matters": inp.get("why_it_matters", ""),
            })
        elif name == "draft_followup":
            result["followup_email"] = inp.get("email_text", "")
    return result

def resolve_still_open(result: dict, prior_open_items: list) -> dict:
    """
    Cross-reference prior open items against current open_items.
    Items from the prior session that don't appear resolved are surfaced
    in result['still_open'].
    """
    if not prior_open_items:
        return result
    current_tasks = [item["task"].lower() for item in result.get("open_items", [])]
    def is_resolved(prior_task: str) -> bool:
        prior_tokens = set(prior_task.lower().split())
        for current in current_tasks:
            current_tokens = set(current.split())
            overlap = prior_tokens & current_tokens
            if len(prior_tokens) > 0 and len(overlap) / len(prior_tokens) > 0.4:
                return False
        return True
    still_open = []
    for item in prior_open_items:
        if not is_resolved(item.get("task", "")):
            still_open.append(item)
    result["still_open"] = still_open
    return result

# ── Main entry point ───────────────────────────────────────────────────────────
def run_meeting_intelligence(
    preprocessed: dict,
    api_key: str,
    prior_open_items: list = None,
    prior_context: list = None,
) -> tuple[dict, str | None]:
    """
    `prior_context` accepts the richer format used by Phase 4 Supabase reads:
    a list of session_results rows (each with an "open_items" key).
    If provided, it is flattened into prior_open_items.
    `prior_open_items` (flat list) is still accepted for in-session recurring mode.
    """
    if prior_context and not prior_open_items:
        # Flatten open_items from all prior sessions; deduplicate by task text
        seen: set[str] = set()
        flattened: list[dict] = []
        for session in prior_context:
            for item in session.get("open_items") or []:
                key = (item.get("task") or "").strip().lower()
                if key and key not in seen:
                    flattened.append(item)
                    seen.add(key)
        prior_open_items = flattened
    prior_open_items = prior_open_items or []
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(preprocessed, prior_open_items)
    # ── Pass 1: extraction ─────────────────────────────────────────────────────
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            tools=TOOLS,
            tool_choice={"type": "auto"},
            messages=[
                {"role": "user", "content": user_prompt}
            ],
        )
    except anthropic.APIConnectionError as e:
        return {}, f"Connection error: {e}"
    except anthropic.RateLimitError:
        return {}, "Rate limit reached. Please wait a moment and try again."
    except anthropic.APIStatusError as e:
        return {}, f"API error {e.status_code}: {e.message}"
    except Exception as e:
        return {}, f"Unexpected error: {e}"
    result = parse_tool_calls(response)
    # Guard: if all four extraction categories are empty, Claude returned no tool calls
    # (possible with tool_choice auto). Surface an error rather than saving an empty
    # session to Supabase, which would corrupt the series history.
    if (
        not result["decisions"]
        and not result["open_items"]
        and not result["blockers"]
        and not result["open_questions"]
    ):
        return {}, (
            "Extraction returned no results. Claude may have responded with text instead of tool calls. "
            "This can happen with very short or ambiguous transcripts. "
            "Please try again — if the problem persists, check that the transcript contains sufficient content."
        )
    # Low-coverage flag: decisions extracted but blockers and open items both empty.
    # This is the category collapse pattern — model terminated after decisions.
    # Flag for UI warning; session still saves (partial data is better than no data).
    result["low_coverage"] = (
        bool(result["decisions"])
        and not result["open_items"]
        and not result["blockers"]
    )
    # ── Pass 2: force draft_followup ───────────────────────────────────────────
    decisions_text = "\n".join(
        f"- {d['description']} (Owner: {d.get('owner') or 'Unassigned'})"
        for d in result["decisions"]
    ) or "None identified."
    open_items_text = "\n".join(
        f"- {i['task']} (Owner: {i.get('owner') or 'Unassigned'}, Deadline: {i.get('deadline') or 'None'})"
        for i in result["open_items"]
    ) or "None identified."
    blockers_text = "\n".join(
        f"- [{b['severity']}] {b['description']}"
        for b in result["blockers"]
    ) or "None identified."
    questions_text = "\n".join(
        f"- {q['question']}"
        for q in result["open_questions"]
    ) or "None identified."
    followup_prompt = f"""You are drafting a follow-up email for a meeting. \
Use the structured analysis below to write a complete, ready-to-send email.
DECISIONS MADE:
{decisions_text}
OPEN ITEMS:
{open_items_text}
BLOCKERS:
{blockers_text}
UNRESOLVED QUESTIONS:
{questions_text}
Draft a professional but direct follow-up email that covers all of the above. \
Include a subject line. Keep it actionable and concise."""
    try:
        followup_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            tools=TOOLS,
            tool_choice={"type": "tool", "name": "draft_followup"},
            messages=[
                {"role": "user", "content": followup_prompt}
            ],
        )
        for block in followup_response.content:
            if block.type == "tool_use" and block.name == "draft_followup":
                result["followup_email"] = block.input.get("email_text", "")
                break
    except Exception:
        pass  # follow-up email is best-effort; don't fail the whole run
    if prior_open_items:
        result = resolve_still_open(result, prior_open_items)
    return result, None
