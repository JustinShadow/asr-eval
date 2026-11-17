"""LDAP 目录认证适配器；登录源和组映射按 M7 §2.1 配置。"""

import ssl
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import ldap3  # type: ignore[import-untyped]  # ldap3 暂无类型标注，边界由单元测试覆盖

from app.core.config import get_settings


class LDAPBackendUnavailable(Exception):
    """目录服务或其配置不可用；不得回退到普通本地账号。"""


class LDAPInvalidCredentials(Exception):
    """目录服务明确拒绝了用户凭证。"""


@dataclass(frozen=True)
class LDAPIdentity:
    """通过目录认证后读取到的用户属性与组。"""

    dn: str
    display_name: str | None
    email: str | None
    groups: list[str]


ROLE_ORDER = {"viewer": 0, "tester": 1, "engineer": 2, "admin": 3}


def role_from_groups(groups: list[str], mapping: dict[str, list[str]]) -> str:
    """按组映射取最高角色；未命中任何组时使用 viewer。"""
    normalized_groups: set[str] = set()
    for group in groups:
        normalized_groups.add(group.strip().casefold())
        first_rdn = group.split(",", 1)[0]
        if "=" in first_rdn:
            normalized_groups.add(first_rdn.split("=", 1)[1].strip().casefold())

    matched = [
        role
        for role, role_groups in mapping.items()
        if role in ROLE_ORDER
        and any(group.strip().casefold() in normalized_groups for group in role_groups)
    ]
    return max(matched, key=ROLE_ORDER.__getitem__, default="viewer")


def _escape_filter_value(value: str) -> str:
    """按 LDAP filter 规则转义用户输入，避免用户名改变过滤器结构。"""
    return (
        value.replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )


def _escape_dn_value(value: str) -> str:
    """按 RFC 4514 转义 DN 属性值，避免用户名改变目标 DN 结构。"""
    escaped: list[str] = []
    for index, char in enumerate(value):
        if char == "\x00":
            escaped.append(r"\00")
        elif (
            char in ',+"\\<>;='
            or (index == 0 and char in " #")
            or (index == len(value) - 1 and char == " ")
        ):
            escaped.append(f"\\{char}")
        else:
            escaped.append(char)
    return "".join(escaped)


def _attribute_values(attributes: dict[str, object], name: str) -> list[str]:
    """兼容 LDAP 属性名单值、多值与大小写差异。"""
    value = next(
        (value for key, value in attributes.items() if key.casefold() == name.casefold()), None
    )
    if value is None:
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    return [
        item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        for item in values
    ]


def _server_and_security() -> tuple[Any, bool]:
    """解析 LDAP 地址并要求 LDAPS 或 StartTLS，避免明文发送密码。"""
    settings = get_settings()
    service_config = (
        settings.ldap_bind_dn,
        settings.ldap_bind_password,
        settings.ldap_user_base_dn,
    )
    service_mode_configured = any(service_config)
    if service_mode_configured and not all(service_config):
        raise LDAPBackendUnavailable("LDAP 服务账号或用户搜索范围配置不完整")
    if not service_mode_configured and not settings.ldap_user_dn_template:
        raise LDAPBackendUnavailable("LDAP 用户 bind 配置未完成")
    if not settings.ldap_server_url:
        raise LDAPBackendUnavailable("LDAP 地址未配置")
    parsed = urlsplit(settings.ldap_server_url)
    if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
        raise LDAPBackendUnavailable("LDAP 地址格式不正确")
    use_ssl = parsed.scheme == "ldaps"
    if (
        not use_ssl
        and not settings.ldap_start_tls
        and (settings.app_env != "dev" or not settings.ldap_allow_insecure)
    ):
        raise LDAPBackendUnavailable("LDAP 明文连接仅允许开发环境显式启用")
    # M7 §2.1 默认要求 TLS；P0 临时兼容仅由 dev + LDAP_ALLOW_INSECURE 双重显式配置放行。
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED,
        ca_certs_file=str(settings.ldap_ca_cert_file) if settings.ldap_ca_cert_file else None,
        valid_names=settings.ldap_tls_valid_names or None,
    )
    server = ldap3.Server(
        parsed.hostname,
        port=parsed.port or (636 if use_ssl else 389),
        use_ssl=use_ssl,
        tls=tls,
        connect_timeout=settings.ldap_connect_timeout_s,
        get_info=ldap3.NONE,
    )
    return server, use_ssl


