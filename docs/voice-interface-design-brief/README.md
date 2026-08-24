# Handoff: V — ambient AI assistant interface

## Overview

V is a personal, always-listening AI assistant — a voice-first agent that lives on an ambient device in a room, with a terminal REPL as its primary text surface and an Obsidian vault as its durable memory. This package specifies the complete visual and interaction design for four real surfaces (ambient device, terminal, per-answer inspection, proactive messaging) plus the identity system and motion spec.

Three product constraints drove the design and must survive implementation:

1. **There is no dashboard.** Obsidian is the dashboard. The one inspection view in this design is scoped to a *single answer*, reachable only from that answer. It has no home, no history, no browsing. Do not grow it into an app.
2. **The cost line is a product feature, not debug output.** Route, latency, price and confidence appear under every answer V gives, in every surface. It is dim, it is never hidden, and it is never behind a flag or a setting.
3. **A wrong transcript is a designed state, not an error.** Persian/English speech-to-text is unreliable. Every write V makes carries a confidence score; low-confidence writes are *held*, not committed, and are correctable in plain language for 24 hours.

## About the design files

The `.dc.html` files in this bundle are **design references created in HTML** — prototypes that show intended look and behaviour. They are not production code and should not be copied into the app.

Your task is to **recreate these designs in the target codebase's existing environment** (React, SwiftUI, Electron, a TUI framework such as Ink/Bubble Tea, etc.), using its established patterns, component library and styling approach. If no environment exists yet, choose the most appropriate stack per surface — note that the terminal surface may genuinely be a terminal program, not a web view, and the spec below is written so it can be implemented either way.

`support.js` is the runtime that makes the prototypes open in a browser. It has no relevance to production. Open any `.dc.html` file directly in a browser to view the design; the left rail switches between the seven sections.

**Which version to build:** `V v3.dc.html`. v1 and v2 are included only to show how the mark and the thinking animation evolved.

## Fidelity

**High-fidelity.** Colors, typography, spacing, motion timings and copy are final and should be reproduced exactly. Every hex value, duration and string in this README is deliberate.

Two areas are intentionally *not* final and are marked as such below: the physical industrial design of the ambient device (only its light behaviour is specified) and the Persian voice copy beyond the handful of strings listed.

---

## Design tokens

### Color

| Token | Hex | ANSI 256 | Use |
|---|---|---|---|
| seed | `#7C5CFF` | 99 | Primary. V's voice, active state, the ✦ glyph, focus. |
| deep | `#5B3FD6` | 62 | Structural violet: outer aperture ring, secondary bars. |
| lift | `#A793FF` | 141 | Highlight violet: core-dot centre, emphasis on dark violet. |
| core-hi | `#D8CEFF` | 189 | Specular highlight inside the core dot only. |
| void | `#08080A` | 232 | App background. |
| surface | `#0A0A0E` | 233 | Terminal body, state cards. |
| panel | `#0E0E12` | 234 | Card background. |
| panel-2 | `#101016` | 234 | Nested card / terminal chrome. |
| line | `#1B1B23` | 235 | Card border. |
| line-2 | `#1E1E28` | 236 | Terminal border, inner dividers. |
| line-3 | `#23232F` | 237 | Emphasised border. |
| hover-line | `#33334A` | 240 | Border on hover. |
| dim-3 | `#3E3E4C` | 239 | Least-important mono text. |
| dim-2 | `#4E4E5E` | 240 | Cost line, metadata. |
| dim-1 | `#5A5A6B` | 241 | Section labels. |
| muted | `#6E6E80` | 243 | Secondary body copy. |
| body | `#7E7E90` / `#8A8A9A` | 244/245 | Body copy. |
| body-hi | `#9E9EAE` | 246 | Emphasised body copy. |
| paper | `#D6D6E2` | 253 | V's spoken text in the terminal. |
| paper-hi | `#EDEDF2` | 255 | Primary text, the user's own input. |
| unsure | `#E8A33D` | 179 | Low confidence, held writes, budget warning. |
| refuse | `#FF6B6B` | 203 | Refusal. Never used for "error". |
| free | `#5CD9A6` | 79 | Local / zero-cost / committed / online. |

