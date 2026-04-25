import ssl
import asyncio
import html
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import websockets

try:
    from Crypto.Cipher import DES
except ImportError:
    DES = None

from app.config import Settings, get_settings
from app.cloudinit import delete_network_snippet
from app.credentials import delete_vm_credentials, get_vm_credentials, save_vm_credentials
from app.db import init_db
from app.ip_pool import (
    add_ip_addresses,
    allocate_ip,
    list_ip_addresses,
    list_ip_addresses_by_vmid,
    release_ip,
    release_ip_by_vmid,
)
from app.metrics import list_metric_samples, record_metric_sample
from app.nat import (
    NatError,
    allocate_nat_lease,
    ensure_nat_ready,
    ensure_nat_rules,
    get_nat_lease,
    list_nat_leases,
    release_nat_lease,
    remove_nat_rules,
)
from app.pve_api import PveApi, PveApiError
from app.reinstall import ReinstallError, resolve_reinstall_template_vmid, run_reinstall
from app.schemas import (
    ConsoleSessionResponse,
    ConsoleTokenRequest,
    ConsoleTokenResponse,
    ImageTemplateResponse,
    IpPoolAddRequest,
    IpPoolAddress,
    VmCredentialsRequest,
    VmCredentialsResponse,
    VmActionResponse,
    VmConfigUpdateRequest,
    VmCreateRequest,
    VmExpirationRequest,
    VmReinstallRequest,
    VmTrafficConfigRequest,
    VmTrafficConfigResponse,
)
from app.security import require_api_token
from app.status import normalize_node_status, normalize_vm_status, vm_tap_traffic
from app.tasks import list_task_logs, record_task_log
from app.traffic import get_vm_traffic_usage, set_vm_traffic_config

app = FastAPI(
    title="CloudVM Platform API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
CONSOLE_TOKENS: dict[str, dict[str, int | float | None]] = {}
METRIC_SAMPLER_TASK: asyncio.Task | None = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    global METRIC_SAMPLER_TASK
    settings = get_settings()
    init_db(settings)
    if settings.nat_enabled:
        try:
            await ensure_nat_ready(settings)
            for nat_lease in list_nat_leases(settings):
                await ensure_nat_rules(settings, nat_lease)
        except Exception:
            pass
    METRIC_SAMPLER_TASK = asyncio.create_task(metric_sampler(settings))


@app.on_event("shutdown")
async def shutdown() -> None:
    if METRIC_SAMPLER_TASK:
        METRIC_SAMPLER_TASK.cancel()


def pve(settings: Settings = Depends(get_settings)) -> PveApi:
    return PveApi(settings)


async def collect_status_snapshot(
    vmid: int | None,
    client: PveApi,
    settings: Settings,
) -> dict[str, object]:
    if vmid is None:
        node_status = await client.node_status(settings.pve_node)
        status = normalize_node_status(settings.pve_node, node_status)
        if settings.nat_enabled:
            leases = list_nat_leases(settings)
            status["nat"] = {
                "enabled": True,
                "bridge": settings.nat_bridge,
                "network": settings.nat_network_cidr,
                "host_ip": settings.nat_host_ip,
                "port_start": settings.nat_port_start,
                "ports_per_vm": settings.nat_ports_per_vm,
                "leased": len(leases),
            }
        return status
    vm_status = await client.vm_status(settings.pve_node, vmid)
    status = normalize_vm_status(vmid, vm_status, vm_tap_traffic(vmid), settings.pve_node)
    nat_lease = get_nat_lease(settings, vmid)
    if nat_lease:
        status["nat"] = nat_lease.model_dump()
        status["network_mode"] = "nat"
    else:
        status["network_mode"] = "public"
    return status


async def metric_sampler(settings: Settings) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            client = PveApi(settings)
            host_status = await collect_status_snapshot(None, client, settings)
            record_metric_sample(settings, host_status)
            for vm in await client.list_vms(settings.pve_node):
                vmid = int(vm.get("vmid"))
                vm_status = await collect_status_snapshot(vmid, client, settings)
                vm_status["traffic_billing"] = get_vm_traffic_usage(
                    settings,
                    vmid,
                    vm_status,
                )
                await enforce_traffic_limit(
                    vmid,
                    vm_status["traffic_billing"],
                    client,
                    settings,
                )
                record_metric_sample(settings, vm_status)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


def public_ws_url(settings: Settings, path: str) -> str:
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
    else:
        base = ""
    return f"{base}{path}"


def cleanup_console_tokens() -> None:
    now = time.time()
    expired = [
        token
        for token, payload in CONSOLE_TOKENS.items()
        if float(payload["expires_at"]) <= now
    ]
    for token in expired:
        CONSOLE_TOKENS.pop(token, None)


def require_console_token(
    token: str | None,
    settings: Settings,
    vmid: int | None = None,
) -> None:
    cleanup_console_tokens()
    payload = CONSOLE_TOKENS.get(token or "")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid console token")
    bound_vmid = payload.get("vmid")
    if bound_vmid is not None and vmid is not None and int(bound_vmid) != vmid:
        raise HTTPException(status_code=403, detail="Console token is not valid for this VM")


def require_websocket_console_token(
    websocket: WebSocket,
    settings: Settings,
    vmid: int,
) -> None:
    token = websocket.query_params.get("token")
    cleanup_console_tokens()
    payload = CONSOLE_TOKENS.get(token or "")
    if not payload:
        raise RuntimeError("Invalid console token")
    bound_vmid = payload.get("vmid")
    if bound_vmid is not None and int(bound_vmid) != vmid:
        raise RuntimeError("Console token is not valid for this VM")


def vmid_from_console_token(token: str | None, settings: Settings) -> int:
    cleanup_console_tokens()
    payload = CONSOLE_TOKENS.get(token or "")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid console token")
    bound_vmid = payload.get("vmid")
    if bound_vmid is None:
        raise HTTPException(status_code=400, detail="Console token is not bound to a VM")
    return int(bound_vmid)


def vmid_from_websocket_console_token(websocket: WebSocket) -> int:
    token = websocket.query_params.get("token")
    cleanup_console_tokens()
    payload = CONSOLE_TOKENS.get(token or "")
    if not payload:
        raise RuntimeError("Invalid console token")
    bound_vmid = payload.get("vmid")
    if bound_vmid is None:
        raise RuntimeError("Console token is not bound to a VM")
    return int(bound_vmid)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/api/v1/auth/check",
    dependencies=[Depends(require_api_token)],
)
async def auth_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/vm/{vmid}", response_class=HTMLResponse)
async def vm_detail_page(vmid: int) -> HTMLResponse:
    path = Path(__file__).parent / "static" / "vm.html"
    page = path.read_text(encoding="utf-8").replace("__VMID__", str(vmid))
    return HTMLResponse(page)


