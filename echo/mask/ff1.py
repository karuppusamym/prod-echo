"""NIST SP 800-38G FF1 format-preserving encryption over AES (see ADR-009).

In-process implementation on top of `cryptography`'s AES-ECB block primitive. Verified against
the NIST FF1 sample vectors in tests/test_mask.py. Numerals are strings over the alphabet
``0-9a-z`` truncated to the radix; results use the same alphabet in lower case.
"""

from __future__ import annotations

import math

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_ROUNDS = 10


MIN_DOMAIN = 1_000_000  # NIST SP 800-38G Rev. 1 minimum radix**minlen


class FF1:
    """FF1 cipher for a fixed AES key (16, 24 or 32 bytes) and radix (2..36)."""

    def __init__(self, key: bytes, radix: int = 10) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError("FF1 key must be 16, 24 or 32 bytes")
        if not 2 <= radix <= 36:
            raise ValueError("FF1 radix must be between 2 and 36")
        self.radix = radix
        self._alphabet = ALPHABET[:radix]
        self._enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()

    # -- primitives ---------------------------------------------------------------------------
    def _ciph(self, block: bytes) -> bytes:
        return self._enc.update(block)

    def _prf(self, data: bytes) -> bytes:
        """CBC-MAC with a zero IV over a multiple-of-16 input; returns the last block."""
        y = bytes(16)
        for i in range(0, len(data), 16):
            y = self._ciph(bytes(a ^ b for a, b in zip(y, data[i : i + 16], strict=True)))
        return y

    def _num(self, s: str) -> int:
        n = 0
        for ch in s:
            n = n * self.radix + self._alphabet.index(ch)
        return n

    def _str(self, x: int, m: int) -> str:
        out = []
        for _ in range(m):
            x, r = divmod(x, self.radix)
            out.append(self._alphabet[r])
        return "".join(reversed(out))

    def _check(self, x: str) -> str:
        x = x.lower()
        if len(x) < 2:
            raise ValueError("FF1 input must have at least 2 numerals")
        if self.radix ** len(x) < MIN_DOMAIN:
            raise ValueError(
                "FF1 domain radix**len must be at least 1,000,000 (NIST SP 800-38G Rev. 1); "
                "small domains are trivially enumerable"
            )
        bad = [c for c in x if c not in self._alphabet]
        if bad:
            raise ValueError(f"FF1 input has numerals outside radix {self.radix}")
        return x

    # -- cipher -------------------------------------------------------------------------------
    def _rounds(self, x: str, tweak: bytes, decrypt: bool) -> str:
        x = self._check(x)
        radix, n, t = self.radix, len(x), len(tweak)
        u, v = n // 2, n - n // 2
        a, b = x[:u], x[u:]
        # b = ceil(ceil(v * log2(radix)) / 8), computed exactly as the byte length of radix^v - 1.
        bb = ((radix**v - 1).bit_length() + 7) // 8
        d = 4 * math.ceil(bb / 4) + 4
        p = (
            bytes([1, 2, 1])
            + radix.to_bytes(3, "big")
            + bytes([10, u % 256])
            + n.to_bytes(4, "big")
            + t.to_bytes(4, "big")
        )
        pad = bytes((-t - bb - 1) % 16)
        order = range(_ROUNDS - 1, -1, -1) if decrypt else range(_ROUNDS)
        for i in order:
            m = u if i % 2 == 0 else v
            src = a if decrypt else b
            q = tweak + pad + bytes([i]) + self._num(src).to_bytes(bb, "big")
            r = self._prf(p + q)
            s = r
            j = 1
            while len(s) < d:
                s += self._ciph(bytes(x ^ y for x, y in zip(r, j.to_bytes(16, "big"), strict=True)))
                j += 1
            y = int.from_bytes(s[:d], "big")
            if decrypt:
                c = (self._num(b) - y) % (radix**m)
                b, a = a, self._str(c, m)
            else:
                c = (self._num(a) + y) % (radix**m)
                a, b = b, self._str(c, m)
        return a + b

    def encrypt(self, x: str, tweak: bytes = b"") -> str:
        return self._rounds(x, tweak, decrypt=False)

    def decrypt(self, x: str, tweak: bytes = b"") -> str:
        return self._rounds(x, tweak, decrypt=True)
