## Fruit Market player: prompt or scripted policy.
##
## Prompt seats deliver guidance to the game.
##
## `PLAYER_SCRIPTED=hauler` registers the seat as the market-making baseline
## and `PLAYER_SCRIPTED=homesteader` as the autarky foil; the server plays
## those deterministically, with no LLM at all.
##
## To field your own policy, reuse this image and set PLAYER_PROMPT:
##   coworld upload-policy <fruit-market-image> --name my-fruit-market \
##     --run /bin/fruit-market-player --secret-env PLAYER_PROMPT="<strategy>"

import
  std/[json, options, os, strutils, times],
  whisky

const
  RegisterResendSeconds = 10.0
    ## An unappliable registration is HELD and RE-SENT rather than dropped
    ## (paintball, 2026-08-25 — a dropped registration silently made a champion
    ## seat play scripted).
  RegisterResendIntervalMs = 2000
  DefaultPrompt = """
Earn craved-food points through funded exact-mirror trades while protecting
hunger and stamina. Read the visible book and choose a useful standing order
each round; do not assume hidden farmer types. Apple farmers can offer 3 apples
for 2 bananas; banana farmers can mirror with 2 bananas for 3 apples. Stock
your own fruit before marketing and adapt to nearby funded offers. Most
partners rendezvous north in rounds 1-2, east 3-4, south 5-6, west 7-8, then
repeat; join them when it helps. Prefer local harvesting and trade to costly
routine river treks. Use hunger and stamina to choose eating or rest. Your say
is spectator-only, so communicate through offers and position. Preserve useful
private observations in short notes. Output one native JSON object with
lowercase enum values and explicit job, eat, say and notes; include fruit,
stall and offer when relevant. Keep notes under 180 characters and say under
50. Omitted offer keeps the standing offer; null withdraws it.
"""

when isMainModule:
  let url = getEnv("COWORLD_PLAYER_WS_URL")
  if url.len == 0:
    quit("COWORLD_PLAYER_WS_URL is not set", 1)
  var prompt = getEnv("PLAYER_PROMPT")
  var scripted = getEnv("PLAYER_SCRIPTED").strip()
  if prompt.strip().len == 0 and scripted.len == 0:
    prompt = DefaultPrompt

  proc promptFrame(): string =
    $ %*{"type": "prompt", "prompt": prompt, "scripted": scripted}

  echo "fruit-market player: connecting to game"
  let socket = newWebSocket(url)
  socket.send(promptFrame())
  echo "fruit-market player: registered ",
    (if scripted.len > 0: "scripted " & scripted
     else: "prompt policy")

  var
    registeredAt = epochTime()
    lastResend = epochTime()
    sawWelcome = false

  while true:
    ## The whole receive loop is wrapped: whisky's `receiveMessage` RAISES on a
    ## close frame or a truncated read, and the game's quit(0) can outrun the
    ## flushed `final` frame. Exiting 0 on a dead socket is what makes the
    ## hosted player container's exit code deterministic (raid, 2026-08-23).
    var received: Option[Message]
    try:
      received = socket.receiveMessage(200)
    except CatchableError as error:
      echo "fruit-market player: socket closed (", error.msg, "), exiting"
      break
    if received.isNone:
      ## No frame this poll. Keep re-sending the registration for ~10 s so a
      ## first send that raced the server's slot admission still lands.
      let now = epochTime()
      if now - registeredAt < RegisterResendSeconds and
          (now - lastResend) * 1000.0 >= RegisterResendIntervalMs.float:
        lastResend = now
        try:
          socket.send(promptFrame())
        except CatchableError:
          echo "fruit-market player: socket closed while re-registering"
          break
      continue
    let message = received.get()
    if message.kind != TextMessage:
      continue
    try:
      let payload = parseJson(message.data)
      case payload{"type"}.getStr()
      of "welcome":
        if not sawWelcome:
          sawWelcome = true
          echo "fruit-market player: seated at slot ",
            payload{"slot"}.getInt(), " as ", payload{"name"}.getStr()
        ## Re-deliver the prompt after the welcome, in case the first send
        ## raced the server's slot registration.
        socket.send(promptFrame())
        registeredAt = epochTime()
      of "final":
        if payload.hasKey("scores") and payload["scores"].kind != JNull:
          echo "fruit-market player: final scores ", payload["scores"]
        else:
          echo "fruit-market player: episode ended without scores (",
            payload{"reason"}.getStr(), ": ",
            payload{"failure_cause"}.getStr(payload{"ending"}.getStr()), ")"
        break
      else:
        discard
    except CatchableError as error:
      echo "fruit-market player: ignoring bad frame: ", error.msg
  try:
    socket.close()
  except CatchableError:
    discard
  quit(0)
