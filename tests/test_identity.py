"""Wallet identity: nonces, sessions, and the link queue.

The link is a label. These tests pin that it attaches, that it survives a save, and that
it cannot be forged — never that it does anything in-world, because it must not.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from src.api.server import SessionStore, _nonce_in, create_app
from src.chain import siwe
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import Agent, IdentityQueue
from src.world.config import WorldConfig
from src.world.rng import TickRng
from src.world.systems import identity, spawning
from src.world.tick import Simulation, create_world, state_hash


def _world(**overrides) -> object:
    return create_world(WorldConfig(seed=1, **overrides))


def _deploy(world, name: str = "Test") -> int:
    """Deploy one agent and return its id."""
    before = set(world.query(Agent))
    spawning.enqueue(world, name, "")
    spawning.run(world, TickRng(1, world.tick + 1, "spawning"))
    return next(e for e in world.query(Agent) if e not in before)


# --- nonces and sessions -----------------------------------------------------


def test_each_challenge_is_unique():
    store = siwe.NonceStore()
    assert store.issue("0xabc").nonce != store.issue("0xabc").nonce


def test_a_nonce_verifies_at_most_once():
    """The replay guard. A challenge that could be consumed twice would let one
    signature open two sessions."""
    store = siwe.NonceStore()
    challenge = store.issue()
    assert store.consume(challenge.nonce) is not None
    assert store.consume(challenge.nonce) is None


def test_an_expired_nonce_is_refused():
    store = siwe.NonceStore(ttl=10)
    challenge = store.issue(now=100)
    assert store.consume(challenge.nonce, now=109) is not None
    later = store.issue(now=200)
    assert store.consume(later.nonce, now=211) is None


def test_a_session_expires():
    store = SessionStore(ttl=10)
    token = store.open("0xABC", now=100)
    assert store.address(token, now=105) == "0xabc"
    assert store.address(token, now=111) == ""


def test_an_unknown_session_names_nobody():
    assert SessionStore().address("not-a-real-token") == ""


def test_the_nonce_is_read_from_the_signed_text():
    """`_nonce_in` must find the nonce inside the message, because trusting a separate
    request field would let a signature be replayed against a fresh challenge."""
    message = siwe.build_message(
        domain="test", address="0xABC", nonce="xyz123", chain_id=4663
    )
    assert _nonce_in(message) == "xyz123"
    assert _nonce_in("no nonce here") == ""


def test_address_shape_is_checked():
    assert siwe.is_address("0x" + "a" * 40)
    assert not siwe.is_address("0xabc")
    assert not siwe.is_address("a" * 42)
    assert not siwe.is_address("0x" + "z" * 40)


def test_verification_fails_closed_on_a_bad_signature():
    """A malformed signature resolves to "no identity" — never to a crash, and never
    to a pass."""
    assert siwe.recover_address("hello", "0xnotasignature") == ""
    assert siwe.verify("hello", "0xnotasignature", "0x" + "a" * 40) is False


# --- the link queue ----------------------------------------------------------


def test_a_link_applies_on_the_next_tick_not_immediately():
    world = _world(agent_count=0)
    agent_id = _deploy(world)

    identity.enqueue(world, agent_id, "0xABC")
    assert world.get(agent_id, Agent).owner_address == ""

    identity.run(world, TickRng(1, 2, "identity"))
    assert world.get(agent_id, Agent).owner_address == "0xabc"


def test_a_native_agent_cannot_be_claimed():
    """Only user-deployed agents can carry a label. The world's own agents are not
    anyone's property."""
    world = create_world(WorldConfig(seed=1, agent_count=1))
    native_id = next(e for e in world.query(Agent))
    assert world.get(native_id, Agent).user_deployed is False

    identity.enqueue(world, native_id, "0xABC")
    identity.run(world, TickRng(1, 1, "identity"))
    assert world.get(native_id, Agent).owner_address == ""


