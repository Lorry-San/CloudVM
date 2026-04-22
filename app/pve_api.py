from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings


class PveApiError(RuntimeError):
    pass


class PveApi:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.pve_host.rstrip("/")
        self.headers = {
            "Authorization": (
                f"PVEAPIToken={settings.pve_api_token_id}="
                f"{settings.pve_api_token_secret}"
            )
        }

    async def request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/api2/json/{path.lstrip('/')}"
        async with httpx.AsyncClient(
            verify=self.settings.pve_verify_ssl,
            timeout=30,
            headers=self.headers,
        ) as client:
            response = await client.request(method, url, data=data)
        if response.status_code >= 400:
            raise PveApiError(f"PVE API {response.status_code}: {response.text}")
        payload = response.json()
        return payload.get("data")

    async def next_vmid(self) -> int:
        data = await self.request("GET", "/cluster/nextid")
        return int(data)

    async def clone_vm(
        self,
        node: str,
        template_vmid: int,
        vmid: int,
        name: str,
        storage: str | None,
    ) -> str:
        data = {
            "newid": vmid,
            "name": name,
            "full": 1,
        }
        if storage:
            data["storage"] = storage
        return await self.request(
            "POST",
            f"/nodes/{node}/qemu/{template_vmid}/clone",
            data,
        )

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        timeout_seconds: int = 300,
        interval_seconds: int = 2,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            status = await self.request("GET", f"/nodes/{node}/tasks/{upid}/status")
            if status.get("status") == "stopped":
                exitstatus = status.get("exitstatus")
                if exitstatus == "OK":
                    return status
                raise PveApiError(f"PVE task failed: {exitstatus}")
            if asyncio.get_running_loop().time() >= deadline:
                raise PveApiError(f"PVE task timed out: {upid}")
            await asyncio.sleep(interval_seconds)

    async def create_vm(
        self,
        node: str,
        vmid: int,
        name: str,
        cores: int,
        memory_mb: int,
        net0: str,
    ) -> str:
        return await self.request(
            "POST",
            f"/nodes/{node}/qemu",
            {
                "vmid": vmid,
                "name": name,
                "cores": cores,
                "memory": memory_mb,
                "net0": net0,
            },
        )

    async def list_vms(self, node: str) -> list[dict[str, Any]]:
        return await self.request("GET", f"/nodes/{node}/qemu")

    async def set_vm_config(self, node: str, vmid: int, data: dict[str, Any]) -> str:
        return await self.request("POST", f"/nodes/{node}/qemu/{vmid}/config", data)

    async def vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        return await self.request("GET", f"/nodes/{node}/qemu/{vmid}/config")

    async def resize_disk(
        self,
        node: str,
        vmid: int,
        disk: str,
        size_gb: int,
    ) -> str:
        return await self.request(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            {"disk": disk, "size": f"{size_gb}G"},
        )

    async def vm_status(self, node: str, vmid: int) -> Any:
        return await self.request("GET", f"/nodes/{node}/qemu/{vmid}/status/current")

    async def node_status(self, node: str) -> dict[str, Any]:
        return await self.request("GET", f"/nodes/{node}/status")

    async def vm_action(self, node: str, vmid: int, action: str) -> str:
        return await self.request("POST", f"/nodes/{node}/qemu/{vmid}/status/{action}")

    async def delete_vm(self, node: str, vmid: int) -> str:
        return await self.request("DELETE", f"/nodes/{node}/qemu/{vmid}")

    async def vnc_proxy(self, node: str, vmid: int) -> dict[str, Any]:
        return await self.request("POST", f"/nodes/{node}/qemu/{vmid}/vncproxy")

    async def vm_term_proxy(self, node: str, vmid: int) -> dict[str, Any]:
        return await self.request("POST", f"/nodes/{node}/qemu/{vmid}/termproxy")

    async def node_term_proxy(self, node: str) -> dict[str, Any]:
        return await self.request("POST", f"/nodes/{node}/termproxy")

    def websocket_url(self, path: str, params: dict[str, Any]) -> str:
        base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api2/json/{path.lstrip('/')}?{urlencode(params)}"
