"""Who is holding a TCP port, and may we take it back?

Preflight used to refuse outright when 4242 was busy: "TCP 4242 is already in
use". That is the right answer for a stranger and the wrong answer for our own
leftover Monado, which is what actually happens. A session's monado-service can
survive a teardown, is reparented to init, and therefore matches no systemd unit -
so the unit-reclaim path added for monado.socket never sees it, 4242 stays held,
and every later start refuses to run.

Resolving the holder is normally easy: the listening socket's inode in
/proc/net/tcp appears as `socket:[inode]` in the owning process's fd table. But
that lookup can come up empty - observed in the wild with the orphaned Monado,
whose fds could not be read from outside its namespace. So "busy with no visible
owner" is a distinct state, not a free port, and it falls back to a name match
before giving up.

Stdlib only, and every filesystem read is injectable so the tests can build a fake
/proc instead of depending on this machine's process table.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TCP_FILES = ("tcp", "tcp6")
LISTEN = "0A"


@dataclass(frozen=True)
class Holder:
    """A process that looks like it owns a port."""

    pid: int
    command: str


@dataclass(frozen=True)
class Plan:
    """What to kill, or why we refuse. Exactly one of the two is populated."""

    kill: list[Holder]
    refuse: str


def listening_inodes(text: str, port: int) -> tuple[str, ...]:
    """Socket inodes in LISTEN state on `port`, from a /proc/net/tcp body."""
    found: list[str] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if local_port == port and fields[3] == LISTEN:
            found.append(fields[9])
    return tuple(found)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _command(pid_dir: Path) -> str:
    """Argv as one line. cmdline is NUL separated, and empty for kernel threads."""
    raw = ""
    try:
        raw = pid_dir.joinpath("cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""
    return " ".join(part for part in raw.split("\x00") if part)


def _digits(proc_root: Path) -> list[Path]:
    return [e for e in _iter(proc_root) if e.name.isdigit()]


def _iter(path: Path) -> list[Path]:
    """It is normal for /proc to change under us: a process exits mid-scan."""
    try:
        return list(path.iterdir())
    except OSError:
        return []


def candidates_by_name(name: str, proc_root: Path = Path("/proc")) -> list[Holder]:
    """Processes whose cmdline mentions `name`, for when the inode cannot be mapped."""
    found: list[Holder] = []
    for entry in _digits(proc_root):
        command = _command(entry)
        if name in command:
            found.append(Holder(pid=int(entry.name), command=command))
    return found


def holders(
    port: int, proc_root: Path = Path("/proc"), net_text: str | None = None
) -> tuple[list[Holder], bool]:
    """(processes holding the port, whether a listener had no identifiable owner)."""
    if net_text is None:
        net_text = "".join(_read(Path("/proc/net") / f) for f in TCP_FILES)

    inodes = listening_inodes(net_text, port)
    if not inodes:
        return [], False

    found: dict[int, Holder] = {}
    for entry in _digits(proc_root):
        fd_dir = entry / "fd"
        if not fd_dir.is_dir():
            continue
        for fd in _iter(fd_dir):
            try:
                target = fd.readlink().as_posix()
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                found[int(entry.name)] = Holder(pid=int(entry.name), command=_command(entry))
                break
    return list(found.values()), not found


def plan(expect: str, proc_root: Path = Path("/proc"), net_text: str | None = None,
         port: int = 4242) -> Plan:
    """Decide whether the port's holder is ours to reclaim.

    Ours means: we identified the holder and every holder's command mentions
    `expect`. Anything else is refused with a message an operator can act on,
    because killing a stranger's listener is worse than not starting.
    """
    found, unresolved = holders(port, proc_root=proc_root, net_text=net_text)
    if found:
        strangers = [h for h in found if expect not in h.command]
        if strangers:
            who = ", ".join(f"pid {h.pid} ({h.command[:60]})" for h in strangers)
            return Plan([], f"port {port} is held by {who}, which is not a {expect} process")
        return Plan(found, "")
    if not unresolved:
        return Plan([], "")

    matches = candidates_by_name(expect, proc_root=proc_root)
    if len(matches) == 1:
        return Plan(matches, "")
    if len(matches) > 1:
        who = ", ".join(f"pid {h.pid}" for h in matches)
        return Plan([], f"port {port} is held by more than one {expect} process ({who})")
    return Plan(
        [],
        f"port {port} is in use but its holder could not be identified "
        f"(no visible {expect} process); stop it and retry",
    )