def test_the_link_queue_is_bounded():
    world = _world(agent_count=0, identity_queue_limit=2)
    for index in range(5):
        identity.enqueue(world, index + 1, f"0x{index}")
    queue = identity.queue(world)
    assert queue is not None
    assert len(queue.pending) == 2


def test_a_later_link_replaces_an_earlier_one_for_the_same_agent():
    world = _world(agent_count=0)
    agent_id = _deploy(world)
    identity.enqueue(world, agent_id, "0xAAA")
    identity.enqueue(world, agent_id, "0xBBB")

    queue = identity.queue(world)
    assert queue is not None
    assert len(queue.pending) == 1

    identity.run(world, TickRng(1, 2, "identity"))
    assert world.get(agent_id, Agent).owner_address == "0xbbb"


def test_agents_owned_by_finds_every_agent_for_one_address():
    world = _world(agent_count=0)
    first = _deploy(world, "A")
    second = _deploy(world, "B")
    third = _deploy(world, "C")

    identity.enqueue(world, first, "0xABC")
    identity.run(world, TickRng(1, 2, "identity"))
    identity.enqueue(world, second, "0xABC")
    identity.enqueue(world, third, "0xDEF")
    identity.run(world, TickRng(1, 3, "identity"))

    assert identity.agents_owned_by(world, "0xabc") == sorted([first, second])
    assert identity.agents_owned_by(world, "0XABC") == sorted([first, second])
    assert identity.agents_owned_by(world, "") == []


def test_an_address_label_changes_nothing_the_world_does():
    """The property the whole feature rests on: linking is not an advantage.

    Two identical worlds, one with a linked agent, must stay identical apart from the
    label itself. If any system ever reads `owner_address`, this goes red.
    """
    def run(link: bool) -> str:
        world = _world(agent_count=0)
        simulation = Simulation(world)
        spawning.enqueue(world, "Subject", "")
        simulation.step()
        agent_id = next(e for e in world.query(Agent))
        if link:
            identity.enqueue(world, agent_id, "0xABC")
        simulation.run(40)
        # Blank the label so the hash compares the *world*, not the annotation.
        world.get(agent_id, Agent).owner_address = ""
        return state_hash(world)

    assert run(True) == run(False)


def test_the_owner_label_survives_a_save(tmp_path):
    """Trap 2: a component missing from COMPONENT_TYPES is silently dropped on reload."""
    world = _world(agent_count=0)
    agent_id = _deploy(world)
    identity.enqueue(world, agent_id, "0xABC")
    identity.run(world, TickRng(1, 2, "identity"))

    store = SqliteWorldStore(tmp_path / "identity.db")
    store.save(world)
    reloaded = store.load()
    store.close()

    assert reloaded.get(agent_id, Agent).owner_address == "0xabc"


def test_a_world_starts_with_an_identity_queue():
    assert create_world(WorldConfig(seed=1, agent_count=0)).first(IdentityQueue) is not None


def test_a_pending_link_survives_a_save(tmp_path):
    """A request in flight across a restart must still land, or a user who linked just
    before a deploy silently loses it."""
    world = _world(agent_count=0)
    agent_id = _deploy(world)
    identity.enqueue(world, agent_id, "0xABC")

    store = SqliteWorldStore(tmp_path / "pending.db")
    store.save(world)
    reloaded = store.load()
    store.close()

    identity.run(reloaded, TickRng(1, 2, "identity"))
    assert reloaded.get(agent_id, Agent).owner_address == "0xabc"


# --- the api boundary --------------------------------------------------------


def _client(tmp_path, **overrides):
    config = WorldConfig(agent_count=4, resource_count=8, **overrides)
    return TestClient(create_app(config, tmp_path / "chain.db"))


