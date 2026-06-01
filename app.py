import streamlit as st
import os
from pypdf import PdfReader
import io

from preprocessing import preprocess_transcript
from prompt import run_meeting_intelligence

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Meeting Intelligence",
    page_icon="🧠",
    layout="wide",
)

# ── Constants ─────────────────────────────────────────────────────────────────
WORD_COUNT_MIN = 200
WORD_COUNT_SOFT_CAP = 15_000

# ── Session state init ────────────────────────────────────────────────────────
if "past_sessions" not in st.session_state:
    st.session_state.past_sessions = []          # list of structured output dicts
if "recurring_mode" not in st.session_state:
    st.session_state.recurring_mode = False
if "run_count" not in st.session_state:
    st.session_state.run_count = 0

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_text_from_pdf(uploaded_file) -> str:
    """Extract plain text from a PDF upload using pypdf."""
    reader = PdfReader(io.BytesIO(uploaded_file.read()))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def extract_text_from_txt(uploaded_file) -> str:
    return uploaded_file.read().decode("utf-8", errors="replace")


def get_transcript_text(input_method, uploaded_file, pasted_text) -> tuple[str, str | None]:
    """
    Returns (raw_text, error_message).
    error_message is None on success.
    """
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
        st.markdown(f"{icon} **[{severity}]** {b['description']}")


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
    """Renders open items carried over from prior sessions in recurring mode."""
    if not still_open:
        return
    st.warning(f"**{len(still_open)} item(s) still open from last session**")
    for item in still_open:
        badge = render_confidence_badge(item.get("confidence", "Low"))
        owner = item.get("owner", "Unassigned")
        st.markdown(f"{badge} {item['task']} — *{owner}*")


def build_plain_text_export(result: dict) -> str:
    """Assembles all five sections into a plain-text string for clipboard/download."""
    lines = []

    lines.append("=" * 60)
    lines.append("MEETING INTELLIGENCE SUMMARY")
    lines.append("=" * 60)

    lines.append("\n## DECISIONS MADE")
    for i, d in enumerate(result.get("decisions", []), 1):
        owner = f" (Owner: {d['owner']})" if d.get("owner") else ""
        conf = d.get("confidence", "")
        lines.append(f"{i}. [{conf}] {d['description']}{owner}")

    lines.append("\n## OPEN ITEMS")
    for item in result.get("open_items", []):
        owner = item.get("owner", "Unassigned")
        deadline = item.get("deadline", "No deadline")
        conf = item.get("confidence", "")
        lines.append(f"• [{conf}] {item['task']} — {owner} — {deadline}")

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


# ── Header ────────────────────────────────────────────────────────────────────
st.title("🧠 Meeting Intelligence")
st.markdown(
    "Paste or upload a meeting transcript. Get structured decisions, "
    "open items, blockers, unresolved questions, and a draft follow-up — ready to act on."
)
st.caption("Transcripts are not stored or logged. All processing is ephemeral.")

st.divider()

# ── Input section ─────────────────────────────────────────────────────────────
st.subheader("1. Transcript Input")

input_method = st.radio(
    "How would you like to provide the transcript?",
    options=["Upload file", "Paste text"],
    horizontal=True,
)

uploaded_file = None
pasted_text = ""

if input_method == "Upload file":
    st.caption("Supported: .txt, .pdf. DOCX support coming in a future version.")
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

# Live word count feedback
live_text = pasted_text if input_method == "Paste text" else ""
if live_text:
    wc = word_count(live_text)
    if wc < WORD_COUNT_MIN:
        st.caption(f"📝 {wc} words — minimum {WORD_COUNT_MIN} required to run.")
    elif wc > WORD_COUNT_SOFT_CAP:
        st.warning(
            f"⚠️ {wc:,} words — above the recommended limit of {WORD_COUNT_SOFT_CAP:,}. "
            "Results may be less precise. Consider splitting into sections."
        )
    else:
        color = "orange" if wc > WORD_COUNT_SOFT_CAP * 0.85 else "normal"
        st.caption(f"📝 {wc:,} words")

