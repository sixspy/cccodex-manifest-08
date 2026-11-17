import pytest
from litestar.testing import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def client(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", db_path)
    init_db(db_path)
    with TestClient(app=app) as c:
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return client
