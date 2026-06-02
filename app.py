"""
app.py — Meeting Intelligence v5
Streamlit UI: input handling, output rendering, session state, Supabase persistence.
"""
import os
import uuid
import streamlit as st
from pypdf import PdfReader
import io
from preprocessing import preprocess_transcript
from prompt import run_meeting_intelligence
from storage import (
    get_supabase_client,
    get_series_names,
    get_or_create_series,
    save_session_result,
    get_all_decisions,
    get_session_count,
    build_friction_report,
    get_series_results,
    rename_series,
    delete_series,
)
# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Meeting Intelligence",
    page_icon="🧠",
    layout="wide",
)
# ── Constants ─────────────────────────────────────────────────────────────────
WORD_COUNT_MIN = 200
WORD_COUNT_SOFT_CAP = 15_000
# ── Session state init ────────────────────────────────────────────────────────
if "session_uuid" not in st.session_state:
    st.session_state.session_uuid = str(uuid.uuid4())
if "past_sessions" not in st.session_state:
    st.session_state.past_sessions = []
if "recurring_mode" not in st.session_state:
    st.session_state.recurring_mode = False
if "run_count" not in st.session_state:
    st.session_state.run_count = 0
if "series_id" not in st.session_state:
    st.session_state.series_id = None
if "series_name" not in st.session_state:
    st.session_state.series_name = ""
if "last_result" not in st.session_state:
    st.session_state.last_result = None
# Series management state
if "rename_input" not in st.session_state:
    st.session_state.rename_input = ""
if "confirm_delete" not in st.session_state:
    st.session_state.confirm_delete = False
# ── Supabase client ───────────────────────────────────────────────────────────
supabase = get_supabase_client()
persistence_available = supabase is not None
# ── Helpers ───────────────────────────────────────────────────────────────────
def extract_text_from_pdf(uploaded_file) -> str:
    reader = PdfReader(io.BytesIO(uploaded_file.read()))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
def extract_text_from_txt(uploaded_file) -> str:
    return uploaded_file.read().decode("utf-8", errors="replace")
def get_transcript_text(input_method, uploaded_file, pasted_text) -> tuple[str, str | None]:
    if input_method == "Upload file":
        if uploaded_file is None:
            return "", "No file uploaded."
        ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
        if ext == "pdf":
            text = extract_text_from_pdf(uploaded_file)
        elif ext == "txt":
            text = extract_text_from_txt(uploaded_file)
        else:
            return "", f"Unsupported file type: .{ext}. Please upload a .txt or .pdf file."
        if not text.strip():
            return "", "The uploaded file appears to be empty or could not be read."
        return text, None
    else:
        if not pasted_text or not pasted_text.strip():
            return "", "No transcript text provided."
        return pasted_text.strip(), None
def word_count(text: str) -> int:
    return len(text.split())
def render_confidence_badge(level: str) -> str:
    colors = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}
    return colors.get(level, "⚪")
def render_decisions(decisions: list):
    if not decisions:
        st.info("No explicit decisions detected.")
        return
    for i, d in enumerate(decisions, 1):
        badge = render_confidence_badge(d.get("confidence", "Low"))
        with st.container():
            col1, col2 = st.columns([0.05, 0.95])
            with col1:
                st.markdown(f"**{i}.**")
            with col2:
                owner = f" — *Owner: {d['owner']}*" if d.get("owner") else ""
                st.markdown(f"{badge} {d['description']}{owner}")
def render_open_items(open_items: list):
    if not open_items:
        st.info("No open items detected.")
        return
    for item in open_items:
        badge = render_confidence_badge(item.get("confidence", "Low"))
        owner = item.get("owner", "Unassigned")
        deadline = item.get("deadline", "No deadline")
        with st.expander(f"{badge} {item['task']}", expanded=True):
            col1, col2 = st.columns(2)
            col1.markdown(f"**Owner:** {owner}")
            col2.markdown(f"**Deadline:** {deadline}")
def render_blockers(blockers: list):
    if not blockers:
        st.success("No blockers or risks identified.")
        return
    for b in blockers:
        severity = b.get("severity", "Medium")
        icon = "🔴" if severity == "High" else "🟡" if severity == "Medium" else "🟢"
        st.write(f"{icon} **[{severity}]** {b['description']}")
