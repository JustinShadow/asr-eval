"""LDAP 组角色映射、过滤器转义和连接流程单元测试。"""

import pytest

from app.core import ldap as ldap_adapter
from app.core.config import get_settings
from app.core.ldap import LDAPInvalidCredentials, authenticate_ldap, role_from_groups


def test_role_mapping_uses_highest_matching_group() -> None:
    groups = ["CN=voice-testers,OU=Groups,DC=example,DC=org", "platform-engineering"]
    mapping = {
        "tester": ["voice-testers"],
        "engineer": ["platform-engineering"],
        "admin": ["platform-admins"],
    }

    assert role_from_groups(groups, mapping) == "engineer"
    assert role_from_groups([], mapping) == "viewer"


def test_ldap_timeout_matches_integer_socket_timeout_contract() -> None:
    assert get_settings().ldap_connect_timeout_s == 5
    assert isinstance(get_settings().ldap_connect_timeout_s, int)


def test_filter_input_is_escaped() -> None:
    assert ldap_adapter._escape_filter_value("a*)(b\\c\x00") == r"a\2a\29\28b\5cc\00"


def test_dn_input_is_escaped() -> None:
    assert ldap_adapter._escape_dn_value(" #a,b+ c ") == r"\ #a\,b\+ c\ "


def test_ldap_bind_and_group_lookup_use_configured_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ldap_server_url", "ldaps://ldap.example.org:636")
    monkeypatch.setattr(settings, "ldap_bind_dn", "cn=service,dc=example,dc=org")
    monkeypatch.setattr(settings, "ldap_bind_password", "service-secret")
    monkeypatch.setattr(settings, "ldap_user_base_dn", "ou=people,dc=example,dc=org")
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username})")
    monkeypatch.setattr(settings, "ldap_group_base_dn", "ou=groups,dc=example,dc=org")
    monkeypatch.setattr(settings, "ldap_tls_valid_names", ["ldap.example.org"])

    class Entry:
        def __init__(self, dn: str, attributes: dict[str, object]) -> None:
            self.entry_dn = dn
            self.entry_attributes_as_dict = attributes

    class FakeConnection:
        def __init__(self, _server: object, *, user: str, password: str, **_kwargs: object) -> None:
            self.user = user
            self.password = password
            self.entries: list[Entry] = []
            self.result: dict[str, str] = {"description": "success"}
            self.closed = True

        def open(self) -> None:
            self.closed = False

        def bind(self) -> bool:
            if self.user.startswith("uid=") and self.password == "wrong":
                self.result = {"description": "invalidCredentials"}
                return False
            return True

        def start_tls(self) -> bool:
            return True

        def search(self, _base: str, query: str, **_kwargs: object) -> bool:
            if "objectClass=groupOfNames" in query:
                self.entries = [Entry("cn=voice-testers,ou=groups", {"cn": ["voice-testers"]})]
            else:
                self.entries = [
                    Entry(
                        "uid=alice,ou=people,dc=example,dc=org",
                        {
                            "displayName": ["Alice Zhang"],
                            "mail": ["alice@example.org"],
                            "memberOf": ["cn=platform-engineering,ou=groups"],
                        },
                    )
                ]
            return True

        def unbind(self) -> bool:
            return True

    tls_options: dict[str, object] = {}
    monkeypatch.setattr(
        ldap_adapter.ldap3,
        "Tls",
        lambda **kwargs: tls_options.update(kwargs) or object(),
    )
    monkeypatch.setattr(ldap_adapter.ldap3, "Server", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ldap_adapter.ldap3, "Connection", FakeConnection)

    identity = authenticate_ldap("alice", "correct-password")
    assert identity.dn == "uid=alice,ou=people,dc=example,dc=org"
    assert identity.display_name == "Alice Zhang"
    assert identity.email == "alice@example.org"
    assert "voice-testers" in identity.groups
    assert tls_options["valid_names"] == ["ldap.example.org"]

    with pytest.raises(LDAPInvalidCredentials):
        authenticate_ldap("alice", "wrong")


def test_ldap_direct_user_dn_bind_uses_tls_and_handles_invalid_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://ldap.example.org")
    monkeypatch.setattr(settings, "ldap_bind_dn", None)
    monkeypatch.setattr(settings, "ldap_bind_password", None)
    monkeypatch.setattr(settings, "ldap_user_base_dn", None)
    monkeypatch.setattr(
        settings,
        "ldap_user_dn_template",
        "cn={username},cn=users,dc=etsus,dc=net",
    )
    monkeypatch.setattr(settings, "ldap_tls_valid_names", ["openldap-host"])

    tls_started: list[bool] = []

    class DirectConnection:
        def __init__(self, _server: object, *, user: str, password: str, **_kwargs: object) -> None:
            self.user = user
            self.password = password
            self.closed = True
            self.result: dict[str, str] = {"description": "success"}
            self.tls_started = False

        def open(self) -> None:
            self.closed = False

        def start_tls(self) -> bool:
            self.tls_started = True
            tls_started.append(True)
            return True

        def bind(self) -> bool:
            if self.password != "correct-password":
                self.result = {"description": "invalidCredentials"}
                return False
            return True

        def unbind(self) -> bool:
            return True

    monkeypatch.setattr(ldap_adapter.ldap3, "Tls", lambda **_kwargs: object())
    monkeypatch.setattr(ldap_adapter.ldap3, "Server", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ldap_adapter.ldap3, "Connection", DirectConnection)

    identity = authenticate_ldap("jichuanjiang", "correct-password")
    assert identity.dn == "cn=jichuanjiang,cn=users,dc=etsus,dc=net"
    assert identity.display_name == "jichuanjiang"
    assert identity.groups == []
    assert tls_started == [True]

    with pytest.raises(LDAPInvalidCredentials):
        authenticate_ldap("jichuanjiang", "wrong-password")


def test_plain_ldap_is_only_allowed_when_explicitly_enabled_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://ldap.example.org")
    monkeypatch.setattr(settings, "ldap_bind_dn", None)
    monkeypatch.setattr(settings, "ldap_bind_password", None)
    monkeypatch.setattr(settings, "ldap_user_base_dn", None)
    monkeypatch.setattr(settings, "ldap_user_dn_template", "cn={username},dc=example,dc=org")
    monkeypatch.setattr(settings, "ldap_start_tls", False)
    monkeypatch.setattr(settings, "ldap_allow_insecure", True)

    monkeypatch.setattr(ldap_adapter.ldap3, "Tls", lambda **_kwargs: object())
    monkeypatch.setattr(ldap_adapter.ldap3, "Server", lambda *_args, **_kwargs: object())
    server, use_ssl = ldap_adapter._server_and_security()
    assert server is not None
    assert use_ssl is False

    monkeypatch.setattr(settings, "app_env", "prod")
    with pytest.raises(ldap_adapter.LDAPBackendUnavailable):
        ldap_adapter._server_and_security()
