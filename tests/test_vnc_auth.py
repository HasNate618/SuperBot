"""Tests for the RSA-AES authentication in spike/vnc_grab.py.

The mock server below speaks the wire protocol that neatvnc (the library behind
Weston 16.0.0's VNC backend) implements, so these tests run the real handshake
and the real encrypted session without a live game session:

  * the server's public key is a freshly generated 1024-bit RSA key and the
    challenge is padded exactly as nettle's rsa_encrypt pads it;
  * the session keys are derived the way neatvnc derives them, and every
    message after the key exchange is framed as `u16 length || AES-EAX
    ciphertext || 16-byte tag`;
  * the credentials the client sends are decrypted and compared byte for byte.

What these tests do not prove: that the live Weston server accepts the real
password. No password for the hidden session is available here, and the tests
never touch it.

The crypto primitives are pinned to published known-answer vectors separately
from the handshake tests, because the mock server and the client share the same
primitive code -- an end-to-end pass alone would not catch a bug that is
symmetric in both directions.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import secrets
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "vnc_grab_test", ROOT / "spike" / "vnc_grab.py"
)
assert SPEC is not None and SPEC.loader is not None
vnc_grab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vnc_grab)

RFB_BANNER = b"RFB 003.008\n"

# 32 bits per pixel, little endian, true colour, 8 bits each, R/G/B at 16/8/0.
PIXEL_FORMAT = (
    bytes([32, 24, 0, 1]) + struct.pack(">HHH", 255, 255, 255)
    + bytes([16, 8, 0, 0, 0, 0])
)
FRAME_WIDTH, FRAME_HEIGHT = 2, 2
FRAME_RGB = bytes([
    255, 0, 0, 0, 255, 0,
    0, 0, 255, 255, 255, 255,
])

# A fixture password, not a credential for anything real.
TEST_PASSWORD = "hunter2"  # noqa: S105


def recv_exact(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError(
                f"mock server: connection closed after {len(buf)} of {count} bytes"
            )
        buf += chunk
    return buf


def _write_public_key(sock, key):
    sock.sendall(
        struct.pack(">I", key.bits)
        + key.modulus.to_bytes(key.size, "big")
        + key.exponent.to_bytes(key.size, "big")
    )


def _read_public_key(sock):
    bits = struct.unpack(">I", recv_exact(sock, 4))[0]
    size = (bits + 7) // 8
    modulus = int.from_bytes(recv_exact(sock, size), "big")
    exponent = int.from_bytes(recv_exact(sock, size), "big")
    return vnc_grab._RsaKey(modulus, exponent, bits)


class MockRfbServer:
    """A minimal neatvnc-shaped RFB server for one connection."""

    def __init__(self, security_types=(vnc_grab.SEC_RSA_AES256,),
                 password=TEST_PASSWORD, bits=1024,
                 reason="Invalid password", allow_disconnect=False):
        self.security_types = tuple(security_types)
        self.password = password
        self.bits = bits
        self.reason = reason
        self.allow_disconnect = allow_disconnect
        self.chosen = None
        self.received_username = None
        self.received_password = None
        self.error = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._listener.close()
        self._thread.join(timeout=30)
        if exc_type is None and self.error is not None:
            raise self.error
        return False

    def _run(self):
        try:
            conn, _ = self._listener.accept()
            conn.settimeout(30)
            with conn:
                self._session(conn)
        except (ConnectionError, vnc_grab.RfbError) as exc:
            if not self.allow_disconnect:
                self.error = exc
        except Exception as exc:  # noqa: BLE001 - re-raised from __exit__
            self.error = exc

    def _session(self, conn):
        conn.sendall(RFB_BANNER)
        recv_exact(conn, 12)  # client version
        conn.sendall(
            bytes([len(self.security_types)]) + bytes(self.security_types)
        )
        self.chosen = recv_exact(conn, 1)[0]
        if self.chosen == vnc_grab.SEC_NONE:
            stream = conn
        else:
            stream = self._rsa_aes(conn, self.chosen)
            if stream is None:
                return
        stream.sendall(struct.pack(">I", 0))  # SecurityResult: OK

        recv_exact(stream, 1)  # ClientInit
        stream.sendall(
            struct.pack(">HH", FRAME_WIDTH, FRAME_HEIGHT) + PIXEL_FORMAT
            + struct.pack(">I", 0)
        )
        self._serve_framebuffer(stream)

    def _rsa_aes(self, conn, security_type):
        key_size, digest = vnc_grab.RSA_AES_PARAMS[security_type]
        server_public, server_private = vnc_grab._rsa_generate(self.bits)
        _write_public_key(conn, server_public)
        client_public = _read_public_key(conn)

        server_random = secrets.token_bytes(key_size)
        conn.sendall(
            struct.pack(">H", client_public.size)
            + vnc_grab._rsa_encrypt(client_public, server_random)
        )
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
        if length != server_public.size:
            raise AssertionError(
                f"client sent a {length}-byte challenge, expected "
                f"{server_public.size}"
            )
        client_random = vnc_grab._rsa_decrypt(
            server_private, recv_exact(conn, length)
        )
        if len(client_random) != key_size:
            raise AssertionError(
                f"client random is {len(client_random)} bytes, expected "
                f"{key_size}"
            )

        # Same derivation as neatvnc's src/auth/rsa-aes.c.
        in_key = digest(server_random + client_random).digest()[:key_size]
        out_key = digest(client_random + server_random).digest()[:key_size]
        stream = vnc_grab._AesEaxStream(conn, in_key, out_key)

        server_bits = struct.pack(">I", server_public.bits)
        client_bits = struct.pack(">I", client_public.bits)
        server_material = (
            server_public.modulus.to_bytes(server_public.size, "big")
            + server_public.exponent.to_bytes(server_public.size, "big")
        )
        client_material = (
            client_public.modulus.to_bytes(client_public.size, "big")
            + client_public.exponent.to_bytes(client_public.size, "big")
        )
        # neatvnc writes its hash as soon as it has sent the challenge.
        stream.sendall(
            digest(server_bits + server_material + client_bits
                   + client_material).digest()
        )
        expected = digest(client_bits + client_material + server_bits
                          + server_material).digest()
        if recv_exact(stream, len(expected)) != expected:
            raise AssertionError("client RSA key hash does not match")

        stream.sendall(bytes([1]))  # subtype: user and password

        username_len = recv_exact(stream, 1)[0]
        self.received_username = recv_exact(stream, username_len).decode()
        password_len = recv_exact(stream, 1)[0]
        self.received_password = recv_exact(stream, password_len).decode()

        if self.received_password != self.password:
            reason = self.reason.encode()
            stream.sendall(
                struct.pack(">II", 1, len(reason)) + reason
            )
            return None
        return stream

    def _serve_framebuffer(self, stream):
        while True:
            kind = recv_exact(stream, 1)[0]
            if kind == 0:  # SetPixelFormat
                recv_exact(stream, 19)
            elif kind == 2:  # SetEncodings
                recv_exact(stream, 1)
                count = struct.unpack(">H", recv_exact(stream, 2))[0]
                recv_exact(stream, 4 * count)
            elif kind == 3:  # FramebufferUpdateRequest
                recv_exact(stream, 9)
                stream.sendall(self._update_message())
                return
            elif kind == 4:  # KeyEvent
                recv_exact(stream, 7)
            elif kind == 6:  # ClientCutText
                recv_exact(stream, 3)
                length = struct.unpack(">I", recv_exact(stream, 4))[0]
                recv_exact(stream, length)
            else:
                raise AssertionError(f"unexpected client message {kind}")

    @staticmethod
    def _update_message():
        header = struct.pack(">BBH", 0, 0, 1)
        rect = struct.pack(
            ">HHHHi", 0, 0, FRAME_WIDTH, FRAME_HEIGHT, vnc_grab.ENC_RAW
        )
        pixels = bytearray()
        for index in range(0, len(FRAME_RGB), 3):
            r, g, b = FRAME_RGB[index:index + 3]
            pixels += ((r << 16) | (g << 8) | b).to_bytes(4, "little")
        return header + rect + bytes(pixels)


class AesKnownAnswerTests(unittest.TestCase):
    def test_aes_block_fips_197(self):
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        vectors = {
            "000102030405060708090a0b0c0d0e0f":
                "69c4e0d86a7b0430d8cdb78070b4c55a",
            "000102030405060708090a0b0c0d0e0f1011121314151617":
                "dda97ca4864cdfe06eaf70a0ec0d7191",
            "000102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f":
                "8ea2b7ca516745bfeafc49904b496089",
        }
        for key, expected in vectors.items():
            with self.subTest(bits=len(key) * 4):
                cipher = vnc_grab._Aes(bytes.fromhex(key))
                self.assertEqual(
                    cipher.encrypt_block(plaintext).hex(), expected
                )

    def test_cmac_rfc_4493(self):
        aes = vnc_grab._Aes(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
        vectors = {
            "": "bb1d6929e95937287fa37d129b756746",
            "6bc1bee22e409f96e93d7e117393172a":
                "070a16b46b4d4144f79bdd9dd04a287c",
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411":
                "dfa66747de9ae63030ca32611497c827",
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52ef"
            "f69f2445df4f9b17ad2b417be66c3710":
                "51f0bebf7e3b9d92fc49741779363cfe",
        }
        for message, expected in vectors.items():
            with self.subTest(length=len(message) // 2):
                self.assertEqual(
                    vnc_grab._cmac(aes, bytes.fromhex(message)).hex(),
                    expected,
                )

    def test_cmac_sp_800_38b_aes192_and_aes256(self):
        message = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52ef"
            "f69f2445df4f9b17ad2b417be66c3710"
        )
        vectors = {
            "8e73b0f7da0e6452c810f32b809079e562f8ead2522c6b7b": {
                0: "d17ddf46adaacde531cac483de7a9367",
                16: "9e99a7bf31e710900662f65e617c5184",
                40: "8a1de5be2eb31aad089a82e6ee908b0e",
            },
            "603deb1015ca71be2b73aef0857d7781"
            "1f352c073b6108d72d9810a30914dff4": {
                0: "028962f61b7bf89efc6b551f4667d983",
                16: "28a7023f452e8f82bd4bf28d8c37c35c",
            },
        }
        for key, expected_by_length in vectors.items():
            aes = vnc_grab._Aes(bytes.fromhex(key))
            for length, expected in expected_by_length.items():
                with self.subTest(bits=len(key) * 4, length=length):
                    self.assertEqual(
                        vnc_grab._cmac(aes, message[:length]).hex(), expected
                    )

    def test_eax_paper_vectors(self):
        # From the EAX paper (Bellare, Rogaway, Wagner), as reproduced in
        # nettle's testsuite/eax-test.c.
        vectors = [
            ("233952DEE4D5ED5F9B9C6D6FF80FF478", "6BFB914FD07EAE6B", "",
             "", "62EC67F9C3A4A407FCB2A8C49031A8B3",
             "E037830E8389F27B025A2D6527E79D01"),
            ("91945D3F4DCBEE0BF45EF52255F095A4", "FA3BFD4806EB53FA", "F7FB",
             "19DD", "BECAF043B0A23D843194BA972C66DEBD",
             "5C4C9331049D0BDAB0277408F67967E5"),
            ("01F74AD64077F2E704C0F60ADA3DD523", "234A3463C1264AC6",
             "1A47CB4933", "D851D5BAE0",
             "70C3DB4F0D26368400A10ED05D2BFF5E",
             "3A59F238A23E39199DC9266626C40F80"),
            ("D07CF6CBB7F313BDDE66B727AFD3C5E8", "33CCE2EABFF5A79D",
             "481C9E39B1", "632A9D131A",
             "8408DFFF3C1A2B1292DC199E46B7D617",
             "D4C168A4225D8E1FF755939974A7BEDE"),
            ("35B6D0580005BBC12B0587124557D2C2", "AEB96EAEBE2970E9",
             "40D0C07DA5E4", "071DFE16C675",
             "FDB6B06676EEDC5C61D74276E1F8E816",
             "CB0677E536F73AFE6A14B74EE49844DD"),
            ("BD8E6E11475E60B268784C38C62FEB22", "D4482D1CA78DCE0F",
             "4DE3B35C3FC039245BD1FB7D", "835BB4F15D743E350E728414",
             "6EAC5C93072D8E8513F750935E46DA1B",
             "ABB8644FD6CCB86947C5E10590210A4F"),
            ("7C77D6E813BED5AC98BAA417477A2E7D", "65D2017990D62528",
             "8B0A79306C9CE7ED99DAE4F87F8DD61636",
             "02083E3979DA014812F59F11D52630DA30",
             "1A8C98DCD73D38393B2BF1569DEEFC19",
             "137327D10649B0AA6E1C181DB617D7F2"),
            ("5FFF20CAFAB119CA2FC73549E20F5B0D", "54B9F04E6A09189A",
             "1BDA122BCE8A8DBAF1877D962B8592DD2D56",
             "2EC47B2C4954A489AFC7BA4897EDCDAE8CC3",
             "DDE59B97D722156D4D9AFF2BC7559826",
             "3B60450599BD02C96382902AEF7F832A"),
            ("A4A4782BCFFD3EC5E7EF6D8C34A56123", "899A175897561D7E",
             "6CF36720872B8513F6EAB1A8A44438D5EF11",
             "0DE18FD0FDD91E7AF19F1D8EE8733938B1E8",
             "B781FCF2F75FA5A8DE97A9CA48E522EC",
             "E7F6D2231618102FDB7FE55FF1991700"),
            ("8395FCF1E95BEBD697BD010BC766AAC3", "126735FCC320D25A",
             "CA40D7446E545FFAED3BD12A740A659FFBBB3CEAB7",
             "CB8920F87A6C75CFF39627B56E3ED197C552D295A7",
             "22E7ADD93CFC6393C57EC0B3C17D6B44",
             "CFC46AFC253B4652B1AF3795B124AB6E"),
        ]
        for key, aad, plaintext, ciphertext, nonce, tag in vectors:
            with self.subTest(plaintext_bytes=len(plaintext) // 2):
                aes = vnc_grab._Aes(bytes.fromhex(key))
                sealed, sealed_tag = vnc_grab._eax_seal(
                    aes, bytes.fromhex(nonce), bytes.fromhex(aad),
                    bytes.fromhex(plaintext),
                )
                self.assertEqual(sealed.hex().upper(), ciphertext)
                self.assertEqual(sealed_tag.hex().upper(), tag)
                self.assertEqual(
                    vnc_grab._eax_open(
                        aes, bytes.fromhex(nonce), bytes.fromhex(aad),
                        sealed, sealed_tag,
                    ),
                    bytes.fromhex(plaintext),
                )

    def test_eax_rejects_a_modified_tag(self):
        aes = vnc_grab._Aes(bytes(16))
        sealed, tag = vnc_grab._eax_seal(aes, bytes(16), b"\x00\x01", b"frame")
        broken = bytes([tag[0] ^ 1]) + tag[1:]
        with self.assertRaises(vnc_grab.RfbError):
            vnc_grab._eax_open(aes, bytes(16), b"\x00\x01", sealed, broken)


class RsaKnownAnswerTests(unittest.TestCase):
    # A 1024-bit key generated once with `openssl genrsa 1024`. The two
    # ciphertexts were produced by OpenSSL 3.6.3 (`pkeyutl -pkeyopt
    # rsa_padding_mode:pkcs1`), which is how the PKCS#1 v1.5 padding here is
    # checked against an implementation that is not this one.
    MODULUS = int(
        "bfdd1758279c7428f52d4e9c0b8294173e623c97066789092ffe35c26e18d9b2"
        "8faaacbef535170a6598f5ba9fd3e93957c633d102074571c9f94a3276e545c"
        "1736dc27e635dd4c7113ef1eecac429d8e370d068a2eed0b0889da4d3a48a05"
        "07e8b31465aa9227b8748f7ec47370841f41150cd53142d3c6a698f880c9bb"
        "cad9",
        16,
    )
    PRIVATE_EXPONENT = int(
        "b6002c9cdb36769a43b96ed067babe702e2552049176b56f99af339ff4806e42"
        "0c368317c1d352bff2105f496143cc01315a3add11678460a48f6deae10e2c5"
        "3e8327659437f861bc7e4f5486b1ba51d76737dbbc91563b390efa51e375b4b"
        "1be9b761a6cd3b650ac61e81ac30ce1beeb0b42b5d14666a0dd7128d233dc4d"
        "581",
        16,
    )
    MESSAGE = b"0123456789abcdef"
    OPENSSL_CIPHERTEXT = bytes.fromhex(
        "b46e9e418afc6df28b0c791fd6ee050c1bd032a3555f3cc12721f56753fdc2ca"
        "02721172f57dd125e3aa333bb1633d98ee2b98387263a5715c70ba57537d5717"
        "3cc76ca7ebf69505ca3d127652df0c2aa0cdfcf3cff8355b2cff0bf4bab8236f"
        "8bb21d0a277d83b8a9724c99548b930606f90a9135e7c81d9b004fa74a48afce"
    )
    # _rsa_encrypt with every padding byte forced to 0xAA; OpenSSL decrypts
    # this back to MESSAGE.
    PADDED_CIPHERTEXT = bytes.fromhex(
        "b7221dfa090336f4d0a38b245dc68e33f407f43c6ccb5cc27ac4f5fb7b459e4e"
        "ecde88974b6a66c944f12da2caad28b6c42971cc2cec6313e59fe8b78f72a121"
        "de7cfb5a960cc8f72177e88a4eba7199112f9627623091e3cf3793519a27425e"
        "fc9501fe41826fcd25722501422979b4e75768cb3cd4b0941f46bf5724822b97"
    )

    def setUp(self):
        self.public = vnc_grab._RsaKey(self.MODULUS, 65537, 1024)
        self.private = vnc_grab._RsaKey(
            self.MODULUS, self.PRIVATE_EXPONENT, 1024
        )

    def test_decrypt_matches_the_openssl_ciphertext(self):
        self.assertEqual(
            vnc_grab._rsa_decrypt(self.private, self.OPENSSL_CIPHERTEXT),
            self.MESSAGE,
        )

    def test_encrypt_matches_the_openssl_ciphertext(self):
        self.assertEqual(
            vnc_grab._rsa_encrypt(
                self.public, self.MESSAGE, randbytes=lambda count: b"\xAA" * count
            ),
            self.PADDED_CIPHERTEXT,
        )

    def test_decrypt_rejects_a_bad_padding_block(self):
        block = b"\x00\x01" + bytes(250) + self.MESSAGE
        ciphertext = pow(
            int.from_bytes(block, "big"), 65537, self.MODULUS
        ).to_bytes(128, "big")
        with self.assertRaises(vnc_grab.RfbError):
            vnc_grab._rsa_decrypt(self.private, ciphertext)

    def test_generate_round_trips(self):
        public, private = vnc_grab._rsa_generate(512)
        self.assertEqual(public.bits, 512)
        self.assertEqual(public.modulus.bit_length(), 512)
        self.assertEqual(public.modulus % 2, 1)
        ciphertext = vnc_grab._rsa_encrypt(public, self.MESSAGE)
        self.assertEqual(vnc_grab._rsa_decrypt(private, ciphertext), self.MESSAGE)


class HandshakeTests(unittest.TestCase):
    def test_rsa_aes256_capture(self):
        with MockRfbServer() as server:
            width, height, rgb = vnc_grab.grab_frame(
                "127.0.0.1", server.port, password=TEST_PASSWORD
            )
        self.assertEqual(server.chosen, vnc_grab.SEC_RSA_AES256)
        self.assertEqual(server.received_username, "")
        self.assertEqual(server.received_password, TEST_PASSWORD)
        self.assertEqual((width, height), (FRAME_WIDTH, FRAME_HEIGHT))
        self.assertEqual(bytes(rgb), FRAME_RGB)

    def test_rsa_aes128_is_used_when_129_is_not_offered(self):
        with MockRfbServer(security_types=(vnc_grab.SEC_RSA_AES,)) as server:
            width, height, rgb = vnc_grab.grab_frame(
                "127.0.0.1", server.port, password=TEST_PASSWORD
            )
        self.assertEqual(server.chosen, vnc_grab.SEC_RSA_AES)
        self.assertEqual(server.received_password, TEST_PASSWORD)
        self.assertEqual((width, height), (FRAME_WIDTH, FRAME_HEIGHT))
        self.assertEqual(bytes(rgb), FRAME_RGB)

    def test_username_is_sent_when_configured(self):
        with MockRfbServer() as server:
            vnc_grab.grab_frame(
                "127.0.0.1", server.port, username="nate",
                password=TEST_PASSWORD,
            )
        self.assertEqual(server.received_username, "nate")

    def test_none_security_still_works(self):
        with MockRfbServer(security_types=(vnc_grab.SEC_NONE,)) as server:
            width, height, rgb = vnc_grab.grab_frame(
                "127.0.0.1", server.port
            )
        self.assertEqual(server.chosen, vnc_grab.SEC_NONE)
        self.assertEqual((width, height), (FRAME_WIDTH, FRAME_HEIGHT))
        self.assertEqual(bytes(rgb), FRAME_RGB)

    def test_wrong_password_is_reported(self):
        with MockRfbServer() as server, self.assertRaises(
            vnc_grab.RfbError
        ) as caught:
            vnc_grab.grab_frame(
                "127.0.0.1", server.port,
                password="not-the-password",  # noqa: S106 - a rejected fixture
            )
        self.assertIn("Invalid password", str(caught.exception))

    def test_wrong_password_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "frame.ppm"
            stderr = io.StringIO()
            with MockRfbServer() as server, mock.patch.dict(
                os.environ, {"VNC_PASSWORD": "not-the-password"}
            ), contextlib.redirect_stderr(stderr):
                code = vnc_grab.main([
                    "--host", "127.0.0.1", "--port", str(server.port),
                    "--out", str(out),
                ])
            self.assertEqual(code, 1)
            self.assertIn("Invalid password", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(out.exists())
        self.assertEqual(server.received_password, "not-the-password")

    def test_apple_dh_only_is_refused(self):
        with MockRfbServer(
            security_types=(vnc_grab.SEC_APPLE_DH,), allow_disconnect=True
        ) as server, self.assertRaises(vnc_grab.RfbError) as caught:
            vnc_grab.grab_frame(
                "127.0.0.1", server.port, password=TEST_PASSWORD
            )
        self.assertIn("Apple DH", str(caught.exception))

    def test_missing_password_names_the_variable(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "frame.ppm"
            stderr = io.StringIO()
            with MockRfbServer(allow_disconnect=True) as server, mock.patch.dict(
                os.environ, {"VNC_PASSWORD": "", "VNC_USERNAME": ""}
            ), contextlib.redirect_stderr(stderr):
                code = vnc_grab.main([
                    "--host", "127.0.0.1", "--port", str(server.port),
                    "--out", str(out),
                ])
            self.assertEqual(code, 1)
            self.assertIn("VNC_PASSWORD", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(out.exists())
        # The client refuses before it commits to a security type, so the
        # server never sees a choice.
        self.assertIsNone(server.chosen)

    def test_password_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            password_file = Path(temp) / "password"
            password_file.write_text(TEST_PASSWORD + "\n", encoding="utf-8")
            out = Path(temp) / "frame.ppm"
            with MockRfbServer() as server, mock.patch.dict(
                os.environ, {"VNC_PASSWORD": "", "VNC_USERNAME": ""}
            ), contextlib.redirect_stdout(io.StringIO()):
                code = vnc_grab.main([
                    "--host", "127.0.0.1", "--port", str(server.port),
                    "--out", str(out),
                    "--password-file", str(password_file),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(server.received_password, TEST_PASSWORD)
            self.assertTrue(out.read_bytes().startswith(b"P6\n2 2\n255\n"))


if __name__ == "__main__":
    unittest.main()
