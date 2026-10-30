#!/usr/bin/env python3
"""Grab one frame from Weston's VNC backend, with no browser in the path.

Why this exists: the game view is only available as the VNC framebuffer.
`weston-screenshooter` blocks in VNC mode (measured), the dashboard canvas is the
operator, and `body_latest.jpg` is the MuJoCo tile. So a glow or slot reading needs
its own RFB client.

Standard library only, and it honours the pixel format the server announces rather
than demanding one. Weston has historically ignored SetPixelFormat, so parsing
`ServerInit` is the difference between working and producing colour-swapped
garbage that still looks plausible.

Usage:
    VNC_PASSWORD=... python3 spike/vnc_grab.py --out /tmp/frame.ppm
    VNC_PASSWORD=... python3 spike/vnc_grab.py --out /tmp/frame.ppm --repeat 3

The password is read from the environment (or `--password-file`) and never
appears in argv, in a log line or in a temporary file. `VNC_USERNAME` is
optional; without it the username field is sent empty.

The output is binary PPM (P6): trivial to write with no dependency, and any image
tool in the venv can convert it to PNG.

Authentication
--------------
Weston 16.0.0's VNC backend (`--backend=vnc ... --disable-transport-layer-security`)
links neatvnc 1.0.3 and offers security types 129 (RSA-AES256), 5 (RSA-AES) and
30 (Apple DH); it does not offer 1 (None). RSA-AES is described by the
implementations, not by RFC 6143. Verified against:

  neatvnc, the server:
    https://github.com/any1/neatvnc/blob/v1.0.3/src/auth/rsa-aes.c
    https://github.com/any1/neatvnc/blob/v1.0.3/src/stream/rsa-aes.c
    https://github.com/any1/neatvnc/blob/v1.0.3/src/crypto/nettle/cipher.c
    https://github.com/any1/neatvnc/blob/v1.0.3/include/rfb-proto.h
  TigerVNC, the reference client:
    https://github.com/TigerVNC/tigervnc/blob/master/common/rfb/CSecurityRSAAES.cxx
    https://github.com/TigerVNC/tigervnc/blob/master/common/rdr/AESInStream.cxx

All integers are big-endian. The keys are plain RSA -- no curve, no
certificate: `modulus_and_exponent` is the modulus and then the exponent, each
written as exactly `size` bytes with leading zero padding, where `size` is the
key length in bytes, `ceil(bits / 8)`. Weston generates a 2048-bit key, and the
client answers with a key of the same bit length. Sequence after the client
sends the chosen security type:

  1. S->C  u32 bits, modulus[size], exponent[size]      server RSA public key
  2. C->S  u32 bits, modulus[size], exponent[size]      client RSA public key
  3. C->S  u16 size, RSAES-PKCS1-v1_5(server key, ClientRandom[16 | 32])
  4. S->C  u16 size, RSAES-PKCS1-v1_5(client key, ServerRandom[16 | 32])
  5. both  ClientSessionKey = H(ServerRandom || ClientRandom)[:16 | 32]
           ServerSessionKey = H(ClientRandom || ServerRandom)[:16 | 32]
           type 5: H = SHA-1, AES-128;  type 129: H = SHA-256, AES-256
  6. C->S  H(u32 client bits || client modulus || client exponent
             || u32 server bits || server modulus || server exponent)
  7. S->C  H(u32 server bits || server modulus || server exponent
             || u32 client bits || client modulus || client exponent)
  8. S->C  u8 credential subtype (1 = user and pass, 2 = password only)
  9. C->S  u8 username_len, username, u8 password_len, password
 10. S->C  u32 security result (0 = OK, then an optional reason string)

Steps 1-4 are plaintext; from step 6 on everything (including ClientInit,
ServerInit and the framebuffer) travels inside an AES-EAX stream. EAX, not CBC
or GCM: a message is `u16 length || ciphertext || 16-byte tag`, the two-byte
length is the associated data, and at most 8192 plaintext bytes go in one
message. There is no IV: the nonce is a 16-byte little-endian counter that
starts at zero in each direction and increments once per message. The RSA
padding is RSAES-PKCS1-v1_5 (nettle's `rsa_encrypt`), not OAEP.

`openssl` cannot produce AES-EAX, and this host has no `openssl` binary at all,
so the primitives are implemented here: AES from FIPS-197 (encryption direction
only -- EAX uses CTR and CMAC, both forward), CMAC from NIST SP 800-38B, EAX
from the EAX paper, and RSA with PKCS#1 v1.5 padding. tests/test_vnc_auth.py
pins each of them to published known-answer vectors.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import socket
import struct
import sys
import time
from pathlib import Path

RFB_VERSION = b"RFB 003.008\n"

# Encoding numbers. Raw is the only one every server must support.
ENC_RAW = 0
ENC_DESKTOP_SIZE = -223

# Security types.
SEC_NONE = 1
SEC_RSA_AES = 5
SEC_APPLE_DH = 30
SEC_RSA_AES256 = 129

SECURITY_NAMES = {
    1: "None",
    2: "VNC (DES)",
    5: "RSA-AES",
    16: "Tight",
    18: "TLS",
    19: "VeNCrypt",
    30: "Apple DH",
    129: "RSA-AES256",
}

# RSA-AES parameters per security type, from neatvnc's server.c.
RSA_AES_PARAMS = {
    SEC_RSA_AES: (16, hashlib.sha1),     # 128-bit AES, SHA-1
    SEC_RSA_AES256: (32, hashlib.sha256),  # 256-bit AES, SHA-256
}

MAX_PLAINTEXT_MESSAGE = 8192  # neatvnc's RSA_AES_BUFFER_SIZE
EAX_TAG_SIZE = 16


class RfbError(RuntimeError):
    """Anything that stops a frame being captured."""


# --------------------------------------------------------------------------
# AES (FIPS-197). Encryption direction only: EAX runs the block cipher forward
# in both CTR mode and CMAC, so no inverse cipher is needed.
# --------------------------------------------------------------------------

def _gf_mul(a: int, b: int) -> int:
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        b >>= 1
    return result


def _build_aes_tables() -> tuple[list[int], list[list[int]]]:
    """Return (sbox, [Te0, Te1, Te2, Te3]) built from the field arithmetic.

    Built rather than transcribed: a typo in a 256-entry table is invisible to
    inspection but shows up as a wrong known-answer vector.
    """
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = (x ^ (x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    sbox = [0] * 256
    for i in range(256):
        inverse = 0 if i == 0 else exp[255 - log[i]]
        s = inverse
        for shift in range(1, 5):
            s ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xFF
        sbox[i] = s ^ 0x63

    tables = [[0] * 256 for _ in range(4)]
    for i, s in enumerate(sbox):
        s2 = _gf_mul(s, 2)
        word = (s2 << 24) | (s << 16) | (s << 8) | (s2 ^ s)
        tables[0][i] = word
        tables[1][i] = ((word >> 8) | (word << 24)) & 0xFFFFFFFF
        tables[2][i] = ((word >> 16) | (word << 16)) & 0xFFFFFFFF
        tables[3][i] = ((word >> 24) | (word << 8)) & 0xFFFFFFFF
    return sbox, tables


AES_SBOX, (AES_TE0, AES_TE1, AES_TE2, AES_TE3) = _build_aes_tables()


def _rot_word(word: int) -> int:
    return ((word << 8) | (word >> 24)) & 0xFFFFFFFF


def _sub_word(word: int) -> int:
    return (
        (AES_SBOX[(word >> 24) & 0xFF] << 24)
        | (AES_SBOX[(word >> 16) & 0xFF] << 16)
        | (AES_SBOX[(word >> 8) & 0xFF] << 8)
        | AES_SBOX[word & 0xFF]
    )


class _Aes:
    """AES-128/192/256 forward cipher with a pre-expanded key schedule."""

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError(f"AES key must be 16, 24 or 32 bytes, not {len(key)}")
        nk = len(key) // 4
        self.rounds = nk + 6
        schedule = [
            int.from_bytes(key[4 * i:4 * i + 4], "big") for i in range(nk)
        ]
        rcon = 1
        for i in range(nk, 4 * (self.rounds + 1)):
            temp = schedule[i - 1]
            if i % nk == 0:
                temp = _sub_word(_rot_word(temp)) ^ (rcon << 24)
                rcon = _gf_mul(rcon, 2)
            elif nk > 6 and i % nk == 4:
                temp = _sub_word(temp)
            schedule.append(schedule[i - nk] ^ temp)
        self._schedule = schedule

    def encrypt_block(self, block: bytes) -> bytes:
        rk = self._schedule
        te0, te1, te2, te3 = AES_TE0, AES_TE1, AES_TE2, AES_TE3
        sbox = AES_SBOX
        s0 = int.from_bytes(block[0:4], "big") ^ rk[0]
        s1 = int.from_bytes(block[4:8], "big") ^ rk[1]
        s2 = int.from_bytes(block[8:12], "big") ^ rk[2]
        s3 = int.from_bytes(block[12:16], "big") ^ rk[3]
        for r in range(1, self.rounds):
            i = r * 4
            t0 = (
                te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF]
                ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ rk[i]
            )
            t1 = (
                te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF]
                ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ rk[i + 1]
            )
            t2 = (
                te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF]
                ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ rk[i + 2]
            )
            t3 = (
                te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF]
                ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ rk[i + 3]
            )
            s0, s1, s2, s3 = t0, t1, t2, t3
        i = self.rounds * 4
        o0 = (
            (sbox[s0 >> 24] << 24) | (sbox[(s1 >> 16) & 0xFF] << 16)
            | (sbox[(s2 >> 8) & 0xFF] << 8) | sbox[s3 & 0xFF]
        ) ^ rk[i]
        o1 = (
            (sbox[s1 >> 24] << 24) | (sbox[(s2 >> 16) & 0xFF] << 16)
            | (sbox[(s3 >> 8) & 0xFF] << 8) | sbox[s0 & 0xFF]
        ) ^ rk[i + 1]
        o2 = (
            (sbox[s2 >> 24] << 24) | (sbox[(s3 >> 16) & 0xFF] << 16)
            | (sbox[(s0 >> 8) & 0xFF] << 8) | sbox[s1 & 0xFF]
        ) ^ rk[i + 2]
        o3 = (
            (sbox[s3 >> 24] << 24) | (sbox[(s0 >> 16) & 0xFF] << 16)
            | (sbox[(s1 >> 8) & 0xFF] << 8) | sbox[s2 & 0xFF]
        ) ^ rk[i + 3]
        return b"".join(w.to_bytes(4, "big") for w in (o0, o1, o2, o3))


# --------------------------------------------------------------------------
# CMAC (NIST SP 800-38B) and EAX (Bellare/Rogaway/Wagner), as used by RFB.
# --------------------------------------------------------------------------

_GF128_REDUCTION = 0x87


def _dbl_gf128(value: int) -> int:
    value <<= 1
    if value >> 128:
        value = (value & ((1 << 128) - 1)) ^ _GF128_REDUCTION
    return value


def _cmac(aes: _Aes, message: bytes) -> bytes:
    subkey1 = _dbl_gf128(int.from_bytes(aes.encrypt_block(bytes(16)), "big"))
    subkey2 = _dbl_gf128(subkey1)
    if not message:
        last = (subkey2 ^ (0x80 << 120)).to_bytes(16, "big")
        return aes.encrypt_block(last)

    blocks = [message[i:i + 16] for i in range(0, len(message), 16)]
    if len(blocks[-1]) == 16:
        blocks[-1] = (
            int.from_bytes(blocks[-1], "big") ^ subkey1
        ).to_bytes(16, "big")
    else:
        padded = blocks[-1] + b"\x80" + bytes(15 - len(blocks[-1]))
        blocks[-1] = (
            int.from_bytes(padded, "big") ^ subkey2
        ).to_bytes(16, "big")

    state = bytes(16)
    for block in blocks:
        state = aes.encrypt_block(
            bytes(a ^ b for a, b in zip(state, block, strict=True))
        )
    return state


def _ctr(aes: _Aes, initial_counter: int, data: bytes) -> bytes:
    out = bytearray()
    counter = initial_counter
    for i in range(0, len(data), 16):
        keystream = aes.encrypt_block(counter.to_bytes(16, "big"))
        chunk = data[i:i + 16]
        out.extend(a ^ b for a, b in zip(chunk, keystream, strict=False))
        counter = (counter + 1) & ((1 << 128) - 1)
    return bytes(out)


def _eax_omac(aes: _Aes, domain: int, data: bytes) -> int:
    """OMAC^domain(data) from the EAX paper: a CMAC with a domain-separated IV."""
    return int.from_bytes(
        _cmac(aes, bytes(15) + bytes([domain]) + data), "big"
    )


def _eax_seal(aes: _Aes, nonce: bytes, aad: bytes, plaintext: bytes):
    nonce_mac = _eax_omac(aes, 0, nonce)
    header_mac = _eax_omac(aes, 1, aad)
    ciphertext = _ctr(aes, nonce_mac, plaintext)
    tag = (nonce_mac ^ header_mac ^ _eax_omac(aes, 2, ciphertext))
    return ciphertext, tag.to_bytes(EAX_TAG_SIZE, "big")


def _eax_open(aes: _Aes, nonce: bytes, aad: bytes, ciphertext: bytes,
              tag: bytes) -> bytes:
    nonce_mac = _eax_omac(aes, 0, nonce)
    header_mac = _eax_omac(aes, 1, aad)
    expected = (nonce_mac ^ header_mac ^ _eax_omac(aes, 2, ciphertext))
    if not hmac.compare_digest(expected.to_bytes(EAX_TAG_SIZE, "big"), tag):
        raise RfbError(
            "VNC message authentication failed: the session keys do not match "
            "the server's (wrong password, or the server rejected the "
            "handshake)"
        )
    return _ctr(aes, nonce_mac, ciphertext)


class _EaxCipher:
    """One direction of the RFB RSA-AES message stream.

    The nonce is a 128-bit little-endian counter starting at zero, and the
    two-byte length prefix is authenticated as associated data; both details
    come from neatvnc's stream/rsa-aes.c and TigerVNC's AESInStream.
    """

    def __init__(self, key: bytes) -> None:
        self._aes = _Aes(key)
        self._counter = bytearray(16)

    def _nonce(self) -> bytes:
        return bytes(self._counter)

    def _advance(self) -> None:
        for i in range(16):
            self._counter[i] = (self._counter[i] + 1) & 0xFF
            if self._counter[i]:
                break

    def seal(self, payload: bytes) -> bytes:
        out = bytearray()
        for start in range(0, len(payload), MAX_PLAINTEXT_MESSAGE):
            chunk = payload[start:start + MAX_PLAINTEXT_MESSAGE]
            header = struct.pack(">H", len(chunk))
            ciphertext, tag = _eax_seal(
                self._aes, self._nonce(), header, chunk
            )
            out += header + ciphertext + tag
            self._advance()
        return bytes(out)

    def open(self, sock) -> bytes:
        header = _recv_exact(sock, 2)
        length = struct.unpack(">H", header)[0]
        body = _recv_exact(sock, length + EAX_TAG_SIZE)
        plaintext = _eax_open(
            self._aes, self._nonce(), header,
            body[:length], body[length:],
        )
        self._advance()
        return plaintext


class _AesEaxStream:
    """Socket-shaped wrapper: `recv` and `sendall` see decrypted bytes."""

    def __init__(self, sock: socket.socket, in_key: bytes, out_key: bytes):
        self._sock = sock
        self._in = _EaxCipher(in_key)
        self._out = _EaxCipher(out_key)
        self._buffer = bytearray()

    def recv(self, count: int) -> bytes:
        # A raw framebuffer is asked for in one call but arrives as a stream of
        # 8 KiB EAX messages, so the buffer grows by append rather than by
        # repeated concatenation.
        while len(self._buffer) < count:
            self._buffer.extend(self._in.open(self._sock))
        chunk = bytes(self._buffer[:count])
        del self._buffer[:count]
        return chunk

    def sendall(self, data: bytes) -> None:
        if data:
            self._sock.sendall(self._out.seal(data))


# --------------------------------------------------------------------------
# RSA with RSAES-PKCS1-v1_5 padding (nettle's rsa_encrypt / rsa_decrypt).
# --------------------------------------------------------------------------

_SMALL_PRIMES = [
    candidate for candidate in range(3, 2000, 2)
    if all(candidate % divisor
           for divisor in range(3, int(candidate ** 0.5) + 1, 2))
]


class _RsaKey:
    """An RSA key. `exponent` is 65537 in a public key, and d in a private one."""

    def __init__(self, modulus: int, exponent: int, bits: int) -> None:
        self.modulus = modulus
        self.exponent = exponent
        self.bits = bits

    @property
    def size(self) -> int:
        return (self.bits + 7) // 8


def _is_probable_prime(candidate: int, rounds: int = 32) -> bool:
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate % prime == 0:
            return candidate == prime
    low_bit = (candidate - 1) & -(candidate - 1)
    shift = low_bit.bit_length() - 1
    odd = (candidate - 1) >> shift
    for _ in range(rounds):
        base = secrets.randbelow(candidate - 3) + 2
        value = pow(base, odd, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(shift - 1):
            value = value * value % candidate
            if value == candidate - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    while True:
        # The top two bits force the product of two such primes to be exactly
        # `bits` long, which is what the server expects us to announce.
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _rsa_generate(bits: int = 2048) -> tuple[_RsaKey, _RsaKey]:
    exponent = 65537
    while True:
        p = _generate_prime(bits // 2)
        q = _generate_prime(bits // 2)
        if p == q:
            continue
        modulus = p * q
        if modulus.bit_length() != bits:
            continue
        totient = (p - 1) * (q - 1)
        if totient % exponent == 0:
            continue
        private_exponent = pow(exponent, -1, totient)
        return (
            _RsaKey(modulus, exponent, bits),
            _RsaKey(modulus, private_exponent, bits),
        )


def _rsa_encrypt(key: _RsaKey, message: bytes,
                 randbytes=secrets.token_bytes) -> bytes:
    """RSAES-PKCS1-v1_5: 0x00 || 0x02 || PS || 0x00 || message.

    `randbytes` exists so the known-answer test can pin the padding bytes.
    """
    size = key.size
    padding_length = size - 3 - len(message)
    if padding_length < 8:
        raise RfbError(
            f"cannot RSA-encrypt {len(message)} bytes with a {key.bits}-bit key"
        )
    padding = bytearray()
    while len(padding) < padding_length:
        padding += bytes(
            b for b in randbytes(padding_length - len(padding)) if b
        )
    block = b"\x00\x02" + bytes(padding) + b"\x00" + message
    return pow(int.from_bytes(block, "big"), key.exponent, key.modulus).to_bytes(
        size, "big"
    )


def _rsa_decrypt(key: _RsaKey, ciphertext: bytes) -> bytes:
    size = key.size
    if len(ciphertext) != size:
        raise RfbError(
            f"RSA ciphertext is {len(ciphertext)} bytes, expected {size}"
        )
    block = pow(
        int.from_bytes(ciphertext, "big"), key.exponent, key.modulus
    ).to_bytes(size, "big")
    if block[0] != 0 or block[1] != 2:
        raise RfbError("RSA challenge is not PKCS#1 v1.5 padded")
    separator = block.find(b"\x00", 2)
    if separator < 10:
        raise RfbError("RSA challenge has a malformed PKCS#1 v1.5 padding block")
    return block[separator + 1:]


# --------------------------------------------------------------------------
# RFB framing helpers.
# --------------------------------------------------------------------------

def _recv_exact(sock, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise RfbError(f"connection closed after {len(buf)} of {count} bytes")
        buf += chunk
    return buf


def _read_u32(sock) -> int:
    return struct.unpack(">I", _recv_exact(sock, 4))[0]


def _read_public_key(sock) -> _RsaKey:
    bits = _read_u32(sock)
    if not 1024 <= bits <= 8192:
        raise RfbError(f"server RSA key is {bits} bits; expected 1024..8192")
    size = (bits + 7) // 8
    modulus = int.from_bytes(_recv_exact(sock, size), "big")
    exponent = int.from_bytes(_recv_exact(sock, size), "big")
    if modulus <= exponent:
        raise RfbError("server RSA key has a modulus smaller than its exponent")
    return _RsaKey(modulus, exponent, bits)


def _write_public_key(sock, key: _RsaKey) -> None:
    size = key.size
    sock.sendall(
        struct.pack(">I", key.bits)
        + key.modulus.to_bytes(size, "big")
        + key.exponent.to_bytes(size, "big")
    )


def _rsa_aes_authenticate(sock, security_type: int, username: str,
                          password: str) -> _AesEaxStream:
    """Run the RSA-AES handshake and return the encrypted stream."""
    key_size, digest = RSA_AES_PARAMS[security_type]

    server_key = _read_public_key(sock)
    client_public, client_private = _rsa_generate(server_key.bits)
    _write_public_key(sock, client_public)

    client_random = secrets.token_bytes(key_size)
    sock.sendall(
        struct.pack(">H", client_public.size)
        + _rsa_encrypt(server_key, client_random)
    )

    challenge_size = struct.unpack(">H", _recv_exact(sock, 2))[0]
    if challenge_size != client_public.size:
        raise RfbError(
            f"server sent a {challenge_size}-byte RSA challenge, expected "
            f"{client_public.size}"
        )
    server_random = _rsa_decrypt(
        client_private, _recv_exact(sock, challenge_size)
    )
    if len(server_random) != key_size:
        raise RfbError(
            f"server random is {len(server_random)} bytes, expected {key_size}"
        )

    # ClientSessionKey and ServerSessionKey, from neatvnc's auth/rsa-aes.c.
    out_key = digest(server_random + client_random).digest()[:key_size]
    in_key = digest(client_random + server_random).digest()[:key_size]
    stream = _AesEaxStream(sock, in_key, out_key)

    client_bits = struct.pack(">I", client_public.bits)
    server_bits = struct.pack(">I", server_key.bits)
    client_material = (
        client_public.modulus.to_bytes(client_public.size, "big")
        + client_public.exponent.to_bytes(client_public.size, "big")
    )
    server_material = (
        server_key.modulus.to_bytes(server_key.size, "big")
        + server_key.exponent.to_bytes(server_key.size, "big")
    )
    stream.sendall(
        digest(client_bits + client_material + server_bits
               + server_material).digest()
    )

    expected = digest(server_bits + server_material + client_bits
                      + client_material).digest()
    received = _recv_exact(stream, len(expected))
    if not hmac.compare_digest(received, expected):
        raise RfbError("server RSA key hash does not match; refusing to continue")

    subtype = _recv_exact(stream, 1)[0]
    if subtype not in (1, 2):
        raise RfbError(f"unknown RSA-AES credential subtype {subtype}")
    username_bytes = username.encode("utf-8") if subtype == 1 else b""
    password_bytes = password.encode("utf-8")
    if len(username_bytes) > 255 or len(password_bytes) > 255:
        raise RfbError("username and password must be at most 255 bytes")
    stream.sendall(
        bytes([len(username_bytes)]) + username_bytes
        + bytes([len(password_bytes)]) + password_bytes
    )
    return stream


def _security_type_error(types: bytes) -> RfbError:
    offered = ", ".join(SECURITY_NAMES.get(t, str(t)) for t in types)
    if list(types) == [SEC_APPLE_DH]:
        return RfbError(
            "server offers only Apple DH (security type 30), which this "
            "grabber does not implement"
        )
    return RfbError(
        f"server requires authentication this grabber does not support: "
        f"{offered or 'none offered'}"
    )


def _read_security_result(sock) -> None:
    result = _read_u32(sock)
    if result == 0:
        return
    reason = ""
    try:
        reason_len = _read_u32(sock)
        reason = _recv_exact(sock, reason_len).decode("utf-8", "replace")
    except RfbError:
        pass  # the reason string is optional (RFB 3.8 only) and the server closes
    # Weston checks the username with getpwnam() and the password with PAM
    # (libweston/backend-vnc/vnc.c, libweston/auth.c), so a local account name
    # is part of the credential, not an optional extra.
    raise RfbError(
        "authentication rejected by the server"
        + (f": {reason}" if reason else "")
        + ". Weston authenticates a local account: set VNC_USERNAME to that "
        "account and VNC_PASSWORD to its password."
    )


def _pixel_decoder(fmt: bytes):
    """Build an RGB converter from a 16-byte RFB PIXEL_FORMAT.

    Layout: bpp, depth, big-endian-flag, true-colour-flag, red-max, green-max,
    blue-max, red-shift, green-shift, blue-shift, then 3 unused bytes.
    """
    bpp, depth, big_endian, true_colour = fmt[0], fmt[1], fmt[2], fmt[3]
    if not true_colour:
        raise RfbError("server announced a colour-map format; not supported")
    if bpp not in (16, 24, 32):
        raise RfbError(f"unsupported bits-per-pixel {bpp}")
    if depth > bpp:
        # A depth wider than the pixel itself means the format was misread, and a
        # misread format produces colour-swapped pixels that still look plausible.
        raise RfbError(f"depth {depth} exceeds bits-per-pixel {bpp}")
    r_max, g_max, b_max = struct.unpack(">HHH", fmt[4:10])
    r_shift, g_shift, b_shift = fmt[10], fmt[11], fmt[12]
    order = "big" if big_endian else "little"
    step = bpp // 8

    def decode(data: bytes) -> bytearray:
        out = bytearray(len(data) // step * 3)
        o = 0
        for i in range(0, len(data) - step + 1, step):
            value = int.from_bytes(data[i:i + step], order)
            r = (value >> r_shift) & r_max
            g = (value >> g_shift) & g_max
            b = (value >> b_shift) & b_max
            # Scale the server's maxima to 8 bits.
            out[o] = r * 255 // r_max if r_max else 0
            out[o + 1] = g * 255 // g_max if g_max else 0
            out[o + 2] = b * 255 // b_max if b_max else 0
            o += 3
        return out

    return decode


def grab_frame(host: str, port: int, username: str = "", password: str = "",
               timeout: float = 10.0) -> tuple[int, int, bytearray]:
    """Return `(width, height, rgb_bytes)` for one full framebuffer update."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)

        banner = _recv_exact(sock, 12)
        if not banner.startswith(b"RFB "):
            raise RfbError(f"not an RFB server: {banner!r}")
        sock.sendall(RFB_VERSION)

        n_types = _recv_exact(sock, 1)[0]
        if n_types == 0:
            reason_len = _read_u32(sock)
            raise RfbError(_recv_exact(sock, reason_len).decode("utf-8", "replace"))
        types = _recv_exact(sock, n_types)
        if SEC_RSA_AES256 in types:
            security_type = SEC_RSA_AES256
        elif SEC_RSA_AES in types:
            security_type = SEC_RSA_AES
        elif SEC_NONE in types:
            security_type = SEC_NONE
        else:
            raise _security_type_error(types)

        if security_type == SEC_NONE:
            sock.sendall(bytes([SEC_NONE]))
            stream = sock
        else:
            if not password:
                raise RfbError(
                    "the server offers "
                    f"{SECURITY_NAMES[security_type]} (security type "
                    f"{security_type}), which needs a password: set "
                    "VNC_PASSWORD=... (or pass --password-file PATH)"
                )
            sock.sendall(bytes([security_type]))
            stream = _rsa_aes_authenticate(sock, security_type, username, password)
        _read_security_result(stream)

        stream.sendall(bytes([1]))  # ClientInit, shared = 1

        head = _recv_exact(stream, 24)
        width, height = struct.unpack(">HH", head[0:4])
        pixel_format = head[4:20]
        name_len = struct.unpack(">I", head[20:24])[0]
        if name_len:
            _recv_exact(stream, name_len)
        decode = _pixel_decoder(pixel_format)

        # Raw only. Anything cleverer would save bandwidth we do not need for one
        # frame and cost a decoder we would have to trust.
        stream.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", ENC_RAW))
        stream.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

        while True:
            msg = _recv_exact(stream, 1)[0]
            if msg == 0:  # FramebufferUpdate
                _recv_exact(stream, 1)
                n_rects = struct.unpack(">H", _recv_exact(stream, 2))[0]
                canvas = bytearray(width * height * 3)
                for _ in range(n_rects):
                    x, y, w, h, encoding = struct.unpack(
                        ">HHHHi", _recv_exact(stream, 12)
                    )
                    if encoding == ENC_DESKTOP_SIZE:
                        width, height = w, h
                        canvas = bytearray(width * height * 3)
                        continue
                    if encoding != ENC_RAW:
                        raise RfbError(f"unexpected encoding {encoding}")
                    rgb = decode(
                        _recv_exact(stream, w * h * (pixel_format[0] // 8))
                    )
                    row_bytes = w * 3
                    for row in range(h):
                        src = row * row_bytes
                        dst = ((y + row) * width + x) * 3
                        canvas[dst:dst + row_bytes] = rgb[src:src + row_bytes]
                if n_rects:
                    return width, height, canvas
            elif msg == 1:  # SetColourMapEntries
                _recv_exact(stream, 3)
                count = struct.unpack(">H", _recv_exact(stream, 2))[0]
                _recv_exact(stream, count * 6)
            elif msg == 2:  # Bell
                continue
            elif msg == 3:  # ServerCutText
                _recv_exact(stream, 3)
                length = _read_u32(stream)
                _recv_exact(stream, length)
            else:
                raise RfbError(f"unexpected server message {msg}")


def write_ppm(path: Path, width: int, height: int, rgb: bytearray) -> int:
    """Write binary PPM and return the byte count."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(rgb))
    return len(header) + len(rgb)


def _credentials(password_file: str | None) -> tuple[str, str]:
    """Read the password from a file or the environment; never from argv."""
    username = os.environ.get("VNC_USERNAME", "")
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise RfbError(f"cannot read --password-file: {exc}") from None
        password = password.rstrip("\r\n")
    else:
        password = os.environ.get("VNC_PASSWORD", "")
    return username, password


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vnc_grab")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5901)
    ap.add_argument("--out", required=True, help="destination .ppm path")
    ap.add_argument("--repeat", type=int, default=1, help="frames to save")
    ap.add_argument("--interval", type=float, default=0.4)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument(
        "--password-file", metavar="PATH",
        help="read the VNC password from PATH instead of $VNC_PASSWORD",
    )
    args = ap.parse_args(argv)

    try:
        username, password = _credentials(args.password_file)
    except RfbError as exc:
        print(f"vnc_grab: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    for index in range(max(1, args.repeat)):
        started = time.monotonic()
        try:
            width, height, rgb = grab_frame(
                args.host, args.port, username, password, args.timeout
            )
        except (OSError, RfbError) as exc:
            print(f"vnc_grab: {exc}", file=sys.stderr)
            return 1
        target = out if args.repeat == 1 else out.with_name(
            f"{out.stem}_{index}{out.suffix}"
        )
        size = write_ppm(target, width, height, rgb)
        print(
            f"vnc_grab: {target} {width}x{height} {size} bytes "
            f"in {(time.monotonic() - started) * 1000:.0f} ms",
            flush=True,
        )
        if index + 1 < args.repeat:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
