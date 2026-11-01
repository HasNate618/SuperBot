#!/usr/bin/env python3
"""Decrypt or encrypt Job Simulator's JobSimulatorSave.save.

    jobsim_save.py decrypt [SRC] [DST]
    jobsim_save.py encrypt [SRC] [DST]

Defaults are JobSimulatorSave.save and JobSimulatorSave.xml next to this file.
The encryption is AES-256-CBC with no padding. Key and IV come from
PBKDF2-HMAC-SHA1(Inputkey, Salt, 1000, 48); the XML is padded to a 16-byte
boundary with trailing carriage returns.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

INPUT_KEY = b"560A11CD-6346-3CF0-C2E8-671F9B2B9EA1"
SALT = b"Lots of text goes here in this place"
ROUNDS = 1000
HERE = Path(__file__).resolve().parent


def derive_key_iv() -> tuple[bytes, bytes]:
    derived = hashlib.pbkdf2_hmac("sha1", INPUT_KEY, SALT, ROUNDS, 48)
    return derived[:32], derived[32:48]


def openssl_command() -> list[str]:
    exe = shutil.which("openssl")
    if exe:
        return [exe]
    nix = shutil.which("nix")
    if nix:
        return [nix, "shell", "nixpkgs#openssl", "--command", "openssl"]
    sys.exit("openssl not found on PATH; on NixOS run inside `nix shell nixpkgs#openssl`")


def transform(data: bytes, decrypt: bool) -> bytes:
    key, iv = derive_key_iv()
    direction = "-d" if decrypt else "-e"
    result = subprocess.run(
        openssl_command()
        + [
            "enc",
            "-aes-256-cbc",
            direction,
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        sys.exit(result.stderr.decode(errors="replace").strip())
    return result.stdout


def decrypt(src: Path, dst: Path) -> None:
    dst.write_bytes(transform(src.read_bytes(), decrypt=True).rstrip(b"\r"))


def encrypt(src: Path, dst: Path) -> None:
    data = src.read_bytes().rstrip(b"\r")
    data += b"\r" * ((16 - len(data) % 16) % 16)
    dst.write_bytes(transform(data, decrypt=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("decrypt", "encrypt"))
    parser.add_argument("src", nargs="?", type=Path)
    parser.add_argument("dst", nargs="?", type=Path)
    args = parser.parse_args()

    if args.command == "decrypt":
        src = args.src or HERE / "JobSimulatorSave.save"
        dst = args.dst or HERE / "JobSimulatorSave.xml"
        decrypt(src, dst)
    else:
        src = args.src or HERE / "JobSimulatorSave.xml"
        dst = args.dst or HERE / "JobSimulatorSave.save"
        encrypt(src, dst)


if __name__ == "__main__":
    main()
