# Pause, resume, release

The bridge writes to real Zigbee lights, which means it shares a radio with —
and can collide with — anything else that also controls those lights: a
script sweeping a room's lights off, a firmware update on a nearby device,
anything sending a burst of commands. Three services let another automation
coordinate with the bridge instead of fighting it.

| Service | Use it for | Effect on the running session |
|---|---|---|
| `hue_entertainment.pause` | a brief courtesy gap (radio airtime) | none — frames keep arriving, they're just not applied |
| `hue_entertainment.resume` | ending a pause early | none |
| `hue_entertainment.release` | an intent change (a lighting sweep) | asks the TV to stop; forces a clean teardown if it doesn't |

All three act on every loaded bridge (in practice, the one). They raise a
`ServiceValidationError` if the integration is not loaded, so a calling
automation sees an error rather than a silent no-op.

## Example: a house-off sweep that stays off

```yaml
alias: Bedtime — everything off
sequence:
  - action: hue_entertainment.release
    data:
      seconds: 3          # give the TV a moment to stop the stream on its own
  - action: light.turn_off
    target:
      area_id: living_room
    data:
      transition: 2
```

Without the `release`, the Ambilight session would relight the living room
on its next frame, and — when the TV eventually stops — the bridge would
"restore" the lights to the state they had *before* the stream started
(on, most likely).

For an automation that only needs the radio quiet for a moment (a scene
across many bulbs, an OTA update), use `pause` instead:

```yaml
  - action: hue_entertainment.pause
    data:
      seconds: 4
  - action: scene.turn_on
    target:
      entity_id: scene.kitchen_cooking
```

## `hue_entertainment.pause` — a courtesy gap

Suppresses the bridge's effect on its lights for `seconds` (default, or
`seconds: 0`, is a couple of seconds; capped at 30 s), then resumes
automatically. Nothing about what these lights should look like has changed
— the session, if any, is left completely alone underneath: frames or
classic-mode commands keep arriving and keep being counted, they're just not
applied. A DTLS handshake or classic command is never blocked by a pause;
only its *effect* on the lights is dropped — including a command a frame
queued a moment before the call: pausing (or releasing) discards anything
already queued but not yet sent, not just new arrivals. Without that, a
command queued an instant earlier would still reach the light during the
"paused" window.

## `hue_entertainment.resume` — end a pause early

Cancels an in-progress pause immediately, before its timer would have. No
effect if nothing is paused, or if a release is already in progress (release
has no early-cancel counterpart of its own — see below).

## `hue_entertainment.release` — an intent change

For when what these lights *should* be doing has actually changed — the
canonical case is a bedtime or arm-away sweep that wants these lights to
switch off and *stay* off. Release:

1. **Discards the pending restore target immediately.** Whatever the caller
   sets next is now correct — there is nothing to restore back to, so the
   session ending later won't relight the room.
2. **Drops frame/command effects immediately**, same as pause — including
   anything already queued but not yet sent, so a live stream can't undo the
   sweep a fraction of a second later.
3. **Flips the bridge's `stream.active` flag to `false`**, so a well-behaved
   TV notices on its next check and disconnects on its own — an ordinary,
   clean teardown.
4. **If the TV doesn't comply within `seconds`** (default/`0` → a couple of
   seconds, capped at 30 s), forces the same teardown anyway: the bridge
   stops feeding the *existing* dead-stream watchdog, which then fires on
   schedule and tears the session down through the normal path. The
   worst-case wait is bounded at `seconds` + the watchdog timeout (5 s),
   never indefinite.

A new session starting while a release is still pending (the TV reconnects)
*is* the release resolving — treated as the old session ending and a fresh
one beginning, with a fresh snapshot, not as "already active".

**Classic mode** (the TV drives the bulbs over plain REST, no stream) has no
session to tear down, so the grace period is the whole guarantee: commands
are dropped for `seconds`, then the next one drives the lights again.

## The contract between them

These are documented guarantees, not implementation happenstance — a caller
shouldn't need to know what else might be calling these services to reason
about the outcome:

- **`release` always wins over an in-flight `pause`.** An intent change
  supersedes a courtesy gap; letting the pause's own timer later re-enable
  things would silently undo the release.
- **`pause` is a no-op while a release is in progress.** Effects are already
  suppressed; a pause's later auto-expiry must not interfere with the
  release.
- **`resume` is a no-op while releasing**, and a no-op if nothing is paused.
  A release resolves via the TV stopping, or via its own grace-period
  timeout — never via a service call.
- **Calling `pause` again while already paused** resets the timer to a fresh
  `seconds` from the moment of the new call (last call wins).
- **Calling `release` again while already releasing** restarts its grace
  period the same way — safe for a caller that isn't sure an earlier call
  landed.

## Observability

Two entities under the bridge device:

- **`binary_sensor.hue_entertainment_bridge_ambilight_active`** answers one
  question only: is the bridge driving these lights *right now* — `on` for
  both a DTLS stream and classic mode, `off` while paused or releasing
  regardless of what's happening underneath. Use it in conditions such as
  "don't run the sunset scene while the TV owns the lights".
- **`sensor.hue_entertainment_bridge_status`** (diagnostic) says *why*: one
  of `idle` / `streaming` / `classic` / `paused` / `releasing`, with
  `paused_remaining_seconds`, `release_forcing` and `underlying_activity`
  attributes while a pause or release is in progress.

Entity IDs depend on your instance; look them up on the bridge device page.
