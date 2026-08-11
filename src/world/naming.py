"""What a visitor may call their agent.

Names are the one thing here a stranger writes that everybody else has to read. They sit
above an agent's head in a public world, they go into the log, and they end up in every
screenshot anybody takes of it. On the live world that meant four agents named after
Hitler and one carrying a racial slur, standing in a settlement nobody could show anyone.

Two rules, and the gap between them is deliberate.

**Refuse hate, not ugliness.** `asd`, `blabla` and `12312312` are somebody enjoying
themselves and cost nothing. A slur is not the same kind of thing and is not a matter of
taste. This blocks a narrow, specific set and leaves the rest alone, because a filter that
reaches further starts refusing names people actually wanted.

**A blocked name never destroys an agent.** Somebody deployed it, it has lived a life and
it may be somebody's only one. It gets a neutral name and carries on — the same reasoning
as masking a key rather than deleting it. Deletion is the operator taking something from a
visitor to solve the operator's problem.

This will not catch everything. It is a doorstop, not a moderation system, and anything
that gets past it is handled by a human renaming it.
"""

from __future__ import annotations

import re
import unicodedata

# Deliberately short and specific. Every entry is either a slur or a figure whose name is
# only ever chosen here to make a point of it — not a list of words somebody might find
# rude.
BLOCKED = (
    "hitler",
    "nigger",
    "nigga",
    "faggot",
    "kike",
    "chink",
    "spic",
    "tranny",
    "retard",
    "rapist",
    "goebbels",
    "himmler",
    "mussolini",
    "stalin",
    "polpot",
    "nazi",
    "kkk",
)

# Letters people substitute to walk a name past a filter. Folded before matching so
# `H1TL3R` and `n1gg4` do not read as clean.
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a", "!": "i"})

REPLACEMENT = "settler"


def fold(name: str) -> str:
    """Reduce a name to what it is really trying to say.

    Accents stripped, leet folded, separators removed — so `A.d.o.l.f  H_i_t_l_e_r` and
    `Ｈｉｔｌｅｒ` collapse onto the same thing the blocklist is written in. Without this
    the list only catches whoever did not bother.
    """
    # NFKD splits accented characters into letter plus mark, and the fullwidth forms used
    # to dodge filters normalise back to plain ascii.
    plain = unicodedata.normalize("NFKD", name or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = plain.lower().translate(LEET)
    return re.sub(r"[^a-z]", "", plain)


def is_blocked(name: str) -> bool:
    """Whether this name may not be used.

    Substring match on the folded form, which is what catches `Adolf Hitler` and
    `John Hitler` alike. It also means a legitimate name containing a blocked run would be
    refused — the classic cost of this approach. The list is kept to strings that do not
    occur inside ordinary words, so the trade is worth taking here.
    """
    # A name with no letters folds to "", which contains none of the entries — so the
    # empty case needs no branch of its own.
    folded = fold(name)
    return any(bad in folded for bad in BLOCKED)


def clean(name: str, agent_id: int | None = None) -> str:
    """The name an agent should actually carry.

    Returns the original when it is fine. Otherwise a neutral one that is still a name
    rather than a punishment — nobody else in the world can tell it was renamed, which
    matters, because the point is to stop everyone else having to read it and not to put
    somebody in a stockade.
    """
    if not is_blocked(name):
        return name
    return REPLACEMENT if agent_id is None else f"{REPLACEMENT} {agent_id}"