def resolve_template_vmid(req: VmCreateRequest, settings: Settings) -> int | None:
    if req.template_vmid is not None:
        return req.template_vmid
    if req.image is None:
        return None
    template_vmid = settings.image_templates.get(req.image)
    if template_vmid is None:
        raise HTTPException(status_code=400, detail=f"Unknown image: {req.image}")
    return template_vmid


def build_cloud_init_config(
    req: VmCreateRequest,
    allocated_ip_config: str | None = None,
    allocated_nameserver: str | None = None,
    prefer_allocated_network: bool = False,
) -> dict[str, object]:
    config: dict[str, object] = {}
    if req.ci_user:
        config["ciuser"] = req.ci_user
    if req.ci_password:
        config["cipassword"] = req.ci_password
    if req.ssh_keys:
        config["sshkeys"] = req.ssh_keys
    if prefer_allocated_network:
        ip_config = allocated_ip_config or req.ip_config
        nameserver = allocated_nameserver or req.nameserver
    else:
        ip_config = req.ip_config or allocated_ip_config
        nameserver = req.nameserver or allocated_nameserver
    if ip_config:
        config["ipconfig0"] = ip_config
    if nameserver:
        config["nameserver"] = nameserver
    if req.searchdomain:
        config["searchdomain"] = req.searchdomain
    if req.boot_order:
        config["boot"] = f"order={req.boot_order}"
    return config


def build_net0_config(req: VmCreateRequest, bridge: str) -> str:
    network = req.network
    model = network.model if network else "virtio"
    selected_bridge = network.bridge if network and network.bridge else bridge
    parts = [model, f"bridge={selected_bridge}"]
    if network and network.rate is not None:
        parts.append(f"rate={network.rate}")
    if network and network.vlan_tag is not None:
        parts.append(f"tag={network.vlan_tag}")
    if network and network.firewall is not None:
        parts.append(f"firewall={1 if network.firewall else 0}")
    return ",".join(parts)


def build_vm_config(req: VmCreateRequest, net0: str) -> dict[str, object]:
    config: dict[str, object] = {
        "cores": req.cores,
        "memory": req.memory_mb,
        "net0": net0,
    }
    return config


def set_net_link_down(net_config: str, link_down: bool) -> str:
    parts = [
        part
        for part in str(net_config).split(",")
        if part and not part.startswith("link_down=")
    ]
    if link_down:
        parts.append("link_down=1")
    return ",".join(parts)


def set_net_rate(net_config: str, rate: float | None) -> str:
    parts = [
        part
        for part in str(net_config).split(",")
        if part and not part.startswith("rate=")
    ]
    if rate is not None:
        parts.append(f"rate={rate}")
    return ",".join(parts)


def net_is_link_down(net_config: str) -> bool:
    return any(
        part == "link_down=1"
        for part in str(net_config).split(",")
    )


def traffic_limit_exceeded(billing: dict[str, object] | None) -> bool:
    if not billing or not billing.get("configured") or not billing.get("quota_gb"):
        return False
    percent = billing.get("percent")
    if percent is not None:
        return float(percent) >= 100
    remaining = billing.get("remaining_gb")
    return remaining is not None and float(remaining) <= 0


