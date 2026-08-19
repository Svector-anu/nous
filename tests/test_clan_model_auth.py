from __future__ import annotations

import os
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.world.config import WorldConfig
from src.world.components import Agent, Standing, Clan


def test_clan_model_auth_and_rate_limit(tmp_path):
    config = WorldConfig(agent_count=0, resource_count=0, chain_identity_enabled=True, llm_provider="anthropic")
    app = create_app(config=config, db_path=tmp_path / "auth.db")

    with TestClient(app) as client:
        world = app.state.simulation.world
        # Create a leader agent owned by owner_addr and promoted to officer.
        owner_addr = "0x" + "a" * 40
        leader = world.create_entity()
        world.add(leader, Agent(name="L", user_deployed=True, owner_address=owner_addr, spawn_tick=0))
        st = Standing()
        st.rank = "officer"
        world.add(leader, st)

        # Create the clan containing the leader.
        clan = Clan(clan_id=42, leader=leader, members=[leader])
        ce = world.create_entity()
        world.add(ce, clan)

        # Open a session for the owner and set a model three times (limit = 3).
        token = app.state.sessions.open(owner_addr)
        headers = {"Authorization": f"Bearer {token}"}
        for i in range(3):
            resp = client.post(f"/clans/42/advisor-model", json={"model": "claude-opus-5"}, headers=headers)
            assert resp.status_code == 200

        # Fourth attempt should be rate-limited.
        resp = client.post(f"/clans/42/advisor-model", json={"model": "claude-opus-5"}, headers=headers)
        assert resp.status_code == 429

        # A different valid session should be forbidden (not an owner officer).
        intruder = app.state.sessions.open("0x" + "b" * 40)
        resp = client.post(f"/clans/42/advisor-model", json={"model": "claude-opus-5"}, headers={"Authorization": f"Bearer {intruder}"})
        assert resp.status_code == 403

        # Operator override should work without a session when env token set.
        os.environ["OPERATOR_OVERRIDE_TOKEN"] = "op-secret"
        resp = client.post(f"/clans/42/advisor-model", json={"model": "claude-opus-5"}, headers={"X-Operator-Token": "op-secret"})
        assert resp.status_code == 200
        del os.environ["OPERATOR_OVERRIDE_TOKEN"]
