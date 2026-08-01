# Onboarding — research, audit, and revised plan

**Status: implemented in `src/viewer/index.html`.**

## What a first-time visitor actually sees today

![current-ui](planish/onboarding_research_frames/current-ui.png)

This is a live 3D world with 112 agents, but the first screen is a **dashboard, not a game**. The
sidebar has roughly **fifteen unrelated controls** before the visitor has done anything: camera
mode, three fly-to buttons, nine statistics, a prediction-market panel, a minimap, an inspector,
a colour toggle, a clan legend, and a deploy form.

Measured on a fresh 1440×900 browser:

- The deploy form — the only way to *be* in the world — is **below the fold**.
- The first text on screen is `drag orbit · shift-drag pan · wheel zoom · click an agent`.
- The markets panel asks for a name and a bet before the visitor has any idea what the numbers
  mean.
- A deployed agent arrives as a single line of text in the sidebar (`nick arrives` in the
  screenshot), while the camera keeps looking elsewhere.

## The four references decoded

### 1. The two Arkion-style screens

Both are full-bleed cyberpunk arena mockups with the same scene behind them.

| | screen 1 | screen 2 |
|---|---|---|
| decisions | **zero** | **one** (log in) |
| text | `ARKION` logo only | title + one line + two fields |
| background | full-bleed neon arena art | **the same art, unchanged** |

Screen 1 is just the art, a glowing cyan `A` mark, and the word `ARKION`. Nothing else.

Screen 2 adds a centered translucent dark panel:

- Title: **“Enter the Arena”**
- Subtitle: **“Log in to unlock missions, rewards, and ranked battles.”**
- Two fields: **Username**, **Password**
- One cyan button: **LOGIN**

The menu never cuts to a new page. The overlay sits on the same continuous world. The one line of
copy promises a *reward*, not an instruction. The two input fields are the only thing that changed
between the screens.

### 2. Hollow Knight

![Hollow Knight title](planish/onboarding_research_frames/hollow-knight-title.png)

- Title screen is the game world: dark, atmospheric, one obvious button.
- Save-slot screen keeps the same background and the same minimal ornament.
- The first minute teaches by doing: no fall damage, destructible walls, combat, currency, and
  hidden paths are all discovered in play, not explained in text.

**What to steal:** atmosphere first, menu as overlay, and teaching through action.  
**What to ignore:** Hollow Knight is a platformer where *you* are in danger. Our visitor is a
spectator, so the danger cannot be the tutorial.

### 3. Tom Clancy’s The Division 2 summary

![Division 2 summary](planish/onboarding_research_frames/division2-summary.png)

- The character/progression screen is a **translucent overlay on the live world**.
- It shows one thing at a glance: level, inventory, and a *recommended next activity*.
- The world keeps moving behind the menu.

**What to steal:** a post-arrival summary card that tells the player what just happened and what
 they can do next, without leaving the world.

### 4. Minecraft

Already researched in the previous pass:

- The title screen is a **fake panorama** — six static images, slow-panned — because rendering a
  live world behind a menu was too expensive.
- Minecraft deliberately has no tutorial; the first night is the teacher.

**What to steal:** we can do what Minecraft faked. Our world is already live, so the title screen
 should be the real world, not images.  
**What to ignore:** the no-tutorial stance. A spectator is not threatened by the world, so silence
 teaches nothing.

## Best-practice audit of the current UI

Against the seven principles you supplied:

| principle | current UI | verdict |
|---|---|---|
| 1. One decision at a time | 15+ controls visible immediately | fail |
| 2. Get to the first win fast | deploy form is below the fold | fail |
| 3. Teach by doing | tooltips and text, no guided action | fail |
| 4. Good defaults | no default path; markets ask for a bet | fail |
| 5. Feedback on everything | status updates, but spawn is tiny text | partial |
| 6. Respect returning players | same wall every time | fail |
| 7. Test the first 60 seconds | not tested | fail |

## Revised flow