Every color has a 256-color terminal fallback because the terminal surface must render correctly in a plain ANSI terminal.

### Typography

- **IBM Plex Sans** — 300/400/500/600/700. All interface chrome, body copy, headings.
- **IBM Plex Mono** — 300/400/500/600. The terminal, every cost line, every route line, all metadata, all numbers. Anything V *says* in text is mono.
- **IBM Plex Sans Arabic** — 300/400/500/600. Persian content only. Same metrics family, so mixed blocks share a baseline.

Scale in use: 9px / 9.5px / 10px / 10.5px / 11px / 11.5px / 12px / 12.5px / 13px / 13.5px / 14.5px / 15px / 16px / 17px / 20px / 24px / 26px / 34px / 44px / 52px / 64px.

Section labels: IBM Plex Mono, 9.5px, `letter-spacing: .2em`, uppercase, `#5A5A6B`.
Cost lines: IBM Plex Mono, 10.5–11px, `#4E4E5E`, with individual values lifted to `#8A8A9A`.
Body copy uses `text-wrap: pretty`.

### Spacing, radius, shadow

- Spacing scale: 4, 5, 6, 8, 9, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 44, 56, 64, 96.
- Radius: 4 (tag) · 7 (small button) · 8–10 (nav item, inner block) · 12–14 (notification, inner card) · 16–18 (state card, motion card) · 20–24 (section card) · 50% (aperture, core dot).
- Section card: `background:#0E0E12; border:1px solid #1B1B23; border-radius:20px; padding:28px`.
- Terminal window shadow: `0 40px 80px -40px rgba(0,0,0,.9)`.
- Notification shadow: `0 20px 44px -18px #000`.
- Glow is `drop-shadow`/`box-shadow` in `rgba(124,92,255, α)` — never a border.

---

## Identity

### The aperture — the mark

Three concentric circles, always: a solid outer ring, a dashed middle ring, a filled core dot. Nothing else. This is the device, the app icon, the favicon, and the ambient orb — they are the same object at different sizes.

At a 62px reference size:
- Outer ring: `inset 0`, `2.5px solid` deep `#5B3FD6`, full circle.
- Dashed ring: inset ≈ 17% of the diameter, `2.5px dashed` seed `#7C5CFF`.
- Core dot: ≈ 25% of the diameter, `border-radius:50%`, `radial-gradient(circle at 34% 30%, #D8CEFF, #8E75FF 62%, #5B3FD6)`.
- Container: `filter: drop-shadow(0 0 Npx rgba(124,92,255, α))` where N and α scale with state intensity.

At 16px it reduces to two rings at 4px stroke with the dash dropped.

**The mark is the state.** The dashed ring's rotation period and the core dot's diameter are bound to V's state (see Ambient below). Implement it as one component that takes a state, not as a static asset plus separate animations.

### The ✦ glyph — the voice

`✦` (U+2726) prefixes everything V *says in text* — terminal output, notifications, spoken-message transcripts. It is never a logo, never in a container, never decorative, and nothing else in the system may use it.

Four colors, and only four:

| Color | Meaning |
|---|---|
| `#7C5CFF` seed | speaking normally |
| `#E8A33D` unsure | low confidence |
| `#FF6B6B` refuse | refused |
| `#3B3B49` grey | degraded / local-only |

**The aperture and ✦ never appear together.** If the aperture is visible, ✦ is redundant.

### Icon set

Single mono glyphs, no icon font, no SVG icon library:

| Glyph | Meaning |
|---|---|
| `◈` | route / model |
| `⟡` | memory / vault note |
| `◉` | confidence score |
| `$` | cost |
| `●` | local, no network |
| `▲` | refused |

