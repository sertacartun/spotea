# StyleSeed — Design Lock
<!-- Selections persist here. This file cannot waive StyleSeed core invariants. -->
- App domain: content
- Surface: mobile-app
- Surface adapter: product-ui
- Page type: dashboard
- Output grammar: consumer-service
- Grammar path: built-in:engine/RULESETS.md
- Grammar fallback: consumer-service
- Reference confidence: n/a
- Brand recipe: native-mobile
- Palette recipe: nocturne-violet
- Key color: #FF4343
- Palette character: deep
- Palette mode: dark
- Palette harmony: auto
- Surface temperature: cool
- Aesthetic profile: none
- Skin: custom
- Primary action: #FF4343
- Font: system-ui
- Radius: soft
- Elevation: tonal
- Density: comfortable
- Motion: Spring restrained
- Imagery/data role: editorial-media
- Signature move: a per-track ambient wash sampled from the cover art and clamped in OKLCH, so the colour changes with the music while every measured contrast stays put
- Studio direction: cover-as-canvas (Studio run .styleseed/studio/spotea)
- Locked: 2026-08-27 (key color revised 2026-08-28)

<!-- Registry project. .styleseed/project.json plus .styleseed/artifacts/*.json are
     authoritative for the twelve surfaces; this file records the shared decisions
     behind them. Note that resolve-context.mjs's legacy --from-lock path drops the
     Brand recipe / Palette recipe lines (it stores them under different keys than
     it reads), so this file must not be resolved directly - use
     `--project-root . --all`, which reads the registry JSON and is correct. -->

<!-- Key color, 2026-08-28. The nocturne-violet recipe still supplies everything
     it was chosen for: the near-black deep/cool surface ramp, the text scale
     measured against it, the tonal elevation. What it no longer supplies is the
     key hue. Its violet shipped for one release and was rejected on sight; the
     brand's red is back in the accent role at #FF4343, which measures 6.02:1 on
     --bg-page. The signature move is unaffected either way - the ambient wash
     samples its hue from the cover on screen and has never read the accent. -->