def render_open_questions(questions: list):
    if not questions:
        st.info("No unresolved questions flagged.")
        return
    for q in questions:
        st.markdown(f"• **{q['question']}**")
        if q.get("why_it_matters"):
            st.caption(f"Why it matters: {q['why_it_matters']}")
def render_followup_email(email_text: str):
    if not email_text:
        st.info("No follow-up draft generated.")
        return
    st.text_area(
        label="Draft follow-up (copy and edit as needed)",
        value=email_text,
        height=300,
        key="followup_email_display",
    )
    st.download_button(
        label="⬇ Download as .txt",
        data=email_text,
        file_name="meeting_followup.txt",
        mime="text/plain",
    )
def render_still_open(still_open: list):
    if not still_open:
        return
    st.warning(f"**{len(still_open)} item(s) still open from last session**")
    for item in still_open:
        badge = render_confidence_badge(item.get("confidence", "Low"))
        owner = item.get("owner", "Unassigned")
        st.markdown(f"{badge} {item['task']} — *{owner}*")
def build_plain_text_export(result: dict, series_name: str = "") -> str:
    """Single-session export: decisions, open items, blockers, questions, follow-up."""
    lines = ["=" * 60, "MEETING INTELLIGENCE SUMMARY"]
    if series_name:
        lines.append(f"Series: {series_name}")
    lines.append("=" * 60)
    lines.append("\n## DECISIONS MADE")
    for i, d in enumerate(result.get("decisions", []), 1):
        owner = f" (Owner: {d['owner']})" if d.get("owner") else ""
        lines.append(f"{i}. [{d.get('confidence','')}] {d['description']}{owner}")
    lines.append("\n## OPEN ITEMS")
    for item in result.get("open_items", []):
        owner = item.get("owner", "Unassigned")
        deadline = item.get("deadline", "No deadline")
        lines.append(f"• [{item.get('confidence','')}] {item['task']} — {owner} — {deadline}")
    lines.append("\n## BLOCKERS AND RISKS")
    for b in result.get("blockers", []):
        lines.append(f"• [{b.get('severity','?')}] {b['description']}")
    lines.append("\n## UNRESOLVED QUESTIONS")
    for q in result.get("open_questions", []):
        lines.append(f"• {q['question']}")
        if q.get("why_it_matters"):
            lines.append(f"  → {q['why_it_matters']}")
    lines.append("\n## DRAFT FOLLOW-UP EMAIL")
    lines.append(result.get("followup_email", ""))
    return "\n".join(lines)
def build_cross_session_export(
    result: dict,
    series_name: str,
    friction: dict,
    all_decisions: list,
) -> str:
    """
    Full cross-session export: single-session output + Friction Report + Decision Log.
    Included in the second download button when series + Supabase are active.
    """
    lines = [build_plain_text_export(result, series_name)]
    # ── Organizational Friction Report ────────────────────────────────────────
    if friction:
        debt = friction.get("execution_debt_score", 0)
        lines.append("\n" + "=" * 60)
        lines.append(f"ORGANIZATIONAL FRICTION REPORT — Execution Debt: {debt}")
        lines.append("=" * 60)
        rb = friction.get("recurring_blockers", [])
        if rb:
            lines.append("\n## RECURRING BLOCKERS")
            for b in rb:
                lines.append(f"• {b['description']} (appeared in {b['seen_in_sessions']} sessions)")
        oi = friction.get("overdue_open_items", [])
        if oi:
            lines.append("\n## OVERDUE OPEN ITEMS")
            for item in oi:
                owner = item.get("owner", "unassigned")
                deadline = item.get("deadline", "unknown")
                lines.append(f"• {item.get('task', '—')} — {owner} — due {deadline}")
        rq = friction.get("recurring_open_questions", [])
        if rq:
            lines.append("\n## PERSISTENTLY UNRESOLVED QUESTIONS")
            for q in rq:
                lines.append(f"• {q['question']} (unresolved across {q['seen_in_sessions']} sessions)")
        if not rb and not oi and not rq:
            lines.append("\nNo recurring friction patterns detected across sessions.")
    # ── Decision Log ─────────────────────────────────────────────────────────
    if all_decisions:
        lines.append("\n" + "=" * 60)
        lines.append(f"DECISION LOG — {series_name}")
        lines.append("=" * 60)
        for d in all_decisions:
            run_date = (d.get("run_date") or "")[:10] or "unknown date"
            owner = d.get("owner") or "unassigned"
            confidence = d.get("confidence", "")
            desc = d.get("description", "—")
            lines.append(f"{run_date}  [{confidence}]  {desc}  — {owner}")
    return "\n".join(lines)
