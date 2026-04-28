#!/usr/bin/env python3
"""
vault_merkle.py — compute the Merkle root of the 21,000-entry Ardi vault.

The on-chain Merkle root anchors the immutable vault. Off-chain proofs let any
party verify that a given (wordId, word, power, languageId) tuple was part of
the original vault, without storing the full 21,000 on-chain.

Leaf format (v1.0 — hash-only):
    keccak256(abi.encodePacked(
        uint256(wordId),
        bytes32(keccak256(bytes(word))),    # NEW: hashed word, not plaintext
        uint16(power),
        uint8(languageId)
    ))

Why hash-only: The plaintext `word` is no longer in the leaf. publishAnswer
on chain takes `wordHash`, verifies (wordId, wordHash, power, lang) is in the
tree, and stores only the hash. Plaintext is supplied later by the winner at
inscribe time, so wordIds without a winner never have plaintext on chain.

Tree algorithm: OpenZeppelin-compatible binary Merkle (sorted-pair hashing).

Usage:
    python3 tools/vault_merkle.py --vault /path/to/riddles.json --out vault_tree.json

Output:
    vault_tree.json with:
        - root: hex string (0x...)
        - leaves: array of hex leaves in vault order
        - tree: full tree (for proof generation)
        - proof_for(wordId): function-equivalent — exposed via --prove flag
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from eth_utils import keccak  # pip install eth-utils
except ImportError:
    sys.stderr.write(
        "ERROR: eth-utils not installed.\n"
        "  pip install --user eth-utils\n"
    )
    sys.exit(1)


LANG_MAP = {"en": 0, "zh": 1, "ja": 2, "ko": 3, "fr": 4, "de": 5}


def leaf_hash(word_id: int, word: str, power: int, language_id: int) -> bytes:
    """Compute the v1.0 leaf:
        keccak256(abi.encodePacked(
            uint256 wordId,
            bytes32 wordHash,    # = keccak256(bytes(word))
            uint16 power,
            uint8 languageId
        ))

    The plaintext `word` is hashed BEFORE concatenation, so the leaf
    fingerprints the (wordId, hashed-word, power, lang) tuple but never
    contains plaintext bytes. publishAnswer on chain consumes wordHash
    directly — the contract never sees the plaintext until the winner
    submits it at inscribe time.

    Power is uint16 (2 bytes) to match ArdiEpochDraw + ArdiNFT inscription
    storage. Originals have power 1-100 (fits in 1 byte) but the on-chain
    abi.encodePacked of `uint16` always emits 2 bytes — so the leaf MUST
    use 2-byte power or Merkle inclusion fails at publishAnswer.
    """
    word_hash = keccak(word.encode("utf-8"))  # bytes32, the new "secret"
    payload = (
        word_id.to_bytes(32, byteorder="big")
        + word_hash                                       # 32 bytes
        + power.to_bytes(2, byteorder="big")
        + language_id.to_bytes(1, byteorder="big")
    )
    return keccak(payload)


def hash_pair(a: bytes, b: bytes) -> bytes:
    """OpenZeppelin sorted-pair hashing."""
    lo, hi = (a, b) if a < b else (b, a)
    return keccak(lo + hi)


def build_tree(leaves: list[bytes]) -> list[list[bytes]]:
    """Return list of levels, level[0] = leaves, level[-1] = [root]."""
    if not leaves:
        return [[b"\x00" * 32]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = []
        for i in range(0, len(cur), 2):
            if i + 1 < len(cur):
                nxt.append(hash_pair(cur[i], cur[i + 1]))
            else:
                # Odd-length: bubble up the lone element
                nxt.append(cur[i])
        levels.append(nxt)
    return levels


def proof_for(levels: list[list[bytes]], leaf_index: int) -> list[bytes]:
    """Compute Merkle proof for leaf at given index."""
    proof = []
    idx = leaf_index
    for level in levels[:-1]:
        sibling_idx = idx ^ 1
        if sibling_idx < len(level):
            proof.append(level[sibling_idx])
        idx //= 2
    return proof


def hex0x(b: bytes) -> str:
    return "0x" + b.hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, help="path to riddles.json (21,000 entries)")
    ap.add_argument("--out", default="vault_tree.json", help="output JSON file")
    ap.add_argument("--prove", type=int, default=None, help="if set, also dump proof for this wordId")
    args = ap.parse_args()

    vault_path = Path(args.vault)
    if not vault_path.exists():
        sys.stderr.write(f"vault file not found: {vault_path}\n")
        sys.exit(1)

    raw = json.load(open(vault_path))
    print(f"loaded {len(raw)} entries from {vault_path}")

    # The riddles.json from wordbank-builder has different field naming. Map:
    # We need: (wordId, word, power, languageId). Some entries lack `id`; assign
    # by index per spec — id 0..20999 = position in the array.
    entries = []
    for idx, r in enumerate(raw):
        word = r["word"]
        lang_str = r["language"]
        power = int(r.get("power", 30))
        if lang_str not in LANG_MAP:
            sys.stderr.write(f"unknown language at idx {idx}: {lang_str}\n")
            sys.exit(1)
        if not (1 <= power <= 100):
            sys.stderr.write(f"power out of range at idx {idx}: {power}\n")
            sys.exit(1)
        entries.append({
            "wordId": idx,
            "word": word,
            "power": power,
            "languageId": LANG_MAP[lang_str],
            "language": lang_str,
        })

    leaves = [
        leaf_hash(e["wordId"], e["word"], e["power"], e["languageId"]) for e in entries
    ]

    levels = build_tree(leaves)
    root = levels[-1][0]

    out = {
        "root": hex0x(root),
        "leafCount": len(leaves),
        "leaves": [hex0x(l) for l in leaves],
        "tree": [[hex0x(h) for h in level] for level in levels],
        "spec": {
            "leafFormat": "keccak256(uint256 wordId || bytes word || uint16 power || uint8 languageId)",
            "languageMap": LANG_MAP,
            "ozCompat": True,
            "sortedPairHashing": True,
        },
    }

    if args.prove is not None:
        wid = args.prove
        if not (0 <= wid < len(leaves)):
            sys.stderr.write(f"--prove wordId out of range: {wid}\n")
            sys.exit(1)
        proof = proof_for(levels, wid)
        out["sampleProof"] = {
            "wordId": wid,
            "word": entries[wid]["word"],
            "power": entries[wid]["power"],
            "languageId": entries[wid]["languageId"],
            "leaf": hex0x(leaves[wid]),
            "proof": [hex0x(h) for h in proof],
        }
        print(f"\nSample proof for wordId {wid} ({entries[wid]['word']}, lang={entries[wid]['language']}):")
        print(f"  leaf:  {hex0x(leaves[wid])}")
        for i, p in enumerate(proof):
            print(f"  [{i}]   {hex0x(p)}")
        print(f"  root:  {hex0x(root)}  (verify by sorted-pair hashing leaf+proof up the tree)")

    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nMerkle root: {hex0x(root)}")
    print(f"Tree levels: {len(levels)}")
    print(f"Wrote: {args.out}  ({Path(args.out).stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
