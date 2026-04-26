"""End-to-end example: register → commit → reveal → inscribe.

Demonstrates the full agent lifecycle. In production an agent would loop
this every epoch and use a real LLM solver instead of the placeholder.

Run:
    AGENT_PK=0x... python3 examples/full_cycle.py
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

# Allow running without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ardi_sdk import ArdiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("example")


# Replace this stub with a real LLM call
def my_solver(riddle_text: str, language: str) -> str:
    """Placeholder solver — replace with Anthropic / OpenAI / local LLM."""
    log.info(f"would solve [{language}] riddle: {riddle_text[:60]}...")
    return "fire"  # always guesses fire — for demo only


def main():
    # Wire the client
    deploy = json.loads(Path("contracts/deployments/local.json").read_text())
    client = ArdiClient(
        rpc_url=os.environ.get("RPC_URL", "http://localhost:8547"),
        coordinator_url=os.environ.get("COORDINATOR_URL", "http://localhost:8080"),
        agent_private_key=os.environ["AGENT_PK"],
        contracts={
            "ardi_nft": deploy["ardiNFT"],
            "ardi_token": deploy["ardiToken"],
            "bond_escrow": deploy["bondEscrow"],
            "epoch_draw": deploy["epochDraw"],
            "mint_controller": deploy["mintController"],
            "mock_awp": deploy["mockAWP"],
        },
        chain_id=int(deploy["chainId"]),
    )
    log.info(f"agent address: {client.address}")

    # 1. Register as miner (one-time)
    if not client.is_miner():
        log.info("registering as miner — needs 10K AWP + KYA")
        client.register_miner()
        log.info(f"  registered ✓")

    # 2. Fetch current epoch
    epoch = client.fetch_current_epoch()
    log.info(f"epoch {epoch.epoch_id}: {len(epoch.riddles)} riddles, "
             f"commit deadline in {epoch.commit_deadline - int(time.time())}s")

    # 3. Pick the highest-power riddle and commit a guess
    target = max(epoch.riddles, key=lambda r: r.power)
    log.info(f"targeting wordId={target.word_id} power={target.power} rarity={target.rarity}")
    guess = my_solver(target.riddle, target.language)

    ticket = client.commit(epoch.epoch_id, target.word_id, guess)
    log.info(f"committed: tx={ticket.tx_hash[:16]}... — REMEMBER THE NONCE")

    # 4. Wait for commit window to close
    wait = epoch.commit_deadline - int(time.time()) + 5
    log.info(f"sleeping {wait}s until commit window closes...")
    time.sleep(max(0, wait))

    # Coordinator publishes answer for our wordId — we don't need to do anything
    # but confirm our reveal will work. Poll the contract.

    # 5. Reveal
    log.info("revealing...")
    client.reveal(ticket.epoch_id, ticket.word_id, ticket.guess, ticket.nonce)

    # 6. Wait for reveal window + draw
    state = client.epoch_state(epoch.epoch_id)
    wait = state["reveal_deadline"] - int(time.time()) + 5
    log.info(f"sleeping {wait}s until reveal window closes + VRF...")
    time.sleep(max(0, wait))

    # 7. Trigger VRF (anyone can; we do it ourselves so we don't have to wait
    #    for someone else)
    n_correct = client.correct_count(epoch.epoch_id, target.word_id)
    log.info(f"{n_correct} correct revealers for this slot")
    if n_correct > 0:
        client.request_draw(epoch.epoch_id, target.word_id)

    # 8. Poll for winner
    for attempt in range(30):
        winner = client.winner_of(epoch.epoch_id, target.word_id)
        if winner != "0x" + "0" * 40 and winner.lower() != "0x0000000000000000000000000000000000000000":
            log.info(f"winner: {winner}")
            break
        log.info(f"  waiting for VRF callback... ({attempt + 1}/30)")
        time.sleep(5)
    else:
        log.warning("VRF never fulfilled in 150s — try cancelStuckDraw later")
        return

    # 9. If we won, mint
    if winner.lower() == client.address.lower():
        log.info("we won — minting Ardinal")
        client.inscribe(epoch.epoch_id, target.word_id)
        log.info(f"  total mints by us: {client.mint_count()}")
    else:
        log.info(f"we did not win this slot — better luck next epoch")


if __name__ == "__main__":
    main()
