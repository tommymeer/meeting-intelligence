"""
storage.py — Supabase persistence layer for Meeting Intelligence Phase 4.
Handles:
- Meeting series creation and lookup
- Session result writes (structured output only, never raw transcripts)
- Cross-session reads for Decision Log and Friction Report
Tables:
    meeting_series  (id uuid PK, name text, created_at timestamptz, session_uuid text)
    session_results (id uuid PK, series_id uuid FK, run_date timestamptz,
                     decisions jsonb, open_items jsonb, blockers jsonb,
                     open_questions jsonb, followup_email text)
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from supabase import create_client, Client
# ---------------------------------------------------------------------------
# Client initialisation
# ---------------------------------------------------------------------------
def get_supabase_client() -> Optional[Client]:
    """
    Return an authenticated Supabase client, or None if env vars are missing.
    Callers should treat None as "persistence unavailable" and degrade gracefully.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    return create_client(url, key)
# ---------------------------------------------------------------------------
# Meeting series helpers
# ---------------------------------------------------------------------------
def list_series(client: Client) -> list[dict]:
    """
    Return all meeting series rows, ordered by most recently created first.
    Each row: {id, name, created_at, session_uuid}
    """
    try:
        response = (
            client.table("meeting_series")
            .select("id, name, created_at, session_uuid")
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []
    except Exception:
        return []
def get_or_create_series(client: Client, name: str, session_uuid: str) -> Optional[str]:
    """
    Look up a meeting series by exact name.
    - If found, return its id (UUID string).
    - If not found, create it and return the new id.
    Returns None on error.
    """
    name = name.strip()
    if not name:
        return None
    try:
        # Try to find existing series by name
        response = (
            client.table("meeting_series")
            .select("id")
            .eq("name", name)
            .limit(1)
            .execute()
        )
        if response.data:
            return response.data[0]["id"]
        # Create new series
        new_id = str(uuid.uuid4())
        client.table("meeting_series").insert(
            {
                "id": new_id,
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "session_uuid": session_uuid,
            }
        ).execute()
        return new_id
    except Exception:
        return None
def get_series_names(client: Client) -> list[str]:
    """Return a flat list of meeting series names for the UI selector."""
    rows = list_series(client)
    return [r["name"] for r in rows]
def rename_series(client: Client, series_id: str, new_name: str) -> bool:
    """
    Rename a meeting series by id.
    Returns True on success, False on failure (including name collision).
    """
    new_name = new_name.strip()
    if not new_name or not series_id:
        return False
    try:
        # Check for name collision (another series with the same name)
        response = (
            client.table("meeting_series")
            .select("id")
            .eq("name", new_name)
            .neq("id", series_id)
            .limit(1)
            .execute()
        )
        if response.data:
            return False  # name already taken
        client.table("meeting_series").update({"name": new_name}).eq("id", series_id).execute()
        return True
    except Exception:
        return False
def delete_series(client: Client, series_id: str) -> bool:
    """
    Delete a meeting series and all its session results (FK cascade handles child rows).
    Returns True on success, False on failure.
    """
    if not series_id:
        return False
    try:
        client.table("meeting_series").delete().eq("id", series_id).execute()
        return True
    except Exception:
        return False
# ---------------------------------------------------------------------------
# Session result writes
# ---------------------------------------------------------------------------
def save_session_result(
    client: Client,
    series_id: str,
    decisions: list[dict],
    open_items: list[dict],
    blockers: list[dict],
    open_questions: list[dict],
    followup_email: str,
) -> bool:
    """
    Persist structured output for one meeting run.
    Raw transcript is never written here — only structured extraction results.
    Returns True on success, False on failure.
    """
    try:
        client.table("session_results").insert(
            {
                "id": str(uuid.uuid4()),
                "series_id": series_id,
                "run_date": datetime.now(timezone.utc).isoformat(),
                "decisions": decisions,
                "open_items": open_items,
                "blockers": blockers,
                "open_questions": open_questions,
                "followup_email": followup_email,
            }
        ).execute()
        return True
    except Exception:
        return False
# ---------------------------------------------------------------------------
# Session result reads
# ---------------------------------------------------------------------------
def get_series_results(client: Client, series_id: str) -> list[dict]:
    """
    Return all session results for a given series, oldest first.
    Each row: {id, series_id, run_date, decisions, open_items, blockers, open_questions, followup_email}
    """
    try:
        response = (
            client.table("session_results")
            .select("*")
            .eq("series_id", series_id)
            .order("run_date", desc=False)
            .execute()
        )
        return response.data or []
    except Exception:
        return []
def get_all_decisions(client: Client, series_id: str) -> list[dict]:
    """
    Flatten all decisions across every session for a series.
    Adds a `run_date` field to each decision dict for display in the Decision Log.
    """
    results = get_series_results(client, series_id)
    decisions = []
    for row in results:
        run_date = row.get("run_date", "")
        for d in row.get("decisions") or []:
            decisions.append({**d, "run_date": run_date})
    return decisions
def get_session_count(client: Client, series_id: str) -> int:
    """Return the number of sessions stored for a given series."""
    try:
        response = (
            client.table("session_results")
            .select("id", count="exact")
            .eq("series_id", series_id)
            .execute()
        )
        return response.count or 0
    except Exception:
        return 0
# ---------------------------------------------------------------------------
# Friction report helpers
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "of", "for", "to", "and", "or",
    "that", "this", "was", "has", "been", "are", "with", "we", "not",
    "be", "at", "by", "on", "as", "but", "if", "its", "our", "from",
    "have", "had", "will", "still", "no", "so", "do", "did", "can",
})

