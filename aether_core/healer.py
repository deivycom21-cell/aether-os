"""Auto-fix loop: run a test/build command, turn its failure into a compact repair context, and
optionally (BYOK) let an LLM repair it iteratively under a safety snapshot.

``prepare`` (always local, $0):
  run command -> parse tracebacks (Python, pytest, Node/TS, tsc, Go, Rust) -> map frames to indexed files
  and enclosing symbols -> scars for those files -> ``query_smart_context`` on the error ->
  ``.aether/last_heal_context.json`` (secret-masked).

``execute`` (``aether heal --execute``):
  snapshot -> up to 3 attempts { model proposes complete files -> secret check -> policy check ->
  write -> re-run } -> success records a scar, failure rolls back to the snapshot.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aether_core.security import detect_secrets

if TYPE_CHECKING:
    from aether_core.kernel import AetherKernel

HEAL_CONTEXT_FILENAME = "last_heal_context.json"
DEFAULT_TIMEOUT_S = 600
MAX_ATTEMPTS = 3
OUTPUT_TAIL_LINES = 60
_THIRD_PARTY = re.compile(r"(site-packages|dist-packages|node_modules|[\\/]lib[\\/]python\d|<frozen|<string>|internal[\\/]|\.cargo[\\/]registry)", re.I)
FRAME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("python", re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[\w<>.]+))?')),
    ("pytest", re.compile(r"^(?P<path>[^\s:\"'][^:\n\"']*?\.pyi?):(?P<line>\d+):", re.M)),
    ("tsc", re.compile(r"(?P<path>[^\s()\"']+\.(?:ts|tsx|mts|cts))\((?P<line>\d+),(?P<col>\d+)\)")),
    ("node", re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s()\"':]+\.(?:[cm]?[jt]sx?|vue|svelte)):(?P<line>\d+):(?P<col>\d+)")),
    ("rust", re.compile(r"--> (?P<path>[^\s:]+\.rs):(?P<line>\d+):(?P<col>\d+)")),
    ("go", re.compile(r"(?P<path>[^\s:\"']+\.go):(?P<line>\d+)(?::\d+)?")),
)
ERROR_LINE = re.compile(
    r"^(?:E\s+|FAILED\s+\S+\s+-\s+)?(?P<etype>(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Failure|Exit)|"
    r"AssertionError|error(?:\[E\d+\])?|error TS\d+|panic)\b:?\s*(?P<msg>.*)$",
    re.M,
)
FILE_BLOCK = re.compile(r"<<<FILE\s+([^\n>]+?)\s*>>>\n(.*?)\n<<<END>>>", re.S)


class HealError(RuntimeError):
    """User-facing healer failure (bad command, policy refusal, missing credentials)."""


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).replace("\r\n", "\n")


def split_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        argv = [str(a) for a in command]
    else:
        parts = shlex.split(command, posix=os.name != "nt")
        argv = [x[1:-1] if len(x) >= 2 and x[0] == x[-1] and x[0] in "\"'" else x for x in parts]
    if not argv:
        raise HealError("empty command")
    return argv


def resolve_argv(argv: list[str]) -> list[str]:
    """Use the current interpreter for ``python``; resolve ``npm``/``npx`` shims (``.cmd`` on Windows)."""
    head = argv[0]
    if head.lower() in {"python", "python3", "py"}:
        return [sys.executable, *argv[1:]]
    if head.lower() == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    found = shutil.which(head)
    if found is None:
        raise HealError(f"command not found on PATH: {head}")
    return [found, *argv[1:]]


def run_command(argv: list[str], cwd: Path, timeout: float = DEFAULT_TIMEOUT_S) -> CommandResult:
    resolved = resolve_argv(argv)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1", FORCE_COLOR="0", PY_COLORS="0")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(resolved, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return CommandResult(argv, -1, out, err + f"\n[aether] timed out after {timeout:.0f}s",
                             (time.perf_counter() - t0) * 1000, True)
    except OSError as exc:
        raise HealError(f"cannot run {argv[0]}: {exc}") from exc
    return CommandResult(argv, proc.returncode, proc.stdout or "", proc.stderr or "", (time.perf_counter() - t0) * 1000)


def _to_rel(raw: str, root: Path) -> str | None:
    raw = raw.strip().strip("\"'")
    if not raw or _THIRD_PARTY.search(raw):
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    p = Path(raw)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
    rel = PurePosixPath(raw.replace("\\", "/"))
    parts = [x for x in rel.parts if x not in (".",)]
    if ".." in parts:
        return None
    return "/".join(parts)


def parse_failure(output: str, root: Path) -> dict[str, Any]:
    """Extract (file, line) frames and error lines from tool output. Frames are ordered most relevant first."""
    frames: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for kind, rx in FRAME_PATTERNS:
        matches = list(rx.finditer(output))
        if kind == "python":
            matches.reverse()  # innermost frame is printed last in Python tracebacks
        for m in matches:
            rel = _to_rel(m.group("path"), root)
            if not rel:
                continue
            line = int(m.group("line"))
            if (rel, line) in seen:
                continue
            seen.add((rel, line))
            frames.append({"file": rel, "line": line, "source": kind,
                           "function": m.groupdict().get("func") or ""})
    errors: list[str] = []
    for m in ERROR_LINE.finditer(output):
        text = f"{m.group('etype')}: {m.group('msg').strip()}".strip().rstrip(":")
        if text not in errors:
            errors.append(text[:300])
    return {"frames": frames, "errors": errors[:12]}


class Healer:
    def __init__(self, kernel: AetherKernel) -> None:
        self.k = kernel

    @property
    def context_path(self) -> Path:
        return self.k.aether_dir / HEAL_CONTEXT_FILENAME

    def _check_command(self, argv: list[str]) -> None:
        if not self.k.policy.command_allowed(argv):
            self.k.bus.publish("POLICY_BLOCKED", {"action": "run_command", "command": " ".join(argv)[:200]})
            raise HealError(
                f"command not allowed by .aether/policy.json: {' '.join(argv)!r}. "
                "Add its prefix to 'allowed_commands' (aether policy --allow-cmd \"...\")."
            )

    def _snippet(self, rel: str, line: int, radius: int = 4) -> tuple[str, list[int]]:
        path = self.k.root / Path(*PurePosixPath(rel).parts)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "", [line, line]
        lo, hi = max(1, line - radius), min(len(lines), line + radius)
        width = len(str(hi))
        text = "\n".join(f"{'>' if i == line else ' '}{i:>{width}} | {lines[i - 1]}" for i in range(lo, hi + 1))
        return text, [lo, hi]

    def _enclosing_symbol(self, rel: str, line: int) -> str:
        f = self.k.db.file_by_path(rel)
        if f is None or f.path != rel:
            return ""
        best = None
        for n in self.k.db.nodes_for_file(f.id):
            if n.kind in {"function", "method", "class"} and n.start_line <= line <= n.end_line:
                if best is None or (n.end_line - n.start_line) < (best.end_line - best.start_line):
                    best = n
        return best.qualname if best else ""

    def build_context(self, output: str, command: str = "", returncode: int | None = None,
                      duration_ms: float = 0.0, max_tokens: int = 1500) -> dict[str, Any]:
        parsed = parse_failure(output, self.k.root)
        frames_out: list[dict[str, Any]] = []
        files: list[str] = []
        for fr in parsed["frames"]:
            if not self.k.policy.can_read(fr["file"]):
                continue
            if not (self.k.root / Path(*PurePosixPath(fr["file"]).parts)).is_file():
                continue
            snippet, span = self._snippet(fr["file"], fr["line"])
            frames_out.append({**fr, "symbol": self._enclosing_symbol(fr["file"], fr["line"]) or fr["function"],
                               "snippet": snippet, "lines": span})
            if fr["file"] not in files:
                files.append(fr["file"])
            if len(frames_out) >= 8:
                break
        scars: list[dict[str, Any]] = []
        for rel in files[:5]:
            for s in self.k.check_scars(rel):
                scars.append({"file": s["file_path"], "severity": s["severity"], "error": s["error_summary"],
                              "fix": s["fix_applied"], "root_cause": s["root_cause"]})
        query_terms = parsed["errors"][:2] + [fr["symbol"].rsplit(".", 1)[-1] for fr in frames_out[:3] if fr["symbol"]]
        query = " ".join(query_terms)[:400].strip()
        context: dict[str, Any] = {}
        if query:
            pkg = self.k.query_smart_context(query, max_tokens=max_tokens)
            context = {"results": pkg.get("results", [])[:6], "relations": pkg.get("relations", [])[:3],
                       "warnings": pkg.get("warnings", []), "metrics": pkg.get("metrics", {})}
        tail = "\n".join(output.strip().splitlines()[-OUTPUT_TAIL_LINES:])
        status = "passing" if returncode == 0 else "failing"
        instructions = (
            "The command passes; nothing to heal." if status == "passing" else
            "Fix the root cause of the first error. Start from the innermost project frame, respect the scars, "
            "keep public signatures stable unless the error requires otherwise, then re-run the command."
        )
        ctx = {
            "command": command,
            "status": status,
            "returncode": returncode,
            "duration_ms": round(duration_ms, 1),
            "errors": parsed["errors"],
            "frames": frames_out,
            "files": files,
            "scars": scars[:10],
            "context": context,
            "output_tail": tail,
            "instructions": instructions,
            "generated_at": time.time(),
        }
        masked, findings = self.k.vault.mask_obj(ctx)
        if findings:
            self.k.bus.publish("SECRET_BLOCKED", {"count": len(findings), "context": "heal_context"})
        return masked

    def write_context(self, ctx: dict[str, Any]) -> Path:
        path = self.context_path
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ctx, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def prepare(self, command: str | list[str] = "", output: str | None = None, timeout: float = DEFAULT_TIMEOUT_S,
                max_tokens: int = 1500) -> dict[str, Any]:
        """Run ``command`` (or analyse ``output``) and write the repair context. Never edits code."""
        if output is None:
            argv = split_command(command)
            self._check_command(argv)
            result = run_command(argv, self.k.root, timeout)
            ctx = self.build_context(result.output, " ".join(argv), result.returncode, result.duration_ms, max_tokens)
        else:
            cmd = command if isinstance(command, str) else " ".join(command)
            ctx = self.build_context(output, cmd, None if not cmd else 1, 0.0, max_tokens)
            if not cmd:
                ctx["status"] = "failing" if ctx["errors"] or ctx["frames"] else "unknown"
        path = self.write_context(ctx)
        ctx["context_file"] = path.relative_to(self.k.root).as_posix()
        keys = [f"{self.k.repo_name}:{f}" for f in ctx["files"]]
        self.k.bus.publish("HEAL_CONTEXT", {"status": ctx["status"], "errors": len(ctx["errors"]),
                                            "files": ctx["files"][:8], "command": ctx["command"][:160]}, keys)
        return ctx

    # ------------------------------------------------------------------ BYOK repair loop
    def _safe_target(self, rel: str) -> Path:
        rel_norm = PurePosixPath(rel.strip().replace("\\", "/"))
        if rel_norm.is_absolute() or ".." in rel_norm.parts or not rel_norm.parts or rel_norm.parts[0] in {".git", ".aether"}:
            raise HealError(f"refusing to write outside the project: {rel}")
        target = (self.k.root / Path(*rel_norm.parts)).resolve()
        if self.k.root.resolve() not in target.parents:
            raise HealError(f"refusing to write outside the project: {rel}")
        if not self.k.policy.can_write(rel_norm.as_posix()):
            self.k.bus.publish("POLICY_BLOCKED", {"action": "write", "file": rel_norm.as_posix()})
            raise HealError(f"{rel_norm.as_posix()} is read-only or denied by .aether/policy.json")
        return target

    def _repair_prompt(self, ctx: dict[str, Any], attempt: int, history: list[str]) -> str:
        blocks: list[str] = []
        for rel in ctx["files"][:4]:
            path = self.k.root / Path(*PurePosixPath(rel).parts)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            masked = self.k.vault.mask(content[:12000]).text
            blocks.append(f"### {rel}\n```\n{masked}\n```")
        brief = {k: ctx[k] for k in ("command", "errors", "frames", "scars") if k in ctx}
        prev = "\n".join(f"- attempt {i + 1}: {h}" for i, h in enumerate(history)) or "- none"
        return (
            f"A command fails in this repository. Attempt {attempt}/{MAX_ATTEMPTS}.\n\n"
            f"## Failure context (JSON)\n```json\n{json.dumps(brief, indent=1, ensure_ascii=False)[:9000]}\n```\n\n"
            f"## Output tail\n```\n{ctx.get('output_tail', '')[-4000:]}\n```\n\n"
            f"## Previous attempts\n{prev}\n\n## Implicated files\n" + "\n\n".join(blocks) +
            "\n\nReturn ONLY the complete corrected content of every file you change, one block per file:\n"
            "<<<FILE relative/path.ext>>>\n<full file content>\n<<<END>>>\n"
            "Do not change tests to make them pass unless the test itself is wrong. Keep "
            "<<AETHER_MASKED_SECRET_N>> placeholders exactly as they are."
        )

    def execute(self, command: str | list[str], max_attempts: int = MAX_ATTEMPTS, llm: Any = None,
                timeout: float = DEFAULT_TIMEOUT_S, keep_failed: bool = False) -> dict[str, Any]:
        argv = split_command(command)
        self._check_command(argv)
        if llm is None:
            from aether_swarm.llm import LLMClient

            llm = LLMClient()
        avail = llm.available()
        if not any(avail.values()):
            raise HealError("no LLM backend available: set ANTHROPIC_API_KEY (pip install 'aether-os[llm]') "
                            "or start Ollama. `aether heal` without --execute still writes the repair context.")
        first = run_command(argv, self.k.root, timeout)
        if first.ok:
            self.write_context(self.build_context(first.output, " ".join(argv), 0, first.duration_ms))
            return {"ok": True, "status": "already_passing", "attempts": [], "snapshot": None}
        snapshot = self.k.create_snapshot("auto: before heal")
        attempts: list[dict[str, Any]] = []
        history: list[str] = []
        result = first
        changed_total: list[str] = []
        for attempt in range(1, max(1, min(max_attempts, MAX_ATTEMPTS)) + 1):
            ctx = self.build_context(result.output, " ".join(argv), result.returncode, result.duration_ms)
            self.write_context(ctx)
            decision = _pick_model(llm, avail, ctx)
            self.k.bus.publish("HEAL_ATTEMPT", {"attempt": attempt, "model": decision.model, "step": "thinking"},
                               [f"{self.k.repo_name}:{f}" for f in ctx["files"]])
            try:
                resp = llm.complete(decision, "You are a senior engineer fixing a failing build. Follow the output format exactly.",
                                    self._repair_prompt(ctx, attempt, history))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user as an attempt result
                attempts.append({"attempt": attempt, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
                history.append(f"model call failed: {exc}")
                break
            blocks = [(m.group(1).strip(), m.group(2)) for m in FILE_BLOCK.finditer(resp.text)]
            if not blocks:
                attempts.append({"attempt": attempt, "ok": False, "error": "model returned no file blocks", "model": decision.model})
                history.append("no file blocks returned")
                continue
            written: list[str] = []
            try:
                for rel, content in blocks:
                    if detect_secrets(content):
                        raise HealError(f"generated content for {rel} contains secret-like material; refusing to write")
                    target = self._safe_target(rel)
                    final = self.k.vault.unmask(content)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".aether-tmp")
                    tmp.write_text(final if final.endswith("\n") else final + "\n", encoding="utf-8")
                    os.replace(tmp, target)
                    written.append(PurePosixPath(rel.replace("\\", "/")).as_posix())
            except HealError as exc:
                attempts.append({"attempt": attempt, "ok": False, "error": str(exc), "model": decision.model})
                history.append(str(exc))
                continue
            changed_total.extend(w for w in written if w not in changed_total)
            self.k.reindex_paths([self.k.root / Path(*PurePosixPath(w).parts) for w in written])
            result = run_command(argv, self.k.root, timeout)
            attempts.append({"attempt": attempt, "ok": result.ok, "files": written, "model": decision.model,
                             "returncode": result.returncode})
            self.k.bus.publish("HEAL_ATTEMPT", {"attempt": attempt, "model": decision.model, "ok": result.ok,
                                                "files": written}, [f"{self.k.repo_name}:{w}" for w in written])
            if result.ok:
                first_error = (ctx["errors"] or ["command failed"])[0]
                for rel in written:
                    self.k.record_scar(rel, f"Auto-healed failure: {first_error}"[:300],
                                       f"aether heal attempt {attempt} via {decision.model} rewrote {', '.join(written)}"[:300],
                                       "medium", root_cause=f"Command `{' '.join(argv)}` failed")
                ok_ctx = self.build_context(result.output, " ".join(argv), 0, result.duration_ms)
                self.write_context(ok_ctx)
                return {"ok": True, "status": "healed", "attempts": attempts, "snapshot": snapshot["id"],
                        "files_changed": changed_total}
            history.append(f"rewrote {', '.join(written)}; still failing: {(parse_failure(result.output, self.k.root)['errors'] or ['?'])[0]}")
        rolled_back = False
        if changed_total and not keep_failed:
            self.k.snapshots.rollback(snapshot["id"], prune_new=True)
            self.k.index(run_plugins=False)
            rolled_back = True
        return {"ok": False, "status": "failed", "attempts": attempts, "snapshot": snapshot["id"],
                "rolled_back": rolled_back, "files_changed": changed_total}


def _pick_model(llm: Any, avail: dict[str, bool], ctx: dict[str, Any]) -> Any:
    from aether_swarm.router import ComplexityRouter, RoutingDecision, Tier, tier_models

    decision = ComplexityRouter().route(" ".join(ctx.get("errors", [])[:3]) or "fix failing build")
    tier = decision.tier
    if tier == Tier.LOCAL_OLLAMA and not avail.get("ollama"):
        tier = Tier.BALANCED_MODEL
    if tier != Tier.LOCAL_OLLAMA and not avail.get("anthropic") and avail.get("ollama"):
        tier = Tier.LOCAL_OLLAMA
    if tier == decision.tier:
        return decision
    models = tier_models()
    model = models.get(tier) or ""
    if tier == Tier.LOCAL_OLLAMA and not model:
        probe = getattr(llm, "ollama", None)
        model = ((probe.models() if probe is not None else []) or ["llama3.1"])[0]
    return RoutingDecision(tier=tier, model=model, complexity=decision.complexity)
