from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_VERSION: str = "2025-04-01-preview"
DEFAULT_BACKUP_DIR: Path = Path(".tmp/project-connection-backups")
MANAGEMENT_SCOPE: str = "https://management.azure.com/.default"
ARM_CONNECTION_ID_PATTERN: re.Pattern[str] = re.compile(
    (
        r"^/subscriptions/(?P<subscription_id>[^/]+)/resourceGroups/"
        r"(?P<resource_group_name>[^/]+)/providers/Microsoft\.CognitiveServices/"
        r"accounts/(?P<account_name>[^/]+)/projects/(?P<project_name>[^/]+)/"
        r"connections/(?P<connection_name>[^/]+)$"
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArmConnectionIdentity:
    """保存 Project Connection 的 ARM 标识信息。"""

    subscription_id: str
    resource_group_name: str
    account_name: str
    project_name: str
    connection_name: str


@dataclass(frozen=True)
class ResetConnectionConfig:
    """保存重建 Project Connection 所需的全部输入。"""

    arm_identity: ArmConnectionIdentity
    target_url: str | None
    client_id: str | None
    client_secret: str | None
    authorization_url: str | None
    token_url: str | None
    refresh_url: str | None
    scopes: list[str] | None
    api_version: str
    backup_dir: Path
    yes: bool
    dry_run: bool


def build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。

    参数：
      无。
    返回：
      配置好的 argparse.ArgumentParser 实例。
    """

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "删除并重建 Azure AI Foundry 的 MCP Project Connection，"
            "用于强制重新触发 OAuth consent。"
        )
    )
    parser.add_argument(
        "--project-connection-id",
        help="完整的 Project Connection ARM ID；若提供则优先从这里解析订阅/资源组/项目等信息。",
    )
    parser.add_argument("--subscription-id", help="Azure Subscription ID。")
    parser.add_argument("--resource-group", help="Foundry 所在的 Resource Group。")
    parser.add_argument("--account-name", help="Foundry Account 名称。")
    parser.add_argument("--project-name", help="Foundry Project 名称。")
    parser.add_argument("--connection-name", help="Project Connection 名称。")
    parser.add_argument(
        "--target-url",
        help="Remote MCP Server endpoint，例如 https://example.com/mcp 。",
    )
    parser.add_argument("--client-id", help="OAuth Client ID。")
    parser.add_argument("--client-secret", help="OAuth Client Secret，可选。")
    parser.add_argument("--authorization-url", help="OAuth Auth URL。")
    parser.add_argument("--token-url", help="OAuth Token URL。")
    parser.add_argument("--refresh-url", help="OAuth Refresh URL。")
    parser.add_argument(
        "--scopes",
        help="OAuth scopes，支持空格或逗号分隔。",
    )
    parser.add_argument(
        "--api-version",
        default=DEFAULT_API_VERSION,
        help=f"ARM API version，默认值为 {DEFAULT_API_VERSION}。",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(DEFAULT_BACKUP_DIR),
        help=f"现有连接备份文件目录，默认值为 {DEFAULT_BACKUP_DIR}。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认，直接执行删除并重建。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要发送的请求体，不实际删除或创建连接。",
    )
    return parser


def read_setting(
    cli_value: str | None,
    env_names: list[str],
    field_name: str,
    required: bool = True,
) -> str | None:
    """按“命令行优先、环境变量兜底”的顺序读取配置值。

    参数：
      cli_value：命令行传入的值。
      env_names：可回退读取的环境变量名称列表。
      field_name：报错时展示的人类可读字段名。
      required：是否必填。
    返回：
      解析得到的字符串；若字段非必填且未找到，则返回 None。
    """

    candidate: str | None = cli_value.strip() if cli_value and cli_value.strip() else None
    if candidate:
        return candidate

    env_name: str
    env_value: str | None
    for env_name in env_names:
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            return env_value.strip()

    if required:
        joined_names: str = ", ".join(env_names) if env_names else "无环境变量兜底"
        raise ValueError(f"缺少必填字段 {field_name}。请通过命令行传入，或设置环境变量：{joined_names}")
    return None


def parse_connection_resource_id(project_connection_id: str) -> ArmConnectionIdentity:
    """解析 Project Connection ARM ID。

    参数：
      project_connection_id：完整的 Project Connection ARM 资源 ID。
    返回：
      解析后的 ArmConnectionIdentity。
    """

    match: re.Match[str] | None = ARM_CONNECTION_ID_PATTERN.match(project_connection_id)
    if not match:
        raise ValueError(
            "MCP Project Connection ID 格式不正确，"
            "预期为 /subscriptions/.../resourceGroups/.../providers/"
            "Microsoft.CognitiveServices/accounts/.../projects/.../connections/... 。"
        )

    groups: dict[str, str] = match.groupdict()
    arm_identity: ArmConnectionIdentity = ArmConnectionIdentity(
        subscription_id=groups["subscription_id"],
        resource_group_name=groups["resource_group_name"],
        account_name=groups["account_name"],
        project_name=groups["project_name"],
        connection_name=groups["connection_name"],
    )
    return arm_identity


def parse_scopes(raw_scopes: str) -> list[str]:
    """把 scopes 字符串解析为列表。

    参数：
      raw_scopes：空格或逗号分隔的 scope 字符串。
    返回：
      去重后且保留原始顺序的 scope 列表。
    """

    tokens: list[str] = [item.strip() for item in re.split(r"[\s,]+", raw_scopes) if item.strip()]
    deduplicated_scopes: list[str] = list(dict.fromkeys(tokens))
    if not deduplicated_scopes:
        raise ValueError("Scopes 不能为空。")
    return deduplicated_scopes


def resolve_arm_identity(arguments: argparse.Namespace) -> ArmConnectionIdentity:
    """从连接 ID 或离散参数中解析 ARM 标识。

    参数：
      arguments：argparse 解析后的命名空间对象。
    返回：
      完整的 ArmConnectionIdentity。
    """

    project_connection_id: str | None = read_setting(
        cli_value=arguments.project_connection_id,
        env_names=["MCP_PROJECT_CONNECTION_ID"],
        field_name="Project Connection ID",
        required=False,
    )
    if project_connection_id:
        parsed_identity: ArmConnectionIdentity = parse_connection_resource_id(project_connection_id)

        explicit_connection_name: str | None = (
            arguments.connection_name.strip()
            if arguments.connection_name and arguments.connection_name.strip()
            else None
        )
        if explicit_connection_name and explicit_connection_name != parsed_identity.connection_name:
            raise ValueError(
                "connection-name 与 project-connection-id 中解析出来的连接名不一致。"
            )
        return parsed_identity

    subscription_id: str = read_setting(
        cli_value=arguments.subscription_id,
        env_names=["AZURE_SUBSCRIPTION_ID", "foundry_subscription_id"],
        field_name="Subscription ID",
    ) or ""
    resource_group_name: str = read_setting(
        cli_value=arguments.resource_group,
        env_names=["AZURE_RESOURCE_GROUP", "foundry_resource_group"],
        field_name="Resource Group",
    ) or ""
    account_name: str = read_setting(
        cli_value=arguments.account_name,
        env_names=["FOUNDRY_ACCOUNT_NAME", "foundry_account_name"],
        field_name="Foundry Account Name",
    ) or ""
    project_name: str = read_setting(
        cli_value=arguments.project_name,
        env_names=["FOUNDRY_PROJECT_NAME", "foundry_project_name"],
        field_name="Foundry Project Name",
    ) or ""
    connection_name: str = read_setting(
        cli_value=arguments.connection_name,
        env_names=["MCP_TOOL_SERVER_NAME"],
        field_name="Connection Name",
    ) or ""

    arm_identity: ArmConnectionIdentity = ArmConnectionIdentity(
        subscription_id=subscription_id,
        resource_group_name=resource_group_name,
        account_name=account_name,
        project_name=project_name,
        connection_name=connection_name,
    )
    return arm_identity


def load_reset_connection_config(arguments: argparse.Namespace) -> ResetConnectionConfig:
    """把命令行参数和环境变量整合为脚本运行配置。

    参数：
      arguments：argparse 解析后的命名空间对象。
    返回：
      ResetConnectionConfig 配置对象。
    """

    arm_identity: ArmConnectionIdentity = resolve_arm_identity(arguments)
    target_url: str | None = read_setting(
        cli_value=arguments.target_url,
        env_names=["MCP_TOOL_SERVER_URL"],
        field_name="Remote MCP Server endpoint",
        required=False,
    )
    client_id: str | None = read_setting(
        cli_value=arguments.client_id,
        env_names=["MCP_OAUTH_CLIENT_ID"],
        field_name="OAuth Client ID",
        required=False,
    )
    client_secret: str | None = read_setting(
        cli_value=arguments.client_secret,
        env_names=["MCP_OAUTH_CLIENT_SECRET"],
        field_name="OAuth Client Secret",
        required=False,
    )
    authorization_url: str | None = read_setting(
        cli_value=arguments.authorization_url,
        env_names=["MCP_OAUTH_AUTH_URL"],
        field_name="OAuth Auth URL",
        required=False,
    )
    token_url: str | None = read_setting(
        cli_value=arguments.token_url,
        env_names=["MCP_OAUTH_TOKEN_URL"],
        field_name="OAuth Token URL",
        required=False,
    )
    refresh_url: str | None = read_setting(
        cli_value=arguments.refresh_url,
        env_names=["MCP_OAUTH_REFRESH_URL"],
        field_name="OAuth Refresh URL",
        required=False,
    )
    raw_scopes: str | None = read_setting(
        cli_value=arguments.scopes,
        env_names=["MCP_OAUTH_SCOPES"],
        field_name="OAuth Scopes",
        required=False,
    )
    scopes: list[str] | None = parse_scopes(raw_scopes) if raw_scopes else None
    backup_dir: Path = Path(arguments.backup_dir).expanduser()

    config: ResetConnectionConfig = ResetConnectionConfig(
        arm_identity=arm_identity,
        target_url=target_url,
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=authorization_url,
        token_url=token_url,
        refresh_url=refresh_url,
        scopes=scopes,
        api_version=arguments.api_version,
        backup_dir=backup_dir,
        yes=bool(arguments.yes),
        dry_run=bool(arguments.dry_run),
    )
    return config


def build_connection_url(arm_identity: ArmConnectionIdentity, api_version: str) -> str:
    """构建 ARM Project Connection 管理接口 URL。

    参数：
      arm_identity：连接资源的 ARM 标识。
      api_version：调用 ARM 管理接口使用的版本号。
    返回：
      可直接发送 HTTP 请求的完整 URL。
    """

    url: str = (
        "https://management.azure.com/subscriptions/"
        f"{arm_identity.subscription_id}/resourceGroups/{arm_identity.resource_group_name}"
        "/providers/Microsoft.CognitiveServices/accounts/"
        f"{arm_identity.account_name}/projects/{arm_identity.project_name}/connections/"
        f"{arm_identity.connection_name}?api-version={api_version}"
    )
    return url


def read_existing_property(
    existing_properties: dict[str, Any],
    property_names: list[str],
) -> Any:
    """按候选字段名顺序从现有连接属性中读取值。

    参数：
      existing_properties：现有连接的 properties 字典。
      property_names：候选字段名列表，按优先顺序匹配。
    返回：
      找到的字段值；若都不存在，则返回 None。
    """

    property_name: str
    for property_name in property_names:
        if property_name in existing_properties:
            return existing_properties[property_name]
    return None


def normalize_existing_scopes(existing_scope_value: Any) -> list[str] | None:
    """把现有连接返回的 scopes 格式统一转换成标准列表。

    参数：
      existing_scope_value：现有连接中的 scopes 字段，可能是字符串、字符串数组或空值。
    返回：
      标准化后的 scope 列表；若无法解析到有效值，则返回 None。
    """

    if existing_scope_value is None:
        return None
    if isinstance(existing_scope_value, str):
        return parse_scopes(existing_scope_value)
    if isinstance(existing_scope_value, list):
        merged_scope_text: str = " ".join(
            str(item).strip() for item in existing_scope_value if str(item).strip()
        )
        if merged_scope_text:
            return parse_scopes(merged_scope_text)
    return None


def require_effective_value(
    explicit_value: Any,
    fallback_value: Any,
    field_name: str,
) -> Any:
    """从显式值或现有连接中解析最终值，并在都缺失时抛错。

    参数：
      explicit_value：命令行或环境变量提供的值。
      fallback_value：从现有连接读取到的值。
      field_name：报错时展示的人类可读字段名。
    返回：
      最终采用的值。
    """

    if explicit_value is not None:
        return explicit_value
    if fallback_value is not None:
        return fallback_value
    raise ValueError(
        f"缺少必填字段 {field_name}，且无法从现有连接配置中自动继承。"
    )


def build_connection_payload_from_existing(
    config: ResetConnectionConfig,
    existing_connection: dict[str, Any] | None,
) -> dict[str, Any]:
    """基于现有连接配置和显式覆盖值构建最终重建请求体。

    参数：
      config：脚本运行配置对象。
      existing_connection：删除前读取到的连接 JSON，可为空。
    返回：
      可以直接传给 ARM PUT 接口的 JSON 请求体。
    """

    existing_properties: dict[str, Any] = (
        existing_connection.get("properties", {})
        if existing_connection is not None
        else {}
    )
    existing_credentials: dict[str, Any] = (
        read_existing_property(existing_properties, ["credentials", "Credentials"]) or {}
    )

    target_url: str = require_effective_value(
        explicit_value=config.target_url,
        fallback_value=read_existing_property(existing_properties, ["target"]),
        field_name="Remote MCP Server endpoint",
    )
    client_id: str = require_effective_value(
        explicit_value=config.client_id,
        fallback_value=read_existing_property(existing_credentials, ["clientId", "ClientId"]),
        field_name="OAuth Client ID",
    )
    client_secret: str | None = require_effective_value(
        explicit_value=config.client_secret,
        fallback_value=read_existing_property(existing_credentials, ["clientSecret", "ClientSecret"]),
        field_name="OAuth Client Secret",
    )
    authorization_url: str = require_effective_value(
        explicit_value=config.authorization_url,
        fallback_value=read_existing_property(existing_properties, ["authorizationUrl", "AuthorizationUrl"]),
        field_name="OAuth Auth URL",
    )
    token_url: str = require_effective_value(
        explicit_value=config.token_url,
        fallback_value=read_existing_property(existing_properties, ["tokenUrl", "TokenUrl"]),
        field_name="OAuth Token URL",
    )
    refresh_url: str = require_effective_value(
        explicit_value=config.refresh_url,
        fallback_value=read_existing_property(existing_properties, ["refreshUrl", "RefreshUrl", "tokenUrl", "TokenUrl"]),
        field_name="OAuth Refresh URL",
    )
    scopes: list[str] = require_effective_value(
        explicit_value=config.scopes,
        fallback_value=normalize_existing_scopes(
            read_existing_property(existing_properties, ["scopes", "Scopes"])
        ),
        field_name="OAuth Scopes",
    )
    group: str = read_existing_property(existing_properties, ["group"]) or "GenericProtocol"
    category: str = read_existing_property(existing_properties, ["category"]) or "RemoteTool"
    is_shared_to_all: bool = bool(
        read_existing_property(existing_properties, ["isSharedToAll"])
    )
    shared_user_list: list[Any] = list(
        read_existing_property(existing_properties, ["sharedUserList"]) or []
    )
    metadata: dict[str, Any] = dict(
        read_existing_property(existing_properties, ["metadata"]) or {"type": "custom_MCP"}
    )
    use_custom_connector: Any = read_existing_property(
        existing_properties,
        ["useCustomConnector"],
    )

    payload: dict[str, Any] = {
        "properties": {
            "authType": "OAuth2",
            "group": group,
            "category": category,
            "expiryTime": None,
            "target": target_url,
            "isSharedToAll": is_shared_to_all,
            "sharedUserList": shared_user_list,
            "TokenUrl": token_url,
            "AuthorizationUrl": authorization_url,
            "RefreshUrl": refresh_url,
            "Scopes": scopes,
            "Credentials": {
                "ClientId": client_id,
                "ClientSecret": client_secret,
            },
            "metadata": metadata,
        }
    }
    if use_custom_connector is not None:
        payload["properties"]["useCustomConnector"] = use_custom_connector
    return payload


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """复制并脱敏请求体中的敏感字段，便于打印日志。

    参数：
      payload：原始请求体。
    返回：
      已脱敏的新字典，不会修改原始对象。
    """

    cloned_payload: dict[str, Any] = deepcopy(payload)
    properties: dict[str, Any] = cloned_payload.get("properties", {})
    credentials: dict[str, Any] = properties.get("Credentials", {})
    if "ClientSecret" in credentials:
        credentials["ClientSecret"] = "***REDACTED***"
    return cloned_payload


def get_management_access_token() -> str:
    """获取 Azure Resource Manager 访问令牌。

    参数：
      无。
    返回：
      可用于调用 ARM 管理接口的 Bearer Token 字符串。
    """

    credential: AzureCliCredential = AzureCliCredential()
    access_token: str = credential.get_token(MANAGEMENT_SCOPE).token
    return access_token


def send_management_request(
    method: str,
    url: str,
    access_token: str,
    expected_status_codes: set[int],
    payload: dict[str, Any] | None = None,
) -> requests.Response:
    """发送 ARM 管理请求并在失败时抛出上下文充分的异常。

    参数：
      method：HTTP 方法，例如 GET、PUT、DELETE。
      url：完整请求 URL。
      access_token：Bearer Token。
      expected_status_codes：允许的 HTTP 状态码集合。
      payload：可选 JSON 请求体。
    返回：
      requests.Response 响应对象。
    """

    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response: requests.Response = requests.request(
        method=method,
        url=url,
        headers=headers,
        json=payload,
        timeout=60,
    )
    if response.status_code not in expected_status_codes:
        response_body: str = response.text.strip()
        raise RuntimeError(
            f"{method} {url} 失败，状态码为 {response.status_code}，响应内容：{response_body}"
        )
    return response


def fetch_existing_connection(url: str, access_token: str) -> dict[str, Any] | None:
    """读取当前 Project Connection，用于备份和诊断。

    参数：
      url：连接的 ARM 管理接口 URL。
      access_token：Bearer Token。
    返回：
      若连接存在，则返回连接的 JSON；若不存在，则返回 None。
    """

    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response: requests.Response = requests.get(url=url, headers=headers, timeout=60)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        response_body: str = response.text.strip()
        raise RuntimeError(
            f"读取现有连接失败，状态码为 {response.status_code}，响应内容：{response_body}"
        )
    return response.json()


def save_backup_file(
    config: ResetConnectionConfig,
    existing_connection: dict[str, Any] | None,
    recreate_payload: dict[str, Any],
) -> Path:
    """把现有连接和即将重建的请求体保存为本地备份文件。

    参数：
      config：脚本运行配置对象。
      existing_connection：删除前读取到的连接 JSON，可为空。
      recreate_payload：即将发送给 PUT 接口的请求体。
    返回：
      生成的备份文件路径。
    """

    config.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp: str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path: Path = (
        config.backup_dir
        / f"{config.arm_identity.connection_name}-{timestamp}.json"
    )
    backup_document: dict[str, Any] = {
        "created_at_utc": timestamp,
        "connection_request_url": build_connection_url(
            arm_identity=config.arm_identity,
            api_version=config.api_version,
        ),
        "existing_connection": existing_connection,
        "recreate_payload": redact_payload(recreate_payload),
    }
    backup_path.write_text(
        json.dumps(backup_document, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return backup_path


def wait_until_connection_deleted(
    url: str,
    access_token: str,
    timeout_seconds: int = 90,
    poll_interval_seconds: int = 3,
) -> None:
    """轮询确认连接已经删除完成，避免紧接着创建时遇到资源仍在释放。

    参数：
      url：连接的 ARM 管理接口 URL。
      access_token：Bearer Token。
      timeout_seconds：最长等待秒数。
      poll_interval_seconds：轮询间隔秒数。
    返回：
      无。
    """

    start_time: float = time.monotonic()
    while True:
        existing_connection: dict[str, Any] | None = fetch_existing_connection(
            url=url,
            access_token=access_token,
        )
        if existing_connection is None:
            return

        elapsed_seconds: float = time.monotonic() - start_time
        if elapsed_seconds >= timeout_seconds:
            raise TimeoutError(
                f"等待连接删除超时，{timeout_seconds} 秒后资源仍然存在。"
            )
        time.sleep(poll_interval_seconds)


def print_execution_summary(config: ResetConnectionConfig, payload: dict[str, Any]) -> None:
    """打印本次删除并重建操作的关键信息。

    参数：
      config：脚本运行配置对象。
      payload：将要发送的创建请求体。
    返回：
      无。
    """

    redacted_payload: dict[str, Any] = redact_payload(payload)
    print("即将执行的 Project Connection 重建配置：")
    print(f"- Subscription ID: {config.arm_identity.subscription_id}")
    print(f"- Resource Group: {config.arm_identity.resource_group_name}")
    print(f"- Account Name: {config.arm_identity.account_name}")
    print(f"- Project Name: {config.arm_identity.project_name}")
    print(f"- Connection Name: {config.arm_identity.connection_name}")
    print(f"- API Version: {config.api_version}")
    print(f"- Dry Run: {config.dry_run}")
    print("- Recreate Payload:")
    print(json.dumps(redacted_payload, ensure_ascii=True, indent=2))


def confirm_execution(config: ResetConnectionConfig) -> None:
    """在非 --yes 模式下向用户确认是否继续执行删除操作。

    参数：
      config：脚本运行配置对象。
    返回：
      无。
    """

    if config.yes or config.dry_run:
        return

    prompt: str = (
        f"确认删除并重建连接 {config.arm_identity.connection_name} 吗？"
        "输入 yes 继续："
    )
    answer: str = input(prompt).strip().lower()
    if answer != "yes":
        raise RuntimeError("用户取消执行。")


def recreate_project_connection(config: ResetConnectionConfig) -> None:
    """执行备份、删除、等待和重建整个流程。

    参数：
      config：脚本运行配置对象。
    返回：
      无。
    """

    connection_url: str = build_connection_url(
        arm_identity=config.arm_identity,
        api_version=config.api_version,
    )
    access_token: str = get_management_access_token()
    existing_connection: dict[str, Any] | None = fetch_existing_connection(
        url=connection_url,
        access_token=access_token,
    )
    payload: dict[str, Any] = build_connection_payload_from_existing(
        config=config,
        existing_connection=existing_connection,
    )
    print_execution_summary(config, payload)
    confirm_execution(config)

    if config.dry_run:
        print("Dry run 模式已启用，未实际删除或创建连接。")
        return

    backup_path: Path = save_backup_file(
        config=config,
        existing_connection=existing_connection,
        recreate_payload=payload,
    )
    print(f"备份文件已写入：{backup_path}")

    if existing_connection is not None:
        send_management_request(
            method="DELETE",
            url=connection_url,
            access_token=access_token,
            expected_status_codes={200, 202, 204},
        )
        print("删除请求已发送，等待连接真正删除完成...")
        wait_until_connection_deleted(
            url=connection_url,
            access_token=access_token,
        )
        print("旧连接已删除。")
    else:
        print("未找到现有连接，将直接创建新连接。")

    create_response: requests.Response = send_management_request(
        method="PUT",
        url=connection_url,
        access_token=access_token,
        expected_status_codes={200, 201},
        payload=payload,
    )
    response_json: dict[str, Any] = create_response.json()
    created_connection_id: str | None = response_json.get("id")
    created_connection_name: str | None = response_json.get("name")
    print(
        "连接已重建成功："
        f"name={created_connection_name or config.arm_identity.connection_name}, "
        f"id={created_connection_id or '未返回 id'}"
    )


def main() -> int:
    """脚本入口，负责参数解析、执行流程和统一错误处理。

    参数：
      无。
    返回：
      进程退出码；0 表示成功，1 表示失败。
    """

    parser: argparse.ArgumentParser = build_argument_parser()
    arguments: argparse.Namespace = parser.parse_args()
    try:
        config: ResetConnectionConfig = load_reset_connection_config(arguments)
        recreate_project_connection(config)
        return 0
    except Exception as error:
        print(f"脚本执行失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