### Rejected directions

The prototype's Identity section shows six explored marks. Recorded so they aren't re-litigated: **A · Seed** (the ✦ in a rounded-square tile) was rejected as generic "sparkle AI". **B · Notch** (V cut out of a disc) is the strongest fallback if the aperture proves too abstract. **C · Aperture** was chosen. **D · Monolith**, **E · Utterance** and **F · Gyro** were rejected — F survives as the *thinking animation*, not the mark.

---

## Screens / views

The prototype presents seven sections in a left rail. Sections 01 and 07 are specification, not product. **Sections 02–06 are the product.**

### 02 — Ambient (the device in the room)

**Purpose.** The only feedback surface on a device with no screen. It answers one question at a glance: is this thing listening to me, and has anything left the room?

**Layout.** A stage card and a side column, `grid-template-columns: repeat(auto-fit, minmax(340px,1fr))`, gap 28. Stage: `border-radius:24px`, `min-height:520px`, background `radial-gradient(700px 460px at 50% 42%, #12101E, #0A0A0E 72%)`, centred column. Corner labels in mono 10px `#4E4E5E`: state name top-left, state metadata top-right.

**Composition, top to bottom.** Aperture stage (square, `width:100%; max-width:300px; aspect-ratio:1`) containing a halo, an optional gyroscope, an optional emit ring, and the aperture · a 21-bar waveform · a caption · a cost line.

- **Halo** — `88%` of the stage, max 260px, circle, `radial-gradient(circle, rgba(124,92,255,α), transparent 66%)`, `vHalo 5s ease-in-out infinite` (scale 1 → 1.18, opacity .30 → .55). α per state below.
- **Aperture** — 152px normally, **92px while thinking** (so the gyroscope reads as bigger than him). Transition `.55s cubic-bezier(.2,.8,.2,1)` on width/height. Container `filter: drop-shadow(0 0 [glow×40]px rgba(124,92,255, glow))`.
- **Waveform** — 21 bars, 3px wide, 5px gap, 2px radius, `transform-origin:bottom`, height transition `.35s cubic-bezier(.2,.8,.2,1)`. Relative heights: `.2 .35 .55 .4 .75 .95 .6 .85 1 .7 .45 .8 1 .62 .9 .5 .72 .38 .55 .3 .18`, rendered as `6 + h×44` px. **These heights must be driven by real mic RMS in production, not replayed.** Three modes: `flat` (all 3px, `#2A2A36`, no animation), `live` (seed `#7C5CFF`, `vBar 900ms ease-in-out` with a 38ms per-bar stagger), `speak` (lift `#9E86FF`, 700ms), `off` (3px, `#1E1E28`, 50% opacity).
- **Caption** — 16px sans, `line-height:1.6`, `text-wrap:pretty`, max 520px, centred.
- **Cost line** — mono 10.5px `#4E4E5E`, `letter-spacing:.04em`, 14px below the caption.

**States.** Selecting a row in the side list drives all of the above.

| State | Label / meta | Aperture ring spin | Core Ø | Glow | Halo | Waveform | Caption | Cost line |
|---|---|---|---|---|---|---|---|---|
| Idle | `IDLE` / `mic hot · nothing leaves the room` | 26s | 26px | .22 | .14 | flat | "Hearing. Not recording. Nothing has left this room." (`#6E6E80`) | `local vad · $0.0000 · 0 bytes transmitted` |
| Wake heard | `WAKE HEARD` / `"V"` | 3.4s | 34px | .55 | .40 | flat + emit ring | "Yes?" (`#EDEDF2`) | `wake word matched locally · 0.02s · $0.0000` |
| Listening | `LISTENING` / `buffering · 3.1s` | 6s | 30px | .60 | .42 | live | the live transcript (`#D6D6E2`) | `whisper-small · local · $0.0000 · not yet sent` |
| Thinking | `THINKING` / `2 tools · 1 vault read` | — (gyroscope) | 16px | .42 | .30 | flat | `…` (`#6E6E80`) | `◈ sonnet-4.5 · 1.2s so far · $0.0026 running` |
| Speaking | `SPEAKING` / `sentence 1 of 2` | 2.2s | 42px | .70 | .50 | speak + emit ring | what he is saying (`#EDEDF2`) | `◈ sonnet-4.5 · 1.24s · $0.0031 · ◉ 0.94` |
| Hard muted | `HARD MUTED` / `ring turned · mic off in hardware` | none | 22px | 0 | 0 | off | "Nothing here is listening." (`#5A5A6B`) | `no process running · $0.0000` |

