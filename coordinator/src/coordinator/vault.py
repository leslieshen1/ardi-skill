"""Vault management — load 21,000 entries, expose riddles publicly, hide answers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LANG_MAP = {"en": 0, "zh": 1, "ja": 2, "ko": 3, "fr": 4, "de": 5}
LANG_REV = {v: k for k, v in LANG_MAP.items()}


@dataclass
class VaultEntry:
    word_id: int
    word: str  # the secret answer
    riddle: str
    power: int
    rarity: str
    language: str
    language_id: int

    def public_dict(self) -> dict:
        """View shown to agents — answer hidden."""
        return {
            "wordId": self.word_id,
            "riddle": self.riddle,
            "power": self.power,
            "rarity": self.rarity,
            "language": self.language,
            "languageId": self.language_id,
        }


class Vault:
    """Loads riddles.json and exposes per-wordId access. Word answers are kept
    in memory and only revealed at mint signing time."""

    def __init__(self, path: str | Path):
        self._entries: list[VaultEntry] = []
        self._load(Path(path))

    def _load(self, path: Path):
        raw = json.load(open(path))
        for idx, r in enumerate(raw):
            lang = r["language"]
            if lang not in LANG_MAP:
                raise ValueError(f"unknown language {lang} at idx {idx}")
            power = int(r.get("power", 30))
            if not 1 <= power <= 100:
                raise ValueError(f"power {power} out of range at idx {idx}")
            self._entries.append(
                VaultEntry(
                    word_id=idx,
                    word=r["word"],
                    riddle=r["riddle"],
                    power=power,
                    rarity=r.get("rarity", "common"),
                    language=lang,
                    language_id=LANG_MAP[lang],
                )
            )
        if len(self._entries) != 21_000:
            # Soft warning — let dev environments use smaller vaults
            print(f"WARN: vault has {len(self._entries)} entries, expected 21,000")

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, word_id: int) -> VaultEntry:
        return self._entries[word_id]

    def all_unsolved_by_rarity(self, minted: set[int]) -> dict[str, list[int]]:
        """Return wordIds grouped by rarity, excluding already-minted ones."""
        out: dict[str, list[int]] = {"common": [], "uncommon": [], "rare": [], "legendary": []}
        for e in self._entries:
            if e.word_id in minted:
                continue
            out[e.rarity].append(e.word_id)
        return out

    def reveal_word(self, word_id: int) -> str:
        return self._entries[word_id].word
