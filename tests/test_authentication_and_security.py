import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from web.app import app, agent_manager
from config.settings import settings


client = TestClient(app)


@pytest.fixture(autouse=True)
async def setup_test_mode():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")
    await agent_manager.crypto_agent.set_mode("simulator")
    agent_manager.forex_agent.is_running = False
    agent_manager.stock_agent.is_running = False
    agent_manager.crypto_agent.is_running = False


def test_auth_config():
    """Verify that public authentication config is accessible without logging in."""
    res = client.get("/api/auth/config")
    assert res.status_code == 200
    data = res.json()
    assert data["auth_enabled"] is True
    assert data["google_allowed_email"] == "oubou68@gmail.com"


def test_unauthenticated_api_access_blocked():
    """Ensure protected API endpoints return 401 Unauthorized when unauthenticated."""
    unauthed_client = TestClient(app)

    res_status = unauthed_client.get("/api/status")
    assert res_status.status_code == 401

    res_candles = unauthed_client.get("/api/candles")
    assert res_candles.status_code == 401

    res_control = unauthed_client.post("/api/control/start", json={"bot_type": "forex"})
    assert res_control.status_code == 401

    res_keys = unauthed_client.post("/api/config/keys", json={})
    assert res_keys.status_code == 401


def test_plain_login_success_with_default_credentials():
    """Verify that login succeeds with the default credentials."""
    unauthed_client = TestClient(app)
    res = unauthed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "AuraQuant_2026!TradeSafe"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "authenticated"
    assert "token" in data
    assert data["user"]["username"] == "admin"
    assert "aura_session" in unauthed_client.cookies

    # Access protected route with session cookie
    res_status = unauthed_client.get("/api/status")
    assert res_status.status_code == 200


def test_plain_login_rejection_with_wrong_password():
    """Verify that login fails with an incorrect password."""
    unauthed_client = TestClient(app)
    res = unauthed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "WrongPassword123!"
    })
    assert res.status_code == 401
    assert "detail" in res.json()


@patch("requests.get")
def test_google_ident_authorized_email(mock_get):
    """Verify Google login succeeds for the strictly allowed email oubou68@gmail.com."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "sub": "google-sub-12345",
        "email": "oubou68@gmail.com",
        "email_verified": "true",
        "name": "Oubou",
        "picture": "https://lh3.googleusercontent.com/photo.jpg"
    }
    mock_get.return_value = mock_resp

    unauthed_client = TestClient(app)
    res = unauthed_client.post("/api/auth/google", json={"credential": "valid_google_oauth2_id_token"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "authenticated"
    assert data["user"]["email"] == "oubou68@gmail.com"
    assert data["user"]["auth_provider"] == "google"

    # Protected endpoint accessible
    res_status = unauthed_client.get("/api/status")
    assert res_status.status_code == 200


@patch("requests.get")
def test_google_ident_rejected_for_unauthorized_email(mock_get):
    """Verify Google login strictly rejects ANY email other than oubou68@gmail.com with 403."""
    unauthorized_emails = [
        "stranger@gmail.com",
        "admin@gmail.com",
        "oubou68@yahoo.com",
        "oubou681@gmail.com"
    ]

    unauthed_client = TestClient(app)

    for email in unauthorized_emails:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "sub": "google-sub-9999",
            "email": email,
            "email_verified": "true",
            "name": "Intruder"
        }
        mock_get.return_value = mock_resp

        res = unauthed_client.post("/api/auth/google", json={"credential": f"token_for_{email}"})
        assert res.status_code == 403
        assert "oubou68@gmail.com" in res.json()["detail"]


@patch("requests.get")
def test_google_ident_rejected_for_unverified_email(mock_get):
    """Verify Google login rejects unverified Google email addresses."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "sub": "google-sub-12345",
        "email": "oubou68@gmail.com",
        "email_verified": "false",
        "name": "Oubou"
    }
    mock_get.return_value = mock_resp

    unauthed_client = TestClient(app)
    res = unauthed_client.post("/api/auth/google", json={"credential": "unverified_email_token"})
    assert res.status_code == 403


@patch("requests.get")
def test_google_ident_rejected_for_fake_or_demo_tokens(mock_get):
    """Verify fake/demo tokens are rejected when Google tokeninfo returns 400."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Invalid Value"
    mock_get.return_value = mock_resp

    unauthed_client = TestClient(app)
    res = unauthed_client.post("/api/auth/google", json={"credential": "mock_google_token:oubou68@gmail.com:Oubou:true"})
    assert res.status_code == 400
    assert "Ungültiger oder abgelaufener Google ID Token" in res.json()["detail"]


def test_authenticated_request_via_bearer_token():
    """Verify protected endpoints work with Authorization: Bearer header."""
    unauthed_client = TestClient(app)
    login_res = unauthed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "AuraQuant_2026!TradeSafe"
    })
    token = login_res.json()["token"]

    # Fresh client without cookies
    bearer_client = TestClient(app)
    res = bearer_client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_websocket_unauthenticated_rejected():
    """Verify WebSocket connection without credentials is rejected with code 1008."""
    unauthed_client = TestClient(app)
    with pytest.raises(Exception):
        with unauthed_client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_websocket_authenticated_accepted():
    """Verify WebSocket connection with token query parameter is accepted."""
    unauthed_client = TestClient(app)
    login_res = unauthed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "AuraQuant_2026!TradeSafe"
    })
    token = login_res.json()["token"]

    with unauthed_client.websocket_connect(f"/ws?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "INITIAL_STATE"


def test_logout():
    """Verify logout invalidates session."""
    authed_client = TestClient(app)
    login_res = authed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "AuraQuant_2026!TradeSafe"
    })
    assert login_res.status_code == 200

    logout_res = authed_client.post("/api/auth/logout")
    assert logout_res.status_code == 200
    assert logout_res.json()["status"] == "logged_out"

    # Status check confirms unauthenticated
    status_res = authed_client.get("/api/auth/status")
    assert status_res.json()["authenticated"] is False


def test_unauthenticated_cockpit_redirects_to_login():
    """Verify / redirects unauthenticated requests to /login so cockpit is NEVER visible."""
    unauthed_client = TestClient(app, follow_redirects=False)
    res = unauthed_client.get("/")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"

    # Following redirect gives 200 on login page
    res_login = unauthed_client.get("/login")
    assert res_login.status_code == 200
    assert "AURA" in res_login.text
    assert "oubou68@gmail.com" in res_login.text


def test_authenticated_cockpit_served():
    """Verify / serves the cockpit when authenticated."""
    authed_client = TestClient(app, follow_redirects=False)
    login_res = authed_client.post("/api/auth/login", json={
        "username": "admin",
        "password": "AuraQuant_2026!TradeSafe"
    })
    assert login_res.status_code == 200

    res = authed_client.get("/")
    assert res.status_code == 200
    assert "AURA QUANT AI" in res.text