# ── Header ────────────────────────────────────────────────────────────────────
st.title("🧠 Meeting Intelligence")
st.markdown(
    "Most meeting tools tell you what was said. "
    "This one tracks what was decided — and what keeps getting left unresolved."
)
st.caption("Raw transcripts are never stored. Only structured output is saved.")
st.markdown(
    "1. Create a meeting series or select an existing one.  \n"
    "2. Upload or paste your meeting transcript.  \n"
    "3. Recurring Mode tracks patterns across sessions — surfacing what keeps getting left unresolved.  \n"
    "4. The tool extracts decisions, open items, blockers, and unresolved questions — "
    "with a draft follow-up ready to send and all analysis exportable via download."
)
st.divider()
# ── Meeting Series selector ───────────────────────────────────────────────────
st.subheader("Meeting Series")
if persistence_available:
    existing_series = get_series_names(supabase)
    series_options = ["— New series —"] + existing_series
    col_sel, col_new = st.columns([2, 3])
    with col_sel:
        selected_option = st.selectbox(
            "Select existing series",
            options=series_options,
            index=0,
            help="Pick a recurring meeting you've tracked before to build on its history.",
        )
    with col_new:
        if selected_option == "— New series —":
            series_name_input = st.text_input(
                "Or name a new series",
                placeholder="e.g. Q3 Planning Sync, Weekly Engineering Standup",
                value=st.session_state.series_name,
            )
            st.session_state.series_name = series_name_input.strip()
        else:
            st.session_state.series_name = selected_option
            st.session_state.recurring_mode = True
            st.markdown(f"**Selected:** {selected_option}")
    if st.session_state.series_name:
        resolved_id = get_or_create_series(
            supabase, st.session_state.series_name, st.session_state.session_uuid
        )
        st.session_state.series_id = resolved_id
        if resolved_id:
            count = get_session_count(supabase, resolved_id)
            if count > 0:
                st.caption(
                    f"📚 {count} session{'s' if count != 1 else ''} on record for this series."
                )
    else:
        st.session_state.series_id = None
        st.caption("Name your meeting series to enable cross-session tracking.")

    # ── Series Management ─────────────────────────────────────────────────────
    if selected_option != "— New series —" and st.session_state.series_id:
        with st.expander("⚙️ Manage this series", expanded=False):
            st.markdown("**Rename series**")
            rename_col, rename_btn_col = st.columns([3, 1])
            with rename_col:
                new_name = st.text_input(
                    "New name",
                    value=st.session_state.series_name,
                    key="rename_input_field",
                    label_visibility="collapsed",
                )
            with rename_btn_col:
                if st.button("Rename", key="rename_btn"):
                    new_name = new_name.strip()
                    if not new_name:
                        st.error("Name cannot be empty.")
                    elif new_name == st.session_state.series_name:
                        st.info("That's already the current name.")
                    else:
                        ok = rename_series(supabase, st.session_state.series_id, new_name)
                        if ok:
                            st.session_state.series_name = new_name
                            st.success(f"Renamed to **{new_name}**.")
                            st.rerun()
                        else:
                            st.error("Rename failed — that name may already be in use.")

            st.markdown("---")
            st.markdown("**Delete series**")
            st.caption(
                "Permanently deletes this series and all its session history. "
                "This cannot be undone."
            )
            confirm = st.checkbox(
                f'I understand — delete "{st.session_state.series_name}" and all its data',
                key="confirm_delete_checkbox",
            )
            if st.button("🗑 Delete series", key="delete_btn", disabled=(not confirm)):
                ok = delete_series(supabase, st.session_state.series_id)
                if ok:
                    st.session_state.series_id = None
                    st.session_state.series_name = ""
                    st.session_state.recurring_mode = False
                    st.session_state.last_result = None
                    st.success("Series deleted.")
                    st.rerun()
                else:
                    st.error("Delete failed. Please try again.")