async def enforce_traffic_limit(
    vmid: int,
    billing: dict[str, object],
    client: PveApi,
    settings: Settings,
) -> str | None:
    if not traffic_limit_exceeded(billing):
        return None
    config = await client.vm_config(settings.pve_node, vmid)
    net0 = str(config.get("net0") or "")
    if not net0 or net_is_link_down(net0):
        return None
    task = await client.set_vm_config(
        settings.pve_node,
        vmid,
        {"net0": set_net_link_down(net0, True)},
    )
    record_task_log(
        settings,
        vmid,
        "traffic_limit_disconnect",
        task_id=task,
        message=f"used={billing.get('used_gb')}GB quota={billing.get('quota_gb')}GB",
    )
    return task


def detect_primary_disk(config: dict[str, object]) -> str:
    for disk in ("scsi0", "virtio0", "sata0", "ide0"):
        value = str(config.get(disk, ""))
        if value and "cloudinit" not in value and "media=cdrom" not in value:
            return disk
    boot = str(config.get("boot", ""))
    match = re.search(r"order=([^,]+)", boot)
    if match:
        for disk in match.group(1).split(";"):
            if disk in config:
                value = str(config.get(disk, ""))
                if "cloudinit" not in value and "media=cdrom" not in value:
                    return disk
    raise HTTPException(status_code=502, detail="Unable to detect primary disk")


@app.post(
    "/api/v1/ip-pool",
    response_model=list[IpPoolAddress],
    dependencies=[Depends(require_api_token)],
)
async def add_ip_pool(
    req: IpPoolAddRequest,
    settings: Settings = Depends(get_settings),
) -> list[IpPoolAddress]:
    if not req.addresses and not req.range:
        raise HTTPException(status_code=400, detail="addresses or range is required")
    return add_ip_addresses(settings, req)


@app.get(
    "/api/v1/ip-pool",
    response_model=list[IpPoolAddress],
    dependencies=[Depends(require_api_token)],
)
async def get_ip_pool(
    status: str | None = None,
    settings: Settings = Depends(get_settings),
) -> list[IpPoolAddress]:
    return list_ip_addresses(settings, status)


@app.get(
    "/api/v1/vms/{vmid}/ips",
    response_model=list[IpPoolAddress],
    dependencies=[Depends(require_api_token)],
)
async def get_vm_ip_pool(
    vmid: int,
    settings: Settings = Depends(get_settings),
) -> list[IpPoolAddress]:
    rows = list_ip_addresses_by_vmid(settings, vmid)
    nat_lease = get_nat_lease(settings, vmid)
    if nat_lease:
        rows.append(
            IpPoolAddress(
                address=nat_lease.address,
                cidr=nat_lease.cidr,
                gateway=nat_lease.gateway,
                nameserver=nat_lease.nameserver,
                bridge=nat_lease.bridge,
                status="allocated",
                vmid=vmid,
                note=f"NAT SSH:{nat_lease.ssh_port} ports:{nat_lease.port_start}-{nat_lease.port_end}",
            )
        )
    return rows


@app.post(
    "/api/v1/ip-pool/{address}/release",
    response_model=IpPoolAddress,
    dependencies=[Depends(require_api_token)],
)
async def release_ip_address(
    address: str,
    settings: Settings = Depends(get_settings),
) -> IpPoolAddress:
    released = release_ip(settings, address)
    if released is None:
        raise HTTPException(status_code=404, detail="IP address not found")
    return released


@app.get(
    "/api/v1/images",
    response_model=list[ImageTemplateResponse],
    dependencies=[Depends(require_api_token)],
)
async def list_images(
    settings: Settings = Depends(get_settings),
) -> list[ImageTemplateResponse]:
    return [
        ImageTemplateResponse(image=name, template_vmid=vmid)
        for name, vmid in sorted(settings.image_templates.items())
    ]


@app.get(
    "/api/v1/reinstall/images",
    response_model=list[ImageTemplateResponse],
    dependencies=[Depends(require_api_token)],
)
async def list_reinstall_images(
    settings: Settings = Depends(get_settings),
) -> list[ImageTemplateResponse]:
    return await list_images(settings)