Five moments. Every one is an **overlay over the live world** — the world never cuts away and never
pauses.

### Screen 0 — attract

- The live world fills the screen, cinematic camera already drifting.
- A dark overlay with the title and one line of stakes.
- One primary button: **“Put yourself in this world.”**
- One first-class escape: **“Just watch.”** Most visitors are spectators, not players.

> Neo-Civilization  
> 112 people are alive in here. Nobody is controlling them.  
> [ Put yourself in this world ] · just watch

This is the answer to “first screens should have real motion.” The motion is the live world, not
a video or a static panorama.

### Screen 1 — name / registration

- One input, pre-filled with a generated name so the default is already valid.
- No password, no email, no personality field.

**Why cut personality from onboarding:** it is stored, displayed, and **read by nothing**
(`AGENTS.md` trap 5). Asking someone to write a personality that changes no behaviour is exactly
the mistake “asking a player to make a decision before they understand the choice.” It can return
later, honestly labeled as a note others can read, once something consumes it.

### Screen 2 — the drop-in

- Overlay dissolves.
- Camera **flies from the current cinematic shot down to street level** and lands on the spawning
  agent.
- HUD names them: “{name} arrives.”
- This is the first win. It costs nothing new: `flyTo`, `streetLevel`, and the HUD already exist.

### Screen 3 — arrival summary

A translucent overlay, Division 2 style, shows the new agent:

- Name, clan, current state.
- One recommended action: **“Will {name} still be alive at tick {now+400}?”** [ yes ] [ no ].

This teaches the market, the tick clock, and the idea of stake in one tap.

### Screen 4 — the living world

- Overlay collapses. The sidebar is reduced to the essentials: camera, status, and a “my agent”
  button.
- Full dashboard is available with one click or for returning visitors.
- Returning visitors skip the attract/name screens entirely and land here.

## What this reuses

- Live world, cinematic director, `flyTo`, `streetLevel`, HUD.
- `/agents` deploy endpoint, `/markets` endpoint, `localStorage`.
- No simulation rule changes. No new art. No build step.

## What is genuinely new

- An overlay layer with ~4 screens of copy.
- A spawn-follow camera call.
- A “seen intro” flag so returning visitors skip straight to the world.
- An arrival summary card and a reduced default sidebar.

## Cost estimate

Viewer-only, roughly **2–3 days**, gated by the usual Python tests and 39 browser checks.

## Implementation screenshots

Attract screen, name screen, and arrival summary:

![attract](planish/onboarding_research_frames/onboarding-attract.png)
![name](planish/onboarding_research_frames/onboarding-name.png)
![summary](planish/onboarding_research_frames/onboarding-summary.png)

## Open questions

1. **Is a name required at all?** We could deploy with a generated name and let renaming happen
   later. That is zero decisions to first win — strongest version of principle 2 — but it costs the
   small ownership moment.
2. **How much brand styling?** The references use painted art and ornate type. We have zero art
   assets by rule, and the live world is the art. I would keep the overlay minimal and use the
   existing monospace/system aesthetic unless you want a logo/typeface decision.
3. **Does the market prompt belong in the first minute?** It is the fastest way to give a spectator
   a stake, but it can also feel like being asked to gamble before you understand the game. This is
   the most arguable screen.

## Not verified

The two Arkion-style screenshots were seen in the conversation and their structure is captured
above: screen 1 is only the logo over the arena, screen 2 is the same arena with a minimal login
overlay. They were not saved as project files.

## Sources

- Minecraft panorama and menu — minecraft.wiki (`Panorama`, `Menu_screen`).
- Hollow Knight interface and onboarding analysis — `champicky.com` and `indiegameculture.com` via
  Exa / agentcash.
- Division 2 UI — original video supplied by user; clean frames extracted with `ffmpeg` for reading, and also verified with `imageio`.
- Current UI screenshot — captured with Playwright against the running viewer.
- The seven onboarding principles and common mistakes — supplied by user.