def _normalize_tokens(text: str) -> frozenset[str]:
    """
    Lowercase, remove punctuation, strip stop words, and loosely stem
    (remove trailing s/ing/ed/ly) to produce a comparable token set.
    """
    import re
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    tokens = set()
    for w in words:
        if w in _STOP_WORDS or len(w) < 3:
            continue
        # Loose stemming: strip common suffixes
        for suffix in ("ing", "ed", "ly"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        if w.endswith("s") and len(w) > 3:
            w = w[:-1]
        tokens.add(w)
    return frozenset(tokens)

def _fuzzy_match(tokens_a: frozenset[str], tokens_b: frozenset[str], threshold: float = 0.4) -> bool:
    """
    Return True if the token overlap exceeds `threshold` of the shorter set.
    Mirrors the approach used in prompt.py's resolve_still_open.
    """
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b)
    shorter = min(len(tokens_a), len(tokens_b))
    return (overlap / shorter) >= threshold

def _group_by_similarity(items: list[tuple[str, str]]) -> list[tuple[str, int]]:
    """
    Group (key_text, display_text) pairs by fuzzy token similarity.
    Returns [(canonical_display_text, group_size), ...] for groups with size >= 2.
    Each session contributes at most one item per group (dedup handled by caller).
    """
    # groups: list of (canonical_display, [token_sets_per_session_occurrence])
    groups: list[tuple[str, list[frozenset]]] = []
    for key_text, display_text in items:
        tokens = _normalize_tokens(key_text)
        matched = False
        for i, (canonical, token_list) in enumerate(groups):
            # Compare against the first (seed) token set for the group
            if _fuzzy_match(tokens, token_list[0]):
                token_list.append(tokens)
                matched = True
                break
        if not matched:
            groups.append((display_text, [tokens]))
    return [(canonical, len(token_list)) for canonical, token_list in groups if len(token_list) >= 2]

def build_friction_report(client: Client, series_id: str) -> dict:
    """
    Analyse cross-session structured data and return a friction report dict:
        {
            "recurring_blockers":       [{"description": ..., "seen_in_sessions": N}],
            "overdue_open_items":        [{task, owner, deadline, ...}],
            "recurring_open_questions":  [{"question": ..., "seen_in_sessions": N}],
            "execution_debt_score":      int,
        }
    Uses fuzzy token-overlap matching to detect recurring items even when
    Claude rephrases them across sessions. Only populated when >= 2 sessions exist.
    """
    results = get_series_results(client, series_id)
    if len(results) < 2:
        return {}

    # --- Recurring blockers ---
    # Collect one entry per unique blocker per session (exact dedup within session,
    # fuzzy grouping across sessions).
    blocker_items: list[tuple[str, str]] = []  # (key_text, display_text)
    for row in results:
        seen_in_session: set[str] = set()
        for b in row.get("blockers") or []:
            desc = b.get("description", "").strip()
            key = desc.lower()
            if key and key not in seen_in_session:
                blocker_items.append((desc, desc))
                seen_in_session.add(key)

    recurring_blockers = [
        {"description": display, "seen_in_sessions": count}
        for display, count in _group_by_similarity(blocker_items)
    ]

    # --- Overdue open items ---
    from datetime import date
    today = date.today()
    overdue_open_items = []
    seen_overdue_tasks: set[str] = set()
    for row in results:
        for item in row.get("open_items") or []:
            task_key = item.get("task", "").strip().lower()
            deadline_str = item.get("deadline", "")
            if not deadline_str or task_key in seen_overdue_tasks:
                continue
            try:
                deadline_date = date.fromisoformat(str(deadline_str)[:10])
                if deadline_date < today:
                    overdue_open_items.append(item)
                    seen_overdue_tasks.add(task_key)
            except (ValueError, TypeError):
                pass

    # --- Recurring open questions ---
    question_items: list[tuple[str, str]] = []  # (key_text, display_text)
    for row in results:
        seen_in_session: set[str] = set()
        for q in row.get("open_questions") or []:
            question = q.get("question", "").strip()
            key = question.lower()
            if key and key not in seen_in_session:
                question_items.append((question, question))
                seen_in_session.add(key)

    recurring_open_questions = [
        {"question": display, "seen_in_sessions": count}
        for display, count in _group_by_similarity(question_items)
    ]

    # --- Execution debt score ---
    all_open_items = sum(len(row.get("open_items") or []) for row in results[-1:])
    debt_score = (
        all_open_items
        + len(overdue_open_items)
        + len(recurring_blockers)
        + len(recurring_open_questions)
    )
    return {
        "recurring_blockers": recurring_blockers,
        "overdue_open_items": overdue_open_items,
        "recurring_open_questions": recurring_open_questions,
        "execution_debt_score": debt_score,
    }
