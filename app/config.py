from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    api_token: str = Field(..., alias="PLATFORM_API_TOKEN")
    pve_host: str = Field(..., alias="PVE_HOST")
    pve_node: str = Field(..., alias="PVE_NODE")
    pve_verify_ssl: bool = Field(default=True, alias="PVE_VERIFY_SSL")
    pve_api_token_id: str = Field(..., alias="PVE_API_TOKEN_ID")
    pve_api_token_secret: str = Field(..., alias="PVE_API_TOKEN_SECRET")

    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    db_path: str = Field(default="./data/platform.db", alias="PLATFORM_DB_PATH")
    snippet_dir: str = Field(
        default="/var/lib/vz/snippets",
        alias="PVE_SNIPPET_DIR",
    )
    snippet_storage: str = Field(default="local", alias="PVE_SNIPPET_STORAGE")
    default_storage: str = Field(default="local-lvm", alias="PVE_DEFAULT_STORAGE")
    default_bridge: str = Field(default="vmbr0", alias="PVE_DEFAULT_BRIDGE")
    nat_enabled: bool = Field(default=True, alias="PVE_NAT_ENABLED")
    nat_bridge: str = Field(default="nat0", alias="PVE_NAT_BRIDGE")
    nat_network_cidr: str = Field(default="192.168.0.0/24", alias="PVE_NAT_NETWORK")
    nat_host_ip: str = Field(default="192.168.0.254", alias="PVE_NAT_HOST_IP")
    nat_port_start: int = Field(default=30001, alias="PVE_NAT_PORT_START")
    nat_ports_per_vm: int = Field(default=10, alias="PVE_NAT_PORTS_PER_VM")
    nat_nameserver: str = Field(default="8.8.8.8", alias="PVE_NAT_NAMESERVER")
    nat_uplink_interface: str = Field(default="", alias="PVE_NAT_UPLINK_INTERFACE")
    nat_ingress_interfaces: str = Field(default="", alias="PVE_NAT_INGRESS_INTERFACES")
    image_templates: dict[str, int] = Field(
        default_factory=dict,
        alias="PVE_IMAGE_TEMPLATES",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
