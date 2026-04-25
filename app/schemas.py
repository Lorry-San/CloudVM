from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VmNetworkConfig(BaseModel):
    mode: Literal["public", "nat"] = "public"
    bridge: str | None = None
    model: Literal["virtio", "e1000", "rtl8139", "vmxnet3"] = "virtio"
    rate: float | None = Field(
        default=None,
        gt=0,
        description="PVE net rate limit value, same unit as qemu net[n] rate.",
    )
    vlan_tag: int | None = Field(default=None, ge=1, le=4094)
    firewall: bool | None = None


class VmCreateRequest(BaseModel):
    vmid: int | None = None
    name: str
    image: str | None = None
    template_vmid: int | None = None
    cores: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=1024, ge=256)
    disk_gb: int | None = Field(default=None, ge=1)
    storage: str | None = None
    bridge: str | None = None
    network: VmNetworkConfig | None = None
    boot_order: str | None = Field(
        default=None,
        description="PVE boot order, for example: scsi0;ide2;net0",
    )
    ci_user: str | None = None
    ci_password: str | None = None
    ssh_keys: str | None = None
    ip_config: str | None = Field(
        default=None,
        description="PVE ipconfig0 value, for example: ip=dhcp",
    )
    nameserver: str | None = None
    searchdomain: str | None = None
    allocate_ip: bool = True
    owner: str | None = None
    expires_at: datetime | None = None
    traffic_limit_gb: float | None = Field(default=None, gt=0)
    traffic_reset_day: int = Field(default=1, ge=1, le=28)
    traffic_reset_hour: int = Field(default=0, ge=0, le=23)
    traffic_reset_timezone: str = "Asia/Shanghai"
    start: bool = True


class VmActionResponse(BaseModel):
    vmid: int
    task: str | None = None
    start_task: str | None = None
    allocated_ip: str | None = None
    nat_ip: str | None = None
    ssh_port: int | None = None
    port_range_start: int | None = None
    port_range_end: int | None = None
    network_mode: Literal["public", "nat"] | None = None
    released_ip: str | None = None
    status: str = "accepted"


class ImageTemplateResponse(BaseModel):
    image: str
    template_vmid: int


class VmExpirationRequest(BaseModel):
    expires_at: datetime | None = None
    action: Literal["pause", "delete"] = "pause"


class VmCredentialsRequest(BaseModel):
    username: str | None = None
    password: str | None = None


class VmCredentialsResponse(BaseModel):
    vmid: int
    username_saved: bool
    password_saved: bool


class VmPasswordUpdateRequest(BaseModel):
    username: str | None = None
    password: str = Field(min_length=1)
    reboot: bool = True


class VmConfigUpdateRequest(BaseModel):
    cores: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=256)
    network_rate: float | None = Field(default=None, gt=0)
    reboot: bool = True


class VmReinstallRequest(BaseModel):
    image: str | None = None
    template_vmid: int | None = Field(default=None, ge=1)
    slot: str | None = Field(default=None, description="Target disk slot, usually virtio0.")
    template_slot: str | None = None
    storage: str | None = None
    disk_size: str | None = Field(default=None, description="Final disk size, for example 40G.")
    ci_user: str | None = None
    password: str | None = None
    nameserver: str | None = None
    start: bool = True
    free_old: bool = True
    dry_run: bool = False


class VmTrafficConfigRequest(BaseModel):
    quota_gb: float | None = Field(default=None, gt=0)
    reset_day: int = Field(default=1, ge=1, le=28)
    reset_hour: int = Field(default=0, ge=0, le=23)
    timezone: str = "Asia/Shanghai"
    reset_usage: bool = False


class VmTrafficConfigResponse(BaseModel):
    vmid: int
    quota_gb: float | None = None
    reset_day: int
    reset_hour: int
    timezone: str
    used_gb: float = 0
    remaining_gb: float | None = None
    percent: float | None = None
    next_reset_at: datetime
    baseline_at: datetime | None = None


class ConsoleSessionResponse(BaseModel):
    vmid: int | None = None
    node: str
    console: Literal["vnc", "xterm"]
    websocket_url: str
    note: str = "Use this platform URL only. PVE host, ticket, and backend URL are not exposed."


class ConsoleTokenRequest(BaseModel):
    vmid: int = Field(description="VMID this console token can open.")
    ttl_seconds: int = Field(default=900, ge=60, le=900)


class ConsoleTokenResponse(BaseModel):
    token: str
    expires_at: datetime
    vmid: int
    console_url: str


class IpPoolAddRequest(BaseModel):
    addresses: list[str] = Field(default_factory=list)
    range: str | None = Field(
        default=None,
        description="CIDR range to import, for example: 203.0.113.32/29",
    )
    cidr: int = Field(ge=1, le=128)
    gateway: str
    nameserver: str | None = None
    bridge: str | None = None
    note: str | None = None


class IpPoolAddress(BaseModel):
    address: str
    cidr: int
    gateway: str
    nameserver: str | None = None
    bridge: str | None = None
    status: Literal["available", "allocated", "reserved"] = "available"
    vmid: int | None = None
    note: str | None = None


class IpLease(BaseModel):
    address: str
    cidr: int
    gateway: str
    nameserver: str | None = None
    bridge: str | None = None
    ip_config: str


class NatLease(BaseModel):
    vmid: int
    address: str
    cidr: int
    gateway: str
    nameserver: str | None = None
    bridge: str
    host_ip: str
    external_host: str
    port_start: int
    port_end: int
    ssh_port: int
    ip_config: str