Core dot animation per state: idle `vBreathe 5s`, wake `vBreathe 1.4s`, listening `vBreathe 2.2s`, thinking `vFlicker 2.2s`, speaking `vBreathe 1.1s`, muted **none**.

While thinking, the aperture's own rings drop to `opacity .28` (outer) and `.2` (dashed) and stop spinning; the gyroscope renders in front.

**Physical rules (side panel, and binding on the hardware).**
- The aperture is the only light on the device. No text, ever.
- Dim violet means hearing but not recording. Nothing has left the room.
- A **hardware mute ring** on the body cuts the mic in hardware. The aperture goes to a flat unlit grey — `#23232F` rings, `#1E1E28` core, zero glow, zero animation — a state no software path can produce. Transition *into* muted is 0ms; transition *out* is 800ms.
- V never speaks first unless the proactive budget allows it **and** he can hear that someone is in the room.

Industrial design of the enclosure is out of scope here; only the light behaviour above is specified.

### 03 — Terminal REPL

**Purpose.** The text surface. Everything V can do by voice, he can do here, and here it is inspectable.

**Layout.** Window `border-radius:14px`, `border:1px solid #1E1E28`, background `#0A0A0E`. Title bar: 11px/16px padding, `#101016`, bottom border `#1E1E28`, three 10px `#2A2A36` dots, `v — repl — 118×34` in mono 10.5px `#6E6E80`, and right-aligned status in mono 10px `#4E4E5E`: `◈ sonnet` · `● ambient` (in `#5CD9A6`) · `$0.19`.

Body is `grid-template-columns: minmax(0,1fr) 280px`, `min-height:520px`. Left = transcript + prompt. Right = a **live region** that never scrolls with the log.

**Transcript.** IBM Plex Mono 12.5px, `line-height:1.85`.
- Boot line, `#4E4E5E`: `v 0.4.1 — ambient link up. 412 notes indexed. budget $2.00/day.`
- User input: `›` in `#6E6E80`, then the text in `#EDEDF2` with 8px left padding.
- V's answer: `✦` (state-colored) + 9px gap + text in `#D6D6E2`.
- Cost line: 23px left padding (aligning under the answer text), mono 11px `#4E4E5E`, values in `#8A8A9A`. Format: `◈ <model> · ⟡ <n> notes · <seconds>s · $<amount> · ◉ <confidence>`. Confidence is colored: ≥0.8 `#5CD9A6`, 0.5–0.8 `#E8A33D`, <0.5 `#E8A33D` with the answer's ✦ also amber.
- Persian content renders in a **bidi block**, never inline in an English sentence: `dir="rtl"`, IBM Plex Sans Arabic 14.5px, `line-height:1.9`, background `#12121A`, `border:1px solid #23232F`, `border-right:2px solid` the confidence color, `border-radius:8px`, padding 11/14. The block owns its direction; all surrounding chrome stays LTR.

**Prompt.** Top border `#1A1A22`, 13px padding above the row. `›` in `#7C5CFF`, transparent borderless input, mono 12.5px, `caret-color:#7C5CFF`, placeholder `ask, or / for commands`, `⏎` hint in `#3E3E4C` at the right.

