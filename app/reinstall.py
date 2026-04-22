from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from app.config import Settings
from app.tasks import record_task_log


DISK_SLOT_ORDER = ("virtio0", "scsi0", "sata0", "ide0")
BACKUP_DIR = Path("/root/cloudvm-reinstall-backups")


class ReinstallError(RuntimeError):
    pass


def resolve_reinstall_template_vmid(image: str | None, template_vmid: int | None, settings: Settings) -> int:
    if template_vmid is not None:
        return template_vmid
    if not image:
        raise ReinstallError("image or template_vmid is required")
    mapped = settings.image_templates.get(image)
    if mapped is None:
        raise ReinstallError(f"Unknown image: {image}")
    return mapped


async def run_reinstall(
    settings: Settings,
    vmid: int,
    template_vmid: int,
    *,
    slot: str | None = None,
    template_slot: str | None = None,
    storage: str | None = None,
    disk_size: str | None = None,
    ci_user: str | None = None,
    password: str | None = None,
    nameserver: str | None = None,
    start: bool = True,
    free_old: bool = False,
    dry_run: bool = False,
    task_id: str | None = None,
) -> None:
    def log(status: str, message: str) -> None:
        record_task_log(settings, vmid, "reinstall", status=status, task_id=task_id, message=message)

    try:
        log("running", f"template={template_vmid} dry_run={dry_run}")
        target_config = await qm_config(vmid, log)
        template_config = await qm_config(template_vmid, log)

        target_slot = slot or first_system_disk(target_config)
        source_slot = template_slot or first_system_disk(template_config)
        if not target_slot:
            raise ReinstallError(f"Unable to detect VM {vmid} system disk")
        if not source_slot:
            raise ReinstallError(f"Unable to detect template {template_vmid} system disk")

        old_line = require_config(target_config, target_slot)
        source_line = require_config(template_config, source_slot)
        old_volume = volume_from_disk_line(old_line)
        source_volume = volume_from_disk_line(source_line)
        old_options = disk_options_without_size(old_line)
        target_storage = storage or storage_from_volume(old_volume) or storage_from_volume(source_volume)
        final_size = disk_size or disk_size_from_line(old_line)
        source_path = (await run_cmd(["pvesm", "path", source_volume], log, dry_run=False)).strip()
        was_running = "status: running" in await run_cmd(["qm", "status", str(vmid)], log, dry_run=False)

        log(
            "running",
            (
                f"slot={target_slot} template_slot={source_slot} old={old_volume} "
                f"source={source_volume} storage={target_storage} size={final_size or 'template'}"
            ),
        )

        await backup_config(vmid, target_config, log, dry_run)

        if was_running:
            await run_cmd(["qm", "shutdown", str(vmid), "--timeout", "60"], log, check=False, dry_run=dry_run)
            if not dry_run and "status: running" in await run_cmd(["qm", "status", str(vmid)], log):
                await run_cmd(["qm", "stop", str(vmid)], log, dry_run=dry_run)

        await run_cmd(["qm", "set", str(vmid), "--delete", target_slot], log, dry_run=dry_run)
        config_after_detach = target_config if dry_run else await qm_config(vmid, log)
        unused_before = set(unused_slots(config_after_detach))

        await run_cmd(["qm", "disk", "import", str(vmid), source_path, target_storage], log, dry_run=dry_run)
        if dry_run:
            log("ok", f"dry run finished before attaching imported disk to {target_slot}")
            return

        config_after_import = await qm_config(vmid, log)
        unused_after = set(unused_slots(config_after_import))
        new_unused = sorted(unused_after - unused_before, key=unused_sort_key)
        if not new_unused:
            raise ReinstallError("Unable to find imported unused disk")

        imported_slot = new_unused[-1]
        imported_volume = volume_from_disk_line(require_config(config_after_import, imported_slot))
        attach_value = imported_volume + ("," + old_options if old_options else "")
        await run_cmd(["qm", "set", str(vmid), f"--{target_slot}", attach_value], log)

        if final_size:
            await run_cmd(["qm", "resize", str(vmid), target_slot, final_size], log, check=False)
        if ci_user:
            await run_cmd(["qm", "set", str(vmid), "--ciuser", ci_user], log)
        if password:
            await run_cmd(["qm", "set", str(vmid), "--cipassword", password], log)
        if nameserver:
            await run_cmd(["qm", "set", str(vmid), "--nameserver", nameserver], log)

        await run_cmd(["qm", "set", str(vmid), "--boot", f"order={target_slot};ide2;net0"], log)
        await run_cmd(["qm", "cloudinit", "update", str(vmid)], log, check=False)

        if free_old:
            await delete_unused_reference(vmid, old_volume, log)
            await run_cmd(["pvesm", "free", old_volume], log, check=False)
        else:
            log("ok", f"old disk kept: {old_volume}; free later with: pvesm free {old_volume}")

        if start:
            await run_cmd(["qm", "start", str(vmid)], log)

        log("ok", f"completed: {target_slot}={imported_volume}")
    except Exception as exc:
        log("error", str(exc))
        raise


async def run_cmd(
    args: list[str],
    log,
    *,
    check: bool = True,
    dry_run: bool = False,
) -> str:
    printable = " ".join(shell_quote(part) for part in args)
    log("running", f"$ {printable}")
    if dry_run:
        return ""

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    if output:
        log("running", output[-900:])
    if check and proc.returncode != 0:
        raise ReinstallError(f"command failed ({proc.returncode}): {printable}")
    return output


async def qm_config(vmid: int, log) -> dict[str, str]:
    output = await run_cmd(["qm", "config", str(vmid)], log)
    config: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            config[key] = value
    return config


async def backup_config(vmid: int, config: dict[str, str], log, dry_run: bool) -> None:
    backup_path = BACKUP_DIR / f"vm-{vmid}-{time.strftime('%Y%m%d-%H%M%S')}.conf"
    log("running", f"backup config to {backup_path}")
    if dry_run:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path.write_text("".join(f"{k}: {v}\n" for k, v in config.items()), encoding="utf-8")


async def delete_unused_reference(vmid: int, volume: str, log) -> None:
    config = await qm_config(vmid, log)
    for slot in unused_slots(config):
        if volume_from_disk_line(config[slot]) == volume:
            await run_cmd(["qm", "set", str(vmid), "--delete", slot], log, check=False)
            return


def first_system_disk(config: dict[str, str]) -> str | None:
    for slot in DISK_SLOT_ORDER:
        value = config.get(slot, "")
        if value and "media=cdrom" not in value and "cloudinit" not in value and not value.startswith("none"):
            return slot
    return None


def require_config(config: dict[str, str], key: str) -> str:
    value = config.get(key)
    if not value:
        raise ReinstallError(f"missing config key: {key}")
    return value


def volume_from_disk_line(line: str) -> str:
    return line.split(",", 1)[0]


def storage_from_volume(volume: str) -> str:
    return volume.split(":", 1)[0] if ":" in volume else ""


def disk_size_from_line(line: str) -> str | None:
    for item in line.split(","):
        key, sep, value = item.partition("=")
        if sep and key == "size":
            return value
    return None


def disk_options_without_size(line: str) -> str:
    return ",".join(item for item in line.split(",")[1:] if item and not item.startswith("size="))


def unused_slots(config: dict[str, str]) -> list[str]:
    return [key for key in config if re.match(r"^unused\d+$", key)]


def unused_sort_key(slot: str) -> int:
    return int(slot.replace("unused", ""))


def shell_quote(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
