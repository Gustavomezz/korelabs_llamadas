from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "korelabs_llamadas"}


def test_root():
    response = client.get("/")
    assert response.status_code == 200
