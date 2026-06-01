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
import json


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
            "and implicit ones ('someone needs to follow up on')."
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
- High: the transcript says it explicitly and unambiguously.
- Medium: strongly implied by context, role, or conversational flow.
- Low: you are inferring — flag it honestly.

Use the tools to record each item as you identify it. Call draft_followup last, \
after all other tools have been called. Do not summarize or editorialize beyond what the transcript supports.\
"""


def build_user_prompt(preprocessed: dict, prior_open_items: list) -> str:
    meta = preprocessed.get("metadata", {})
    commitments = preprocessed.get("explicit_commitments", [])
    decision_signals = preprocessed.get("decision_signals", [])
    questions = preprocessed.get("questions", [])
    normalized_text = preprocessed.get("normalized_text", "")

    lines = []

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
        for i, s in enumerate(decision_signals[:20], 1):  # cap at 20
            lines.append(f"{i}. {s['text']}")
        lines.append("")

    if commitments:
        lines.append("## COMMITMENT SIGNALS (flagged by preprocessing)")
        lines.append("These sentences contain language associated with assignments or commitments.")
        for i, s in enumerate(commitments[:30], 1):  # cap at 30
            lines.append(f"{i}. {s['text']}")
        lines.append("")

    if questions:
        lines.append("## QUESTIONS FLAGGED BY PREPROCESSING")
        lines.append("Evaluate which of these remain unresolved in the transcript.")
        for i, q in enumerate(questions[:20], 1):  # cap at 20
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
        "still_open": [],  # populated from prior_open_items cross-reference
    }

    for block in response.content:
        if block.type != "tool_use":
            continue

        name = block.name
        inp = block.input  # already a dict from the SDK

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

    Heuristic: if a prior item's task text has low token overlap with any
    current open item, it's likely still open.
    """
    if not prior_open_items:
        return result

    current_tasks = [item["task"].lower() for item in result.get("open_items", [])]

    def is_resolved(prior_task: str) -> bool:
        prior_tokens = set(prior_task.lower().split())
        for current in current_tasks:
            current_tokens = set(current.split())
            overlap = prior_tokens & current_tokens
            # If >40% of prior task tokens appear in a current task, consider it carried forward
            # (still open, not resolved)
            if len(prior_tokens) > 0 and len(overlap) / len(prior_tokens) > 0.4:
                return False  # still open
        return True  # resolved — not re-surfaced

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
) -> tuple[dict, str | None]:
    """
    Call Claude with tool use and return structured output.

    Returns (result_dict, error_message).
    error_message is None on success.
    """
    prior_open_items = prior_open_items or []

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(preprocessed, prior_open_items)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
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

    if prior_open_items:
        result = resolve_still_open(result, prior_open_items)

    return result, None
