"""Context compressor: hybrid search (FTS5 BM25 + hashed TF-IDF vectors fused with RRF) and
structured context packages with exact token-savings accounting.

Instead of handing an agent whole 2,000-line files, a package contains:
  1. signatures + docstrings of the most relevant symbols,
  2. the exact code fragments matching the query,
  3. direct dependencies and dependents ("what breaks if I touch this"),
  4. scars (past bugs) attached to the files involved.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from aether_core.database import AetherDB, NodeRecord
    from aether_core.event_bus import EventBus
    from aether_core.security import PromptInjectionDetector, SecretVault

# ---------------------------------------------------------------------------- tokens

# Standard input prices in USD per million tokens (Anthropic first-party API, Nov 2026).
MODEL_PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
}
REFERENCE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

_TOKEN_PIECES = re.compile(r"[A-Za-z]+|\d{1,3}|[^\sA-Za-z\d]")
_LONG_WORDS = re.compile(r"[A-Za-z]{5,}")


@lru_cache(maxsize=1)
def _tiktoken_encoder() -> Any:
    if os.environ.get("AETHER_DISABLE_TIKTOKEN"):
        return None
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - optional dependency, may also fail offline
        return None


def estimate_tokens(text: str) -> int:
    """Token count: exact via ``tiktoken`` when installed, otherwise a calibrated estimator.

    The estimator splits into word/number/symbol pieces and charges long words one token per
    ~4 characters, which tracks BPE tokenizers within ~10% on source code and English prose.
    """
    if not text:
        return 0
    enc = _tiktoken_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # noqa: BLE001
            pass
    pieces = len(_TOKEN_PIECES.findall(text))
    # Words longer than 4 letters cost one extra token per additional ~4 characters.
    extra = sum((len(w) - 1) // 4 for w in _LONG_WORDS.findall(text))
    newlines = text.count("\n")
    return max(1, int((pieces + extra) * 0.92 + newlines * 0.35))


def tokens_to_usd(tokens: int, model: str) -> float:
    price = MODEL_PRICES_PER_MTOK.get(model, MODEL_PRICES_PER_MTOK["claude-sonnet-5-5"])["input"]
    return tokens * price / 1_000_000


def savings_breakdown(raw_tokens: int, compressed_tokens: int) -> dict[str, Any]:
    saved = max(0, raw_tokens - compressed_tokens)
    pct = (saved / raw_tokens * 100.0) if raw_tokens else 0.0
    return {
        "raw_tokens": raw_tokens,
        "compressed_tokens": compressed_tokens,
        "saved_tokens": saved,
        "savings_pct": round(pct, 2),
        "saved_usd": {alias: round(tokens_to_usd(saved, model), 6) for alias, model in REFERENCE_MODELS.items()},
    }


def primary_price_model() -> str:
    alias = os.environ.get("AETHER_PRICE_MODEL", "sonnet").lower()
    return REFERENCE_MODELS.get(alias, alias if alias in MODEL_PRICES_PER_MTOK else REFERENCE_MODELS["sonnet"])


# ---------------------------------------------------------------------------- vectors


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_STOP = frozenset(
    "the a an and or of to in for on is are be by with as at it this that from self cls none true false "
    "return def class import if else elif not none str int dict list any".split()
)


def _terms(text: str) -> list[str]:
    from aether_core.database import split_identifier

    out: list[str] = []
    for w in _WORD.findall(text):
        lw = w.lower()
        if lw not in _STOP:
            out.append(lw)
        for sub in split_identifier(w):
            if len(sub) > 1 and sub != lw and sub not in _STOP:
                out.append(sub)
    return out


def _bucket(term: str, dim: int) -> int:
    # FNV-1a: stable across processes (unlike ``hash``)
    h = 2166136261
    for ch in term.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h % dim


class VectorIndex:
    """Hashed TF-IDF vectors (no model downloads) held in a dense float32 numpy matrix."""

    def __init__(self) -> None:
        self.version = -1
        self.ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self.matrix: np.ndarray = np.zeros((0, 1), dtype=np.float32)
        self.idf: np.ndarray = np.zeros(1, dtype=np.float32)
        self.dim = 1
        self._lock = threading.Lock()

    def build(self, docs: list[tuple[int, str]], version: int) -> None:
        n = len(docs)
        dim = 1024 if n < 8000 else 512
        rows: list[dict[int, float]] = []
        df = np.zeros(dim, dtype=np.float32)
        for _, text in docs:
            counts = Counter(_bucket(t, dim) for t in _terms(text))
            rows.append({b: 1.0 + math.log(c) for b, c in counts.items()})
            for b in counts:
                df[b] += 1
        idf = np.log((1 + n) / (1 + df)).astype(np.float32) + 1.0
        matrix = np.zeros((n, dim), dtype=np.float32)
        for i, row in enumerate(rows):
            if row:
                idx = np.fromiter(row.keys(), dtype=np.int64)
                matrix[i, idx] = np.fromiter(row.values(), dtype=np.float32)
        matrix *= idf
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix /= norms
        with self._lock:
            self.ids = np.array([d[0] for d in docs], dtype=np.int64)
            self.matrix = matrix
            self.idf = idf
            self.dim = dim
            self.version = version

    def search(self, query: str, k: int = 50) -> list[tuple[int, float]]:
        with self._lock:
            if self.matrix.shape[0] == 0:
                return []
            q = np.zeros(self.dim, dtype=np.float32)
            for t, c in Counter(_bucket(t, self.dim) for t in _terms(query)).items():
                q[t] = (1.0 + math.log(c)) * self.idf[t]
            norm = float(np.linalg.norm(q))
            if norm == 0:
                return []
            scores = self.matrix @ (q / norm)
            k = min(k, scores.shape[0])
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [(int(self.ids[i]), float(scores[i])) for i in top if scores[i] > 0.05]


KIND_BOOST = {
    "function": 1.15, "method": 1.15, "class": 1.2, "route": 1.25, "interface": 1.05, "type": 1.0,
    "module": 0.85, "section": 0.9, "api_call": 0.9, "dependency": 0.7, "script": 0.8, "docker_stage": 0.9,
    "table": 1.0, "package": 0.8,
}


class HybridSearcher:
    def __init__(self, db: AetherDB) -> None:
        self.db = db
        self.vectors = VectorIndex()
        self._build_lock = threading.Lock()

    def ensure_vectors(self) -> None:
        version = self.db.index_version()
        if self.vectors.version == version:
            return
        with self._build_lock:
            if self.vectors.version != version:
                self.vectors.build(self.db.all_nodes_for_search(), version)

    def search(self, query: str, limit: int = 20) -> list[tuple[NodeRecord, float]]:
        self.ensure_vectors()
        fts = self.db.fts_search(query, limit=80)
        vec = self.vectors.search(query, k=80)
        fused: dict[int, float] = defaultdict(float)
        for rank, (nid, _) in enumerate(fts):
            fused[nid] += 1.0 / (60 + rank)
        for rank, (nid, _) in enumerate(vec):
            fused[nid] += 1.0 / (60 + rank)
        if not fused:
            return []
        candidates = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: limit * 3]
        nodes = {n.id: n for n in self.db.nodes_by_ids([nid for nid, _ in candidates])}
        from aether_core.database import split_identifier

        q_words = {w.lower() for w in _WORD.findall(query)}
        q_terms = set(_terms(query)) | q_words
        scored: list[tuple[NodeRecord, float]] = []
        for nid, score in candidates:
            node = nodes.get(nid)
            if node is None:
                continue
            boost = KIND_BOOST.get(node.kind, 1.0)
            name_parts = set(split_identifier(node.name))
            if node.name.lower() in q_words:
                boost *= 1.8
            elif name_parts:
                overlap = len(name_parts & q_terms) / len(name_parts)
                boost *= 1.0 + 0.6 * overlap
            if "/test" in f"/{node.path}" or node.path.startswith("tests/"):
                boost *= 0.85
            scored.append((node, score * boost))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]


# ---------------------------------------------------------------------------- packages


RELEVANCE_CUTOFF = 0.35
SIGNATURE_ONLY_KINDS = frozenset({"api_call", "dependency", "script", "env_var", "route", "docker_stage", "package"})
CONTAINER_KINDS = frozenset({"module", "section", "class"})


def extract_snippet(body: str, query_terms: set[str], start_line: int, max_lines: int = 24) -> tuple[str, int, int]:
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body, start_line, start_line + max(0, len(lines) - 1)
    hits = [i for i, ln in enumerate(lines) if any(t in ln.lower() for t in query_terms)]
    if not hits:
        chunk = lines[:max_lines]
        return "\n".join(chunk) + "\n…", start_line, start_line + max_lines - 1
    # choose the window of max_lines that covers the most hits
    best_start, best_count = 0, -1
    for h in hits:
        s = max(0, min(h - 3, len(lines) - max_lines))
        count = sum(1 for x in hits if s <= x < s + max_lines)
        if count > best_count:
            best_start, best_count = s, count
    chunk = lines[best_start : best_start + max_lines]
    prefix = "…\n" if best_start > 0 else ""
    suffix = "\n…" if best_start + max_lines < len(lines) else ""
    return prefix + "\n".join(chunk) + suffix, start_line + best_start, start_line + best_start + len(chunk) - 1


def _first_paragraph(doc: str, limit: int = 300) -> str:
    para = doc.strip().split("\n\n", 1)[0].strip()
    return para if len(para) <= limit else para[: limit - 1] + "…"


@dataclass(slots=True)
class ContextPackage:
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.data


class ContextEngine:
    def __init__(
        self,
        db: AetherDB,
        bus: EventBus,
        vault: SecretVault,
        injection_detector: PromptInjectionDetector,
    ) -> None:
        self.db = db
        self.bus = bus
        self.vault = vault
        self.injection = injection_detector
        self.searcher = HybridSearcher(db)

    # ------------------------------------------------------------------ query
    def query(self, query: str, max_tokens: int = 1500, limit: int = 12) -> dict[str, Any]:
        t0 = time.perf_counter()
        query = query.strip()
        max_tokens = max(200, int(max_tokens))
        hits = self.searcher.search(query, limit=max(limit * 2, 20))
        q_terms = {w.lower() for w in _WORD.findall(query) if len(w) > 2}
        from aether_core.database import split_identifier

        for w in list(q_terms):
            q_terms.update(s for s in split_identifier(w) if len(s) > 2)

        results: list[dict[str, Any]] = []
        files_touched: dict[int, tuple[str, str, int]] = {}
        budget_reserve = min(450, max_tokens // 3)
        used = 0
        seen_quals: set[str] = set()
        covered: dict[int, list[tuple[int, int]]] = defaultdict(list)  # file_id -> line ranges already sent
        top_score = hits[0][1] if hits else 0.0
        for node, score in hits:
            if node.qualname in seen_quals:
                continue
            if results and score < top_score * RELEVANCE_CUTOFF:
                break  # everything after this is noise; sending it would cost more than it saves
            if any(s <= node.start_line and node.end_line <= e for s, e in covered[node.file_id]):
                continue  # already inside a fragment we are sending (e.g. a method of an included class)
            snippet, s_line, e_line = ("", node.start_line, node.end_line)
            if node.body and node.kind not in SIGNATURE_ONLY_KINDS:
                snippet, s_line, e_line = extract_snippet(node.body, q_terms, node.start_line)
                if node.kind in CONTAINER_KINDS and not any(t in node.body.lower() for t in q_terms):
                    snippet, s_line, e_line = ("", node.start_line, node.end_line)
            item: dict[str, Any] = {
                "repo": node.repo_name,
                "file": node.path,
                "symbol": node.qualname,
                "kind": node.kind,
                "signature": node.signature,
                "lines": [s_line, e_line],
                "score": round(score, 4),
            }
            if node.docstring:
                item["doc"] = _first_paragraph(node.docstring)
            if snippet:
                item["snippet"] = snippet
            cost = estimate_tokens(json.dumps(item, ensure_ascii=False))
            if used + cost > max_tokens - budget_reserve:
                # Degrade gracefully: signature-only entry if the snippet doesn't fit.
                item.pop("snippet", None)
                cost = estimate_tokens(json.dumps(item, ensure_ascii=False))
                if used + cost > max_tokens - budget_reserve:
                    continue
            used += cost
            seen_quals.add(node.qualname)
            if "snippet" in item:
                covered[node.file_id].append((s_line, e_line))
            results.append(item)
            if node.file_id not in files_touched:
                f = self.db.file_by_id(node.file_id)
                if f is not None:
                    files_touched[node.file_id] = (f.repo_name, f.path, f.token_estimate)
            if len(results) >= limit:
                break

        # Dependencies / impact for the top files.
        relations: list[dict[str, Any]] = []
        for fid, (repo, path, _) in list(files_touched.items())[:4]:
            deps = self.db.file_dependencies(fid)
            dependents = self.db.file_dependents(fid)
            relations.append(
                {
                    "file": path,
                    "repo": repo,
                    "depends_on": sorted({d["dst_path"] for d in deps if d["dst_path"]})[:8],
                    "used_by": sorted({d["path"] for d in dependents})[:8],
                    "used_by_count": len({d["file_id"] for d in dependents}),
                    "cross_repo_consumers": sorted({f"{d['repo_name']}:{d['path']}" for d in dependents if d["repo_name"] != repo})[:6],
                }
            )

        scars: list[dict[str, Any]] = []
        scar_nodes: list[str] = []
        for fid, (repo, path, _) in files_touched.items():
            for s in self.db.scars_for_path(path):
                scars.append(
                    {"file": s.file_path, "severity": s.severity, "error": s.error_summary,
                     "root_cause": s.root_cause, "fix": s.fix_applied, "symbol": s.symbol, "source": s.source}
                )
                scar_nodes.append(f"{repo}:{path}")
        scars = scars[:10]

        warnings: list[str] = []
        injected_files: list[str] = []
        for r in results:
            if r.get("snippet"):
                rep = self.injection.analyze(r["snippet"])
                if rep.is_suspicious:
                    injected_files.append(r["file"])
                    r["snippet"] = "[AETHER: snippet withheld - possible prompt injection detected]"
        if injected_files:
            warnings.append(f"Prompt-injection patterns found in: {', '.join(sorted(set(injected_files)))}")
        if scars:
            warnings.insert(0, f"⚠ {len(scars)} scar(s) recorded on these files — read them before editing.")

        package: dict[str, Any] = {
            "query": query,
            "results": results,
            "relations": relations,
            "scars": scars,
            "warnings": warnings,
        }
        masked, findings = self.vault.mask_obj(package)
        package = masked
        raw_tokens = sum(tok for _, _, tok in files_touched.values())
        compressed_tokens = estimate_tokens(json.dumps(package, ensure_ascii=False))
        metrics = savings_breakdown(raw_tokens, compressed_tokens)
        latency = (time.perf_counter() - t0) * 1000
        metrics["latency_ms"] = round(latency, 2)
        metrics["files_considered"] = len(files_touched)
        metrics["secrets_masked"] = len(findings)
        package["metrics"] = metrics

        primary = primary_price_model()
        saved_usd = tokens_to_usd(metrics["saved_tokens"], primary)
        self.db.record_telemetry("context_query", raw_tokens, compressed_tokens, saved_usd, latency,
                                 {"query": query[:200], "results": len(results)})
        node_keys = [f"{repo}:{path}" for repo, path, _ in files_touched.values()]
        self.bus.publish(
            "CONTEXT_QUERY",
            {"query": query[:200], "results": len(results), "saved_tokens": metrics["saved_tokens"],
             "saved_usd": round(saved_usd, 6), "latency_ms": metrics["latency_ms"]},
            node_keys,
        )
        if scars:
            self.bus.publish("SCAR_TRIGGERED", {"count": len(scars), "files": sorted({s["file"] for s in scars})},
                             sorted(set(scar_nodes)))
        if findings:
            for f in findings:
                self.db.add_security_finding("secret_masked", f.rule, f.severity, "", f.line, f.preview)
            self.bus.publish("SECRET_BLOCKED", {"count": len(findings), "rules": sorted({f.rule for f in findings}),
                                                "context": "query_smart_context"}, node_keys)
        if injected_files:
            self.bus.publish("INJECTION_DETECTED", {"files": sorted(set(injected_files)), "context": "query_smart_context"},
                             node_keys)
        return package

    # ------------------------------------------------------------------ impact
    def impact_analysis(self, file_path: str, max_depth: int = 3) -> dict[str, Any]:
        f = self.db.file_by_path(file_path)
        if f is None:
            return {"file": file_path, "found": False, "error": "File not indexed. Run `aether index` first."}
        direct = self.db.file_dependents(f.id)
        visited: dict[int, int] = {f.id: 0}
        frontier = [f.id]
        transitive: list[dict[str, Any]] = []
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            nxt: list[int] = []
            for fid in frontier:
                for d in self.db.file_dependents(fid):
                    did = int(d["file_id"])
                    if did in visited:
                        continue
                    visited[did] = depth
                    nxt.append(did)
                    transitive.append({"file": d["path"], "repo": d["repo_name"], "depth": depth, "via": d["kind"]})
            frontier = nxt
        nodes = self.db.nodes_for_file(f.id)
        routes = [
            {"method": n.metadata.get("method"), "path": n.metadata.get("path"), "handler": n.metadata.get("handler", n.qualname)}
            for n in nodes if n.kind == "route"
        ]
        consumers = [
            {"file": d["path"], "repo": d["repo_name"], "call": d["dst_name"]}
            for d in direct if d["kind"] == "http_call"
        ]
        cross_repo = sorted({t["repo"] for t in transitive if t["repo"] != f.repo_name})
        public_symbols = [n.qualname for n in nodes if n.kind in {"function", "class", "method", "interface"}][:30]
        scars = [s.to_dict() for s in self.db.scars_for_path(f.path)]
        direct_files = sorted({d["path"] for d in direct})
        risk = min(1.0, 0.08 * len(direct_files) + 0.03 * len(transitive) + 0.2 * len(cross_repo)
                   + 0.1 * len(routes) + 0.15 * len([s for s in scars if s["severity"] in {"critical", "high"}]))
        level = "critical" if risk >= 0.75 else "high" if risk >= 0.5 else "medium" if risk >= 0.2 else "low"
        result = {
            "file": f.path,
            "repo": f.repo_name,
            "found": True,
            "direct_dependents": direct_files,
            "transitive_dependents": transitive[:60],
            "endpoints_defined": routes,
            "endpoint_consumers": consumers,
            "linked_repos_affected": cross_repo,
            "public_symbols": public_symbols,
            "scars": scars,
            "risk_score": round(risk, 3),
            "risk_level": level,
            "recommendation": (
                "Low blast radius: safe to edit with local tests."
                if level == "low"
                else "Run the dependents' tests and review endpoint consumers before merging."
                if level in {"medium", "high"}
                else "Critical blast radius: snapshot first (create_snapshot) and coordinate cross-repo changes."
            ),
        }
        keys = [f"{f.repo_name}:{f.path}"] + [f"{t['repo']}:{t['file']}" for t in transitive[:40]]
        self.bus.publish("IMPACT_ANALYSIS", {"file": f.path, "risk_level": level, "dependents": len(transitive)}, keys)
        return result