st.divider()

# ── Options ───────────────────────────────────────────────────────────────────
st.subheader("2. Options")

# Recurring mode — only show toggle after first successful run
if st.session_state.run_count >= 1:
    st.session_state.recurring_mode = st.toggle(
        "Recurring meeting mode",
        value=st.session_state.recurring_mode,
        help="Track open items across sessions within this browser tab. "
             "Data does not persist if the tab is closed.",
    )
else:
    st.session_state.recurring_mode = False

# Anonymization placeholder (Phase 3 — gated off)
anon_mode = False
st.caption(
    "🔒 Anonymization mode (replaces names and company references before sending to AI) — coming in a future version."
)

st.divider()

# ── Run ───────────────────────────────────────────────────────────────────────
st.subheader("3. Run")

api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    st.error("ANTHROPIC_API_KEY not found. Set it as an environment variable or in Streamlit secrets.")

run_button = st.button("▶ Analyze Transcript", type="primary", disabled=(not api_key))

if run_button:
    # Extract raw text
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

    # Preprocessing
    with st.spinner("Preprocessing transcript..."):
        preprocessed = preprocess_transcript(raw_text)

    # Quality gate warning (does not block run)
    quality = preprocessed.get("quality", {})
    if quality.get("flagged"):
        st.warning(
            f"⚠️ Transcript quality notice: {quality.get('reason', 'Low quality detected')}. "
            "Review outputs carefully."
        )

    # Prior session context for recurring mode
    prior_open_items = []
    if st.session_state.recurring_mode and st.session_state.past_sessions:
        last_session = st.session_state.past_sessions[-1]
        prior_open_items = last_session.get("open_items", [])

    # Claude call
    with st.spinner("Analyzing with Claude..."):
        result, api_error = run_meeting_intelligence(
            preprocessed=preprocessed,
            api_key=api_key,
            prior_open_items=prior_open_items,
        )

    if api_error:
        st.error(f"API error: {api_error}")
        st.stop()

    # Store in session
    st.session_state.past_sessions.append(result)
    st.session_state.run_count += 1

    # ── Output rendering ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("📋 Analysis Results")

    # Low-confidence notice
    high_conf_count = sum(
        1 for d in result.get("decisions", []) if d.get("confidence") == "High"
    )
    if high_conf_count == 0 and result.get("decisions"):
        st.info(
            "Few explicit decisions detected — this may reflect meeting style. "
            "Review Medium and Low confidence items carefully."
        )

    # Still open from last session (recurring mode)
    if st.session_state.recurring_mode and result.get("still_open"):
        with st.expander("🔁 Still Open From Last Session", expanded=True):
            render_still_open(result["still_open"])

    # Five sections
    tabs = st.tabs([
        "✅ Decisions",
        "📌 Open Items",
        "🚧 Blockers",
        "❓ Unresolved Questions",
        "✉️ Follow-up Draft",
    ])

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

    # Export
    st.divider()
    plain_export = build_plain_text_export(result)
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

    # Recurring mode prompt (shown after first successful run if not already enabled)
    if st.session_state.run_count == 1 and not st.session_state.recurring_mode:
        st.info(
            "Running this meeting weekly? Enable **Recurring Mode** above the Run button "
            "on your next upload to track open items across sessions."
        )

# ── Confidence legend ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Confidence Key")
    st.markdown("🟢 **High** — explicitly stated in transcript")
    st.markdown("🟡 **Medium** — strongly implied")
    st.markdown("🔴 **Low** — inferred; review carefully")
    st.divider()
    st.markdown("### About")
    st.markdown(
        "Built by [Thomas Meerschwam](https://thomasmeerschwam.substack.com) "
        "as part of an operator AI tools portfolio."
    )
    st.caption("v1.0 · Phase 1")