@app.post(
    "/api/v1/vms",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_vm(
    req: VmCreateRequest,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    lease = None
    nat_lease = None
    try:
        vmid = req.vmid or await client.next_vmid()
        storage = req.storage or settings.default_storage
        bridge = req.bridge or settings.default_bridge
        template_vmid = resolve_template_vmid(req, settings)
        network_mode = req.network.mode if req.network else "public"
        if network_mode == "nat":
            await ensure_nat_ready(settings)
            nat_lease = allocate_nat_lease(settings, vmid)
            bridge = settings.nat_bridge
        elif req.allocate_ip and not req.ip_config:
            lease = allocate_ip(settings, vmid)
            if lease is None:
                raise HTTPException(status_code=409, detail="No available IP address")
            if lease.bridge:
                bridge = lease.bridge
        net0 = build_net0_config(req, bridge)
        if template_vmid:
            task = await client.clone_vm(
                settings.pve_node,
                template_vmid,
                vmid,
                req.name,
                storage,
            )
            await client.wait_for_task(settings.pve_node, task)
            await client.set_vm_config(
                settings.pve_node,
                vmid,
                build_vm_config(req, net0),
            )
            vm_config = await client.vm_config(settings.pve_node, vmid)
            cloud_init_config = build_cloud_init_config(
                req,
                allocated_ip_config=nat_lease.ip_config if nat_lease else (lease.ip_config if lease else None),
                allocated_nameserver=nat_lease.nameserver if nat_lease else (lease.nameserver if lease else None),
                prefer_allocated_network=nat_lease is not None,
            )
            if req.disk_gb:
                primary_disk = detect_primary_disk(vm_config)
                await client.resize_disk(
                    settings.pve_node,
                    vmid,
                    primary_disk,
                    req.disk_gb,
                )
            if cloud_init_config:
                await client.set_vm_config(settings.pve_node, vmid, cloud_init_config)
        else:
            task = await client.create_vm(
                settings.pve_node,
                vmid,
                req.name,
                req.cores,
                req.memory_mb,
                net0,
            )
            vm_config = await client.vm_config(settings.pve_node, vmid)
            cloud_init_config = build_cloud_init_config(
                req,
                allocated_ip_config=nat_lease.ip_config if nat_lease else (lease.ip_config if lease else None),
                allocated_nameserver=nat_lease.nameserver if nat_lease else (lease.nameserver if lease else None),
                prefer_allocated_network=nat_lease is not None,
            )
            if cloud_init_config:
                await client.set_vm_config(settings.pve_node, vmid, cloud_init_config)
        if nat_lease:
            await ensure_nat_rules(settings, nat_lease)
        start_task = None
        if req.start:
            start_task = await client.vm_action(settings.pve_node, vmid, "start")
        # Default username to root on create if user didn't specify one.
        default_user = (req.ci_user or "").strip() or "root"
        save_vm_credentials(settings, vmid, default_user, req.ci_password)
        if req.traffic_limit_gb:
            set_vm_traffic_config(
                settings,
                vmid,
                VmTrafficConfigRequest(
                    quota_gb=req.traffic_limit_gb,
                    reset_day=req.traffic_reset_day,
                    reset_hour=req.traffic_reset_hour,
                    timezone=req.traffic_reset_timezone,
                ),
            )
        record_task_log(
            settings,
            vmid,
            "create",
            task_id=start_task or task,
            message=f"name={req.name}",
        )
        return VmActionResponse(
            vmid=vmid,
            task=task,
            start_task=start_task,
            allocated_ip=lease.address if lease else None,
            nat_ip=nat_lease.address if nat_lease else None,
            ssh_port=nat_lease.ssh_port if nat_lease else None,
            port_range_start=nat_lease.port_start if nat_lease else None,
            port_range_end=nat_lease.port_end if nat_lease else None,
            network_mode="nat" if nat_lease else "public",
        )
    except NatError as exc:
        if nat_lease:
            try:
                await remove_nat_rules(settings, nat_lease)
            except Exception:
                pass
            release_nat_lease(settings, vmid)
        if lease:
            release_ip_by_vmid(settings, vmid)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PveApiError as exc:
        if nat_lease:
            try:
                await remove_nat_rules(settings, nat_lease)
            except Exception:
                pass
            release_nat_lease(settings, vmid)
        if lease:
            release_ip_by_vmid(settings, vmid)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(
    "/api/v1/vms",
    dependencies=[Depends(require_api_token)],
)
async def list_vms(
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    try:
        vms = await client.list_vms(settings.pve_node)
        nat_map = {lease.vmid: lease for lease in list_nat_leases(settings)}
        for vm in vms:
            lease = nat_map.get(int(vm.get("vmid")))
            if lease:
                vm["network_mode"] = "nat"
                vm["nat"] = lease.model_dump()
            else:
                vm["network_mode"] = "public"
        return vms
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(
    "/api/v1/vms/{vmid}",
    dependencies=[Depends(require_api_token)],
)
async def get_vm(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
):
    try:
        return await client.vm_status(settings.pve_node, vmid)
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.put(
    "/api/v1/vms/{vmid}/config",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def update_vm_config(
    vmid: int,
    req: VmConfigUpdateRequest,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    data: dict[str, object] = {}
    if req.cores is not None:
        data["cores"] = req.cores
    if req.memory_mb is not None:
        data["memory"] = req.memory_mb
    try:
        if req.network_rate is not None:
            config = await client.vm_config(settings.pve_node, vmid)
            net0 = str(config.get("net0") or "")
            if not net0:
                raise HTTPException(status_code=404, detail="VM net0 does not exist")
            data["net0"] = set_net_rate(net0, req.network_rate)
        if not data:
            raise HTTPException(status_code=400, detail="No config changes provided")
        task = await client.set_vm_config(settings.pve_node, vmid, data)
        reboot_task = None
        if req.reboot:
            status = await client.vm_status(settings.pve_node, vmid)
            if str(status.get("status")) == "running":
                reboot_task = await client.vm_action(settings.pve_node, vmid, "reboot")
        record_task_log(
            settings,
            vmid,
            "config_update",
            task_id=reboot_task or task,
            message=",".join(sorted(data.keys())),
        )
        return VmActionResponse(
            vmid=vmid,
            task=task,
            start_task=reboot_task,
            status="config_updated",
        )
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/v1/vms/{vmid}/reinstall",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def reinstall_vm(
    vmid: int,
    req: VmReinstallRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        template_vmid = resolve_reinstall_template_vmid(req.image, req.template_vmid, settings)
    except ReinstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = f"reinstall-{vmid}-{int(time.time())}"
    record_task_log(
        settings,
        vmid,
        "reinstall",
        status="queued",
        task_id=task_id,
        message=f"template={template_vmid} image={req.image or '-'}",
    )
    background_tasks.add_task(
        run_reinstall,
        settings,
        vmid,
        template_vmid,
        slot=req.slot,
        template_slot=req.template_slot,
        storage=req.storage,
        disk_size=req.disk_size,
        ci_user=req.ci_user,
        password=req.password,
        nameserver=req.nameserver,
        start=req.start,
        free_old=req.free_old,
        dry_run=req.dry_run,
        task_id=task_id,
    )
    if req.ci_user or req.password:
        save_vm_credentials(settings, vmid, req.ci_user or "root", req.password)
    return VmActionResponse(vmid=vmid, task=task_id, status="reinstall_queued")


@app.get(
    "/api/v1/vms/{vmid}/traffic",
    response_model=VmTrafficConfigResponse,
    dependencies=[Depends(require_api_token)],
)
async def get_vm_traffic_config(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    try:
        status = await collect_status_snapshot(vmid, client, settings)
        return get_vm_traffic_usage(settings, vmid, status)
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.put(
    "/api/v1/vms/{vmid}/traffic",
    response_model=VmTrafficConfigResponse,
    dependencies=[Depends(require_api_token)],
)
async def update_vm_traffic_config(
    vmid: int,
    req: VmTrafficConfigRequest,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    try:
        status = await collect_status_snapshot(vmid, client, settings)
        result = set_vm_traffic_config(settings, vmid, req, status)
        record_task_log(
            settings,
            vmid,
            "traffic_config",
            message=f"quota={req.quota_gb}, reset={req.reset_day} {req.reset_hour}:00",
        )
        return result
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def build_status_response(
    vmid: int | None,
    client: PveApi,
    settings: Settings,
) -> dict[str, object]:
    try:
        status = await collect_status_snapshot(vmid, client, settings)
        if vmid is not None:
            status["traffic_billing"] = get_vm_traffic_usage(settings, vmid, status)
            task = await enforce_traffic_limit(
                vmid,
                status["traffic_billing"],
                client,
                settings,
            )
            if task:
                status["traffic_billing"]["enforced_task"] = task
        record_metric_sample(settings, status)
        return status
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(
    "/api/v1/status",
    dependencies=[Depends(require_api_token)],
)
async def get_status(
    vmid: int | None = None,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await build_status_response(vmid, client, settings)


@app.get(
    "/status",
    dependencies=[Depends(require_api_token)],
)
async def get_status_alias(
    vmid: int | None = None,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await build_status_response(vmid, client, settings)


@app.get(
    "/api/v1/metrics/history",
    dependencies=[Depends(require_api_token)],
)
async def get_metric_history(
    vmid: int | None = None,
    hours: int = 24,
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    return list_metric_samples(settings, vmid, hours)


@app.get(
    "/api/v1/vms/{vmid}/tasks",
    dependencies=[Depends(require_api_token)],
)
async def get_vm_task_logs(
    vmid: int,
    limit: int = 50,
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    return list_task_logs(settings, vmid, limit)


@app.post(
    "/api/v1/vms/{vmid}/pause",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def pause_vm(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        task = await client.vm_action(settings.pve_node, vmid, "stop")
        record_task_log(settings, vmid, "stop", task_id=task)
        return VmActionResponse(vmid=vmid, task=task)
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/v1/vms/{vmid}/network/disconnect",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def disconnect_vm_network(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        config = await client.vm_config(settings.pve_node, vmid)
        net0 = str(config.get("net0") or "")
        if not net0:
            raise HTTPException(status_code=404, detail="VM net0 does not exist")
        task = await client.set_vm_config(
            settings.pve_node,
            vmid,
            {"net0": set_net_link_down(net0, True)},
        )
        record_task_log(settings, vmid, "network_disconnect", task_id=task)
        return VmActionResponse(vmid=vmid, task=task, status="network_disconnected")
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/v1/vms/{vmid}/network/connect",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def connect_vm_network(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        config = await client.vm_config(settings.pve_node, vmid)
        net0 = str(config.get("net0") or "")
        if not net0:
            raise HTTPException(status_code=404, detail="VM net0 does not exist")
        task = await client.set_vm_config(
            settings.pve_node,
            vmid,
            {"net0": set_net_link_down(net0, False)},
        )
        record_task_log(settings, vmid, "network_connect", task_id=task)
        return VmActionResponse(vmid=vmid, task=task, status="network_connected")
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/v1/vms/{vmid}/resume",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def resume_vm(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        task = await client.vm_action(settings.pve_node, vmid, "start")
        record_task_log(settings, vmid, "start", task_id=task)
        return VmActionResponse(vmid=vmid, task=task)
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete(
    "/api/v1/vms/{vmid}",
    response_model=VmActionResponse,
    dependencies=[Depends(require_api_token)],
)
async def delete_vm(
    vmid: int,
    client: PveApi = Depends(pve),
    settings: Settings = Depends(get_settings),
) -> VmActionResponse:
    try:
        nat_lease = get_nat_lease(settings, vmid)
        try:
            await client.vm_action(settings.pve_node, vmid, "stop")
        except PveApiError:
            pass
        task = await client.delete_vm(settings.pve_node, vmid)
        released = release_ip_by_vmid(settings, vmid)
        if nat_lease:
            try:
                await remove_nat_rules(settings, nat_lease)
            except Exception:
                pass
            release_nat_lease(settings, vmid)
        delete_network_snippet(settings, vmid)
        delete_vm_credentials(settings, vmid)
        record_task_log(settings, vmid, "delete", task_id=task)
        return VmActionResponse(
            vmid=vmid,
            task=task,
            released_ip=released.address if released else None,
            nat_ip=nat_lease.address if nat_lease else None,
            ssh_port=nat_lease.ssh_port if nat_lease else None,
            status="deleting",
        )
    except PveApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/v1/vms/{vmid}/expiration",
    dependencies=[Depends(require_api_token)],
)
async def set_expiration(vmid: int, req: VmExpirationRequest) -> dict[str, object]:
    # Persistence and scheduler wiring will be added with the DB migration.
    return {"vmid": vmid, "expires_at": req.expires_at, "action": req.action}


@app.get(
    "/api/v1/vms/{vmid}/credentials",
    dependencies=[Depends(require_api_token)],
)
async def get_saved_vm_credentials(
    vmid: int,
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    credentials = get_vm_credentials(settings, vmid)
    return {
        "vmid": vmid,
        "username": credentials.get("username"),
        "password": credentials.get("password"),
        "username_saved": bool(credentials.get("username")),
        "password_saved": bool(credentials.get("password")),
    }


@app.put(
    "/api/v1/vms/{vmid}/credentials",
    response_model=VmCredentialsResponse,
    dependencies=[Depends(require_api_token)],
)
async def set_vm_credentials(
    vmid: int,
    req: VmCredentialsRequest,
    settings: Settings = Depends(get_settings),
) -> VmCredentialsResponse:
    username_saved, password_saved = save_vm_credentials(
        settings,
        vmid,
        req.username,
        req.password,
    )
    return VmCredentialsResponse(
        vmid=vmid,
        username_saved=username_saved,
        password_saved=password_saved,
    )


@app.post(
    "/api/v1/consoles/vnc/{vmid}",
    response_model=ConsoleSessionResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_vnc_session(
    vmid: int,
    settings: Settings = Depends(get_settings),
) -> ConsoleSessionResponse:
    return ConsoleSessionResponse(
        vmid=vmid,
        node=settings.pve_node,
        console="vnc",
        websocket_url=public_ws_url(settings, f"/ws/vnc/{vmid}"),
    )


@app.post(
    "/api/v1/consoles/token",
    response_model=ConsoleTokenResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_console_token(
    req: ConsoleTokenRequest,
    settings: Settings = Depends(get_settings),
) -> ConsoleTokenResponse:
    cleanup_console_tokens()
    token = secrets.token_urlsafe(32)
    expires_at_ts = time.time() + min(req.ttl_seconds, 900)
    CONSOLE_TOKENS[token] = {
        "vmid": req.vmid,
        "expires_at": expires_at_ts,
    }
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=timezone.utc)
    base = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
    return ConsoleTokenResponse(
        token=token,
        expires_at=expires_at,
        vmid=req.vmid,
        console_url=f"{base}/console?token={token}",
    )


def render_vnc_console_page(vmid: int, token: str | None, settings: Settings) -> HTMLResponse:
    credentials = get_vm_credentials(settings, vmid)
    paste_username = credentials.get("username")
    paste_password = credentials.get("password")
    has_both_credentials = bool(paste_username) and bool(paste_password)
    ws_url_path = f"/ws/vnc?token={token}"
    ws_scheme = "wss" if settings.public_base_url.startswith("https://") else "ws"
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_base}{ws_url_path}"
    else:
        ws_url = f"{ws_scheme}://{html.escape('{host}')}{ws_url_path}"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VM {vmid} VNC</title>
  <style>
    html, body {{
      height: 100%;
      margin: 0;
      background: #111827;
      color: #e5e7eb;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    #bar {{
      height: 40px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 12px;
      background: #0f172a;
      border-bottom: 1px solid #334155;
      box-sizing: border-box;
    }}
    #screen {{
      height: calc(100% - 40px);
      width: 100%;
      overflow: hidden;
      background: #000;
    }}
    button {{
      background: #2563eb;
      color: white;
      border: 0;
      border-radius: 4px;
      padding: 6px 10px;
      cursor: pointer;
    }}
    #status {{
      color: #cbd5e1;
      font-size: 13px;
    }}
    button:disabled {{
      opacity: .5;
      cursor: not-allowed;
    }}
  </style>
</head>
<body>
  <div id="bar">
    <strong>VM {vmid}</strong>
    <button id="send-ctrl-alt-del">Ctrl+Alt+Del</button>
    <button id="type-username" {"disabled" if not has_both_credentials else ""}>输入用户名</button>
    <button id="type-password" {"disabled" if not has_both_credentials else ""}>输入密码</button>
    <button id="type-login" {"disabled" if not has_both_credentials else ""}>输入账号密码</button>
    <button id="send-enter">Enter</button>
    <span id="status">connecting</span>
  </div>
  <div id="screen"></div>
  <script type="module">
    import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.2.0/core/rfb.js';

    const host = window.location.host;
    const wsUrl = {ws_url!r}.replace('{{host}}', host);
    const pasteUsername = {json.dumps(paste_username)};
    const pastePassword = {json.dumps(paste_password)};
    const status = document.getElementById('status');
    const rfb = new RFB(document.getElementById('screen'), wsUrl);
    rfb.scaleViewport = true;
    rfb.resizeSession = true;
    rfb.viewOnly = false;

    rfb.addEventListener('connect', () => status.textContent = 'connected');
    rfb.addEventListener('disconnect', (event) => {{
      status.textContent = event.detail.clean ? 'disconnected' : 'connection failed';
    }});
    rfb.addEventListener('credentialsrequired', () => {{
      status.textContent = 'credentials required';
    }});
    document.getElementById('send-ctrl-alt-del').addEventListener('click', () => {{
      rfb.sendCtrlAltDel();
    }});
    function sleep(ms) {{
      return new Promise((resolve) => setTimeout(resolve, ms));
    }}
    function keysymFor(char) {{
      const codePoint = char.codePointAt(0);
      if (codePoint === 10 || codePoint === 13) return 0xff0d;
      if (codePoint === 9) return 0xff09;
      if (codePoint >= 0x20 && codePoint <= 0x7e) return codePoint;
      return codePoint;
    }}
    function sendKeysym(keysym) {{
      rfb.sendKey(keysym, null, true);
      rfb.sendKey(keysym, null, false);
    }}
    async function typeText(text, pressEnter = false) {{
      if (!text) return;
      status.textContent = 'typing';
      rfb.focus();
      for (const char of text) {{
        sendKeysym(keysymFor(char));
        await sleep(18);
      }}
      if (pressEnter) {{
        sendKeysym(0xff0d);
      }}
      status.textContent = 'connected';
    }}
    document.getElementById('type-username').addEventListener('click', () => {{
      typeText(pasteUsername, true);
    }});
    document.getElementById('type-password').addEventListener('click', () => {{
      typeText(pastePassword, true);
    }});
    document.getElementById('type-login').addEventListener('click', async () => {{
      await typeText(pasteUsername, true);
      await sleep(450);
      await typeText(pastePassword, true);
    }});
    document.getElementById('send-enter').addEventListener('click', () => {{
      sendKeysym(0xff0d);
    }});
  </script>
</body>
</html>
"""
    return HTMLResponse(page)


@app.get("/console", response_class=HTMLResponse)
async def bound_vnc_console_page(
    token: str | None = None,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    vmid = vmid_from_console_token(token, settings)
    return render_vnc_console_page(vmid, token, settings)


@app.get("/console/vnc/{vmid}", response_class=HTMLResponse)
async def vnc_console_page(
    vmid: int,
    token: str | None = None,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    require_console_token(token, settings, vmid)
    return render_vnc_console_page(vmid, token, settings)


@app.post(
    "/api/v1/consoles/xterm/{vmid}",
    response_model=ConsoleSessionResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_xterm_session(
    vmid: int,
    settings: Settings = Depends(get_settings),
) -> ConsoleSessionResponse:
    return ConsoleSessionResponse(
        vmid=vmid,
        node=settings.pve_node,
        console="xterm",
        websocket_url=public_ws_url(settings, f"/ws/xterm/{vmid}"),
    )


async def proxy_pve_websocket(
    websocket: WebSocket,
    target_url: str,
    auth_header: str,
    verify_ssl: bool,
    vnc_password: str | None = None,
) -> None:
    ssl_context = None
    if target_url.startswith("wss://") and not verify_ssl:
        ssl_context = ssl._create_unverified_context()
    await websocket.accept()
    if vnc_password and DES is None:
        await websocket.close(code=1011)
        return
    async with websockets.connect(
        target_url,
        additional_headers={"Authorization": auth_header},
        ssl=ssl_context,
    ) as upstream:
        if vnc_password:
            authenticated = await authenticate_vnc_stream(websocket, upstream, vnc_password)
            if not authenticated:
                await websocket.close(code=1008)
                return

        async def client_to_pve() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if "bytes" in message and message["bytes"] is not None:
                        await upstream.send(message["bytes"])
                    elif "text" in message and message["text"] is not None:
                        await upstream.send(message["text"])
            except WebSocketDisconnect:
                await upstream.close()

        async def pve_to_client() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        import asyncio

        await asyncio.gather(client_to_pve(), pve_to_client())


def reverse_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    if DES is None:
        raise RuntimeError("pycryptodome is required for VNC authentication proxy")
    key = bytes(reverse_bits(byte) for byte in password.encode("utf-8")[:8])
    key = key.ljust(8, b"\x00")
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)


def ws_message_bytes(message: dict[str, object]) -> bytes:
    data = message.get("bytes")
    if data is not None:
        return data  # type: ignore[return-value]
    text = message.get("text")
    if text is not None:
        return str(text).encode("latin1")
    return b""


def upstream_bytes(message: object) -> bytes:
    if isinstance(message, bytes):
        return message
    return str(message).encode("latin1")


async def authenticate_vnc_stream(
    websocket: WebSocket,
    upstream,
    password: str,
) -> bool:
    server_version = upstream_bytes(await upstream.recv())
    await websocket.send_bytes(server_version)

    client_version = ws_message_bytes(await websocket.receive())
    await upstream.send(client_version)

    security_types = upstream_bytes(await upstream.recv())
    if not security_types:
        return False
    if security_types[0] == 0:
        await websocket.send_bytes(security_types)
        return False
    if 2 not in security_types[1:]:
        await websocket.send_bytes(security_types)
        return True

    # Browser sees a no-auth RFB server; the platform handles PVE VNCAuth.
    await websocket.send_bytes(b"\x01\x01")
    client_security_type = ws_message_bytes(await websocket.receive())
    if client_security_type != b"\x01":
        return False

    await upstream.send(b"\x02")
    challenge = upstream_bytes(await upstream.recv())
    if len(challenge) != 16:
        return False
    await upstream.send(vnc_auth_response(password, challenge))

    auth_result = upstream_bytes(await upstream.recv())
    await websocket.send_bytes(auth_result)
    return auth_result == b"\x00\x00\x00\x00"


@app.websocket("/ws/vnc")
async def bound_vnc_websocket(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
):
    try:
        vmid = vmid_from_websocket_console_token(websocket)
    except RuntimeError:
        await websocket.close(code=1008)
        return
    client = PveApi(settings)
    proxy = await client.vnc_proxy(settings.pve_node, vmid)
    target = client.websocket_url(
        f"/nodes/{settings.pve_node}/qemu/{vmid}/vncwebsocket",
        {"port": proxy["port"], "vncticket": proxy["ticket"]},
    )
    await proxy_pve_websocket(
        websocket,
        target,
        client.headers["Authorization"],
        settings.pve_verify_ssl,
        proxy["ticket"],
    )


@app.websocket("/ws/vnc/{vmid}")
async def vnc_websocket(
    websocket: WebSocket,
    vmid: int,
    settings: Settings = Depends(get_settings),
):
    try:
        require_websocket_console_token(websocket, settings, vmid)
    except RuntimeError:
        await websocket.close(code=1008)
        return
    client = PveApi(settings)
    proxy = await client.vnc_proxy(settings.pve_node, vmid)
    target = client.websocket_url(
        f"/nodes/{settings.pve_node}/qemu/{vmid}/vncwebsocket",
        {"port": proxy["port"], "vncticket": proxy["ticket"]},
    )
    await proxy_pve_websocket(
        websocket,
        target,
        client.headers["Authorization"],
        settings.pve_verify_ssl,
        proxy["ticket"],
    )


@app.websocket("/ws/xterm/{vmid}")
async def xterm_websocket(
    websocket: WebSocket,
    vmid: int,
    settings: Settings = Depends(get_settings),
):
    try:
        require_websocket_console_token(websocket, settings, vmid)
    except RuntimeError:
        await websocket.close(code=1008)
        return
    client = PveApi(settings)
    proxy = await client.vm_term_proxy(settings.pve_node, vmid)
    target = client.websocket_url(
        f"/nodes/{settings.pve_node}/qemu/{vmid}/vncwebsocket",
        {
            "port": proxy["port"],
            "vncticket": proxy["ticket"],
        },
    )
    await proxy_pve_websocket(
        websocket,
        target,
        client.headers["Authorization"],
        settings.pve_verify_ssl,
    )