def test_chain_config_reports_a_disabled_deployment(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/chain/config").json()
        assert body["identity_enabled"] is False
        assert body["ready"] is False
        assert body["chain_id"] == 4663
        assert body["chain_name"] == "Robinhood Chain"


def test_chain_config_states_that_markets_are_demo(tmp_path):
    """The viewer reads this to label the market card. It must never claim real money
    while `real_money_enabled` is false."""
    with _client(tmp_path) as client:
        body = client.get("/chain/config").json()
        assert body["real_money_enabled"] is False
        assert body["markets_are_demo"] is True


def test_a_nonce_is_refused_while_identity_is_disabled(tmp_path):
    with _client(tmp_path) as client:
        assert client.post("/chain/nonce", json={"address": ""}).status_code == 403


def test_linking_is_refused_while_identity_is_disabled(tmp_path):
    with _client(tmp_path) as client:
        response = client.post(
            "/agents/1/link", json={"address": "0x" + "a" * 40, "session": "x"}
        )
        assert response.status_code == 403


def test_a_nonce_is_issued_when_identity_is_enabled(tmp_path):
    with _client(tmp_path, chain_identity_enabled=True) as client:
        response = client.post("/chain/nonce", json={"address": "0x" + "a" * 40})
        assert response.status_code == 200
        body = response.json()
        assert body["nonce"]
        assert body["chain_id"] == 4663
        # The nonce the client must sign is inside the message it signs.
        assert _nonce_in(body["message"]) == body["nonce"]


def test_a_malformed_address_is_refused(tmp_path):
    with _client(tmp_path, chain_identity_enabled=True) as client:
        assert client.post("/chain/nonce", json={"address": "nope"}).status_code == 422


def test_linking_without_a_session_is_refused(tmp_path):
    """The authorization check. An unauthenticated caller must not be able to put their
    address on somebody else's agent."""
    with _client(tmp_path, chain_identity_enabled=True) as client:
        response = client.post(
            "/agents/1/link",
            json={"address": "0x" + "a" * 40, "session": "forged-token"},
        )
        assert response.status_code == 401


# --- ownership enforcement on rest and delete --------------------------------
#
# The hole these close: before ownership was enforced, `/agents/{id}/rest` and
# `DELETE /agents/{id}` checked only `user_deployed`, so any visitor could rest or
# delete any other visitor's agent. Deletion is irreversible.


def _linked_agent(app, address: str) -> int:
    """Deploy an agent and link it to `address`, applied through a real tick."""
    world = app.state.simulation.world
    agent_id = _deploy(world)
    identity.enqueue(world, agent_id, address)
    identity.run(world, TickRng(1, world.tick + 1, "identity"))
    return agent_id


def test_a_stranger_cannot_rest_a_linked_agent(tmp_path):
    owner = "0x" + "a" * 40
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, owner)
        response = client.post(f"/agents/{agent_id}/rest", json={"rest": True})
        assert response.status_code == 403

        # And the agent is untouched — a refused request must change nothing.
        world = app.state.simulation.world
        assert world.get(agent_id, Agent).rest_mode is False


def test_a_stranger_cannot_delete_a_linked_agent(tmp_path):
    owner = "0x" + "a" * 40
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, owner)
        assert client.delete(f"/agents/{agent_id}").status_code == 403
        # Still alive. This is the one that cannot be undone.
        assert app.state.simulation.world.try_get(agent_id, Agent) is not None


def test_a_forged_session_token_is_refused(tmp_path):
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, "0x" + "a" * 40)
        response = client.post(
            f"/agents/{agent_id}/rest",
            json={"rest": True},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 403


def test_another_wallets_session_is_refused(tmp_path):
    """Authenticated is not the same as authorized. A real session for the wrong
    wallet must still be refused."""
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, "0x" + "a" * 40)
        intruder = app.state.sessions.open("0x" + "b" * 40)
        response = client.post(
            f"/agents/{agent_id}/rest",
            json={"rest": True},
            headers={"Authorization": f"Bearer {intruder}"},
        )
        assert response.status_code == 403


