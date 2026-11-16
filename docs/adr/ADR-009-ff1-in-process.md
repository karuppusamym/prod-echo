# ADR-009 FF1 is implemented in-process over `cryptography`'s AES

Status: accepted · 2026-11-15

## Context
Masking (I-17, ADR-003) needs format-preserving encryption for SSNs, Luhn-checked numbers and phone
subscriber digits. docs/06 left the choice open: a reviewed library, or a thin wrapper around
Bouncy Castle's `FPEEngine` behind a Java service if the crypto team prefers. A Java service adds a
second runtime, a network hop in the data path and a process to deploy, for a function that is a
few dozen lines over an AES block cipher.

## Decision
Implement NIST SP 800-38G FF1 (encrypt and decrypt, radix 2..36) in `echo/mask/ff1.py`, using only
the AES-ECB block primitive from `cryptography` (already a dependency). The FF1 key for each
technique is HMAC-SHA256(environment key, technique label). The implementation is verified in
`tests/test_mask.py` against the NIST FF1 AES-128 samples 1–3, plus encrypt/decrypt round trips.

## Consequences
No extra service or JVM; masking stays a pure in-process function. We own the correctness of the
FF1 code, so the NIST vectors are a required test. FF1 is used here for deterministic masking of
test data, not to protect secrets at rest. Revisit if the crypto team requires a reviewed or
certified library: the `FF1` class has one constructor and `encrypt`/`decrypt`, so it can be
swapped for a wrapper around such a library without touching the techniques.