**Slash suggestions.** Typing `/` opens a palette 9px above the prompt: 392px wide, `#111118`, `border:1px solid #2A2A38`, `border-radius:10px`, 6px padding, shadow `0 26px 50px -20px rgba(0,0,0,.95)`, enter animation `vRise 140ms`. Each row: mono 11.5px, 8px/10px padding, `border-radius:7px`, three columns — command in `#7C5CFF` (88px fixed), description in `#8A8A9A` (flex), spoken equivalent in `#3E3E4C` 10px. First match is highlighted `#1A1524`. Filtering is prefix-match on the command name. If nothing matches: `no command matches — press enter to send as plain language`.

| Command | Description | Spoken equivalent |
|---|---|---|
| `/why` | open the route, cost and memory behind the last answer | "why did you say that" |
| `/quiet` | no unprompted messages until tomorrow | "leave me alone" |
| `/close` | end the session, flush pending writes | "that's it for now" |
| `/cost` | today, this session, and the month's projection | "what have you cost me" |
| `/undo` | revert the last thing written to the vault | "no, take that back" |
| `/mem` | what he holds about a person or project | "what do you know about x" |
| `/local` | local models only until further notice | "stay offline" |

**Slash is a hint, not a language.** Every command is a shortcut for something V already understands in plain language, and the palette shows that phrasing next to it. Never add a command without a spoken equivalent.

**Side pane** (`#0C0C11`, left border `#1A1A22`, 18px padding, 20px gaps), four blocks:
1. `SESSION` — turns / spent / local (as `9 / 14`, green) / writes, as mono 11px label-value rows.
2. `BUDGET · TODAY` — 5px track `#1A1A22`, fill `linear-gradient(90deg,#5B3FD6,#9E86FF)`, caption `$0.19 of $2.00 · 5 proactive left`.
3. `PENDING WRITES` — cards with a 2px left border in the confidence color: amber `#E8A33D` + `◉ 0.41 · unconfirmed`, green `#5CD9A6` + `◉ 0.94 · committed`. Path in mono 10.5px `#D6D6E2`.
4. Footer note, mono 9.5px `#3E3E4C`.

### 04 — Inspection (route · memory · cost)

**Purpose.** One answer, opened up. Reached **only** from the answer it explains, via `/why`.

This is deliberately not a dashboard. No home, no history, no navigation, nothing to browse. Everything durable already lives in the vault. Read the intro copy in the prototype before extending this screen.

**Layout.** `repeat(auto-fit, minmax(340px,1fr))`, gap 28. Left: route timeline. Right: cost card above memory card.

**Route timeline.** Rows of `grid-template-columns: 15px 1fr`, gap 15. Each row: a 7px dot (green `#5CD9A6` local · seed `#7C5CFF` remote model · amber `#E8A33D` held) with a 1px `#22222D` connector below it (hidden on the last row); then step name in mono 12.5px `#EDEDF2`, elapsed time in mono 10px `#4E4E5E`, a tag chip (mono 9px, `letter-spacing:.1em`, 2px/7px, radius 4 — sonnet `#1D1636`/`#A793FF`, held `#2A1F0E`/`#E8A33D`, local `#0F2620`/`#5CD9A6`), and a 12px `#7E7E90` explanation. Rows enter with `vSlideIn 400ms`. Footer: `total | 3.34s · $0.0039 · 2 of 4 steps local`.

The explanations are the point of this view — they say *why*, not *what*: e.g. "Escalated because the low-confidence spans needed judgement, not lookup. Haiku was tried first and declined."

**Cost card.** Headline `$0.19` in mono 44px weight 300, `letter-spacing:-.02em`, with `today · of $2.00` beside it. Then four rows, each a label/amount pair over a 4px `#181820` track: sonnet-4.5 · 5 turns `$0.1420` (74%, `#7C5CFF`), haiku · 4 turns `$0.0380` (20%, `#5B3FD6`), tts · 11 sentences `$0.0100` (5%, `#A793FF`), local · 9 turns `$0.0000` (1%, `#5CD9A6`). Footer: "At this rate the month lands at **$5.70**. Nine of fourteen turns never left the machine."

