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
    default_storage: str = Field(default="local-lvm", alias="PVE_DEFAULT_STORAGE")
    default_bridge: str = Field(default="vmbr0", alias="PVE_DEFAULT_BRIDGE")
    image_templates: dict[str, int] = Field(
        default_factory=dict,
        alias="PVE_IMAGE_TEMPLATES",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
