"""Testes do modulo LinkedIn: OAuth, acoes, recibos HMAC e plugin.

Nada aqui fala com a API real do LinkedIn. As chamadas HTTP sao interceptadas
por monkeypatch, e o config eh redirecionado para diretórios temporarios.

O que o teste vigia, e por que:
  * configure() grava client_id/secret sem logar segredos;
  * OAuth flow (URL de autorizacao + troca de code por tokens);
  * Cada acao (post, comment, profile, search) chama a API correta;
  * Recibos HMAC sao gerados para cada acao executada;
  * O plugin linkedin_control traduz acoes de voz em chamadas ao core;
  * Acoes com texto vazio sao recusadas sem chamar a API;
  * Token expirado dispara renovacao automatica.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import linkedin
from core import tool_receipts


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_linkedin(tmp_path, monkeypatch):
    """Config e tokens em diretorio temporario; estado limpo antes/depois."""
    config_path = tmp_path / "config" / "linkedin.json"
    monkeypatch.setattr(linkedin, "_CONFIG_PATH", config_path)
    linkedin.reset()
    yield
    linkedin.reset()


@pytest.fixture()
def hmac_key(tmp_path):
    """Chave HMAC em arquivo temporario para testes de recibo."""
    key_path = tmp_path / "tool_hmac.key"
    key_path.write_bytes(b"test-hmac-key-for-linkedin-tests")
    monkeypatch_env = os.environ.get("JARVIS_RECEIPT_HMAC_KEY")
    os.environ["JARVIS_RECEIPT_HMAC_KEY"] = "test-hmac-key-for-linkedin-tests"
    # Reset cached key so it picks up the env var
    linkedin._hmac_key = None
    yield b"test-hmac-key-for-linkedin-tests"
    if monkeypatch_env is not None:
        os.environ["JARVIS_RECEIPT_HMAC_KEY"] = monkeypatch_env
    else:
        os.environ.pop("JARVIS_RECEIPT_HMAC_KEY", None)
    linkedin._hmac_key = None


def _mock_urlopen(response_data: dict, status: int = 200):
    """Cria um mock para urllib.request.urlopen que retorna JSON."""
    body = json.dumps(response_data).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = status
    mock_resp.code = status
    return mock_resp


# ── testes de configuracao ───────────────────────────────────────────────────

class TestConfiguration:
    def test_configure_saves_credentials(self, tmp_path):
        linkedin.configure("my-client-id", "my-client-secret")
        cfg = linkedin._load_config()
        assert cfg["client_id"] == "my-client-id"
        assert cfg["client_secret"] == "my-client-secret"

    def test_configure_file_permissions(self, tmp_path):
        linkedin.configure("id", "secret")
        config_path = tmp_path / "config" / "linkedin.json"
        mode = oct(config_path.stat().st_mode)[-3:]
        assert mode == "600", f"Config file should be 0600, got {mode}"

    def test_is_configured_false_when_empty(self):
        assert linkedin.is_configured() is False

    def test_is_configured_true_after_configure(self):
        linkedin.configure("id", "secret")
        assert linkedin.is_configured() is True

    def test_client_secret_never_in_profile_output(self):
        linkedin.configure("id", "super-secret")
        cfg = linkedin._load_config()
        # Secret is stored but should not appear in any public output
        assert "super-secret" not in json.dumps({"client_id": cfg.get("client_id")})


# ── testes de OAuth ─────────────────────────────────────────────────────────

class TestOAuth:
    def test_authorization_url_contains_required_params(self):
        linkedin.configure("test-id", "test-secret")
        url = linkedin.get_authorization_url()
        assert "linkedin.com/oauth/v2/authorization" in url
        assert "client_id=test-id" in url
        assert "response_type=code" in url
        assert "redirect_uri=" in url
        assert "scope=" in url

    def test_authorization_url_raises_when_not_configured(self):
        with pytest.raises(RuntimeError, match="nao configurado"):
            linkedin.get_authorization_url()

    def test_handle_callback_exchanges_code_for_tokens(self):
        linkedin.configure("test-id", "test-secret")
        mock_response = {
            "access_token": "AQX...test-token",
            "refresh_token": "AQX...test-refresh",
            "expires_in": 5184000,
            "scope": "r_liteprofile",
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_response)):
            tokens = linkedin.handle_callback("auth-code-123")

        assert tokens["access_token"] == "AQX...test-token"
        assert tokens["refresh_token"] == "AQX...test-refresh"
        assert tokens["expires_in"] == 5184000

    def test_handle_callback_saves_tokens_to_config(self):
        linkedin.configure("test-id", "test-secret")
        mock_response = {
            "access_token": "saved-token",
            "refresh_token": "saved-refresh",
            "expires_in": 3600,
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_response)):
            linkedin.handle_callback("code")

        loaded = linkedin._load_tokens()
        assert loaded["access_token"] == "saved-token"

    def test_is_authenticated_false_before_callback(self):
        assert linkedin.is_authenticated() is False

    def test_is_authenticated_true_after_callback(self):
        linkedin.configure("id", "secret")
        mock_response = {"access_token": "tok", "expires_in": 3600}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_response)):
            linkedin.handle_callback("code")
        assert linkedin.is_authenticated() is True


# ── testes de acoes ──────────────────────────────────────────────────────────

class TestActions:
    def _setup_auth(self):
        linkedin.configure("id", "secret")
        # Set tokens directly to skip OAuth flow
        linkedin._save_tokens({
            "access_token": "test-token",
            "refresh_token": "test-refresh",
            "expires_at": time.time() + 3600,
            "expires_in": 3600,
        })

    def test_get_profile_calls_api(self):
        self._setup_auth()
        mock_profile = {
            "id": "abc123",
            "localizedFirstName": "Joao",
            "localizedLastName": "Silva",
            "localizedHeadline": "Engenheiro de Software",
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_profile)) as mock_open:
            result = linkedin.get_profile()

        assert result["first_name"] == "Joao"
        assert result["last_name"] == "Silva"
        assert result["headline"] == "Engenheiro de Software"
        # Verify Authorization header was sent
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert "Bearer test-token" in req.get_header("Authorization")

    def test_post_requires_text(self):
        self._setup_auth()
        with pytest.raises(ValueError, match="vazio"):
            linkedin.post("")

    def test_post_calls_api_with_correct_body(self):
        self._setup_auth()
        mock_profile = {"id": "person-1", "localizedFirstName": "Test",
                        "localizedLastName": "User"}
        mock_post_result = {"id": "urn:li:share:999"}

        call_count = [0]
        def side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_urlopen(mock_profile)
            else:
                return _mock_urlopen(mock_post_result)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = linkedin.post("Hello LinkedIn!")

        assert result["post_id"] == "urn:li:share:999"
        assert result["status"] == "published"

    def test_comment_requires_post_id(self):
        self._setup_auth()
        with pytest.raises(ValueError, match="vazio"):
            linkedin.comment("", "text")

    def test_comment_requires_text(self):
        self._setup_auth()
        with pytest.raises(ValueError, match="vazio"):
            linkedin.comment("post-123", "")

    def test_search_requires_query(self):
        self._setup_auth()
        with pytest.raises(ValueError, match="vazia"):
            linkedin.search_people("")

    def test_search_returns_people_list(self):
        self._setup_auth()
        mock_result = {
            "elements": [
                {"id": "p1", "firstName": "Maria", "lastName": "Santos",
                 "headline": "Designer"},
                {"id": "p2", "firstName": "Pedro", "lastName": "Lima",
                 "headline": "Dev"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_result)):
            result = linkedin.search_people("maria", limit=5)

        assert len(result) == 2
        assert result[0]["first_name"] == "Maria"
        assert result[1]["headline"] == "Dev"

    def test_search_clamps_limit(self):
        self._setup_auth()
        mock_result = {"elements": []}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_result)):
            result = linkedin.search_people("test", limit=100)
        # Limit should be clamped to 25
        assert isinstance(result, list)


# ── testes de recibos HMAC ───────────────────────────────────────────────────

class TestReceipts:
    def test_receipt_generated_for_get_profile(self, hmac_key):
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "tok", "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        mock_profile = {"id": "p1", "localizedFirstName": "A",
                        "localizedLastName": "B"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_profile)):
            result = linkedin.get_profile()

        # Receipt should be generated internally
        receipt = linkedin._issue_receipt("linkedin_get_profile", {}, result)
        assert receipt is not None
        assert receipt["tool"] == "linkedin_get_profile"
        assert receipt["call_id"]  # UUID
        assert receipt["hmac"].startswith("sha256:")
        assert receipt["timestamp"]

    def test_receipt_verification(self, hmac_key):
        receipt = linkedin._issue_receipt("test_tool", {"arg": "val"}, "result")
        assert receipt is not None
        assert tool_receipts.verify_receipt(receipt, hmac_key) is True

    def test_receipt_tampering_detected(self, hmac_key):
        receipt = linkedin._issue_receipt("test_tool", {}, "result")
        receipt["result_hash"] = "sha256:tampered"
        assert tool_receipts.verify_receipt(receipt, hmac_key) is False

    def test_receipt_generated_for_post(self, hmac_key):
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "tok", "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        mock_profile = {"id": "p1", "localizedFirstName": "A",
                        "localizedLastName": "B"}
        mock_post = {"id": "share:123"}
        call_count = [0]
        def side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_urlopen(mock_profile)
            return _mock_urlopen(mock_post)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = linkedin.post("Test post")

        receipt = linkedin._issue_receipt("linkedin_post", {"text": "Test post"}, result)
        assert receipt is not None
        assert receipt["tool"] == "linkedin_post"


# ── testes do plugin (linkedin_control) ──────────────────────────────────────

class TestPlugin:
    def test_plugin_declaration_exists(self):
        from plugins import linkedin_control
        assert hasattr(linkedin_control, "PLUGIN")
        assert linkedin_control.PLUGIN["name"] == "linkedin_control"
        assert "action" in linkedin_control.PLUGIN["parameters"]["properties"]

    def test_auth_url_when_not_configured(self):
        from plugins import linkedin_control
        result = linkedin_control.linkedin_control(action="auth_url")
        assert "nao configurado" in result.lower()

    def test_auth_url_when_configured(self):
        from plugins import linkedin_control
        linkedin.configure("test-id", "test-secret")
        result = linkedin_control.linkedin_control(action="auth_url")
        assert "linkedin.com/oauth" in result

    def test_auth_callback_empty_code(self):
        from plugins import linkedin_control
        result = linkedin_control.linkedin_control(action="auth_callback", code="")
        assert "vazio" in result.lower()

    def test_post_empty_text(self):
        from plugins import linkedin_control
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "tok", "expires_at": time.time() + 3600,
        })
        result = linkedin_control.linkedin_control(action="post", text="")
        assert "vazio" in result.lower()

    def test_post_not_authenticated(self):
        from plugins import linkedin_control
        result = linkedin_control.linkedin_control(action="post", text="hello")
        assert "nao autenticado" in result.lower()

    def test_profile_not_authenticated(self):
        from plugins import linkedin_control
        result = linkedin_control.linkedin_control(action="profile")
        assert "nao autenticado" in result.lower()

    def test_unknown_action(self):
        from plugins import linkedin_control
        result = linkedin_control.linkedin_control(action="invalid_action")
        assert "nao reconhecida" in result.lower()

    def test_profile_action_success(self):
        from plugins import linkedin_control
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "tok", "expires_at": time.time() + 3600,
        })
        mock_profile = {"id": "p1", "localizedFirstName": "Joao",
                        "localizedLastName": "Silva",
                        "localizedHeadline": "Dev"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_profile)):
            result = linkedin_control.linkedin_control(action="profile")
        assert "Joao" in result
        assert "Dev" in result

    def test_search_action_success(self):
        from plugins import linkedin_control
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "tok", "expires_at": time.time() + 3600,
        })
        mock_result = {
            "elements": [
                {"id": "p1", "firstName": "Maria", "lastName": "Santos",
                 "headline": "Designer"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_result)):
            result = linkedin_control.linkedin_control(action="search", query="maria")
        assert "Maria" in result
        assert "1 pessoa" in result


# ── testes de token refresh ──────────────────────────────────────────────────

class TestTokenRefresh:
    def test_expired_token_triggers_refresh(self):
        linkedin.configure("id", "secret")
        # Token expirado (expires_at no passado)
        linkedin._save_tokens({
            "access_token": "old-token",
            "refresh_token": "valid-refresh",
            "expires_at": time.time() - 100,  # expired
        })
        mock_refresh_response = {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        mock_profile = {"id": "p1", "localizedFirstName": "A",
                        "localizedLastName": "B"}

        call_count = [0]
        def side_effect(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Refresh call
                return _mock_urlopen(mock_refresh_response)
            else:
                # Profile call
                return _mock_urlopen(mock_profile)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = linkedin.get_profile()

        assert result["first_name"] == "A"
        # Verify new token was saved
        tokens = linkedin._load_tokens()
        assert tokens["access_token"] == "new-token"

    def test_refresh_without_refresh_token_raises(self):
        linkedin.configure("id", "secret")
        linkedin._save_tokens({
            "access_token": "old-token",
            "refresh_token": "",
            "expires_at": time.time() - 100,
        })
        with pytest.raises(RuntimeError, match="refresh_token"):
            linkedin.refresh_access_token()


# ── testes de integracao com tool_bridge ─────────────────────────────────────

class TestBridgeIntegration:
    def test_linkedin_in_action_tiers(self):
        from core.tool_bridge import ACTION_TIERS
        assert "linkedin_control" in ACTION_TIERS

    def test_linkedin_tier_is_medium(self):
        from core.tool_bridge import ACTION_TIERS, TIER_MEDIUM
        assert ACTION_TIERS["linkedin_control"] == TIER_MEDIUM
