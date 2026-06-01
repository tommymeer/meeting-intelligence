"""
preprocessing.py — Deterministic layer for Meeting Intelligence.

Runs before Claude sees anything. Responsible for:
  - Timestamp stripping
  - Speaker label normalization
  - Explicit commitment detection
  - Question extraction
  - Metadata extraction (title, date, attendees)
  - Quality scoring and flagging

Returns a structured dict that prompt.py consumes.
No API calls. No Claude. Pure Python.
"""

import re
from collections import Counter


# ── Regex patterns ─────────────────────────────────────────────────────────────

# Timestamp formats: [00:14:32], [00:14], 00:14:32, (00:14:32), 1:23:45
_TIMESTAMP_PATTERNS = [
    r'\[\d{1,2}:\d{2}(?::\d{2})?\]',   # [00:14] or [00:14:32]
    r'\(\d{1,2}:\d{2}(?::\d{2})?\)',   # (00:14) or (00:14:32)
    r'(?<!\w)\d{1,2}:\d{2}:\d{2}(?!\w)',  # bare 00:14:32 not inside a word
]
_TIMESTAMP_RE = re.compile('|'.join(_TIMESTAMP_PATTERNS))

# Speaker label: "John:" or "JOHN SMITH:" or "Speaker 1:" at start of line
_SPEAKER_RE = re.compile(r'^([A-Za-z][A-Za-z0-9 _\-\.]{0,40}):\s*', re.MULTILINE)

# Explicit commitment signals
_COMMITMENT_PATTERNS = [
    r"\bI(?:'ll| will)\b.{5,120}",                        # I'll / I will
    r"\bwe(?:'ll| will)\b.{5,120}",                       # we'll / we will
    r"\baction item\b.{0,120}",                            # action item
    r"\bfollow[\s-]?up\b.{0,120}",                        # follow up / follow-up
    r"\bby (?:end of )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|eod|eow|next week)\b.{0,80}",
    r"\bby (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2}\b.{0,80}",
    r"\bdeadline\b.{0,120}",
    r"\bwho(?:'s| is) (?:going to |handling |owning |taking ).{5,100}",
    r"\b(?:you|he|she|they|someone)\s+(?:should|needs? to|will)\b.{5,100}",
    r"\bowned? by\b.{0,80}",
    r"\bresponsible for\b.{0,80}",
    r"\btake(?:s)? (?:this|that|it)\b.{0,80}",
    r"\bsend(?:ing)?\b.{5,80}",
    r"\bschedule\b.{5,80}",
    r"\bblock(?:ing)? (?:time|calendar)\b.{0,80}",
]
_COMMITMENT_RE = [re.compile(p, re.IGNORECASE) for p in _COMMITMENT_PATTERNS]

# Decision signals
_DECISION_PATTERNS = [
    r"\bwe(?:'ve)? decided\b.{5,150}",
    r"\bwe(?:'re)? going (?:to|with)\b.{5,150}",
    r"\bdecision\b.{0,150}",
    r"\bagreed(?: to| that| on)?\b.{5,150}",
    r"\bapproved\b.{0,150}",
    r"\blet(?:'s| us) go with\b.{5,150}",
    r"\bfinal(?:ized|ly)?\b.{5,100}",
    r"\bconfirmed\b.{0,120}",
    r"\bmoving forward with\b.{5,150}",
    r"\bwe(?:'ll| will) (?:use|go with|proceed with|adopt|implement)\b.{5,150}",
]
_DECISION_RE = [re.compile(p, re.IGNORECASE) for p in _DECISION_PATTERNS]

# Question detection: sentence ending in "?"
_QUESTION_RE = re.compile(r'[A-Z][^.!?]{10,200}\?')

# Common filler / non-question fragments to skip
_QUESTION_FILLER = re.compile(
    r'^(okay|right|so|yeah|yes|no|sure|great|got it|makes sense)\??$',
    re.IGNORECASE,
)

# Meeting header metadata
_TITLE_RE = re.compile(
    r'(?:meeting|title|subject|re)[\s:]+([^\n]{5,80})',
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r'(?:date|recorded|session)[\s:]+([^\n]{5,40})',
    re.IGNORECASE,
)
_ATTENDEES_RE = re.compile(
    r'(?:attendees?|participants?|present|in attendance)[\s:]+([^\n]{5,200})',
    re.IGNORECASE,
)


# ── Core functions ─────────────────────────────────────────────────────────────

def strip_timestamps(text: str) -> str:
    """Remove timestamp tokens from transcript text."""
    return _TIMESTAMP_RE.sub('', text)


def normalize_speakers(text: str) -> tuple[str, dict]:
    """
    Best-effort speaker normalization.

    Finds all unique speaker labels, attempts to consolidate near-duplicates
    (e.g. 'John D.' and 'John' → 'John'), and returns the cleaned text plus
    a mapping of original → normalized labels.
    """
    raw_labels = _SPEAKER_RE.findall(text)
    unique_labels = list(dict.fromkeys(l.strip() for l in raw_labels))

    # Build a normalization map: try to consolidate by first token
    norm_map = {}
    first_token_seen = {}  # first_token → canonical label

    for label in unique_labels:
        first = label.split()[0].rstrip('.,').lower()
        if first in first_token_seen:
            norm_map[label] = first_token_seen[first]
        else:
            first_token_seen[first] = label
            norm_map[label] = label

    # Apply normalization back to text
    def replace_speaker(m):
        original = m.group(1).strip()
        normalized = norm_map.get(original, original)
        return f"{normalized}: "

    normalized_text = _SPEAKER_RE.sub(replace_speaker, text)
    return normalized_text, norm_map


