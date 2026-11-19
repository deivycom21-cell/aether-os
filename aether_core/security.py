"""Data firewall: secret detection (regex + Shannon entropy), reversible in-memory vault,
prompt-injection detection and filesystem security scans.

Nothing in this module ever writes a raw secret to disk: the vault lives in process RAM only.
"""

from __future__ import annotations

import base64
import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MASK_PREFIX = "<<AETHER_MASKED_SECRET_"
MASK_SUFFIX = ">>"
MASK_PATTERN = re.compile(r"<<AETHER_MASKED_SECRET_(\d+)>>")

ENTROPY_THRESHOLD = 4.5


@dataclass(frozen=True, slots=True)
class SecretRule:
    name: str
    pattern: re.Pattern[str]
    severity: str = "critical"
    group: int = 0  # capture group holding the secret value


def _rx(p: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(p, flags)


# Order matters: more specific rules first (Anthropic before OpenAI's generic ``sk-``).
SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule(
        "private_key_block",
        _rx(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    SecretRule("anthropic_api_key", _rx(r"\bsk-ant-(?:api|admin|oat)?\d{0,2}-?[A-Za-z0-9_\-]{20,}")),
    SecretRule("openai_api_key", _rx(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    SecretRule("aws_access_key_id", _rx(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    SecretRule(
        "aws_secret_access_key",
        _rx(r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})\b"),
        group=1,
    ),
    SecretRule("github_token", _rx(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    SecretRule("github_fine_grained_pat", _rx(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b")),
    SecretRule("stripe_key", _rx(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    SecretRule("slack_token", _rx(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b")),
    SecretRule("google_api_key", _rx(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    SecretRule("jwt", _rx(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), "high"),
    SecretRule(
        "connection_string_password",
        _rx(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/]+:([^@\s/]{4,})@"),
        "high",
        group=1,
    ),
    SecretRule(
        "generic_secret_assignment",
        _rx(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|private[_-]?key)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"
        ),
        "high",
        group=1,
    ),
)

_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
_PLACEHOLDER_WORDS = re.compile(
    r"(?i)(example|placeholder|your[_-]?|xxxx|changeme|dummy|sample|test_key|redacted|\*{4,}|<.*>)"
)


def shannon_entropy(data: str) -> float:
    """Shannon entropy in bits per character."""
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_random(token: str) -> bool:
    has_digit = any(ch.isdigit() for ch in token)
    has_upper = any(ch.isupper() for ch in token)
    has_lower = any(ch.islower() for ch in token)
    classes = sum([has_digit, has_upper, has_lower])
    if classes < 2 or not has_digit and len(token) < 32:
        return False
    if "/" in token and token.count("/") > 2 and not token.endswith("="):
        return False  # very likely a path
    if re.fullmatch(r"[a-z_]+(?:_[a-z0-9]+)+", token):
        return False  # snake_case identifier
    return True


@dataclass(slots=True)
class SecretFinding:
    rule: str
    severity: str
    start: int
    end: int
    line: int
    preview: str
    entropy: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "preview": self.preview,
            "entropy": round(self.entropy, 3),
        }


def _preview(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{'*' * 6}…{secret[-2:]}"


def detect_secrets(text: str, entropy_threshold: float = ENTROPY_THRESHOLD) -> list[SecretFinding]:
    """Find secrets in ``text``. Overlapping matches are resolved in favour of the earliest rule."""
    findings: list[SecretFinding] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < e and b > s for s, e in taken)

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    for rule in SECRET_RULES:
        for m in rule.pattern.finditer(text):
            start, end = m.span(rule.group)
            if start < 0 or overlaps(start, end):
                continue
            value = m.group(rule.group)
            if MASK_PATTERN.search(value) or _PLACEHOLDER_WORDS.search(value):
                continue
            if rule.name == "generic_secret_assignment" and shannon_entropy(value) < 3.0:
                continue
            taken.append((start, end))
            findings.append(
                SecretFinding(rule.name, rule.severity, start, end, line_of(start), _preview(value),
                              shannon_entropy(value))
            )

    for m in _ENTROPY_CANDIDATE.finditer(text):
        start, end = m.span()
        token = m.group(0)
        if overlaps(start, end) or MASK_PATTERN.search(token):
            continue
        ent = shannon_entropy(token)
        if ent > entropy_threshold and _looks_random(token) and not _PLACEHOLDER_WORDS.search(token):
            taken.append((start, end))
            findings.append(
                SecretFinding("high_entropy_string", "medium", start, end, line_of(start), _preview(token), ent)
            )
    findings.sort(key=lambda f: f.start)
    return findings


@dataclass(slots=True)
class MaskResult:
    text: str
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def masked_count(self) -> int:
        return len(self.findings)


class SecretVault:
    """Reversible secret masking. Mappings live only in RAM and are never persisted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_token: dict[str, str] = {}
        self._by_secret: dict[str, str] = {}
        self._counter = 0

    def __len__(self) -> int:
        return len(self._by_token)

    def _token_for(self, secret: str) -> str:
        existing = self._by_secret.get(secret)
        if existing:
            return existing
        self._counter += 1
        token = f"{MASK_PREFIX}{self._counter}{MASK_SUFFIX}"
        self._by_token[token] = secret
        self._by_secret[secret] = token
        return token

    def mask(self, text: str, entropy_threshold: float = ENTROPY_THRESHOLD) -> MaskResult:
        findings = detect_secrets(text, entropy_threshold)
        if not findings:
            return MaskResult(text, [])
        out: list[str] = []
        cursor = 0
        with self._lock:
            for f in findings:
                out.append(text[cursor : f.start])
                out.append(self._token_for(text[f.start : f.end]))
                cursor = f.end
        out.append(text[cursor:])
        return MaskResult("".join(out), findings)

    def unmask(self, text: str) -> str:
        with self._lock:
            return MASK_PATTERN.sub(lambda m: self._by_token.get(m.group(0), m.group(0)), text)

    def mask_obj(self, obj: Any) -> tuple[Any, list[SecretFinding]]:
        """Recursively mask every string inside a JSON-like structure."""
        collected: list[SecretFinding] = []

        def walk(o: Any) -> Any:
            if isinstance(o, str):
                res = self.mask(o)
                collected.extend(res.findings)
                return res.text
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(v) for v in o]
            if isinstance(o, tuple):
                return tuple(walk(v) for v in o)
            return o

        return walk(obj), collected

    def clear(self) -> None:
        with self._lock:
            self._by_token.clear()
            self._by_secret.clear()


# ---------------------------------------------------------------------------- prompt injection


@dataclass(frozen=True, slots=True)
class InjectionRule:
    name: str
    pattern: re.Pattern[str]
    weight: float


INJECTION_RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        "override_instructions",
        _rx(r"(?i)\b(ignore|disregard|forget|override)\b[^\n]{0,30}\b(all |any |the )?(previous|prior|above|earlier|preceding|system)\b[^\n]{0,20}\b(instructions?|prompts?|rules|directives|context)"),
        0.9,
    ),
    InjectionRule("new_instructions", _rx(r"(?i)\b(new|updated|real) (system )?instructions?\s*[:\-]"), 0.6),
    InjectionRule("system_prompt_probe", _rx(r"(?i)\b(reveal|print|show|output|repeat|leak)\b[^\n]{0,30}\bsystem prompt"), 0.8),
    InjectionRule("system_prompt_mention", _rx(r"(?i)\bsystem prompt\b"), 0.3),
    InjectionRule("role_hijack", _rx(r"(?i)\byou are now\b|\bfrom now on,? you (are|will|must)\b|\bact as (an? )?(unrestricted|jailbroken|DAN)\b"), 0.7),
    InjectionRule("chat_template_tokens", _rx(r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[INST\]|<<SYS>>|\bBEGIN SYSTEM\b|\n\s*(Human|Assistant|System):\s"), 0.7),
    InjectionRule("secrecy_directive", _rx(r"(?i)\b(do not|don't|never) (tell|inform|mention|reveal)[^\n]{0,20}\b(the )?(user|human|developer)"), 0.7),
    InjectionRule("exfiltration", _rx(r"(?i)\b(exfiltrate|send|upload|post)\b[^\n]{0,40}\b(secrets?|api keys?|credentials|tokens?|\.env|ssh keys?)\b[^\n]{0,40}\b(to|https?://)"), 0.9),
    InjectionRule("remote_shell_pipe", _rx(r"(?i)\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]{0,120}\|\s*(sh|bash|zsh|iex|powershell|python)\b"), 0.8),
    InjectionRule("tool_abuse", _rx(r"(?i)\b(run|execute)\b[^\n]{0,20}\b(rm -rf|del /f|format c:|mkfs|:\(\)\{)"), 0.8),
    InjectionRule("agent_targeting", _rx(r"(?i)\b(AI|LLM|language model|assistant|agent|claude|copilot|cursor)\b[^\n]{0,30}\b(must|should|needs? to)\b[^\n]{0,30}\b(ignore|execute|run|delete|send)\b"), 0.6),
)

_HIDDEN_CHARS = {
    "​": "ZERO WIDTH SPACE",
    "‌": "ZERO WIDTH NON-JOINER",
    "‍": "ZERO WIDTH JOINER",
    "⁠": "WORD JOINER",
    "﻿": "ZERO WIDTH NO-BREAK SPACE",
    "‪": "LRE", "‫": "RLE", "‬": "PDF", "‭": "LRO", "‮": "RLO",
    "⁦": "LRI", "⁧": "RLI", "⁨": "FSI", "⁩": "PDI",
}
_COMMENT_RX = re.compile(r"<!--([\s\S]*?)-->|/\*([\s\S]*?)\*/|(?:#|//)\s?(.*)$", re.M)
_B64_RX = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


@dataclass(slots=True)
class InjectionFinding:
    rule: str
    line: int
    excerpt: str
    weight: float
    in_comment: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "line": self.line,
            "excerpt": self.excerpt,
            "weight": self.weight,
            "in_comment": self.in_comment,
        }


@dataclass(slots=True)
class InjectionReport:
    findings: list[InjectionFinding]
    score: float

    @property
    def is_suspicious(self) -> bool:
        return self.score >= 0.6

    @property
    def severity(self) -> str:
        if self.score >= 1.5:
            return "critical"
        if self.score >= 0.9:
            return "high"
        if self.score >= 0.6:
            return "medium"
        return "low" if self.findings else "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "severity": self.severity,
            "suspicious": self.is_suspicious,
            "findings": [f.to_dict() for f in self.findings],
        }


class PromptInjectionDetector:
    """Heuristic detector for instructions aimed at AI agents hidden in external content."""

    def __init__(self, rules: Iterable[InjectionRule] = INJECTION_RULES) -> None:
        self.rules = tuple(rules)

    def analyze(self, text: str) -> InjectionReport:
        findings: list[InjectionFinding] = []
        comment_spans = [m.span() for m in _COMMENT_RX.finditer(text)]

        def in_comment(pos: int) -> bool:
            return any(s <= pos < e for s, e in comment_spans)

        def line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        seen: set[tuple[str, int]] = set()
        for rule in self.rules:
            for m in rule.pattern.finditer(text):
                ln = line_of(m.start())
                if (rule.name, ln) in seen:
                    continue
                seen.add((rule.name, ln))
                hidden = in_comment(m.start())
                weight = rule.weight * (1.25 if hidden else 1.0)
                excerpt = text[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ").strip()
                findings.append(InjectionFinding(rule.name, ln, excerpt[:160], round(weight, 3), hidden))

        hidden_chars = [(i, ch) for i, ch in enumerate(text) if ch in _HIDDEN_CHARS]
        if hidden_chars:
            first = hidden_chars[0][0]
            bidi = any(ch in "‪‫‬‭‮⁦⁧⁨⁩" for _, ch in hidden_chars)
            findings.append(
                InjectionFinding(
                    "hidden_unicode_bidi" if bidi else "hidden_unicode",
                    line_of(first),
                    ", ".join(sorted({_HIDDEN_CHARS[ch] for _, ch in hidden_chars}))[:160],
                    0.8 if bidi else 0.4,
                )
            )

        for m in _B64_RX.finditer(text):
            blob = m.group(0)
            try:
                decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if not decoded or sum(ch.isprintable() for ch in decoded) / len(decoded) < 0.9:
                continue
            inner = PromptInjectionDetector(self.rules).analyze_plain(decoded)
            if inner:
                findings.append(
                    InjectionFinding("base64_encoded_instructions", line_of(m.start()), decoded[:120], 0.9, in_comment(m.start()))
                )

        score = sum(f.weight for f in findings)
        return InjectionReport(findings, score)

    def analyze_plain(self, text: str) -> bool:
        """Cheap check without recursion (used for decoded payloads)."""
        norm = unicodedata.normalize("NFKC", text)
        return any(r.weight >= 0.6 and r.pattern.search(norm) for r in self.rules)


# ---------------------------------------------------------------------------- filesystem scan

SCAN_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".aether",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", "target",
    ".idea", ".vscode", "site-packages", ".cache", "coverage",
}
SCAN_MAX_BYTES = 1_000_000
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3", ".wav",
    ".db", ".sqlite", ".sqlite3", ".pyc", ".class", ".jar", ".lock",
}


def iter_scannable_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SCAN_SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix.lower() not in _BINARY_EXT:
                try:
                    if entry.stat().st_size <= SCAN_MAX_BYTES:
                        yield entry
                except OSError:
                    continue


def read_text_safely(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace")


@dataclass(slots=True)
class ScanReport:
    root: str
    files_scanned: int
    secrets: list[dict[str, Any]]
    injections: list[dict[str, Any]]

    @property
    def risk_level(self) -> str:
        if any(s["severity"] == "critical" for s in self.secrets) or any(
            i["severity"] == "critical" for i in self.injections
        ):
            return "critical"
        if self.secrets or any(i["severity"] == "high" for i in self.injections):
            return "high"
        if self.injections:
            return "medium"
        return "clean"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "risk_level": self.risk_level,
            "secret_count": len(self.secrets),
            "injection_count": len(self.injections),
            "secrets": self.secrets,
            "injections": self.injections,
        }


class SecurityScanner:
    """Walks a directory tree looking for leaked secrets and prompt-injection payloads."""

    def __init__(
        self,
        detector: PromptInjectionDetector | None = None,
        on_secret: Callable[[str, SecretFinding], None] | None = None,
        on_injection: Callable[[str, InjectionReport], None] | None = None,
    ) -> None:
        self.detector = detector or PromptInjectionDetector()
        self.on_secret = on_secret
        self.on_injection = on_injection

    def scan_text(self, text: str, rel_path: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        secrets: list[dict[str, Any]] = []
        for f in detect_secrets(text):
            secrets.append({"file": rel_path, **f.to_dict()})
            if self.on_secret:
                self.on_secret(rel_path, f)
        report = self.detector.analyze(text)
        injection = None
        if report.is_suspicious:
            injection = {"file": rel_path, **report.to_dict()}
            if self.on_injection:
                self.on_injection(rel_path, report)
        return secrets, injection

    def scan_path(self, target: Path, base: Path | None = None) -> ScanReport:
        target = target.resolve()
        base = (base or (target if target.is_dir() else target.parent)).resolve()
        secrets: list[dict[str, Any]] = []
        injections: list[dict[str, Any]] = []
        count = 0
        for path in iter_scannable_files(target):
            text = read_text_safely(path)
            if text is None:
                continue
            count += 1
            try:
                rel = path.resolve().relative_to(base).as_posix()
            except ValueError:
                rel = path.as_posix()
            s, inj = self.scan_text(text, rel)
            secrets.extend(s)
            if inj:
                injections.append(inj)
        return ScanReport(str(target), count, secrets, injections)
