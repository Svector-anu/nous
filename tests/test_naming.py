"""What a visitor may call their agent.

A name is the one thing here a stranger writes that everyone else has to read. It stands
above an agent's head in a public world and lands in every screenshot taken of it. Nothing
filtered it for two thousand days, and the live world ended up with four agents named
after Hitler and one carrying a racial slur.

The line these draw: refuse hate, not ugliness. `asd` and `12312312` are somebody enjoying
themselves and cost nothing.
"""

from __future__ import annotations

import pytest

from src.world import naming
from src.world.components import Agent
from src.world.config import WorldConfig
from src.world.systems import spawning
from src.world.tick import Simulation, create_world, rename_the_unprintable

CONFIG = WorldConfig(seed=4, agent_count=3, resource_count=6)


# --- what is refused ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Hitler", "Adolf Hitler", "John Hitler", "hitler", "HITLER", "Nigga", "nigger"],
)
def test_the_names_actually_found_on_the_live_world_are_refused(name):
    assert naming.is_blocked(name)


@pytest.mark.parametrize(
    "name",
    [
        "H1TL3R",
        "A.d.o.l.f H_i_t_l_e_r",
        "h i t l e r",
        "H-I-T-L-E-R",
        "n1gg4",
        "Ｈｉｔｌｅｒ",
    ],
)
def test_the_obvious_ways_around_it_are_refused_too(name):
    """A list that only catches whoever did not bother is not doing anything. Separators,
    leet substitutions and fullwidth forms all fold onto the same string before matching."""
    assert naming.is_blocked(name), f"{name!r} walked past the filter"


# --- what is left alone ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["elsie", "Bob", "asd", "blabla", "12312312", "xxxx", "大王", "0xSkyway", "shuai", "Walker"],
)
def test_ugly_names_are_none_of_our_business(name):
    """All taken from the live world. A filter that reaches past hate starts refusing
    names people actually wanted, and being unimpressive is not an offence."""
    assert not naming.is_blocked(name)


@pytest.mark.parametrize("name", ["", "   ", "!!!", "123"])
def test_a_name_with_no_letters_is_not_blocked(name):
    """It folds to nothing, and nothing must not match every entry on the list."""
    assert not naming.is_blocked(name)


def test_a_clean_name_is_returned_untouched():
    assert naming.clean("elsie", 7) == "elsie"


# --- a blocked name never costs somebody their agent -------------------------------


def test_a_blocked_name_becomes_a_plain_one():
    """Renamed, not punished. Nobody else in the world can tell it was renamed, which is
    the point — everyone stops having to read it, and the owner is not put in a stockade.
    """
    cleaned = naming.clean("Adolf Hitler", 42)

    assert not naming.is_blocked(cleaned)
    assert "hitler" not in cleaned.lower()
    assert cleaned == "settler 42"


def test_a_deployed_agent_with_a_blocked_name_still_exists():
    """The rule that matters most. Somebody deployed it and it may be the only one they
    have — deleting it would be the operator taking something from a visitor to solve the
    operator's problem."""
    world = create_world(CONFIG)
    spawning.enqueue(world, "Adolf Hitler", "keen")
    Simulation(world).run(2)

    deployed = [e for e in world.query(Agent) if world.get(e, Agent).user_deployed]
    assert len(deployed) == 1, "the agent was dropped instead of renamed"
    assert not naming.is_blocked(world.get(deployed[0], Agent).name)


def test_an_ordinary_name_survives_the_spawn_path():
    world = create_world(CONFIG)
    spawning.enqueue(world, "elsie", "curious")
    Simulation(world).run(2)

    names = [world.get(e, Agent).name for e in world.query(Agent) if world.get(e, Agent).user_deployed]
    assert names == ["elsie"]


# --- the ones already living in the world ------------------------------------------


def test_agents_already_in_a_running_world_are_renamed():
    """The filter arrived after two thousand days of world, so it catches nothing already
    standing in it. Without this the live world keeps its four Hitlers forever."""
    world = create_world(CONFIG)
    entity = next(iter(world.query(Agent)))
    world.get(entity, Agent).name = "Hitler"

    renamed = rename_the_unprintable(world)

    assert len(renamed) == 1
    assert not naming.is_blocked(world.get(entity, Agent).name)


def test_the_sweep_leaves_everybody_else_alone():
    world = create_world(CONFIG)
    before = {e: world.get(e, Agent).name for e in world.query(Agent)}

    assert rename_the_unprintable(world) == []
    assert {e: world.get(e, Agent).name for e in world.query(Agent)} == before


def test_the_sweep_renames_nobody_twice():
    """It runs on every resume, so it has to be idempotent — a name it already fixed must
    not be fixed again into something else."""
    world = create_world(CONFIG)
    entity = next(iter(world.query(Agent)))
    world.get(entity, Agent).name = "Hitler"

    rename_the_unprintable(world)
    settled = world.get(entity, Agent).name

    assert rename_the_unprintable(world) == []
    assert world.get(entity, Agent).name == settled


def test_the_sweep_never_removes_an_agent():
    world = create_world(CONFIG)
    for entity in world.query(Agent):
        world.get(entity, Agent).name = "Hitler"
    before = len(list(world.query(Agent)))

    rename_the_unprintable(world)

    assert len(list(world.query(Agent))) == before
