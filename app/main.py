import re
import ssl

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import websockets

from app.config import Settings, get_settings
from app.cloudinit import delete_network_snippet
from app.db import init_db
from app.ip_pool import (
    add_ip_addresses,
    allocate_ip,
    list_ip_addresses,
    release_ip,
    release_ip_by_vmid,
)
from app.pve_api import PveApi, PveApiError
from app.schemas import (
    ConsoleSessionResponse,
    ImageTemplateResponse,
    IpPoolAddRequest,
    IpPoolAddress,
    VmActionResponse,
    VmCreateRequest,
    VmExpirationRequest,
)
from app.security import require_api_token

app = FastAPI(title="PVETrafficManager Platform API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    init_db(get_settings())


def pve(settings: Settings = Depends(get_settings)) -> PveApi:
    return PveApi(settings)


def public_ws_url(settings: Settings, path: str) -> str:
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
    else:
        base = ""
    return f"{base}{path}"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
) -> dict[str, object]:
    config: dict[str, object] = {}
    if req.ci_user:
        config["ciuser"] = req.ci_user
    if req.ci_password:
        config["cipassword"] = req.ci_password
    if req.ssh_keys:
        config["sshkeys"] = req.ssh_keys
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
    try:
        vmid = req.vmid or await client.next_vmid()
        storage = req.storage or settings.default_storage
        bridge = req.bridge or settings.default_bridge
        template_vmid = resolve_template_vmid(req, settings)
        lease = None
        if req.allocate_ip and not req.ip_config:
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
                allocated_ip_config=lease.ip_config if lease else None,
                allocated_nameserver=lease.nameserver if lease else None,
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
                allocated_ip_config=lease.ip_config if lease else None,
                allocated_nameserver=lease.nameserver if lease else None,
            )
            if cloud_init_config:
                await client.set_vm_config(settings.pve_node, vmid, cloud_init_config)
        start_task = None
        if req.start:
            start_task = await client.vm_action(settings.pve_node, vmid, "start")
        return VmActionResponse(
            vmid=vmid,
            task=task,
            start_task=start_task,
            allocated_ip=lease.address if lease else None,
        )
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
        return VmActionResponse(vmid=vmid, task=task)
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
        try:
            await client.vm_action(settings.pve_node, vmid, "stop")
        except PveApiError:
            pass
        task = await client.delete_vm(settings.pve_node, vmid)
        released = release_ip_by_vmid(settings, vmid)
        delete_network_snippet(settings, vmid)
        return VmActionResponse(
            vmid=vmid,
            task=task,
            released_ip=released.address if released else None,
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
) -> None:
    ssl_context = None
    if target_url.startswith("wss://") and not verify_ssl:
        ssl_context = ssl._create_unverified_context()
    await websocket.accept()
    async with websockets.connect(
        target_url,
        additional_headers={"Authorization": auth_header},
        ssl=ssl_context,
    ) as upstream:
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


@app.websocket("/ws/vnc/{vmid}")
async def vnc_websocket(
    websocket: WebSocket,
    vmid: int,
    settings: Settings = Depends(get_settings),
):
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
    )


@app.websocket("/ws/xterm/{vmid}")
async def xterm_websocket(
    websocket: WebSocket,
    vmid: int,
    settings: Settings = Depends(get_settings),
):
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