else:
    series_name_input = st.text_input(
        "Meeting series name (optional)",
        placeholder="e.g. Q3 Planning Sync",
        value=st.session_state.series_name,
        help="Used for in-session recurring mode. Cross-session persistence requires Supabase.",
    )
    st.session_state.series_name = series_name_input.strip()
    st.caption(
        "⚠️ Cross-session persistence not configured. "
        "History exists only within this browser session."
    )
st.divider()
# ── Transcript Input ──────────────────────────────────────────────────────────
st.subheader("Transcript Input")
input_method = st.radio(
    "How would you like to provide the transcript?",
    options=["Upload file", "Paste text"],
    horizontal=True,
)
uploaded_file = None
pasted_text = ""
if input_method == "Upload file":
    uploaded_file = st.file_uploader(
        "Upload transcript",
        type=["txt", "pdf"],
        label_visibility="collapsed",
    )
    st.caption("Optimized for meetings up to ~60 minutes / 15,000 words.")
else:
    pasted_text = st.text_area(
        "Paste transcript here",
        height=300,
        placeholder="Paste your meeting transcript...",
        label_visibility="collapsed",
    )
# Live word count (paste only)
if input_method == "Paste text" and pasted_text:
    wc = word_count(pasted_text)
    if wc < WORD_COUNT_MIN:
        st.caption(f"📝 {wc} words — minimum {WORD_COUNT_MIN} required to run.")
    elif wc > WORD_COUNT_SOFT_CAP:
        st.warning(
            f"⚠️ {wc:,} words — above the recommended limit of {WORD_COUNT_SOFT_CAP:,}. "
            "Results may be less precise. Consider splitting into sections."
        )
    else:
        st.caption(f"📝 {wc:,} words")
st.divider()
# ── Options ───────────────────────────────────────────────────────────────────
st.subheader("Options")
st.session_state.recurring_mode = st.toggle(
    "Recurring meeting mode",
    value=st.session_state.recurring_mode,
    help=(
        "Tracks open items across sessions and surfaces what keeps getting left unresolved. "
        "Requires a named meeting series for cross-session persistence."
    ),
)
if st.session_state.recurring_mode:
    if not st.session_state.series_name:
        st.warning("Name your meeting series above to enable cross-session tracking.")
    elif not persistence_available:
        st.info(
            "In-session recurring mode active — history persists within this browser tab only. "
            "Configure Supabase credentials for cross-session persistence."
        )
    else:
        st.success("Cross-session tracking active for this series.")
anon_mode = False
st.caption(
    "For sensitive meetings, consider removing specific names and figures before pasting. "
    "Transcripts are processed by Anthropic's API and not stored."
)
st.divider()
# ── Run ───────────────────────────────────────────────────────────────────────
st.subheader("Run")
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    st.error(
        "ANTHROPIC_API_KEY not found. "
        "Set it as an environment variable or in Streamlit secrets."
    )
