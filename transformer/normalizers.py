"""
normalizers.py — All value normalization lives here.
Every normalizer is a pure function: same input → same output, never raises.
Returns None on failure so callers can decide how to handle missing values.
"""

from __future__ import annotations
import re
import unicodedata
from typing import Optional


# ─── Phone → E.164 ──────────────────────────────────────────────────────────

# Strip everything that isn't a digit or leading +
_NON_DIGIT = re.compile(r"[^\d+]")

# Common country dial codes we infer when absent (default: India +91 for context, US +1 as fallback)
_DEFAULT_COUNTRY_CODE = "91"  # configurable


def normalize_phone(raw: str, default_country_code: str = _DEFAULT_COUNTRY_CODE) -> Optional[str]:
    """
    Normalize a phone number to E.164 format (+<country><number>).
    Returns None if the input is clearly not a phone number.
    """
    if not raw or not isinstance(raw, str):
        return None

    cleaned = _NON_DIGIT.sub("", raw.strip())

    # Already has a leading + (cleaned already retains it from _NON_DIGIT.sub)
    if raw.strip().startswith("+"):
        pass
    else:
        # Heuristic: 10-digit numbers without country code → prepend default
        if len(cleaned) == 10:
            cleaned = f"+{default_country_code}{cleaned}"
        elif len(cleaned) == 11 and cleaned.startswith("1"):
            cleaned = f"+{cleaned}"
        elif len(cleaned) > 7:
            cleaned = f"+{cleaned}"
        else:
            return None  # Too short to be a real phone

    # Sanity check: E.164 is 7–15 digits after the +
    digits_only = cleaned.lstrip("+")
    if not (7 <= len(digits_only) <= 15):
        return None

    return cleaned


# ─── Date → YYYY-MM ──────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12",
}

_YYYY_MM_RE = re.compile(r"^(\d{4})[/\-.](\d{1,2})$")
_MM_YYYY_RE = re.compile(r"^(\d{1,2})[/\-.](\d{4})$")
_YYYY_RE = re.compile(r"^(\d{4})$")
_MON_YYYY_RE = re.compile(r"^([A-Za-z]+)[,\s]+(\d{4})$")
_YYYY_MON_RE = re.compile(r"^(\d{4})[,\s]+([A-Za-z]+)$")


def normalize_date(raw: str) -> Optional[str]:
    """
    Parse a messy date string into YYYY-MM.
    Returns just 'YYYY' string if month is unknown.
    Returns None if unparseable.
    """
    if not raw or not isinstance(raw, str):
        return None

    s = raw.strip()
    if s.lower() in ("present", "current", "now", "ongoing", "—", "-", ""):
        return None  # Caller treats None end date as "current"

    m = _YYYY_MM_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}"

    m = _MM_YYYY_RE.match(s)
    if m:
        return f"{m.group(2)}-{m.group(1).zfill(2)}"

    m = _MON_YYYY_RE.match(s)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        if month:
            return f"{m.group(2)}-{month}"

    m = _YYYY_MON_RE.match(s)
    if m:
        month = _MONTH_MAP.get(m.group(2).lower())
        if month:
            return f"{m.group(1)}-{month}"

    m = _YYYY_RE.match(s)
    if m:
        return m.group(1)  # Year only

    return None


# ─── Skills → Canonical Names ─────────────────────────────────────────────────

# Map common aliases → canonical name
_SKILL_ALIASES: dict[str, str] = {
    # Python ecosystem
    "python3": "Python",
    "py": "Python",
    "python 3": "Python",
    # JavaScript
    "javascript": "JavaScript",
    "js": "JavaScript",
    "es6": "JavaScript",
    "ecmascript": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    # Web frameworks
    "reactjs": "React",
    "react.js": "React",
    "vuejs": "Vue.js",
    "vue": "Vue.js",
    "angularjs": "Angular",
    # Databases
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mongo": "MongoDB",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "mssql": "SQL Server",
    "ms sql": "SQL Server",
    # Cloud
    "aws": "AWS",
    "amazon web services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "google cloud platform": "GCP",
    "azure": "Azure",
    "microsoft azure": "Azure",
    # ML/AI
    "machine learning": "Machine Learning",
    "ml": "Machine Learning",
    "deep learning": "Deep Learning",
    "dl": "Deep Learning",
    "nlp": "NLP",
    "natural language processing": "NLP",
    "computer vision": "Computer Vision",
    "cv": "Computer Vision",
    # Tools
    "git": "Git",
    "github": "GitHub",
    "gitlab": "GitLab",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    # Languages
    "golang": "Go",
    "go lang": "Go",
    "c++": "C++",
    "cplusplus": "C++",
    "c#": "C#",
    "csharp": "C#",
    "ruby on rails": "Ruby on Rails",
    "ror": "Ruby on Rails",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    # Data
    "pandas": "Pandas",
    "numpy": "NumPy",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "tensorflow": "TensorFlow",
    "tf": "TensorFlow",
    "pytorch": "PyTorch",
    "torch": "PyTorch",
}


def normalize_skill(raw: str) -> str:
    """
    Return canonical skill name. If no alias found, title-case the input.
    Never returns empty string.
    """
    if not raw or not isinstance(raw, str):
        return raw or ""

    stripped = raw.strip()
    lower = stripped.lower()

    if lower in _SKILL_ALIASES:
        return _SKILL_ALIASES[lower]

    # Title-case as fallback
    return stripped.title()


def normalize_skills(raw_list: list[str]) -> list[str]:
    """Normalize a list of skill strings, deduplicating by canonical name."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_list:
        canonical = normalize_skill(raw)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


# ─── Location → ISO-3166 alpha-2 country ─────────────────────────────────────

_COUNTRY_ALIASES: dict[str, str] = {
    "united states": "US",
    "usa": "US",
    "us": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "india": "IN",
    "canada": "CA",
    "australia": "AU",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "singapore": "SG",
    "netherlands": "NL",
    "holland": "NL",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "switzerland": "CH",
    "austria": "AT",
    "japan": "JP",
    "china": "CN",
    "south korea": "KR",
    "korea": "KR",
    "brazil": "BR",
    "mexico": "MX",
    "spain": "ES",
    "italy": "IT",
    "portugal": "PT",
    "ireland": "IE",
    "new zealand": "NZ",
    "israel": "IL",
    "uae": "AE",
    "united arab emirates": "AE",
    "remote": None,  # Remote is not a country
}


def normalize_country(raw: str) -> Optional[str]:
    """Return ISO-3166 alpha-2 code for a country string, or None if unknown."""
    if not raw or not isinstance(raw, str):
        return None
    lower = raw.strip().lower()
    if lower in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[lower]
    # If already looks like alpha-2
    if re.match(r"^[A-Za-z]{2}$", raw.strip()):
        return raw.strip().upper()
    return None


def normalize_email(raw: str) -> Optional[str]:
    """Basic email normalization: lowercase, strip whitespace."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    if "@" in cleaned and "." in cleaned.split("@")[-1]:
        return cleaned
    return None


def normalize_name(raw: str) -> Optional[str]:
    """Normalize a person's name: strip, title-case, remove extra spaces."""
    if not raw or not isinstance(raw, str):
        return None
    # Remove accents for comparison but keep original characters
    name = " ".join(raw.strip().split())  # collapse whitespace
    if len(name) < 2:
        return None
    return name.title()
