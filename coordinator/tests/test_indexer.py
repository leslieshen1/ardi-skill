"""Test indexer event handlers update tokens table correctly.

This test stubs the web3 layer — we only test the handler functions and
holder_powers() aggregation. Full chain-event integration is exercised by
scripts/anvil_e2e.py.
"""
import tempfile

import pytest


@pytest.fixture
def indexer():
    from coordinator.indexer import Indexer

    db = tempfile.mktemp(suffix="_idx.db")
    # Pass a dummy RPC; we won't call .run_loop in tests
    idx = Indexer.__new__(Indexer)
    idx.db_path = db
    idx.poll_interval = 5
    idx._stopped = False
    # init schema
    with idx._conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_state (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_id INTEGER PRIMARY KEY, owner TEXT COLLATE NOCASE,
                power INTEGER NOT NULL, language_id INTEGER NOT NULL,
                word TEXT, generation INTEGER NOT NULL DEFAULT 0,
                burned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    yield idx


def _evt(args: dict):
    """Mimic web3 event log entry."""

    class _E:
        pass

    e = _E()
    e.__getitem__ = lambda self, k: {"args": args}[k]  # type: ignore
    return {"args": args}


def test_inscribe_creates_token(indexer):
    indexer._handle_inscribed({
        "args": {
            "agent": "0x1111111111111111111111111111111111111111",
            "tokenId": 1,
            "wordId": 0,
            "word": "bitcoin",
            "power": 100,
            "languageId": 0,
        }
    })
    powers = indexer.holder_powers()
    assert "0x1111111111111111111111111111111111111111" in powers
    assert powers["0x1111111111111111111111111111111111111111"] == 100


def test_transfer_changes_owner(indexer):
    indexer._handle_inscribed({
        "args": {"agent": "0x1111111111111111111111111111111111111111", "tokenId": 1,
                 "wordId": 0, "word": "x", "power": 50, "languageId": 0},
    })
    indexer._handle_transfer({
        "args": {
            "from": "0x1111111111111111111111111111111111111111",
            "to": "0x2222222222222222222222222222222222222222",
            "tokenId": 1,
        }
    })
    powers = indexer.holder_powers()
    assert "0x1111111111111111111111111111111111111111" not in powers
    assert powers["0x2222222222222222222222222222222222222222"] == 50


def test_fusion_burns_parents_mints_child(indexer):
    indexer._handle_inscribed({
        "args": {"agent": "0xA", "tokenId": 1, "wordId": 0, "word": "fire",
                 "power": 80, "languageId": 0},
    })
    indexer._handle_inscribed({
        "args": {"agent": "0xA", "tokenId": 2, "wordId": 1, "word": "water",
                 "power": 60, "languageId": 0},
    })
    indexer._handle_fused({
        "args": {
            "holder": "0xA",
            "tokenIdA": 1,
            "tokenIdB": 2,
            "newTokenId": 21001,
            "newWord": "steam",
            "newPower": 280,
            "newLanguageId": 0,
            "generation": 1,
        }
    })
    powers = indexer.holder_powers()
    assert powers["0xa"] == 280  # 80+60 fused into 280


def test_fusion_failed_burns_loser(indexer):
    indexer._handle_inscribed({
        "args": {"agent": "0xA", "tokenId": 1, "wordId": 0, "word": "fire",
                 "power": 80, "languageId": 0},
    })
    indexer._handle_inscribed({
        "args": {"agent": "0xA", "tokenId": 2, "wordId": 1, "word": "tofu",
                 "power": 30, "languageId": 0},
    })
    indexer._handle_fusion_failed({
        "args": {"holder": "0xA", "tokenIdA": 1, "tokenIdB": 2, "burnedId": 2},
    })
    powers = indexer.holder_powers()
    assert powers["0xa"] == 80  # tofu burned, fire (80) remains


def test_total_active_power(indexer):
    indexer._handle_inscribed({
        "args": {"agent": "0xA", "tokenId": 1, "wordId": 0, "word": "x",
                 "power": 100, "languageId": 0},
    })
    indexer._handle_inscribed({
        "args": {"agent": "0xB", "tokenId": 2, "wordId": 1, "word": "y",
                 "power": 50, "languageId": 0},
    })
    assert indexer.total_active_power() == 150


def test_indexer_requires_at_least_one_rpc_url():
    """The constructor must reject empty RPC config — silent fallthrough
    would mask a misconfigured production deploy."""
    from coordinator.indexer import Indexer
    import pytest as _pt
    with _pt.raises(ValueError, match="at least one"):
        Indexer(ardi_nft_addr="0x" + "1" * 40, db_path="/tmp/x.db")


def test_indexer_failover_rotates_through_urls():
    """Failover should cycle through the URL list when the active one is dead.
    We don't actually need a live RPC — just check the index moves and the
    new contract instance is wired up."""
    from coordinator.indexer import Indexer
    import tempfile, sys, types

    # Stub Web3 so we don't need real network — return objects whose is_connected
    # alternates so failover test exercises both paths.
    real_w3_mod = sys.modules.get("web3")
    # Simply test the cycling logic: monkey-patch _connect to return a mock that
    # claims connected only for the second URL.
    db = tempfile.mktemp(suffix="_idx.db")
    idx = Indexer(
        rpc_url="http://primary.invalid",
        rpc_urls=["http://primary.invalid", "http://backup.invalid"],
        ardi_nft_addr="0x" + "1" * 40,
        db_path=db,
    )
    assert len(idx._rpc_urls) == 2
    assert idx._rpc_idx == 0

    # Simulate failover: replace _connect with a fake that returns a "connected"
    # web3 only for the backup URL.
    class _Fake:
        def __init__(self, url):
            self._url = url
        def is_connected(self):
            return "backup" in self._url
        @property
        def eth(self):
            class _Eth:
                def contract(self, address, abi):
                    return types.SimpleNamespace(address=address)
            return _Eth()

    idx._connect = lambda url: _Fake(url)
    assert idx._failover() is True
    # After failover, idx points at the second URL
    assert idx._rpc_idx == 1
    assert "backup" in idx._rpc_urls[idx._rpc_idx]
