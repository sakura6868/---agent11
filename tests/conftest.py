"""Keep regression tests offline and isolated from real user data."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
_workspace = TemporaryDirectory(prefix="campus-agent-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + _workspace.name.replace("\\", "/") + "/tests.db"
os.environ["RAG_USE_ST"] = "0"
os.environ["AGENT_LLM"] = "0"
os.environ["AGENT_LLM_API_KEY"] = ""
os.environ["WEB_SEARCH_PROVIDER"] = "none"
os.environ["WEB_SEARCH_API_KEY"] = ""
os.environ["ADMIN_API_TOKEN"] = "isolated-regression-token"
os.environ["AUTH_TOKEN_SECRET"] = "isolated-regression-session-secret"


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    import db

    db.init_db()
    yield
    db.get_engine().dispose()
    _workspace.cleanup()
