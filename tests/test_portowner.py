import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clank import portowner

# One LISTEN (0A) socket on 4242 (0x1092) and one ESTABLISHED (01) on the same port.
TCP_TEXT = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1092 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 3859874 1
   1: 0100007F:1092 0100007F:A1B2 01 00000000:00000000 00:00000000 00000000  1000        0 4125407 1
   2: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 9999999 1
"""


def make_proc(root: Path, procs: dict[int, str], inodes: dict[int, str]) -> None:
    """Build a fake /proc: <pid>/cmdline plus one socket fd symlink per pid."""
    for pid, command in procs.items():
        pdir = root / str(pid)
        (pdir / "fd").mkdir(parents=True)
        (pdir / "cmdline").write_bytes(command.encode().replace(b" ", b"\x00") + b"\x00")
        if pid in inodes:
            os.symlink(f"socket:[{inodes[pid]}]", pdir / "fd" / "3")


class ListeningInodeTests(unittest.TestCase):
    def test_finds_only_the_listening_socket_on_the_port(self) -> None:
        self.assertEqual(portowner.listening_inodes(TCP_TEXT, 4242), ("3859874",))

    def test_ignores_other_ports(self) -> None:
        self.assertEqual(portowner.listening_inodes(TCP_TEXT, 4243), ())

    def test_tolerates_a_header_only_file(self) -> None:
        header = TCP_TEXT.splitlines()[0]
        self.assertEqual(portowner.listening_inodes(header, 4242), ())


class HolderTests(unittest.TestCase):
    def test_finds_the_process_holding_an_inode(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            make_proc(root, {4242: "monado-service", 999: "bash"}, {4242: "3859874"})
            found, unresolved = portowner.holders(
                4242, proc_root=root, net_text=TCP_TEXT
            )
        self.assertFalse(unresolved)
        self.assertEqual([h.pid for h in found], [4242])
        self.assertIn("monado-service", found[0].command)

    def test_reports_unresolved_when_the_fd_is_not_readable(self) -> None:
        # The real failure that blocked a session start: a listener whose socket
        # inode belongs to a process whose /proc/<pid>/fd cannot be read.
        with TemporaryDirectory() as raw:
            root = Path(raw)
            make_proc(root, {4242: "monado-service"}, {})
            found, unresolved = portowner.holders(
                4242, proc_root=root, net_text=TCP_TEXT
            )
        self.assertEqual(found, [])
        self.assertTrue(unresolved, "a busy port with no visible owner must not look free")


class CandidatesTests(unittest.TestCase):
    def test_matches_by_command_name(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            make_proc(root, {7: "monado-service --foo", 8: "bash -c hi"}, {})
            found = portowner.candidates_by_name("monado", proc_root=root)
        self.assertEqual([h.pid for h in found], [7])


class PlanTests(unittest.TestCase):
    def _plan(self, procs, inodes, expect="monado"):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            make_proc(root, procs, inodes)
            return portowner.plan(expect, proc_root=root, net_text=TCP_TEXT)

    def test_reclaims_our_own_monado(self) -> None:
        result = self._plan({4242: "monado-service --remote"}, {4242: "3859874"})
        self.assertEqual([h.pid for h in result.kill], [4242])
        self.assertEqual(result.refuse, "")

    def test_refuses_a_stranger(self) -> None:
        result = self._plan({4242: "game-server --port 4242"}, {4242: "3859874"})
        self.assertEqual(result.kill, [])
        self.assertIn("game-server", result.refuse)

    def test_falls_back_to_a_name_match_when_the_owner_is_invisible(self) -> None:
        result = self._plan({1410672: "monado-service", 5: "bash"}, {})
        self.assertEqual([h.pid for h in result.kill], [1410672])
        self.assertEqual(result.refuse, "")

    def test_refuses_when_the_holder_cannot_be_identified_at_all(self) -> None:
        result = self._plan({5: "bash"}, {})
        self.assertEqual(result.kill, [])
        self.assertIn("could not be identified", result.refuse)

    def test_refuses_ambiguous_name_matches(self) -> None:
        result = self._plan({10: "monado-service", 11: "monado-service"}, {})
        self.assertEqual(result.kill, [])
        self.assertIn("more than one", result.refuse)

    def test_free_port_plans_nothing(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            result = portowner.plan("monado", proc_root=root, net_text="")
        self.assertEqual((result.kill, result.refuse), ([], ""))


if __name__ == "__main__":
    unittest.main()
