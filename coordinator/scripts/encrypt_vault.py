#!/usr/bin/env python3
"""
encrypt_vault.py — operator-only CLI to encrypt/decrypt the vault file.

Usage:
    # Encrypt (one-time, before deployment)
    python3 encrypt_vault.py encrypt \
        --in /path/to/riddles.json \
        --out /path/to/vault.enc

    # Decrypt to verify (operator-only check)
    python3 encrypt_vault.py decrypt \
        --in /path/to/vault.enc \
        --out /tmp/vault.json   # use ramdisk in production

The passphrase is read from $ARDI_VAULT_PASS or prompted interactively.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Add src to path so we can import secure_vault
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from coordinator.secure_vault import decrypt_vault_file, encrypt_vault_file  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_enc = sub.add_parser("encrypt", help="encrypt riddles.json → vault.enc")
    p_enc.add_argument("--in", dest="inp", required=True)
    p_enc.add_argument("--out", required=True)

    p_dec = sub.add_parser("decrypt", help="decrypt vault.enc → riddles.json (verify only)")
    p_dec.add_argument("--in", dest="inp", required=True)
    p_dec.add_argument("--out", required=True)

    args = ap.parse_args()

    passphrase = os.environ.get("ARDI_VAULT_PASS")
    if not passphrase:
        passphrase = getpass.getpass("Vault passphrase: ")

    if args.cmd == "encrypt":
        if Path(args.out).exists():
            print(f"Output file {args.out} exists; refusing to overwrite", file=sys.stderr)
            sys.exit(1)
        encrypt_vault_file(args.inp, args.out, passphrase)
        # Set restrictive permissions (owner read-only)
        os.chmod(args.out, 0o400)
        print(f"Encrypted vault → {args.out} (mode 0400)")
    elif args.cmd == "decrypt":
        plaintext = decrypt_vault_file(args.inp, passphrase)
        Path(args.out).write_bytes(plaintext)
        os.chmod(args.out, 0o400)
        print(f"Decrypted → {args.out} (mode 0400)")
        print("WARNING: plaintext on disk. Wipe immediately after use.")


if __name__ == "__main__":
    main()