**Memory card.** Rows with a `⟡` in `#7C5CFF`, the vault path in mono 11px `#D6D6E2` (ellipsised), a snippet in 11px `#6E6E80`, and a relevance score in mono 10px `#4E4E5E`. **Every row opens the real file in Obsidian.** Nothing is stored here that isn't already in the vault.

### 05 — States

Four states drawn in full, each a card with a colored mono 10px header (`letter-spacing:.14em`) over a mono 12px body:

- **LOW CONFIDENCE · THE TRANSCRIPT IS WRONG** (`#E8A33D`). Amber ✦. V says what he heard, shows the raw transcript in an amber-bordered block with `◉ 0.38 · held for 24h · not written`, then offers three affordances: `file it`, `drop it` (7px-radius `#14141B` buttons, `#2A2A38` border, hover border `#7C5CFF`) and a third, borderless, `or just tell me`. The buttons exist because the moment of low confidence is the only moment a button beats a sentence — but plain language must always work too.
- **CORRECTED · PLAIN LANGUAGE** (`#5CD9A6`). Input "no, that was a lyric". V: "Unfiled. I've marked lyric-like lines from that recording as low-value so I stop offering them." Cost line `◈ local · 0.08s · $0.0000 · 1 write reverted`. A correction is free and is itself a memory.
- **REFUSED · RULE 4 HELD** (`#FF6B6B`). Red ✦. "No. Anything that leaves the machine gets confirmed. The draft is ready — say the word and it goes." Footer: `refusal is not an error · no retry offered · $0.0000`. Refusals are never styled as errors and never offer a retry.
- **DEGRADED · NO NETWORK** (`#6E6E80`). Grey ✦, body text at `#8A8A9A`, plus a 45° hairline hatch overlay (`repeating-linear-gradient(45deg, transparent 0 9px, rgba(255,255,255,.012) 9px 18px)`, `pointer-events:none`). "I'm local only right now. I can search the vault and take notes; I can't reason about anything I haven't already read."

**Specified but not drawn** — implement from these descriptions:

| State | Spec |
|---|---|
| empty | No log, no greeting. Just the prompt and the budget line. V does not introduce himself. |
| thinking >4s | The route line appears early, step by step, so the wait is legible instead of blank. |
| interrupted | Speech stops mid-word. The partial answer stays printed, marked incomplete. No apology. |
| budget spent | Cost line turns amber. V answers but will not initiate. Local models continue free. |
| conflict | Two vault notes disagree. Both are shown, neither is resolved for you. |
| first run | Indexing progress as a single count. No onboarding, no tour, no sample questions. |
| stale | An answer older than the file it came from is marked `⟡ stale` rather than silently refreshed. |
| queued | Offline turns hold in a numbered list, replayed in order with their original timestamps. |

### 06 — Proactive

**Purpose.** One message, three bodies. V picks the **quietest channel that will still work**: if the ambient link hears him in the room, he says it; if the terminal is focused, he prints it; otherwise it is an OS notification.

Three columns, `repeat(auto-fit, minmax(280px,1fr))`.

- **Spoken · in the room.** Aperture at 76px in speaking configuration (dashed ring 5s, core `vBreathe 1.2s`), the sentence centred at 13.5px, and `spoken · 1 of 6 today · $0.0004`.
- **Printed · terminal focused.** Idle marker `… idle 18m …`, then a block with a 2px `#5B3FD6` left border entering on `vSlideIn 500ms`: seed ✦ + the sentence, and `unprompted · dismiss with /quiet`. Below it the prompt with a blinking caret (`vCaret 1.1s step-end infinite`).
- **Notified · away.** A macOS-style toast: `rgba(30,30,40,.86)`, `backdrop-filter: blur(20px)`, `border:1px solid #2E2E3C`, `border-radius:14px`, 14px padding, the 28px aperture as its icon, `V` / `now` in 11.5px `#9E9EAE`, the message in 12.5px `#EDEDF2`. Enters on `vSettle 600ms`. Rules beneath it: ignored once → retried tomorrow; ignored twice → channel spent; **never a badge, never a sound.**