def extract_metadata(raw_text: str) -> dict:
    """
    Extract meeting title, date, and attendees from the first 30 lines of the
    transcript, where headers are most likely to appear.
    """
    header = '\n'.join(raw_text.splitlines()[:30])

    title_match = _TITLE_RE.search(header)
    date_match = _DATE_RE.search(header)
    attendees_match = _ATTENDEES_RE.search(header)

    return {
        "title": title_match.group(1).strip() if title_match else None,
        "date": date_match.group(1).strip() if date_match else None,
        "attendees_raw": attendees_match.group(1).strip() if attendees_match else None,
    }


def detect_explicit_commitments(text: str) -> list[dict]:
    """
    Scan for sentences matching commitment patterns.
    Returns a list of dicts with the matched text and pattern category.
    """
    # Split into sentences (rough — good enough for preprocessing signal)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    found = []
    seen = set()

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 15:
            continue
        for pattern in _COMMITMENT_RE:
            m = pattern.search(sentence)
            if m and sentence not in seen:
                found.append({
                    "text": sentence[:300],  # cap length
                    "type": "commitment",
                })
                seen.add(sentence)
                break  # one match per sentence is enough

    return found


def detect_decision_signals(text: str) -> list[dict]:
    """
    Scan for sentences matching decision patterns.
    Returns a list of dicts with the matched text.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text)
    found = []
    seen = set()

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 15:
            continue
        for pattern in _DECISION_RE:
            m = pattern.search(sentence)
            if m and sentence not in seen:
                found.append({
                    "text": sentence[:300],
                    "type": "decision_signal",
                })
                seen.add(sentence)
                break

    return found


def extract_questions(text: str) -> list[str]:
    """
    Extract sentences ending in '?' that look like genuine questions.
    Filters out filler fragments.
    """
    candidates = _QUESTION_RE.findall(text)
    questions = []
    seen = set()

    for q in candidates:
        q = q.strip()
        core = q.rstrip('?').strip()
        if _QUESTION_FILLER.match(core):
            continue
        if q in seen:
            continue
        questions.append(q)
        seen.add(q)

    return questions


def count_speakers(text: str) -> int:
    """Count unique speaker labels detected in the transcript."""
    labels = _SPEAKER_RE.findall(text)
    unique = set(l.strip().lower() for l in labels)
    return len(unique)


def score_quality(text: str, word_count: int, speaker_count: int) -> dict:
    """
    Run quality checks and return a quality dict.
    flagged=True means the UI should warn the user but not block the run.
    """
    reasons = []

    if word_count < 200:
        reasons.append("transcript is too short for reliable analysis")

    if speaker_count == 0:
        reasons.append("no speaker labels detected — transcript may be unformatted")
    elif speaker_count == 1:
        reasons.append("only one speaker detected — may be a monologue or notes, not a meeting")

    # Garbled content heuristic: very high ratio of short lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        short_line_ratio = sum(1 for l in lines if len(l.split()) < 3) / len(lines)
        if short_line_ratio > 0.6:
            reasons.append("high proportion of very short lines — transcript may be fragmented")

    # Repetition heuristic: if top 5-word ngram appears > 20 times, likely garbled
    words = text.lower().split()
    if len(words) >= 5:
        ngrams = [' '.join(words[i:i+5]) for i in range(len(words) - 4)]
        top = Counter(ngrams).most_common(1)
        if top and top[0][1] > 20:
            reasons.append("high phrase repetition detected — transcript may be corrupted")

    flagged = len(reasons) > 0
    return {
        "flagged": flagged,
        "reason": "; ".join(reasons) if reasons else None,
        "word_count": word_count,
        "speaker_count": speaker_count,
    }


# ── Anonymization ─────────────────────────────────────────────────────────────

# Financial pattern replacements (regex, runs before spaCy)
_FINANCIAL_PATTERNS = [
    # Revenue / ARR / MRR figures: $47M, $2.3M ARR, $150K
    (re.compile(
        r'\$\s*\d+(?:[.,]\d+)*\s*(?:M|K|B|million|billion|thousand)?\b'
        r'(?:\s*(?:ARR|MRR|ARR|revenue|burn|runway|contract|deal|raise|round))?',
        re.IGNORECASE), '[REVENUE_FIGURE]'),
    # Valuation: valued at $X, at a $X valuation
    (re.compile(
        r'(?:valued?\s+at|valuation\s+of|at\s+a)\s+\$\s*\d+(?:[.,]\d+)*\s*(?:M|K|B|million|billion)?',
        re.IGNORECASE), '[VALUATION]'),
    # Margin / growth / percentage figures: 23%, up 34%, margins at 18%
    (re.compile(
        r'\b\d+(?:\.\d+)?\s*%(?:\s*(?:margin|growth|increase|decrease|churn|retention|conversion|adoption))?',
        re.IGNORECASE), '[PERCENTAGE]'),
    # Headcount: 23 engineers, team of 45, 120 employees
    (re.compile(
        r'\b\d+\s*(?:engineers?|developers?|employees?|headcount|people|hires?|reps?|salespeople)\b',
        re.IGNORECASE), '[HEADCOUNT]'),
    # Runway: 18 months of runway
    (re.compile(
        r'\b\d+\s*months?\s+(?:of\s+)?runway\b',
        re.IGNORECASE), '[RUNWAY]'),
]


def anonymize_transcript(text: str) -> tuple[str, dict]:
    """
    Anonymize sensitive entities in a transcript before it hits Claude.

    Two passes:
      1. Regex pass — replaces financial figures and metrics with labeled placeholders
      2. spaCy NER pass — replaces person names, organizations, and products

    Returns (anonymized_text, entity_map) where entity_map records replacements
    so the user can map back if needed.
    """
    entity_map: dict[str, str] = {}

    # ── Pass 1: financial regex ───────────────────────────────────────────────
    for pattern, placeholder in _FINANCIAL_PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            if match.strip() and match.strip() not in entity_map:
                entity_map[match.strip()] = placeholder
        text = pattern.sub(placeholder, text)

    # ── Pass 2: spaCy NER ─────────────────────────────────────────────────────
    try:
        import spacy
        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            # Model not available — skip NER, return financial-redacted text only
            return text, entity_map

        doc = nlp(text)

        # Build ordered replacement map: longer matches first to avoid partial replacements
        person_counter = 0
        person_map: dict[str, str] = {}  # original name → Person A/B/C

        replacements: list[tuple[int, int, str]] = []

        for ent in doc.ents:
            original = ent.text.strip()
            if not original:
                continue

            if ent.label_ == "PERSON":
                if original not in person_map:
                    label = f"Person {chr(65 + person_counter)}"  # A, B, C...
                    person_map[original] = label
                    entity_map[original] = label
                    person_counter += 1
                replacements.append((ent.start_char, ent.end_char, person_map[original]))

            elif ent.label_ in ("ORG", "COMPANY"):
                replacements.append((ent.start_char, ent.end_char, "[Organization]"))
                entity_map[original] = "[Organization]"

            elif ent.label_ in ("PRODUCT", "WORK_OF_ART"):
                replacements.append((ent.start_char, ent.end_char, "[Product]"))
                entity_map[original] = "[Product]"

        # Apply replacements in reverse order to preserve character offsets
        replacements.sort(key=lambda x: x[0], reverse=True)
        text_list = list(text)
        for start, end, replacement in replacements:
            text_list[start:end] = list(replacement)
        text = "".join(text_list)

    except ImportError:
        pass  # spaCy not available — return financial-redacted text only

    return text, entity_map


# ── Main entry point ───────────────────────────────────────────────────────────

def preprocess_transcript(raw_text: str, anonymize: bool = False) -> dict:
    """
    Full preprocessing pipeline. Called by app.py before the Claude API call.

    Returns a structured dict:
    {
        "normalized_text": str,          # cleaned transcript Claude will reason over
        "metadata": {
            "title": str | None,
            "date": str | None,
            "attendees_raw": str | None,
        },
        "explicit_commitments": [        # list of commitment-signal sentences
            {"text": str, "type": "commitment"},
            ...
        ],
        "decision_signals": [            # list of decision-signal sentences
            {"text": str, "type": "decision_signal"},
            ...
        ],
        "questions": [str, ...],         # list of question sentences
        "speaker_map": {str: str},       # original → normalized speaker labels
        "quality": {
            "flagged": bool,
            "reason": str | None,
            "word_count": int,
            "speaker_count": int,
        },
    }
    """
    # Step 1: strip timestamps
    text = strip_timestamps(raw_text)

    # Step 2: normalize speakers
    text, speaker_map = normalize_speakers(text)

    # Step 3: metadata from raw header (before normalization artifacts)
    metadata = extract_metadata(raw_text)

    # Step 4: anonymization (optional — runs before Claude sees anything)
    entity_map: dict = {}
    if anonymize:
        text, entity_map = anonymize_transcript(text)

    # Step 5: extract signals from normalized (and optionally anonymized) text
    explicit_commitments = detect_explicit_commitments(text)
    decision_signals = detect_decision_signals(text)
    questions = extract_questions(text)

    # Step 6: quality gate
    wc = len(text.split())
    speaker_count = count_speakers(raw_text)  # use raw for speaker count
    quality = score_quality(text, wc, speaker_count)

    return {
        "normalized_text": text.strip(),
        "metadata": metadata,
        "explicit_commitments": explicit_commitments,
        "decision_signals": decision_signals,
        "questions": questions,
        "speaker_map": speaker_map,
        "quality": quality,
        "entity_map": entity_map,  # populated only when anonymize=True
        "anonymized": anonymize,
    }
