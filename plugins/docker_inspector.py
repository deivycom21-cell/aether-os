"""Docker inspector: maps docker-compose services, Dockerfiles, ports and running containers
into the graph so agents know which service owns which port and what depends on what."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from aether_core.ast_indexer import IgnoreRules, iter_source_files
from aether_core.context_engine import estimate_tokens
from plugins.base import BaseAetherPlugin

COMPOSE_NAMES = re.compile(r"^(docker-)?compose(\.[\w-]+)?\.ya?ml$", re.I)


def _find_compose_files(root: Path) -> list[Path]:
    rules = IgnoreRules(root)
    out = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            rel = e.relative_to(root).as_posix()
            if e.is_dir() and not e.is_symlink():
                if not rules.ignored(rel, True):
                    stack.append(e)
            elif COMPOSE_NAMES.match(e.name):
                out.append(e)
    return sorted(out)


def _normalize_ports(raw: Any) -> list[str]:
    ports: list[str] = []
    for p in raw or []:
        if isinstance(p, dict):
            published, target = p.get("published"), p.get("target")
            ports.append(f"{published}:{target}" if published else str(target))
        else:
            ports.append(str(p))
    return ports


def parse_compose(text: str) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(text) or {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, svc in services.items():
        svc = svc or {}
        build = svc.get("build")
        if isinstance(build, dict):
            context = str(build.get("context", "."))
            dockerfile = str(build.get("dockerfile", "Dockerfile"))
        elif isinstance(build, str):
            context, dockerfile = build, "Dockerfile"
        else:
            context, dockerfile = "", ""
        deps = svc.get("depends_on") or []
        if isinstance(deps, dict):
            deps = list(deps.keys())
        env = svc.get("environment") or {}
        env_keys = list(env.keys()) if isinstance(env, dict) else [str(e).split("=", 1)[0] for e in env]
        out[str(name)] = {
            "image": str(svc.get("image", "")),
            "build_context": context,
            "dockerfile": dockerfile,
            "ports": _normalize_ports(svc.get("ports")),
            "expose": [str(x) for x in svc.get("expose") or []],
            "depends_on": [str(d) for d in deps],
            "env_keys": env_keys,  # names only, values are never stored
            "volumes": [str(v) if not isinstance(v, dict) else str(v.get("source", "")) for v in svc.get("volumes") or []],
            "command": svc.get("command") if isinstance(svc.get("command"), str) else " ".join(map(str, svc.get("command") or [])),
        }
    return out


def running_containers(timeout: float = 4.0) -> list[dict[str, str]]:
    exe = shutil.which("docker")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "ps", "--format", "{{json .}}"], capture_output=True, timeout=timeout, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append({"name": d.get("Names", ""), "image": d.get("Image", ""), "ports": d.get("Ports", ""), "status": d.get("Status", "")})
    return out


class DockerInspector(BaseAetherPlugin):
    name = "docker_inspector"
    description = "Maps docker-compose services, Dockerfiles, exposed ports and running containers into the graph."

    def is_applicable(self) -> bool:
        for _, root in self.repo_roots():
            if _find_compose_files(root) or any(p.name.lower().startswith("dockerfile") for p in iter_source_files(root)):
                return True
        return False

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        containers = running_containers()
        summary: dict[str, Any] = {"compose_files": [], "services": [], "dockerfiles": [], "running_containers": len(containers)}
        for repo_name, root in self.repo_roots():
            repo_id = db.upsert_repo(repo_name, root, repo_name == self.kernel.repo_name)
            for df in iter_source_files(root):
                if df.name.lower().startswith("dockerfile"):
                    summary["dockerfiles"].append(f"{repo_name}:{df.relative_to(root).as_posix()}")
            for cf in _find_compose_files(root):
                rel = cf.relative_to(root).as_posix()
                text = cf.read_text(encoding="utf-8", errors="replace")
                try:
                    services = parse_compose(text)
                except yaml.YAMLError as exc:
                    summary.setdefault("errors", []).append(f"{rel}: {exc}")
                    continue
                nodes: list[dict[str, Any]] = [{
                    "kind": "module", "name": cf.name, "qualname": rel,
                    "signature": f"services: {', '.join(services)}", "docstring": "docker compose stack",
                    "body": text[:4000], "start_line": 1, "end_line": text.count("\n") + 1, "metadata": {"language": "yaml"},
                }]
                edges: list[dict[str, Any]] = []
                for svc, info in services.items():
                    running = [c for c in containers if svc in c["name"]]
                    ports = info["ports"] + info["expose"]
                    line = next((i + 1 for i, ln in enumerate(text.splitlines()) if re.match(rf"^\s+{re.escape(svc)}:\s*$", ln)), 1)
                    nodes.append({
                        "kind": "service", "name": svc, "qualname": f"{rel}::{svc}",
                        "signature": f"service {svc}" + (f" image={info['image']}" if info["image"] else "")
                                     + (f" ports={','.join(ports)}" if ports else ""),
                        "docstring": f"depends_on: {', '.join(info['depends_on']) or '-'}; env: {', '.join(info['env_keys'][:20]) or '-'}",
                        "body": "", "start_line": line, "end_line": line,
                        "metadata": {**info, "running": bool(running), "containers": running},
                    })
                    for dep in info["depends_on"]:
                        edges.append({"kind": "depends_on", "dst_name": f"service:{dep}", "src_qualname": f"{rel}::{svc}", "metadata": {}})
                    if info["build_context"]:
                        dockerfile = PurePosixPath(PurePosixPath(rel).parent, info["build_context"], info["dockerfile"])
                        edges.append({"kind": "builds", "dst_name": _norm(str(dockerfile)), "src_qualname": f"{rel}::{svc}", "metadata": {}})
                    summary["services"].append({"repo": repo_name, "service": svc, "ports": ports, "running": bool(running)})
                sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
                fid = db.replace_file(repo_id, rel, "yaml", sha, len(text.encode("utf-8")), text.count("\n") + 1,
                                      estimate_tokens(text), nodes, edges)
                # Resolve 'builds' edges to concrete Dockerfiles.
                for e in db.query("SELECT id, dst_name FROM edges WHERE src_file_id = ? AND kind = 'builds'", (fid,)):
                    target = db.file_by_path(e["dst_name"], repo_name)
                    if target is not None and target.path == e["dst_name"]:
                        db.resolve_edge(int(e["id"]), target.id, None)
                summary["compose_files"].append(f"{repo_name}:{rel}")
        return summary


def _norm(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)