**Budget.** Displayed as `1 / 6` in mono 34px weight 300 plus six 34×6px pips (spent = `#7C5CFF` with a `0 0 12px` glow, unspent = `#1E1E28`). The budget is spent by **interruption, not by tokens** — a message V decides not to send costs nothing and refills nothing. Silence is the default, not a saving.

Example copy for all three bodies: "Thursday's the last dentist slot this month." (spoken form expands to "The dentist thing you weren't sure about — Thursday's the last slot this month.")

---

## Interactions & behavior

### Navigation
Left rail, 232px, `#0B0B0F`, right border `#1B1B23`, sticky full height. Items: 8/10px padding, radius 8, mono index number at 10px/50% opacity, 12.5px label. Inactive `#7E7E90`; hover `background:#15151C; color:#EDEDF2`; active `background:#17172080; color:#EDEDF2; box-shadow: inset 2px 0 0 #7C5CFF`. Transition 150ms on background and color. Rail footer shows live status in mono 10px: ambient online, today's spend, note count.

This rail is a **prototype navigation device only** — in production these are separate surfaces, not tabs.

### Terminal input
Controlled input. On every keystroke: if the value starts with `/`, prefix-filter the command list and show the palette; otherwise hide it. Enter with a matching command runs it; Enter with anything else sends as plain language.

### Correction flow
A low-confidence write is *held*, not committed, for 24 hours. It can be resolved three ways, all equivalent: the offered buttons, a plain-language correction at any later point in the conversation, or editing the note in Obsidian directly. Resolving it is free and is recorded as a memory so the same class of mistake stops being offered.

---

## Motion spec

**Two curves only.** `cubic-bezier(.2,.8,.2,1)` for anything arriving; `ease-in-out` for anything breathing. Nothing bounces.

**Durations:** 120ms for state changes, 320ms for arrival, 900ms+ for anything that loops. Nothing in between.

### Keyframes

| Name | Definition |
|---|---|
| `vBreathe` | `0%,100% { scale(1); opacity:.85 }` · `50% { scale(1.045); opacity:1 }` |
| `vHalo` | `0%,100% { scale(1); opacity:.30 }` · `50% { scale(1.18); opacity:.55 }` |
| `vRing` | `0% { scale(.85); opacity:.7 }` → `100% { scale(1.6); opacity:0 }` |
| `vOrbit` | `to { rotate(360deg) }` |
| `vBar` | `0%,100% { scaleY(.18) }` · `50% { scaleY(1) }` |
| `vCaret` | `0%,49% { opacity:1 }` · `50%,100% { opacity:0 }` |
| `vRise` | `from { opacity:0; translateY(6px) }` → `to { opacity:1 }` |
| `vSettle` | `0% { opacity:0; translateY(10px) scale(.985) }` · `60% { opacity:1; translateY(-2px) scale(1.002) }` · `100% { none }` |
| `vSlideIn` | `from { opacity:0; translateX(14px) }` → `to { opacity:1 }` |
| `vFlicker` | `0%,100% { opacity:.9 }` · `42% { opacity:.5 }` · `47% { opacity:1 }` |
| `vGx` | `rotateX(74deg) rotateZ(0 → 360deg)` |
| `vGy` | `rotateY(72deg) rotateZ(0 → -360deg)` |
| `vGz` | `rotateX(28deg) rotateY(46deg) rotateZ(0 → 360deg)` |

### The six motions