def test_the_owner_can_rest_their_own_agent(tmp_path):
    """The other half: the check must not lock out the person it exists to serve."""
    owner = "0x" + "a" * 40
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, owner)
        token = app.state.sessions.open(owner)
        response = client.post(
            f"/agents/{agent_id}/rest",
            json={"rest": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202


def test_the_owner_can_delete_their_own_agent(tmp_path):
    owner = "0x" + "a" * 40
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _linked_agent(app, owner)
        token = app.state.sessions.open(owner)
        response = client.delete(
            f"/agents/{agent_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert app.state.simulation.world.try_get(agent_id, Agent) is None


def test_an_unlinked_agent_stays_open_to_anyone(tmp_path):
    """Deliberate: the demo has to be playable without a wallet. Only a *linked*
    agent is protected — linking is what claims it."""
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        agent_id = _deploy(app.state.simulation.world)
        assert client.post(f"/agents/{agent_id}/rest", json={"rest": True}).status_code == 202


def test_ownership_is_not_enforced_while_identity_is_disabled(tmp_path):
    """With the feature off, behaviour is exactly what it was before this change."""
    app = create_app(
        WorldConfig(agent_count=0, resource_count=8, chain_identity_enabled=False),
        tmp_path / "own.db",
    )
    with TestClient(app) as client:
        world = app.state.simulation.world
        agent_id = _deploy(world)
        world.get(agent_id, Agent).owner_address = "0x" + "a" * 40
        assert client.post(f"/agents/{agent_id}/rest", json={"rest": True}).status_code == 202


# --- the message has to name the site the user is on --------------------------


def test_the_challenge_names_the_host_the_browser_is_on(tmp_path):
    """eip-4361 binds a signature to a domain, and a wallet compares the one in the
    message against the page that asked. a mismatch is what a phishing site looks like,
    so metamask warns and the user cancels — which is what "cancelled" meant."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=4, chain_identity_enabled=True),
        tmp_path / "siwe.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        message = client.post("/chain/nonce", json={"address": "0x" + "a" * 40}).json()["message"]

    assert message.startswith("nous.city wants you to sign in")
    assert "URI: https://nous.city" in message
    assert "https://nous\n" not in message  # the old hardcoded value


def test_a_local_challenge_is_not_claimed_to_be_https(tmp_path):
    """A wallet checking the uri against the page would fail on https for a plain-http
    development server."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=4, chain_identity_enabled=True),
        tmp_path / "siwe.db",
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        message = client.post("/chain/nonce", json={"address": "0x" + "a" * 40}).json()["message"]

    assert message.startswith("127.0.0.1 wants you to sign in")
    assert "URI: http://127.0.0.1" in message


def test_the_challenge_carries_issued_at(tmp_path):
    """Required by eip-4361, not optional. a wallet parses this text — metamask
    recognises a sign-in request and renders it as one — and a message that looks like
    eip-4361 but fails the parse is treated as suspicious rather than shown plainly.
    omitting a required field does not produce a plainer prompt, it produces a rejected
    one, which reaches the app as error 4001 and reads as "the user cancelled"."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=4, chain_identity_enabled=True),
        tmp_path / "issued.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        message = client.post("/chain/nonce", json={"address": "0x" + "a" * 40}).json()["message"]

    issued = [l for l in message.splitlines() if l.startswith("Issued At: ")]
    assert issued, "eip-4361 requires Issued At"
    # ISO 8601 with a zone, which is what the spec asks for.
    assert issued[0].endswith("Z")


def test_the_message_matches_the_eip_4361_grammar():
    """Checked field by field against the spec rather than eyeballed.

    Two bugs shipped here because this was hand-written from memory: a hardcoded domain
    and a missing Issued At. Both produced a message metamask refused to parse, and a
    refused parse arrives as error 4001 — identical to the user pressing cancel. So the
    grammar is asserted, not assumed.
    """
    import re

    message = siwe.build_message(
        domain="nous.city", address="0x" + "A" * 40, nonce="abc123xyz789",
        chain_id=4663, uri="https://nous.city",
    )
    lines = message.splitlines()

    assert re.match(r"^\S+ wants you to sign in with your Ethereum account:$", lines[0])
    assert re.match(r"^0x[0-9a-fA-F]{40}$", lines[1])
    assert lines[2] == ""
    assert lines[3].strip(), "statement"
    assert lines[4] == ""
    assert any(l.startswith("URI: ") for l in lines)
    assert "Version: 1" in lines
    assert any(l.startswith("Chain ID: ") for l in lines)
    # The spec requires at least 8 alphanumerics of nonce.
    assert re.search(r"^Nonce: [a-zA-Z0-9]{8,}$", message, re.M)
    assert re.search(r"^Issued At: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", message, re.M)


# --- ownership at spawn, not a moment later ----------------------------------


def test_a_deploy_with_a_session_is_owned_from_the_tick_it_exists(tmp_path):
    """Claiming after the fact left a window — however short — in which a stranger could
    claim it first. "Unclaimed means anyone may claim it" is only safe while nobody else
    is watching."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=6, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        simulation = app.state.simulation
        simulation.store = None
        world = simulation.world
        address = "0x" + "d" * 40
        session = app.state.sessions.open(address)

        body = client.post(
            "/agents", json={"name": "Kamir", "personality": "", "session": session}
        ).json()
        assert body["owned"] is True

        simulation.step()
        agent = next(
            world.get(e, Agent) for e in world.query(Agent) if world.get(e, Agent).user_deployed
        )
        assert agent.owner_address == address.lower(), "owned on the tick it appeared"


def test_a_deploy_without_a_session_is_nobody_s(tmp_path):
    """Deploying without a wallet still works — that is what keeps the world playable
    without one — and the agent simply carries no owner."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=6, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        simulation = app.state.simulation
        simulation.store = None
        body = client.post("/agents", json={"name": "Bo", "personality": ""}).json()
        assert body["owned"] is False

        simulation.step()
        world = simulation.world
        agent = next(
            world.get(e, Agent) for e in world.query(Agent) if world.get(e, Agent).user_deployed
        )
        assert agent.owner_address == ""


def test_a_forged_session_owns_nothing(tmp_path):
    """The session is the proof. A string that is not one buys no ownership."""
    app = create_app(
        WorldConfig(agent_count=2, resource_count=6, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        simulation = app.state.simulation
        simulation.store = None
        body = client.post(
            "/agents", json={"name": "Mal", "personality": "", "session": "not-a-token"}
        ).json()
        assert body["owned"] is False


def test_two_deploys_of_one_name_do_not_take_each_other(tmp_path):
    """Names are not unique. Resolving by name has to pick the agent that was actually
    deployed by the person who proved a wallet, not whichever shares the name."""
    from src.world.systems import identity as identity_system

    app = create_app(
        WorldConfig(agent_count=2, resource_count=6, chain_identity_enabled=True),
        tmp_path / "own.db",
    )
    with TestClient(app, base_url="https://nous.city") as client:
        simulation = app.state.simulation
        simulation.store = None
        world = simulation.world

        # One anonymous deploy lands first and stays unowned.
        client.post("/agents", json={"name": "Twin", "personality": ""})
        simulation.step()

        session = app.state.sessions.open("0x" + "e" * 40)
        client.post("/agents", json={"name": "Twin", "personality": "", "session": session})
        simulation.step()

        twins = sorted(
            e for e in world.query(Agent)
            if world.get(e, Agent).user_deployed and world.get(e, Agent).name == "Twin"
        )
        assert len(twins) == 2
        # The newest is the one that was paid for with a session.
        assert world.get(twins[-1], Agent).owner_address == ("0x" + "e" * 40)
        assert world.get(twins[0], Agent).owner_address == ""
