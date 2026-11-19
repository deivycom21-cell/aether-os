"""Shared fixtures: realistic multi-repo sample projects (FastAPI backend + Next.js/TS frontend)."""

from __future__ import annotations

import os
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from aether_core.kernel import AetherKernel

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

BACKEND_FILES = {
    "app/__init__.py": '"""Backend package."""\n',
    "app/models.py": '''"""Domain models."""
from dataclasses import dataclass


@dataclass
class User:
    """A registered user."""
    id: int
    email: str


class UserRepository:
    """Stores users in memory."""

    def __init__(self) -> None:
        self._users: dict[int, User] = {}

    def get_user(self, user_id: int) -> User | None:
        """Return the user with ``user_id`` or None."""
        return self._users.get(user_id)

    def save(self, user: User) -> None:
        self._users[user.id] = user
''',
    "app/payments.py": '''"""Payment processing with retry logic."""
from .models import User


def charge_customer(user: User, amount_cents: int, currency: str = "usd") -> dict:
    """Charge a customer; retries idempotently on network failure."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    return {"user": user.id, "amount": amount_cents, "currency": currency, "status": "charged"}
''',
    "app/api.py": '''"""HTTP API."""
from fastapi import APIRouter, FastAPI

from app.models import UserRepository
from app.payments import charge_customer

app = FastAPI()
router = APIRouter(prefix="/api")
repo = UserRepository()


@router.get("/users/{user_id}")
def read_user(user_id: int) -> dict:
    """Fetch one user by id."""
    user = repo.get_user(user_id)
    return {"id": user.id, "email": user.email} if user else {}


@router.post("/payments")
def create_payment(user_id: int, amount: int) -> dict:
    """Charge the user."""
    user = repo.get_user(user_id)
    return charge_customer(user, amount)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
''',
    "tests/test_payments.py": '''from app.payments import charge_customer


def test_positive_amount():
    assert True
''',
    "README.md": "# Backend\n\n## Payments\nCharges customers through `charge_customer`.\n\n## Users\nCRUD for users.\n",
    "Dockerfile": "FROM python:3.12-slim AS runtime\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.api:app\"]\n",
    ".gitignore": "*.log\nbuild/\n.env\n",
    "node_modules/leftpad/index.js": "module.exports = () => 1;\n",
    "debug.log": "ignored log line\n",
}

FRONTEND_FILES = {
    "package.json": '{"name": "web", "version": "1.0.0", "scripts": {"dev": "next dev", "test": "jest"}, '
                    '"dependencies": {"react": "^18.3.1", "next": "14.2.3"}}',
    "src/api/client.ts": '''/** Thin HTTP client for the backend API. */
import { formatMoney } from "../utils/format";

export interface UserDto { id: number; email: string }

/** Load one user from the backend. */
export async function fetchUser(id: number): Promise<UserDto> {
  const res = await fetch(`/api/users/${id}`);
  return res.json();
}

export const payInvoice = async (userId: number, amount: number) => {
  const res = await fetch("/api/payments", { method: "POST", body: JSON.stringify({ userId, amount }) });
  return formatMoney(amount) + (await res.text());
};
''',
    "src/utils/format.ts": '''export function formatMoney(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export class Formatter {
  constructor(private locale: string) {}
  format(value: number): string {
    return value.toLocaleString(this.locale);
  }
}
''',
    "app/api/hello/route.ts": "export async function GET() { return Response.json({ hello: 'world' }); }\n",
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    proc = subprocess.run([GIT or "git", "-C", str(cwd), *args], capture_output=True, text=True, env=env, check=True)
    return proc.stdout


def git_init(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial commit")


def fake_secret(prefix: str, length: int, alphabet: str = string.ascii_letters + string.digits, seed: int = 7) -> str:
    """Build secret-shaped strings at runtime so no real-looking credential lives in the repo."""
    rng = random.Random(seed + length + len(prefix))
    return prefix + "".join(rng.choice(alphabet) for _ in range(length))


@pytest.fixture()
def backend_repo(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "backend", BACKEND_FILES)


@pytest.fixture()
def frontend_repo(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "frontend", FRONTEND_FILES)


@pytest.fixture()
def kernel(backend_repo: Path, frontend_repo: Path, monkeypatch: pytest.MonkeyPatch) -> AetherKernel:
    monkeypatch.setenv("AETHER_OLLAMA_URL", "http://127.0.0.1:9")
    k = AetherKernel(backend_repo)
    k.link_repo(frontend_repo)
    k.index(run_plugins=False)
    yield k
    k.close()


@pytest.fixture()
def git_kernel(backend_repo: Path, monkeypatch: pytest.MonkeyPatch) -> AetherKernel:
    if GIT is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("AETHER_OLLAMA_URL", "http://127.0.0.1:9")
    git_init(backend_repo)
    k = AetherKernel(backend_repo)
    k.index(run_plugins=False)
    yield k
    k.close()