1. **Breathe — idle.** Aperture core scales 1 → 1.045 over 4–5s, ease-in-out, infinite; halo opacity .30 → .55.
2. **Hear — listening.** 21 bars, `scaleY .18 → 1`, 900ms, 38ms stagger. Driven by real RMS, not faked.
3. **Think — gyroscope.** Three rings on a `perspective:600px`, `transform-style:preserve-3d` wrapper sized 80% of the stage (max 238px):
   - ring 1: `inset 0`, `1px solid rgba(124,92,255,.55)` with `border-top-color: rgba(167,147,255,.95)`, `vGx 2.6s linear infinite`
   - ring 2: `inset 22px`, `1px solid rgba(124,92,255,.4)` with `border-left-color: rgba(167,147,255,.85)`, `vGy 3.9s linear infinite`
   - ring 3: `inset 44px`, `1px dashed rgba(124,92,255,.32)`, `vGz 6.2s linear infinite`
   The differing single-colored border edge on rings 1 and 2 is what makes the 3D rotation legible — do not use uniform borders.
   **This is semantic:** one ring per active tool. A one-step answer spins one ring; a four-tool answer visibly spins more. Never spin three rings for a single lookup.
4. **Settle — the answer arrives.** `translateY(+10 → 0)`, 320ms, 120ms stagger between lines, cost line last at +200ms. **Never a typewriter effect.**
5. **Speak — outbound.** Rings scale .85 → 1.6 and fade to 0 over 2.2s. One ring per sentence; never a second loop on the same sentence.
6. **Muted — hardware.** No animation at all. That is the point. 0ms into the state, 800ms out of it.

### Reduced motion
Every loop stops. Breathing becomes a static opacity. **The aperture still changes color and size** — color is information, motion is not.

### Never
No typewriter text. No bounce. No parallax. No skeleton shimmer over real content. No progress bar that lies. Nothing in the terminal moves except the caret and the arrival of a line.

### The one rule
> If motion is the only thing telling you a state changed, the state is not designed. Color and text carry it; motion only makes it pleasant.

---

## State management

```
ambientState   : 'idle' | 'wake' | 'listening' | 'thinking' | 'speaking' | 'muted'
                 (muted is read from hardware, never set by software)
activeTools    : number          — drives how many gyroscope rings spin
transcript     : { text, confidence, lang }[]
pendingWrites  : { path, confidence, status: 'held' | 'committed', heldUntil }[]
budget         : { spentToday, dailyCap, proactiveUsed, proactiveCap }
sessionStats   : { turns, spent, localTurns, totalTurns, writes }
network        : 'online' | 'local'
lastAnswer     : { route: RouteStep[], cost: CostRow[], memory: MemoryHit[] }
                 — the only thing /why can open
input          : string          — terminal, drives the slash palette
```

Data needs: local VAD and wake-word detection (never remote); local STT with per-span confidence; vault search returning path + snippet + score; per-call model cost accounting; an OS notification channel with delivery/ignore feedback so the budget can be decremented on interruption rather than on send.

## Assets

No image, icon-font or SVG-library assets. The mark is three CSS circles; the icon set is six Unicode glyphs. Only external dependency is Google Fonts: IBM Plex Sans, IBM Plex Mono, IBM Plex Sans Arabic. Self-host these in production.

## Files

| File | Contents |
|---|---|
| `V v3.dc.html` | **Build from this.** Final design, Aperture mark, gyroscope thinking animation. |
| `V v2.dc.html` | Intermediate — the six-mark exploration grid with all rejected directions. Reference only. |
| `V v1.dc.html` | First pass, ✦-tile mark and a spinning-disc thinking state. Historical only. |
| `support.js` | Prototype runtime. Required to open the HTML; irrelevant to production. |

Open any file directly in a browser. Use the left rail to move between sections; on **Ambient** click the state list to drive the aperture, and on **Terminal** type `/` in the prompt to see the live command palette.