run_button = st.button("▶ Analyze Transcript", type="primary", disabled=(not api_key))
if run_button:
    raw_text, extraction_error = get_transcript_text(input_method, uploaded_file, pasted_text)
    if extraction_error:
        st.error(extraction_error)
        st.stop()
    wc = word_count(raw_text)
    if wc < WORD_COUNT_MIN:
        st.error(
            f"Transcript is too short ({wc} words). "
            f"Please provide at least {WORD_COUNT_MIN} words for meaningful analysis."
        )
        st.stop()
    if wc > WORD_COUNT_SOFT_CAP:
        st.warning(
            f"This transcript is longer than recommended ({wc:,} words). "
            "Results may be less precise — consider splitting into sections."
        )
    with st.spinner("Preprocessing transcript..."):
        preprocessed = preprocess_transcript(raw_text)
    quality = preprocessed.get("quality", {})
    if quality.get("flagged"):
        st.warning(
            f"⚠️ Transcript quality notice: {quality.get('reason', 'Low quality detected')}. "
            "Review outputs carefully."
        )
    prior_open_items = []
    prior_context = None
    if st.session_state.recurring_mode:
        if persistence_available and st.session_state.series_id:
            prior_context = get_series_results(supabase, st.session_state.series_id)
        elif st.session_state.past_sessions:
            prior_open_items = st.session_state.past_sessions[-1].get("open_items", [])
    with st.spinner("Analyzing with Claude..."):
        result, api_error = run_meeting_intelligence(
            preprocessed=preprocessed,
            api_key=api_key,
            prior_open_items=prior_open_items,
            prior_context=prior_context,
        )
    if api_error:
        st.error(f"API error: {api_error}")
        st.stop()
    if persistence_available and st.session_state.series_id:
        saved = save_session_result(
            client=supabase,
            series_id=st.session_state.series_id,
            decisions=result.get("decisions", []),
            open_items=result.get("open_items", []),
            blockers=result.get("blockers", []),
            open_questions=result.get("open_questions", []),
            followup_email=result.get("followup_email", ""),
        )
        if not saved:
            st.warning(
                "⚠️ Results could not be saved to the database. "
                "Check Supabase credentials in Streamlit secrets."
            )
    st.session_state.past_sessions.append(result)
    st.session_state.run_count += 1
    st.session_state.last_result = result
