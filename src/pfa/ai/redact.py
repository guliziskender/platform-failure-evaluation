"""Secret redaction. Runs on all evidence before classification, rules, output or any AI call.

Collectors already skip the obvious places secrets live (env var values, Secret objects). This
catches the rest: secrets that apps print in their logs, pass as command-line args, or that turn
up in event and error messages. Each match becomes a typed placeholder such as
`<REDACTED:jwt>`, so a reader (or a model) still knows something was there and what kind.

When in doubt it redacts: a false positive costs a little context, a miss leaks a secret. The
one false positive it must avoid is mangling Kubernetes messages the rules depend on, which
`tests/test_redact.py` checks against real captured evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from pfa.models import Evidence

PLACEHOLDER = "<REDACTED:{kind}>"
_ALREADY_REDACTED = "<REDACTED:"

# Key names whose value is a secret: password=..., "api_key": "...", PGPASSWORD=..., --token x
_SECRET_WORD = (
    r"passw(?:or)?d|pwd|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credentials?|auth[_-]?key|session[_-]?id|cookie"
)
_SECRET_KEY = rf"[A-Za-z0-9_.-]*(?:{_SECRET_WORD})[A-Za-z0-9_.-]*"
# Values that say nothing secret; redacting them would only remove context.
# "not set" and "missing" are what apps print about a missing variable, which the rules read.
_HARMLESS_VALUES = {
    *("", "true", "false", "null", "none", "nil", "yes", "no", "required", "missing"),
    *("not set", "unset", "empty", "undefined", "not found", "invalid"),
}
# Not a value itself: references ($VAR, ${VAR}, Kubernetes $(VAR), {{ template }}, <your-key>),
# masks (***, xxx), and YAML's block markers (| or >), whose value is on the next lines.
_REFERENCE = re.compile(r"^(?:\$\{?\w+\}?|\$\(\w+\)|\{\{.*\}\}|<[^>]*>|\*+|x+|[|>][-+0-9]*)$", re.I)

_Replacer = Callable[[re.Match[str]], str]


def _placeholder(kind: str) -> str:
    return PLACEHOLDER.format(kind=kind)


def _key_kind(key: str) -> str:
    key = key.lower()
    if "pass" in key or "pwd" in key:
        return "password"
    if "token" in key:
        return "token"
    if "key" in key:
        return "api_key"
    if "cred" in key:
        return "credential"
    if "session" in key or "cookie" in key:
        return "session"
    return "secret"


def _is_secret_value(value: str) -> bool:
    return (
        value.lower() not in _HARMLESS_VALUES
        and not value.startswith(_ALREADY_REDACTED)
        and not _REFERENCE.match(value)
    )


def _whole(kind: str) -> _Replacer:
    return lambda m: _placeholder(kind)


def _keep_prefix(kind: str) -> _Replacer:
    """Keep group 1 (e.g. "Bearer "), replace group 2."""
    return lambda m: m.group(1) + _placeholder(kind)


_QUOTES = {"edq": '\\"', "dq": '"', "sq": "'"}


def _key_value(m: re.Match[str]) -> str:
    quote = next((q for group, q in _QUOTES.items() if m.group(group) is not None), "")
    if quote:
        value, trailing = m.group(next(g for g, q in _QUOTES.items() if q == quote)), ""
    else:
        bare = m.group("bare")
        value = bare.rstrip()
        trailing = bare[len(value) :]
    if not _is_secret_value(value):
        return m.group(0)
    key = m.group("key")
    return f"{key}{m.group('sep')}{quote}{_placeholder(_key_kind(key))}{quote}{trailing}"


def _block_value(m: re.Match[str]) -> str:
    body = m.group("body")
    if not _is_secret_value(body.strip()):
        return m.group(0)
    indent = body[: len(body) - len(body.lstrip(" \t"))]
    newline = "\n" if body.endswith("\n") else ""
    return f"{m.group('head')}{indent}{_placeholder(_key_kind(m.group('key')))}{newline}"


# A quoted value: \"x\" (JSON escaped inside a log line's string), "x" (may contain ' and \"),
# or 'x'. The escaped form comes first: it starts with a backslash, the others with a quote.
_QUOTED = (
    r'\\"(?P<edq>(?:[^"\\\n]|\\[^"\n])*)\\"'
    r'|"(?P<dq>(?:[^"\\\n]|\\.)*)"'
    r"|'(?P<sq>[^'\n]*)'"
)
# A comma, & or ; continues a value unless the next key=value starts there: so `pw=ab;cd` loses
# all of `ab;cd`, while `user=a&token=b` and `id=sa;Password=x` keep their other pairs.
_NEXT_PAIR = r"(?![ \t]*[\w.-]+[ \t]*[:=])"


def _key_value_pattern(sep: str, bare: str) -> re.Pattern[str]:
    # Separators stay on one line: a key at the end of a line has no value, and must not take
    # the next line's first word.
    return re.compile(
        rf"(?<![\w.-])(?P<key>{_SECRET_KEY})(?P<sep>{sep})(?:{_QUOTED}|(?P<bare>{bare}))", re.I
    )


def _auth_header(m: re.Match[str]) -> str:
    scheme, value = (m.group("scheme") or "").lower(), m.group(3)
    # Without a scheme, only a token-like value counts: registries write
    # "may require authorization: server message: ...", which the rules read.
    if not _is_secret_value(value) or (not scheme and not re.search(r"\d", value)):
        return m.group(0)
    kind = "basic_auth" if scheme == "basic" else "bearer_token"
    return m.group(1) + _placeholder(kind)


def _flag_value(m: re.Match[str]) -> str:
    if not _is_secret_value(m.group("value")):
        return m.group(0)
    return m.group("flag") + m.group("space") + _placeholder(_key_kind(m.group("flag")))


# Order matters: whole structures (key blocks, URLs, JWTs) before the generic key=value pairs,
# so a JWT in `token=eyJ...` is reported as a jwt.
_PATTERNS: list[tuple[str, re.Pattern[str], _Replacer]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        _whole("private_key"),
    ),
    # The tail of a key block whose BEGIN line was cut off when the log was trimmed.
    (
        "private_key",
        re.compile(r"(?:^[A-Za-z0-9+/=]{16,}\r?\n)+-----END [A-Z ]*PRIVATE KEY-----", re.M),
        _whole("private_key"),
    ),
    # postgres://user:pass@host -> postgres://user:<REDACTED:password>@host (host stays: useful)
    (
        "password",
        # Scheme length is bounded: unbounded, long hyphenated text made this quadratic.
        re.compile(r"(\b[a-z][a-z0-9+.-]{0,30}://[^\s:/@<]*:)([^\s@/]+)(?=@)", re.I),
        _keep_prefix("password"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        _whole("jwt"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"),
        _whole("aws_access_key"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        _whole("github_token"),
    ),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), _whole("slack_token")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), _whole("google_api_key")),
    (
        "api_key",
        # Stripe sk_live_/rk_test_..., Anthropic sk-ant-..., OpenAI sk-proj-... and legacy sk-<48>.
        # Not a bare `sk-` prefix: pod names like sk-learn-api-7d9f8 would match.
        re.compile(
            r"\b(?:[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|sk-(?:ant|proj)-[A-Za-z0-9_-]{20,}"
            r"|sk-[A-Za-z0-9]{32,})"
        ),
        _whole("api_key"),
    ),
    # An Authorization header's value is always a credential, whatever it looks like.
    (
        "auth_header",
        re.compile(
            r"(\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?"
            r"(?:(?P<scheme>basic|bearer|token|digest)\s+)?)([^\s\"',;]{6,})",
            re.I,
        ),
        _auth_header,
    ),
    # Outside a header, "Bearer x" / "Basic x" only count when x looks like a token (has a digit,
    # or base64 padding), so prose like "basic authentication" or "Bearer realm=" is left alone.
    (
        "bearer_token",
        re.compile(r"(\bbearer\s+)((?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,})", re.I),
        _keep_prefix("bearer_token"),
    ),
    (
        "basic_auth",
        re.compile(r"(\bbasic\s+)((?=[A-Za-z0-9+/]*[\d=])[A-Za-z0-9+/]{8,}={0,2})", re.I),
        _keep_prefix("basic_auth"),
    ),
    # client_secret: |  followed by indented lines (a YAML block value): the lines go.
    (
        "block_value",
        re.compile(
            rf"^(?P<head>(?P<indent>[ \t]*)(?P<key>{_SECRET_KEY})[\"']?:[ \t]*[|>][-+0-9]*[ \t]*\n)"
            r"(?P<body>(?:(?P=indent)[ \t]+\S[^\n]*(?:\n|$))+)",
            re.I | re.M,
        ),
        _block_value,
    ),
    # password=x, DB_PASSWORD=x, ?token=x&a=b, Password=x; (connection strings). The value ends
    # at whitespace, so `password=x user=y` keeps the user.
    (
        "key_value",
        _key_value_pattern(
            r"(?:\\?[\"'])?[ \t]*=[ \t]*",
            rf"[^\s\"',&;](?:[^\s\",&;]|[,&;](?=[^\s,&;]){_NEXT_PAIR})*",
        ),
        _key_value,
    ),
    # password: x, "api_key": "x", \"password\":\"x\". Unquoted, the value runs to the end of
    # the line: `password: correct horse battery` is one value. A space before ":" does not
    # count: kubelet writes `volume "db-password" : secret "db" not found`, which rules read.
    (
        "key_value",
        _key_value_pattern(
            r"(?:\\?[\"'])?:[ \t]*",
            rf"[^\s\"',}}](?:[^\n\",}}]|,{_NEXT_PAIR})*",
        ),
        _key_value,
    ),
    # --password hunter2 (flag and value separated by a space)
    (
        "flag",
        re.compile(
            rf"(?<![\w-])(?P<flag>--?{_SECRET_KEY})(?P<space>\s+)"
            r"(?P<value>[^\s\"'-][^\s\"']*)",
            re.I,
        ),
        _flag_value,
    ),
]


_FLAG_ONLY = re.compile(rf"--?{_SECRET_KEY}", re.I)


def redact_text(text: str) -> tuple[str, Counter[str]]:
    """Replace every secret in `text` with a placeholder. Returns the text and counts by kind."""
    counts: Counter[str] = Counter()
    for _, pattern, replace in _PATTERNS:

        def counted(m: re.Match[str], replace: _Replacer = replace) -> str:
            new = replace(m)
            if new != m.group(0):
                kind = re.search(r"<REDACTED:(\w+)>", new)
                counts[kind.group(1) if kind else "secret"] += 1
            return new

        text = pattern.sub(counted, text)
    return text, counts


def redact_data(data: Any) -> tuple[Any, Counter[str]]:
    """Redact every string inside nested dicts and lists. Keys are left alone.

    Also handles flags split across list items, as in a container's args:
    ["--password", "hunter2"] -> ["--password", "<REDACTED:password>"].
    """
    counts: Counter[str] = Counter()

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            new, found = redact_text(value)
            counts.update(found)
            return new
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return _walk_list(value, walk, counts)
        return value

    return walk(data), counts


def _walk_list(items: Iterable[Any], walk: Callable[[Any], Any], counts: Counter[str]) -> list:
    out: list[Any] = []
    secret_follows = False
    for item in items:
        if (
            secret_follows
            and isinstance(item, str)
            and not item.startswith("-")
            and _is_secret_value(item)
        ):
            kind = _key_kind(out[-1])
            out.append(_placeholder(kind))
            counts[kind] += 1
            secret_follows = False
            continue
        secret_follows = isinstance(item, str) and bool(_FLAG_ONLY.fullmatch(item))
        out.append(walk(item))
    return out


def redact_evidence(evidence: Evidence) -> Evidence:
    """A redacted copy of one Evidence. Idempotent, so it is safe to run twice."""
    summary, counts = redact_data(evidence.summary)
    raw_excerpt, error = evidence.raw_excerpt, evidence.error
    if raw_excerpt:
        raw_excerpt, found = redact_text(raw_excerpt)
        counts.update(found)
    if error:
        error, found = redact_text(error)
        counts.update(found)
    total = Counter(evidence.redactions) + counts
    return evidence.model_copy(
        update={
            "summary": summary,
            "raw_excerpt": raw_excerpt,
            "error": error,
            "redacted": True,
            "redactions": dict(sorted(total.items())),
        }
    )


def redact_all(evidence: list[Evidence]) -> list[Evidence]:
    return [redact_evidence(e) for e in evidence]


def require_redacted(evidence: list[Evidence]) -> None:
    """Guard for code that sends evidence off the machine (the AI layer)."""
    unredacted = [e.source for e in evidence if not e.redacted]
    if unredacted:
        raise ValueError(f"evidence not redacted: {', '.join(unredacted)}")