def _open_connection(server: Any, *, use_ssl: bool, user: str, password: str) -> Any:
    """建立已加密传输的 LDAP 连接；不自动跟随外部 referral。"""
    settings = get_settings()
    connection = ldap3.Connection(
        server,
        user=user,
        password=password,
        receive_timeout=settings.ldap_connect_timeout_s,
        auto_referrals=False,
        raise_exceptions=False,
    )
    try:
        connection.open()
        if connection.closed:
            raise LDAPBackendUnavailable("LDAP 连接失败")
        if not use_ssl and settings.ldap_start_tls and not connection.start_tls():
            raise LDAPBackendUnavailable("LDAP StartTLS 协商失败")
        return connection
    except Exception:
        # TLS 握手已断开时 unbind 可能再次抛错，需保留首次连接错误供上层归类。
        with suppress(Exception):
            connection.unbind()
        raise


def _authenticate_user_dn_template(
    server: Any, *, use_ssl: bool, username: str, password: str, template: str
) -> LDAPIdentity:
    """按目录提供的 DN 模板直接 bind；无组声明时由 M7 默认映射为 viewer。"""
    try:
        user_dn = template.format(username=_escape_dn_value(username))
    except (KeyError, ValueError) as exc:
        raise LDAPBackendUnavailable("LDAP 用户 DN 模板格式不正确") from exc

    connection: Any = None
    try:
        connection = _open_connection(server, use_ssl=use_ssl, user=user_dn, password=password)
        if not connection.bind():
            if _result_description(connection) == "invalidcredentials":
                raise LDAPInvalidCredentials
            raise LDAPBackendUnavailable("LDAP 用户 bind 失败")
        return LDAPIdentity(dn=user_dn, display_name=username, email=None, groups=[])
    finally:
        if connection is not None:
            connection.unbind()


def _result_description(connection: Any) -> str:
    """读取 LDAP 返回码描述，不暴露服务端诊断或凭证。"""
    result = getattr(connection, "result", {})
    if isinstance(result, dict):
        return str(result.get("description", "")).casefold()
    return ""


def authenticate_ldap(username: str, password: str) -> LDAPIdentity:
    """按服务账号搜索或目录提供的 DN 模板校验 LDAP 用户。"""
    service_connection: Any = None
    user_connection: Any = None
    try:
        server, use_ssl = _server_and_security()
        settings = get_settings()
        if not settings.ldap_bind_dn:
            if not settings.ldap_user_dn_template:
                raise LDAPBackendUnavailable("LDAP 用户 DN 模板未配置")
            return _authenticate_user_dn_template(
                server,
                use_ssl=use_ssl,
                username=username,
                password=password,
                template=settings.ldap_user_dn_template,
            )
        service_connection = _open_connection(
            server,
            use_ssl=use_ssl,
            user=str(settings.ldap_bind_dn),
            password=str(settings.ldap_bind_password),
        )
        if not service_connection.bind():
            raise LDAPBackendUnavailable("LDAP 服务账号 bind 失败")
        user_filter = settings.ldap_user_filter.format(username=_escape_filter_value(username))
        if not service_connection.search(
            settings.ldap_user_base_dn,
            user_filter,
            search_scope=ldap3.SUBTREE,
            attributes=["displayName", "cn", "mail", "memberOf"],
        ):
            raise LDAPBackendUnavailable("LDAP 用户查询失败")
        if len(service_connection.entries) != 1:
            raise LDAPInvalidCredentials
        entry = service_connection.entries[0]
        dn = str(entry.entry_dn)
        attributes = entry.entry_attributes_as_dict
        groups = _attribute_values(attributes, "memberOf")

        if settings.ldap_group_base_dn:
            group_filter = f"(&(objectClass=groupOfNames)(member={_escape_filter_value(dn)}))"
            if not service_connection.search(
                settings.ldap_group_base_dn,
                group_filter,
                search_scope=ldap3.SUBTREE,
                attributes=["cn"],
            ):
                raise LDAPBackendUnavailable("LDAP 组查询失败")
            groups.extend(
                value
                for group_entry in service_connection.entries
                for value in _attribute_values(group_entry.entry_attributes_as_dict, "cn")
            )

        user_connection = _open_connection(
            server,
            use_ssl=use_ssl,
            user=dn,
            password=password,
        )
        if not user_connection.bind():
            if _result_description(user_connection) == "invalidcredentials":
                raise LDAPInvalidCredentials
            raise LDAPBackendUnavailable("LDAP 用户 bind 失败")

        display_name = next(
            iter(
                _attribute_values(attributes, "displayName") or _attribute_values(attributes, "cn")
            ),
            None,
        )
        email = next(iter(_attribute_values(attributes, "mail")), None)
        return LDAPIdentity(dn=dn, display_name=display_name, email=email, groups=groups)
    except (LDAPBackendUnavailable, LDAPInvalidCredentials):
        raise
    except Exception as exc:
        raise LDAPBackendUnavailable("LDAP 服务不可用") from exc
    finally:
        if user_connection is not None:
            user_connection.unbind()
        if service_connection is not None:
            service_connection.unbind()