# ── Output ────────────────────────────────────────────────────────────────────
if st.session_state.last_result:
    result = st.session_state.last_result
    st.divider()
    # ── Organizational Friction Report ────────────────────────────────────────
    friction = {}
    if persistence_available and st.session_state.series_id:
        session_count = get_session_count(supabase, st.session_state.series_id)
        if session_count >= 2:
            friction = build_friction_report(supabase, st.session_state.series_id)
    elif st.session_state.recurring_mode and len(st.session_state.past_sessions) >= 2:
        from collections import Counter
        all_sessions = st.session_state.past_sessions
        blocker_counter: Counter = Counter()
        blocker_display: dict = {}
        for sess in all_sessions:
            seen = set()
            for b in sess.get("blockers", []):
                key = b.get("description", "").strip().lower()
                if key and key not in seen:
                    blocker_counter[key] += 1
                    blocker_display[key] = b.get("description", key)
                    seen.add(key)
        recurring_blockers = [
            {"description": blocker_display[k], "seen_in_sessions": v}
            for k, v in blocker_counter.items() if v >= 2
        ]
        question_counter: Counter = Counter()
        question_display: dict = {}
        for sess in all_sessions:
            seen = set()
            for q in sess.get("open_questions", []):
                key = q.get("question", "").strip().lower()
                if key and key not in seen:
                    question_counter[key] += 1
                    question_display[key] = q.get("question", key)
                    seen.add(key)
        recurring_questions = [
            {"question": question_display[k], "seen_in_sessions": v}
            for k, v in question_counter.items() if v >= 2
        ]
        if recurring_blockers or recurring_questions:
            friction = {
                "recurring_blockers": recurring_blockers,
                "overdue_open_items": [],
                "recurring_open_questions": recurring_questions,
                "execution_debt_score": len(recurring_blockers) + len(recurring_questions),
            }
    if friction:
        debt = friction.get("execution_debt_score", 0)
        debt_icon = "🔴" if debt >= 8 else "🟡" if debt >= 4 else "🟢"
        st.subheader(f"⚠️ Organizational Friction Report — Execution Debt: {debt_icon} {debt}")
        rb = friction.get("recurring_blockers", [])
        oi = friction.get("overdue_open_items", [])
        rq = friction.get("recurring_open_questions", [])
        if rb:
            st.markdown("**🔁 Recurring Blockers**")
            for b in rb:
                st.markdown(f"- {b['description']} *(appeared in {b['seen_in_sessions']} sessions)*")
        if oi:
            st.markdown("**📅 Overdue Open Items**")
            for item in oi:
                st.markdown(
                    f"- {item.get('task','—')} — "
                    f"**{item.get('owner','unassigned')}** — "
                    f"due {item.get('deadline','unknown')}"
                )
        if rq:
            st.markdown("**❓ Persistently Unresolved Questions**")
            for q in rq:
                st.markdown(f"- {q['question']} *(unresolved across {q['seen_in_sessions']} sessions)*")
        if not rb and not oi and not rq:
            st.success("No recurring friction patterns detected across sessions.")
        st.divider()
    # ── Analysis Results ──────────────────────────────────────────────────────
    st.subheader("📋 Analysis Results")
    high_conf_count = sum(
        1 for d in result.get("decisions", []) if d.get("confidence") == "High"
    )
    if high_conf_count == 0 and result.get("decisions"):
        st.info(
            "Few explicit decisions detected — this may reflect meeting style. "
            "Review Medium and Low confidence items carefully."
        )
    if st.session_state.recurring_mode and result.get("still_open"):
        with st.expander("🔁 Still Open From Last Session", expanded=True):
            render_still_open(result["still_open"])
    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_labels = [
        "✅ Decisions",
        "📌 Open Items",
        "🚧 Blockers",
        "❓ Unresolved Questions",
        "✉️ Follow-up Draft",
    ]
    if persistence_available and st.session_state.series_id:
        tab_labels.append("📋 Decision Log")
    tabs = st.tabs(tab_labels)
    with tabs[0]:
        render_decisions(result.get("decisions", []))
    with tabs[1]:
        render_open_items(result.get("open_items", []))
    with tabs[2]:
        render_blockers(result.get("blockers", []))
    with tabs[3]:
        render_open_questions(result.get("open_questions", []))
    with tabs[4]:
        render_followup_email(result.get("followup_email", ""))
    # Decision Log tab data (also used for cross-session export)
    all_decisions_for_export = []
    if len(tabs) > 5:
        with tabs[5]:
            if st.session_state.series_id:
                all_decisions_for_export = get_all_decisions(supabase, st.session_state.series_id)
                if all_decisions_for_export:
                    st.caption(
                        f"{len(all_decisions_for_export)} decision"
                        f"{'s' if len(all_decisions_for_export) != 1 else ''} on record for "
                        f"**{st.session_state.series_name}**."
                    )
                    for d in reversed(all_decisions_for_export):
                        run_date = (d.get("run_date") or "")[:10] or "unknown date"
                        owner = d.get("owner") or "unassigned"
                        badge = render_confidence_badge(d.get("confidence", "Low"))
                        col1, col2 = st.columns([0.15, 0.85])
                        with col1:
                            st.caption(run_date)
                        with col2:
                            st.markdown(f"{badge} {d.get('description','—')} — *{owner}*")
                else:
                    st.info("No decisions recorded yet for this series.")
            else:
                st.info("Name a meeting series above to enable the Decision Log.")
    # ── Export ────────────────────────────────────────────────────────────────
    st.divider()
    plain_export = build_plain_text_export(result, st.session_state.series_name)
    # Determine if cross-session export is available
    has_cross_session = (
        persistence_available
        and st.session_state.series_id
        and (friction or all_decisions_for_export)
    )
    if has_cross_session:
        # Fetch decisions if not already loaded (series has sessions but no Decision Log tab data)
        if not all_decisions_for_export:
            all_decisions_for_export = get_all_decisions(supabase, st.session_state.series_id)
        cross_export = build_cross_session_export(
            result,
            st.session_state.series_name,
            friction,
            all_decisions_for_export,
        )
        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            st.download_button(
                label="⬇ Download this session (.txt)",
                data=plain_export,
                file_name="meeting_summary.txt",
                mime="text/plain",
            )
        with dl_col2:
            st.download_button(
                label="⬇ Download full series report (.txt)",
                data=cross_export,
                file_name=f"meeting_series_{st.session_state.series_name.replace(' ', '_').lower()}.txt",
                mime="text/plain",
                help="Includes this session's analysis, the Friction Report, and the full Decision Log.",
            )
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                label="⬇ Download full summary (.txt)",
                data=plain_export,
                file_name="meeting_summary.txt",
                mime="text/plain",
            )
        with col2:
            st.code(plain_export, language=None)
    if (
        st.session_state.run_count >= 1
        and not st.session_state.recurring_mode
        and not st.session_state.series_name
    ):
        st.info(
            "Running this meeting weekly? Name your series above and enable "
            "**Recurring Mode** to track open items across sessions."
        )
