"""Source-level guards on the shipped JavaScript; there is no JS test runner in this project."""

import re
from pathlib import Path

CORE_JS = Path("app/static/js/core.js")
JS_DIR = Path("app/static/js")


def _function_body(source: str, name: str) -> str:
    """The text of `export [async] function <name>(...) { ... }`, brace-matched."""
    needle = f"export function {name}("
    if needle not in source:
        needle = f"export async function {name}("
    start = source.index(needle)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_escape_html_escapes_quotes() -> None:
    """escapeHtml must escape quotes: every caller interpolates it into a double-quoted attribute."""
    body = _function_body(CORE_JS.read_text(), "escapeHtml")

    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert entity in body, f"escapeHtml no longer escapes {char!r} to {entity}"

    # The unsafe implementation that already came back once.
    assert "innerHTML" not in body, "escapeHtml is back to the textContent/innerHTML form, which leaves quotes intact"


def test_escape_html_is_the_only_escaper_used_in_markup() -> None:
    """No module builds attribute markup with a second, hand-rolled escaper."""
    offenders: list[str] = []
    for path in sorted(JS_DIR.rglob("*.js")):
        source = path.read_text()
        # `="${...}"` interpolations, i.e. a value landing in attribute position.
        for match in re.finditer(r'="\$\{([^}]+)\}"', source):
            expression = match.group(1)
            if "escapeHtml" in expression:
                continue
            # Values that cannot carry a quote: `?? ""` fallbacks over ids/durations, and String(bool).
            if re.fullmatch(r"[\w.?\s]*(\?\?\s*\"\")?", expression):
                continue
            offenders.append(f"{path}: {match.group(0)}")

    assert not offenders, "attribute interpolation without escapeHtml:\n" + "\n".join(offenders)


def test_service_worker_ignores_cross_origin_requests() -> None:
    """Intercepting no-cors cross-origin <img> requests made remote artwork fail with ERR_FAILED."""
    source = (JS_DIR / "sw.js").read_text()

    # Exact expressions, since looser phrases also appear in sw.js's comments.
    check = "url.origin !== self.location.origin"
    write = "cache.put(request, copy)"

    assert check in source, (
        "sw.js no longer compares the request's origin, so it is intercepting "
        "remote artwork again"
    )
    assert write in source, "sw.js's caching call moved; this guard needs updating"
    # Compared inside the fetch listener: networkFirst() is declared above it.
    handler = source[source.index('self.addEventListener("fetch"') :]
    assert handler.index(check) < handler.index("networkFirst("), (
        "the origin check must short-circuit before the request is handed to "
        "the caching path"
    )


def test_the_service_worker_falls_back_to_cache_when_the_network_hangs() -> None:
    """The app's host can hang instead of rejecting (Tailscale CGNAT), so the fallback needs a timeout."""
    source = (JS_DIR / "sw.js").read_text()

    assert "NETWORK_TIMEOUT_MS" in source, (
        "sw.js has no network timeout — a hanging server (VPN off, captive "
        "portal, black-holed route) never reaches the cache fallback"
    )
    # Sliced by hand: a service worker is not a module, so there is no `export function`.
    body = source[source.index("function networkFirst(") :]
    body = body[: body.index("\n}\n")]
    assert "setTimeout(" in body, "networkFirst no longer races the network against a clock"
    assert "caches.match(request)" in body, "networkFirst never consults the cache"
    # One timeout for the whole launch: hung requests hold sockets (~6 per origin) and queue the shell.
    assert "unreachableUntil" in body, (
        "networkFirst no longer short-circuits once the server has been seen "
        "not answering — every module pays the timeout again, in series"
    )
    # The timeout must not abort: a slow network should still land and refresh the cache.
    for aborting in ("AbortController", "signal"):
        assert aborting not in body, (
            f"networkFirst aborts the in-flight request ({aborting}) — a slow "
            "network then never refreshes the cache"
        )


def test_service_worker_api_prefixes_have_no_trailing_slash() -> None:
    """A trailing slash would stop the prefix matching the bare route, caching it instead of excluding it."""
    source = (JS_DIR / "sw.js").read_text()

    match = re.search(r"const API_PREFIXES = \[(.*?)\];", source, re.DOTALL)
    assert match, "sw.js's API_PREFIXES list moved or was renamed; this guard needs updating"
    prefixes = re.findall(r'"([^"]+)"', match.group(1))
    assert prefixes, "found API_PREFIXES but no string entries in it"

    trailing_slash = [p for p in prefixes if p.endswith("/")]
    assert not trailing_slash, f"these API_PREFIXES entries have a trailing slash: {trailing_slash}"

    # The fetch handler must use the boundary-aware matcher, not a raw startsWith loop.
    assert "isApiPath(url.pathname)" in source, (
        "sw.js's fetch handler no longer calls isApiPath — bare API routes "
        "can get cached again"
    )


def test_report_playback_only_sends_the_unexpected_events() -> None:
    """Pinned as an exact set so a change to the allowlist is a deliberate edit here."""
    source = (JS_DIR / "player.js").read_text()

    match = re.search(r"const REPORTED_EVENTS = new Set\(\[(.*?)\]\);", source, re.S)
    assert match, "REPORTED_EVENTS allowlist not found in player.js"
    kept = {name.strip().strip('"') for name in match.group(1).split(",") if name.strip()}

    assert kept == {
        "play-rejected",
        "playback-stalled",
        "prepare-failed",
        "outgoing-ended",
        "track-ended",
        "retry-rejected",
        "visibility-changed",
        "media-session-action",
        "audio-session",
        "early-handoff",
        # Temporary diagnostics for the prefetch handoff; removed once that fix is verified.
        "handoff-cached",
        "handoff-device",
        "handoff-missed",
    }

    # One contiguous block: _function_body's brace matching trips on `detail = {}`.
    assert (
        "export function reportPlayback(event, detail = {}) {\n"
        "  if (!REPORTED_EVENTS.has(event)) return;"
    ) in source, (
        "REPORTED_EVENTS is defined but reportPlayback no longer checks "
        "against it — every event is being sent again"
    )


def test_the_volume_slider_is_gated_on_ios_as_well_as_on_the_write_taking() -> None:
    """iOS 18 reads back a written volume, so feature detection alone shows a dead slider."""
    source = (JS_DIR / "player.js").read_text()

    assert "function isIOSWebKit()" in source, (
        "the iOS gate is gone — a volume slider that does nothing is back on every iPhone"
    )
    assert "volumeIsSettable(activeAudio())" in source, "the feature-detection gate is gone"
    assert "&& !isIOSWebKit()" in source, (
        "the volume slider is no longer gated on both the write taking *and* not being "
        "iOS; feature detection alone can show a dead control on an iPhone"
    )
    # iPadOS 13+ claims to be a Mac, so the sniff has to look past the name.
    assert "maxTouchPoints" in source, (
        "isIOSWebKit no longer distinguishes an iPad from a Mac, and iPadOS reports "
        "itself as Macintosh"
    )


def test_wire_scrollers_does_not_leak_a_listener_or_observer_per_row() -> None:
    """wireScrollers() re-runs after every swap, so its observer and listener must be module singletons."""
    source = (JS_DIR / "home" / "scrollers.js").read_text()

    mouseup_registrations = source.count('addEventListener("mouseup"')
    assert mouseup_registrations == 1, (
        f"expected exactly one window mouseup listener, found {mouseup_registrations} "
        "— a per-row registration inside wireScrollers is back"
    )
    # The single registration must be at module scope, not merely moved.
    wire_scrollers_start = source.index("export function wireScrollers")
    assert source.index('addEventListener("mouseup"') < wire_scrollers_start, (
        "the mouseup listener is registered inside wireScrollers — it will "
        "run again, and leak again, on every fragment swap"
    )

    assert source.count("new ResizeObserver(") == 1, (
        "wireScrollers should create exactly one ResizeObserver kind of call "
        "site — if a per-swap observer is still made without being tracked "
        "for disconnection, the leak is back"
    )
    assert "observer.disconnect()" in source, (
        "no disconnect() call on the tracked observer — old rows' ResizeObservers "
        "are never torn down (a mention of .disconnect() in a comment doesn't count)"
    )


def test_initial_tab_is_never_restored_from_local_storage() -> None:
    """A fresh open with no hash starts on Home."""
    index = Path("app/templates/index.html").read_text()
    tabs = (JS_DIR / "home" / "tabs.js").read_text()

    assert "spotea-active-tab" not in index, (
        "index.html's pre-paint script reads the remembered tab again — a "
        "fresh open no longer starts on Home"
    )
    assert "spotea-active-tab" not in tabs, "home/tabs.js is writing the remembered tab again"
    assert "localStorage." not in tabs, "home/tabs.js is back to persisting the active tab"


def test_opening_explore_never_shows_a_loading_placeholder() -> None:
    """The shelves refresh in the background; entering the tab never swaps in a spinner."""
    source = (JS_DIR / "home" / "explore.js").read_text()
    setup = _function_body(source, "setupRecommendations")

    activation = setup[setup.index("onTabActivated") :]
    assert "loadRecommendations()" in activation and "placeholder" not in activation, (
        "switching to Explore passes a placeholder again, so the tab is "
        "something you wait on rather than something you enter"
    )
    assert "renderedPayload" in source, (
        "the unchanged-batch check is gone — every re-check re-renders every "
        "shelf, and every <img> in it, identically"
    )

def test_an_artist_name_is_only_a_link_when_there_is_an_artist_to_open() -> None:
    """A song result doesn't always carry a channel id; without one the name is text, not a dead button."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    body = source[source.index("function artistNameHtml(") :]
    body = body[: body.index("\n}\n")]

    assert "if (!name || !item.channel_id) return name;" in body, (
        "artistNameHtml no longer falls back to plain text for a result with "
        "no channel id — the name renders as a button that opens nothing"
    )


def test_the_artist_link_is_handled_before_the_card_it_sits_inside() -> None:
    """The whole .rec-card is a play target, so the artist check must run first."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    listener = source[source.index('body.addEventListener("click"') :]
    listener = listener[: listener.index("\n  });")]

    assert listener.index('closest(".artist-link")') < listener.index('closest(".rec-card")'), (
        "the .rec-card branch is checked before .artist-link — clicking an "
        "artist's name plays the song instead of opening their page"
    )


def test_a_channel_result_opens_the_artist_route() -> None:
    """The server decides whether an id is an artist or a channel (services/remote_detail.py)."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert 'openDetail("yt-channel"' not in source, (
        "a channel result opens yt-channel directly again — an artist's "
        "track list is never reached, only their uploads"
    )
    assert source.count('openDetail("yt-artist", channelCard.dataset.channelId') == 1
    assert source.count('openDetail("yt-artist", row.dataset.channelId') == 1


def test_the_scroller_module_has_no_import_cycle() -> None:
    """library.js imports detail.js, so the scroller importing library.js would be a cycle."""
    detail = (JS_DIR / "home" / "detail.js").read_text()
    scrollers = (JS_DIR / "home" / "scrollers.js").read_text()

    assert 'from "./scrollers.js"' in detail, "detail.js no longer wires the profile's shelves"
    assert 'from "./library.js"' not in detail, (
        "detail.js imports library.js, which imports detail.js — an import cycle"
    )
    assert not [line for line in scrollers.split("\n") if line.startswith("import ")], (
        "the scroller module took on a dependency — it is meant to be a leaf"
    )


def test_both_panel_swaps_run_the_same_wiring() -> None:
    source = (JS_DIR / "home" / "detail.js").read_text()

    # A cached remote fragment, a freshly fetched one, and the device's own Downloads panel.
    assert source.count("  afterPanelSwap();") == 3, (
        "a swap path skips afterPanelSwap — its shelves won't drag-scroll, "
        "or its shuffle button won't match the current preference"
    )


def test_a_release_card_opens_by_browse_id() -> None:
    """A single has no audioPlaylistId, so the browse id is the only identifier for both."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert 'openDetail("yt-release", card.dataset.releaseId)' in source
    # Rendered in the artist profile (#detail-panel) and Home's New releases shelf.
    assert source.count("openReleaseCard(") == 3


def test_a_single_is_resolved_before_any_history_is_pushed() -> None:
    """A one-track release plays instead of opening a panel, so it must not push a history entry."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    resolve = source.index("await resolveRelease(id)")
    push = source.index("history.pushState")
    assert resolve < push, "resolveRelease must run before openDetail pushes history"


def test_playing_a_standalone_track_sets_a_one_track_queue() -> None:
    """Otherwise noteCurrent clears the queue and the queue panel goes blank."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    play = source.index("export async function playRemoteVideo")
    set_queue = source.index('setQueue({ kind: "single" }, [data.content_id])', play)
    open_player = source.index("openPlayer(data.content_id)", play)
    # noteCurrent drops any queue the new track isn't in, so the queue has to exist first.
    assert set_queue < open_player


def test_following_someone_does_not_navigate_anywhere() -> None:
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert "event.detail.browseId" not in source


def test_the_follow_event_carries_what_the_server_decided() -> None:
    """Off the response: the server says whose page the channel URL turned out to be."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    assert "browseId: data.artist.browse_id || null" in source


def test_the_first_sync_counts_as_an_artist_still_filling_in() -> None:
    """"syncing" is the only phase, and Library's card polls on it."""
    initial_sync = Path("app/services/initial_sync.py").read_text()
    library = (JS_DIR / "home" / "library.js").read_text()

    assert 'ACTIVE_PHASES = frozenset({"syncing"})' in initial_sync
    assert "/artists/syncing" in library


def test_every_module_import_resolves() -> None:
    """Every named import must be exported by an existing file; one broken import blanks the page."""
    modules = {path.resolve(): path.read_text() for path in JS_DIR.rglob("*.js")}

    exported = {}
    for path, source in modules.items():
        names = set(re.findall(r"^export (?:async )?function (\w+)", source, re.M))
        names |= set(re.findall(r"^export (?:const|let|class) (\w+)", source, re.M))
        exported[path] = names

    broken = []
    for path, source in modules.items():
        for match in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*"([^"]+)"', source):
            target = (path.parent / match.group(2)).resolve()
            if target not in modules:
                broken.append(f"{path.name} imports a file that doesn't exist: {match.group(2)}")
                continue
            for name in (n.strip().split(" as ")[0] for n in match.group(1).split(",") if n.strip()):
                if name not in exported[target]:
                    broken.append(f"{path.name} imports {name}, which {target.name} does not export")

    assert not broken, "\n".join(broken)


def test_the_lyrics_panel_only_listens_to_the_audio_element() -> None:
    """The lyrics panel must not control playback; the player's iOS behaviour breaks easily from outside."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    for forbidden in (".play()", ".pause()", ".src =", ".load()", "new Audio"):
        assert forbidden not in source, f"lyrics.js touches the audio element: {forbidden}"
    assert 'onPlayerEvent("timeupdate"' in source


def test_lyrics_are_not_fetched_until_the_tab_is_opened() -> None:
    """A miss costs live YouTube requests, so load() only runs once the lyrics tab is selected."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    # Every call site of load() sits after a check that the lyrics tab is selected.
    for match in re.finditer(r"^\s*(?:else )?(?:if \([^)]*\) )?load\(", source, re.M):
        before = source[: match.start()]
        assert 'selected !== "lyrics"' in before or "isLyrics" in before, (
            "load() is reachable without the Lyrics tab being selected"
        )


def test_the_pinned_panel_breakpoint_matches_the_stylesheet() -> None:
    """If the breakpoints drift, the panel is invisible-but-loading or visible-but-empty."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert 'matchMedia("(min-width: 900px)")' in overlay
    assert "@media (min-width: 900px)" in css


def test_the_desktop_panel_does_not_decide_how_tall_the_card_is() -> None:
    """The card's height must come from the player column so the tab strip sits level with Collapse."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    desktop = css[css.index("@media (min-width: 900px) {") :]
    start = desktop.index(".queue-panel-inner {")
    inner = desktop[start : desktop.index("}", start)]

    assert "position: relative" in inner
    assert "overflow: hidden" in inner
    # A percentage max-height against an auto-height parent caps nothing.
    panel_start = desktop.index(".queue-panel {")
    assert "max-height" not in desktop[panel_start : desktop.index("}", panel_start)]
    # And the list itself has to be the thing that scrolls.
    assert '.queue-panel-inner > [role="tabpanel"] {' in desktop


def test_the_overlay_centres_the_card_safely() -> None:
    """`safe center`: a plain-centred item taller than its container has an unreachable top."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert "align-items: safe center" in css
    overlay = css[css.index(".player-overlay {") :]
    assert "align-items: center;" not in overlay[: overlay.index("}")]


def test_a_remote_track_row_says_it_is_clickable() -> None:
    """.track-link is a <button> in remote rows, whose default cursor is an arrow."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    start = css.index("button.track-link {")
    assert "cursor: pointer" in css[start : css.index("}", start)]


def test_the_desktop_panel_is_not_centred_against_the_card() -> None:
    """Stretching keeps the tab strip in place as the queue fills and empties."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    desktop = css[css.index("@media (min-width: 900px) {") :]
    panel = desktop[desktop.index(".queue-panel {") : desktop.index("}", desktop.index(".queue-panel {"))]
    assert "align-self: stretch" in panel
    assert "align-self: center" not in panel


def test_moods_is_the_first_shelf_in_explore() -> None:
    """Moods is the only shelf that needs nothing followed and nothing typed."""
    source = (JS_DIR / "home" / "explore.js").read_text()
    shelves = source[source.index("const shelves = ["): source.index("].join(\"\")")]

    calls = re.findall(r"(moodsShelfHtml|shelfHtml)\(", shelves)
    assert calls[0] == "moodsShelfHtml", f"Moods is not first — order is {calls}"


def test_the_moods_shelf_does_not_promise_genres() -> None:
    """Genre categories can't be parsed by ytmusicapi (music.MOOD_SECTION), so only moods are listed."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert "Moods &amp; genres" not in source
    assert '<h3 class="shelf-title">Moods</h3>' in source


def test_there_is_no_second_module_editing_interests() -> None:
    assert not (JS_DIR / "home" / "onboarding.js").exists()

    for path in JS_DIR.rglob("*.js"):
        if path.name == "settings.js":
            continue
        source = path.read_text()
        assert ".genre-chip" not in source, f"{path.name} still reaches for the chips"


def test_settings_reads_the_picker_rather_than_a_second_copy_of_it() -> None:
    """Selection is server-rendered into aria-pressed; no second copy as JSON."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert 'getAttribute("aria-pressed")' in source
    assert "dataset.interests" not in source
    for gone in ("interests-input", "interests-form", "interest-chip-remove", "renderInterests"):
        assert gone not in source, gone


def test_the_first_run_releases_the_overlay_when_it_is_done() -> None:
    """The overlay is also Settings' picker, so finishing the first run must remove data-required."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert 'removeAttribute("data-required")' in source

    core = (JS_DIR / "core.js").read_text()
    # Checked at click time: a wiring-time flag can't express "locked for part of a session".
    assert "dismissible" not in core, "a fixed flag is what this replaced"
    assert core.count("isRequired(overlay)") >= 2  # the Escape path and the click paths


def test_the_interests_modal_outranks_the_generic_one() -> None:
    """Both are single-class selectors, so .modal-interests must come after .modal to win."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert css.index("\n.modal {") < css.index("\n.modal-interests {")


def test_the_first_run_is_fullscreen_and_the_settings_one_is_not() -> None:
    """Fullscreen is scoped to [data-required]; the Settings picker is not fullscreen."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    fullscreen = css[css.index("#interests-overlay[data-required] .modal-interests {") :][:400]
    assert "height: 100%" in fullscreen
    assert "max-width: none" in fullscreen
    assert "modal-overlay-full" not in css, "the class this replaced applied to both modes"


def test_lyrics_ignores_its_own_smooth_scroll() -> None:
    """The automatic smooth scroll fires `scroll` events that must not arm the manual-scroll grace period."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    assert "autoScrolling = true" in source
    assert "!autoScrolling" in source, "the scroll listener has to check it"
    assert "scrollend" in source, "and hand the list back as soon as it settles"


def test_opening_the_lyrics_tab_jumps_to_the_line_being_sung() -> None:
    """The tab switch resets scroll, so opening mid-verse must jump to the current line."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    assert "syncActiveLine({ force: true })" in source


def test_the_keyboard_is_measured_without_the_scroll_offset() -> None:
    """Subtracting visualViewport.offsetTop shrinks the answer as the page scrolls with the keyboard open."""
    source = (JS_DIR / "viewport.js").read_text()

    assert "const covered = window.innerHeight - viewport.height;" in source
    # Scoped to the measuring function, not a file-wide ban.
    body = _function_body(source, "installKeyboardInset")
    # In the note explaining why, not in the arithmetic.
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("//"))
    assert "viewport.offsetTop" not in code


def test_a_focused_text_field_hides_the_bottom_furniture_on_its_own() -> None:
    """Focus is a second signal that a scroll can't talk out of a keyboard being up."""
    source = (JS_DIR / "viewport.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert '"focusin"' in source and '"focusout"' in source
    assert 'classList.toggle("is-typing"' in source
    assert "body.is-typing .tabs" in css
    assert "body.is-typing .mini-player" in css


def test_a_toggle_that_is_on_outranks_a_stuck_hover() -> None:
    """Touch browsers keep :hover after a tap, so the on-state must outrank every hover rule."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert ".btn-transport.btn-shuffle.is-on" in css
    assert ".btn-quiet-icon.btn-shuffle.is-on" in css
    assert "\n.btn-shuffle.is-on," not in css, "two classes is not enough — see the docstring"


def test_the_pinned_playlists_hero_badge_is_smaller_than_an_artists() -> None:
    """Scoped to .channel-hero-avatar so the library grid's 44px badge is left alone."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert ".channel-hero-avatar.channel-card-icon {" in css
    assert ".channel-card-avatar.channel-card-icon" not in css, (
        "the grid badge matches the artist avatars beside it on purpose"
    )


def test_the_players_artist_line_opens_the_artist() -> None:
    """Announced rather than called: importing home/detail.js here would be a cycle."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    detail = (JS_DIR / "home" / "detail.js").read_text()

    assert "OPEN_ARTIST" in overlay and "OPEN_ARTIST" in detail
    # Collapsed, not closed: closing stops the music.
    assert "collapsePlayer();" in overlay[overlay.index("artistPageId") :]


def test_a_song_search_result_plays_from_anywhere_on_the_row() -> None:
    """The artist link inside the row is still checked first so it isn't swallowed."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    handler = source[source.index('videoResults.addEventListener("click"') :][:900]
    assert handler.index(".artist-link") < handler.index(".video-search-result")
    assert "playRemoteVideo(row.dataset" in handler


def test_repeat_all_does_not_wrap_a_single_track_queue() -> None:
    """`1 % 1` is 0, so wrapping a one-track queue would make next/previous restart the track."""
    source = (JS_DIR / "home" / "queue.js").read_text()

    assert "if (state.order.length < 2) return null;" in source


def test_the_queue_is_dragged_closed_by_its_own_top_edge() -> None:
    """The tab strip is the sheet's top edge, directly above what moves."""
    js = (JS_DIR / "home" / "overlay.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert 'panel.querySelector(".panel-tabs")' in js
    assert ".player-overlay.is-queue-open .panel-tabs {\n  touch-action: none;" in css
    assert ".player-overlay.is-queue-open .player-art,\n.player-overlay.is-queue-open .player-meta {\n  touch-action: none;" not in css


def test_a_drag_does_not_also_switch_the_tab_it_started_on() -> None:
    """A time window, not a "swallow the next click" flag: a touch drag doesn't always produce a click."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "closedByDragAt" in source
    assert "CLICK_AFTER_DRAG_MS" in source
    assert "dataset.dragged" not in source, "the flag this replaced ate real taps"


def test_explores_search_field_and_tabs_are_one_sticky_block() -> None:
    """Two stacked sticky elements would need a hardcoded offset; it pins below the sticky app header."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()
    viewport = (JS_DIR / "viewport.js").read_text()

    head = css[css.index(".explore-search-head {") :][:260]
    assert "position: sticky" in head
    assert "top: var(--app-header-height);" in css
    assert "--app-header-height" in viewport
    assert "ResizeObserver" in viewport


def test_a_music_video_row_is_swapped_for_the_song_before_it_plays() -> None:
    """Video entries have a 16:9 still, no lyrics and a different recording."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "songVersionOf" in source
    assert "is_music_video" in source

    # The prefetch's download request swaps server-side (start_download), so no client ordering is needed.
    prefetch = source[source.index("async function cacheUpcoming(contentId) {") :]
    prefetch = prefetch[: prefetch.index("\n}\n")]
    assert "songVersionOf" not in prefetch

    # The cold open draws from the row before any download, so it swaps for itself.
    assert "await songVersionOf(data);" in source


def test_start_playback_does_not_reload_the_track_already_loaded() -> None:
    """Assigning `src` resets playback to 0:00 even for the same URL, freezing a locked iOS queue."""
    source = (JS_DIR / "player.js").read_text()

    # How "already loaded" is decided may change; that it gates the assignment may not.
    assert "const alreadyLoaded = loadedContentId === contentId && !audio.ended;" in source, (
        "startPlayback no longer checks whether this track is already loaded — "
        "a repeat call restarts the playing track from 0:00"
    )
    assert "if (alreadyLoaded && !audio.paused) return;" in source, (
        "startPlayback no longer returns early for a track that is already "
        "playing — a repeat call is meant to be a no-op, not a restart"
    )
    # The assignment has to be *inside* the guard, not merely preceded by it.
    assert "if (!alreadyLoaded) {" in source and "audio.src = prefetched || streamUrl;" in source, (
        "audio.src is assigned unconditionally again"
    )
    guard = source.index("if (!alreadyLoaded) {")
    assert guard < source.index("audio.src = prefetched || streamUrl;"), (
        "the src assignment is no longer inside the already-loaded guard"
    )


def test_the_stall_watchdog_does_not_trust_an_unpaused_element() -> None:
    """An unpaused element pinned at 0 is the worse silent state; being paused is not part of the check."""
    source = (JS_DIR / "player.js").read_text()

    assert "if (!audio.paused || audio.currentTime > 0) return;" not in source, (
        "the stall watchdog treats an unpaused element as healthy again — the "
        "state that stopped a real session is invisible to it"
    )
    assert "if (audio.currentTime > 0) return;" in source, "the stall watchdog no longer checks the position"


def test_the_prefetch_guard_is_set_only_once_the_prefetch_goes_out() -> None:
    """prefetchedFor is once-per-track, so it must not be set before the queue was consulted."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    # Contiguous text: peekNextId is also called earlier in the file.
    assert "prefetchedFor = playing;\n  const upcoming = peekNextId();" not in source, (
        "prefetchedFor is marked before peekNextId() is consulted — a track "
        "whose queue was momentarily empty never prefetches again"
    )
    assert "const upcoming = peekNextId();\n  if (upcoming == null) return;" in source, (
        "the prefetch no longer bails out before marking the guard"
    )


def test_the_stall_watchdog_does_not_interfere_with_a_loading_element() -> None:
    """Calling audio.load() aborts a backgrounded iOS page's pending play(), killing playback."""
    source = (JS_DIR / "player.js").read_text()

    # Comment lines stripped first: the comment above the fix names the call
    # it is warning against, and matching that would be self-defeating.
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    assert "audio.load()" not in code, (
        "the stall watchdog calls audio.load() again — it aborts the in-flight "
        "play() and throws away the buffering, which killed playback outright"
    )
    assert "if (!audio.paused) return;" in source, (
        "the watchdog no longer leaves an unpaused element alone"
    )


def test_a_visibility_change_is_recorded_while_a_track_is_loaded() -> None:
    """A waking lock screen makes no request of its own, so the beacon is the only trace."""
    source = (JS_DIR / "player.js").read_text()

    body = _function_body(source, "installVisibilityBreadcrumb")
    assert 'document.addEventListener("visibilitychange"' in body
    assert "if (!contentId) return;" in body, (
        "the visibility breadcrumb fires outside playback — every app switch "
        "becomes a beacon"
    )
    assert 'reportPlayback("visibility-changed"' in body

    index = (JS_DIR / "pages" / "index.js").read_text()
    assert "installVisibilityBreadcrumb();" in index, "the breadcrumb is defined but never installed"


def test_a_prefetched_track_is_played_from_memory_rather_than_refetched() -> None:
    """The next track's bytes are fetched ahead and handed over as an object URL, not refetched."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    player = (JS_DIR / "player.js").read_text()

    assert "URL.createObjectURL(blob)" in overlay, (
        "the prefetch no longer pulls the next track's audio into the page — "
        "downloading it server-side alone does not make a handoff free"
    )
    assert "offerPrefetchedAudio(contentId, prefetchedAudio)" in overlay, (
        "openPlayer fetches the bytes but never hands them to the player, so "
        "the element goes to the network anyway"
    )
    assert "audio.src = prefetched || streamUrl;" in player, (
        "startPlayback ignores the prefetched bytes and always assigns the "
        "stream URL"
    )


def test_the_loaded_track_is_not_identified_by_comparing_urls() -> None:
    """A prefetched track has a blob: URL, so currentSrc can't identify the loaded track."""
    player = (JS_DIR / "player.js").read_text()
    overlay = (JS_DIR / "home" / "overlay.js").read_text()

    for name, source in (("player.js", player), ("home/overlay.js", overlay)):
        assert "currentSrc.endsWith" not in source, (
            f"{name} identifies the loaded track by comparing URLs again — a "
            "prefetched track's blob: URL never matches, so it reads as a "
            "different track than the one playing"
        )

    assert "const alreadyLoaded = loadedContentId === contentId && !audio.ended;" in player, (
        "startPlayback no longer checks the tracked id"
    )
    assert "if (finished && loadedTrackId() !== finished) {" in overlay, (
        "the ended handler no longer checks what the element is actually loaded with"
    )


def test_every_prefetched_object_url_is_released() -> None:
    """Object URLs pin whole audio Blobs until revoked, on every path, not just the happy one."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    player = (JS_DIR / "player.js").read_text()

    minted = overlay.count("URL.createObjectURL(") + player.count("URL.createObjectURL(")
    assert minted == 1, (
        f"object URLs are minted in {minted} places — one owner was the point, "
        "since every one of them has to be revoked somewhere"
    )

    # Dropped without playing: the queue moved on, or a different track was opened.
    assert "if (upcomingTrack?.id !== id) {\n    URL.revokeObjectURL(objectUrl);" in overlay, (
        "a prefetch superseded while its bytes were in flight leaks its Blob"
    )
    # Braced form, distinct from the close path's one-liner, so each revoke is checked separately.
    assert re.search(
        r"if \(upcomingTrack\?\.objectUrl\) \{\n(?:\s*//[^\n]*\n)*\s*URL\.revokeObjectURL\(upcomingTrack\.objectUrl\);",
        overlay,
    ), (
        "bytes prefetched for a track the user then skipped past leak their Blob"
    )
    assert "if (upcomingTrack?.objectUrl) URL.revokeObjectURL(upcomingTrack.objectUrl);" in overlay, (
        "closing the player leaks whatever it had prefetched"
    )

    # Replaced on the happy path, and dropped outright when the player closes.
    assert "if (loadedObjectUrl) URL.revokeObjectURL(loadedObjectUrl);" in player, (
        "the outgoing track's Blob is never released, so a queue holds every "
        "track it has played"
    )
    assert "export function releaseAudio() {" in player, (
        "there is no way left for closePlayer to release what is loaded"
    )


def test_the_prefetch_goes_out_with_the_track_not_part_way_through_it() -> None:
    """The prefetch starts with the track, not after a delay, so an early Next finds it ready."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "PREFETCH_AFTER_SECONDS" not in source, (
        "the prefetch is gated on elapsed playback again — Next is slow for "
        "exactly as long as the gate lasts"
    )
    body = source[source.index("let prefetchedFor = null;") :]
    body = body[: body.index("cacheUpcoming(upcoming);")]
    assert "currentTime" not in body, (
        "the prefetch handler is looking at the playhead again"
    )


def test_the_upcoming_track_is_published_before_its_audio_is_fetched() -> None:
    """Published off the download's answer (post-swap row), before the audio fetch and its long poll."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    body = source[source.index("async function cacheUpcoming(contentId) {") :]
    body = body[: body.index("\n}\n")]

    published = body.index("upcomingTrack = { id, data, objectUrl: null };")
    assert published < body.index("cacheUpcomingAudio(id)"), (
        "the upcoming track is published only after its audio is fetched — a "
        "Next press before then finds nothing and repeats the whole chain"
    )
    assert published < body.index("while (Date.now() - startedAt"), (
        "the upcoming track is published after the poll loop, which can run "
        "for the whole budget before it ever gets there"
    )
    # Writing after the handoff took the entry would resurrect a cache for the playing track.
    assert "if (upcomingTrack?.id !== id) return;" in body[published:], (
        "the post-publish writes don't re-check that the entry is still ours"
    )


def test_a_press_that_has_to_ask_the_server_says_so_immediately() -> None:
    """Gated on a track already being open, and undone if the fetch fails."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    body = source[source.index("export async function openPlayer(") :]
    body = body[: body.index("\n  document.querySelector(\".player-title\")")]

    shown = body.index("if (wasOpen) showPreparing();")
    fetched = body.index("const res = await api(`/content/${contentId}`);")
    assert shown < fetched, "the spinner goes up only after the round trip it exists to cover"
    assert "if (wasOpen) clearPreparing();" in body, (
        "a failed load leaves the spinner up and the transport disabled"
    )


def test_the_handoff_beacon_says_whether_the_bytes_were_in_memory() -> None:
    """`buffered` says whether the handoff ran from a blob or the network."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    # rindex: the repeat-"one" branch has its own earlier track-ended call.
    ended = source[source.rindex('reportPlayback("track-ended"') :]
    ended = ended[: ended.index(");")]
    assert "buffered: Boolean(upcomingTrack?.objectUrl)" in ended, (
        "track-ended no longer reports whether the handoff had the audio in memory"
    )


def test_lock_screen_taps_leave_a_trace() -> None:
    """A beacon from inside the handler separates a refused play() from a frozen page."""
    player = (JS_DIR / "player.js").read_text()
    overlay = (JS_DIR / "home" / "overlay.js").read_text()

    for action in ("play", "pause"):
        handler = player[player.index(f'setActionHandler("{action}"') :]
        handler = handler[: handler.index("});")]
        assert f'reportMediaSessionAction("{action}")' in handler, (
            f"the media-session {action} handler no longer reports being invoked"
        )

    for action in ("nexttrack", "previoustrack"):
        handler = overlay[overlay.index(f'setActionHandler(\n      "{action}"') :]
        handler = handler[: handler.index(": null")]
        assert f'reportMediaSessionAction("{action}")' in handler, (
            f"the media-session {action} handler no longer reports being invoked"
        )


def test_every_beacon_stamps_the_audio_session_state() -> None:
    """Audio Session state and visibility ride along on every beacon at no extra cost."""
    source = (JS_DIR / "player.js").read_text()

    body = source[source.index("export function reportPlayback(") :]
    body = body[: body.index("\n}")]
    assert 'audioSession: navigator.audioSession?.state ?? "unsupported"' in body, (
        "reportPlayback no longer stamps the audio-session state on beacons"
    )
    assert 'reportPlayback("audio-session"' in source, (
        "OS interruptions (statechange) are no longer reported at all"
    )


def test_position_state_is_published_only_while_audio_renders() -> None:
    """A playbackRate of zero throws, so position state is only set while rendering with a finite duration."""
    source = (JS_DIR / "player.js").read_text()

    assert "playbackRate: rendering" not in source, (
        "setPositionState is being fed a conditional playbackRate again — "
        "zero is a TypeError per spec, so the 0 branch silently never reports"
    )
    body = source[source.index("const syncPositionState = ") :]
    body = body[: body.index("};")]
    assert "if (!rendering) return;" in body, (
        "position state is being reported for an element that isn't rendering "
        "— the lock screen's clock will tick over silence"
    )
    assert "Number.isFinite(audio.duration)" in body, (
        "an Infinity duration reaches setPositionState, which throws on it"
    )
    assert "Math.min(audio.currentTime, audio.duration)" in body, (
        "mid-seek position > duration is a TypeError, not a correction"
    )


def test_the_page_declares_itself_a_media_player_to_the_os() -> None:
    """WebKit's Audio Session API; Safari-only and experimental, so feature-detected."""
    source = (JS_DIR / "player.js").read_text()

    assert '"audioSession" in navigator' in source
    assert 'navigator.audioSession.type = "playback"' in source, (
        "the audio session type is no longer declared"
    )


def test_unchanged_metadata_is_republished_until_a_held_publish_lands() -> None:
    """iOS may drop a publish in the silent gap, so the cache only short-circuits after a held-session one."""
    source = (JS_DIR / "player.js").read_text()

    assert "if (key === publishedNowPlaying && publishedWhileHeld) return;" in source, (
        "the metadata cache no longer distinguishes a held-session publish "
        "from one made in the silent gap — either every `playing` republishes "
        "(card rebuilds) or none does (stuck Dynamic Island)"
    )
    assert "applyNowPlayingMetadata({ held: true })" in source, (
        "the `playing` handler no longer marks its publish as held"
    )


def test_a_finished_queue_leaves_the_os_now_playing_surface() -> None:
    """A Now Playing card left up after the queue ends is dead weight on the Dynamic Island."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()

    assert "if (next == null) clearNowPlayingMetadata();" in overlay, (
        "a queue running out no longer clears the OS Now Playing surface"
    )
    # closePlayer must clear through the same helper, or the publish cache goes stale.
    body = overlay[overlay.index("export function closePlayer(") :]
    body = body[: body.index("\n}")]
    assert "clearNowPlayingMetadata();" in body, (
        "closePlayer clears mediaSession by hand (or not at all) instead of "
        "through clearNowPlayingMetadata, desyncing the publish cache"
    )


def test_the_background_handoff_happens_before_the_cliff() -> None:
    """A backgrounded page freezes at `ended`, so the swap happens while the track still renders."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    handler = source[source.index("let earlyHandoffFor = null;") :]
    handler = handler[: handler.index("});")]

    assert 'if (document.visibilityState === "visible") return;' in handler, (
        "the early handoff runs in the foreground too, cutting the tail off "
        "every track for a freeze that only threatens hidden pages"
    )
    assert "if (upcomingTrack?.id !== String(next) || !upcomingTrack.objectUrl) return;" in handler, (
        "the early handoff no longer requires the bytes in memory — it would "
        "trade the end of this track for a network stall it can't afford either"
    )
    assert "if (audio.paused) return;" in handler, (
        "a paused element parked near the end of a track would auto-advance"
    )
    assert "audio.currentTime = 0;" in handler, (
        "repeat-one no longer loops by rewinding before the end — it falls "
        "back to rewinding after `ended`, on the far side of the cliff"
    )
    assert "EARLY_HANDOFF_SECONDS" in handler

    # `ended` remains the fallback for foreground playback, a missing blob, or a paused element.
    ended = source[source.rindex('reportPlayback("track-ended"') :]
    assert "playFromQueue(next);" in ended, (
        "the ended handler no longer advances — the early handoff is now the "
        "only path forward and every case it declines just stops"
    )


def test_the_device_lookup_is_skipped_when_a_prefetch_already_holds_the_bytes() -> None:
    """On a prefetch hit, the handoff must not await the offline lookup (iOS may defer it)."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    guard = source.index("if (!prefetchedAudio) {")
    lookup = source.index("const savedUrl = await openTrackUrl(contentId);")
    offer = source.index("offerPrefetchedAudio(contentId, prefetchedAudio);")
    assert guard < lookup < offer, (
        "the device lookup must run only on a prefetch miss, and before "
        "ownership of the bytes passes to player.js"
    )


def test_a_track_played_off_the_device_ignores_what_the_server_says_about_it() -> None:
    """A track played off the device ignores the server's status and is_unavailable."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert 'root.dataset.status = playingFromDevice ? "ready" : data.status;' in source, (
        "a device-played track still defers to the server's status, so a "
        "cleared server library makes saved tracks re-download to play"
    )
    assert "playingFromDevice && data.is_unavailable" in source, (
        "a device-played track is still skipped when YouTube has since "
        "pulled the video it came from"
    )


def test_the_offline_cover_is_fetched_through_the_same_origin_proxy() -> None:
    """A cross-origin image fetch is opaque and unreadable, so covers go through the proxy."""
    source = (JS_DIR / "offline.js").read_text()

    assert "ytimg.com" not in source and "ggpht.com" not in source, (
        "offline.js fetches a cover straight from YouTube's CDN — the "
        "response is opaque and the stored Blob would be unreadable"
    )


def test_offline_metadata_and_audio_live_in_separate_stores() -> None:
    """IndexedDB materialises whole records, so listing saved songs must not load their Blobs."""
    source = (JS_DIR / "offline.js").read_text()

    assert source.count("createObjectStore(") == 3, (
        "expected three object stores — metadata and kept lists separate from the audio Blobs"
    )
    listing = source[source.index("async function listSaved()") :]
    listing = listing[: listing.index("\n}")]
    assert "BLOB_STORE" not in listing, (
        "listSaved reads the blob store, so every listing loads every saved "
        "song's audio into memory"
    )


def test_elements_js_hides_are_not_pinned_open_by_a_display_rule() -> None:
    """An explicit `display` beats [hidden], so each JS-hidden element needs its own [hidden] rule."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert ".offline-banner[hidden]" in css, (
        "no [hidden] rule for .offline-banner, which is display: flex — it "
        "would show permanently, for everyone, connection or not"
    )
    assert ".btn-quiet[hidden]" in css, (
        "no [hidden] rule for .btn-quiet, which is display: inline-block — "
        "any quiet button the JS hides would stay on screen"
    )
    assert ".prompt-label[hidden]" in css, (
        "no [hidden] rule for .prompt-label, which is display: block"
    )


def test_the_installed_app_can_actually_pick_up_a_new_worker() -> None:
    """An installed PWA rarely navigates, so it must check for a new worker itself."""
    body = _function_body((JS_DIR / "resume.js").read_text(), "registerServiceWorker")

    assert ".update()" in body, (
        "nothing asks the browser to re-check /sw.js, so an installed PWA can "
        "keep an old worker (and its cache) indefinitely"
    )
    assert "visibilitychange" in body, (
        "the update check only runs on load — for an installed app, coming "
        "back to the foreground is the only regular event there is"
    )
    assert "controllerchange" in body, (
        "a new worker takes over without the page it is now driving ever "
        "being re-rendered from it"
    )
    # A first-ever install also fires controllerchange; reloading then would reload every first visit.
    assert "hadController" in body, (
        "the controllerchange reload is unguarded, so a first install reloads "
        "the page it just claimed"
    )


def test_every_js_module_is_in_the_service_worker_precache() -> None:
    """A missing module blanks the offline app; sw.js itself is excluded on purpose."""
    source = (JS_DIR / "sw.js").read_text()
    listed = set(re.findall(r'"(/static/[^"]+)"', source))

    on_disk = {
        f"/static/js/{path.relative_to(JS_DIR).as_posix()}"
        for path in JS_DIR.rglob("*.js")
        if path.name != "sw.js"
    }
    missing = sorted(on_disk - listed)
    assert not missing, f"JS modules not in sw.js's PRECACHE_URLS: {missing}"

    stale = sorted(url for url in listed if url.endswith(".js") and url not in on_disk)
    assert not stale, f"PRECACHE_URLS lists JS that no longer exists: {stale}"

    assert "/static/css/style.css" in listed, "the stylesheet is not precached"


def test_the_precache_never_stores_a_redirect_or_an_error() -> None:
    """"/" answers 200 with the login page after session expiry, so response.ok is not enough."""
    source = (JS_DIR / "sw.js").read_text()

    install = source[source.index('addEventListener("install"') :]
    install = install[: install.index("self.skipWaiting()")]
    assert "response.redirected" in install, (
        "the precache stores redirected responses, so an expired session pins "
        "the login page as the offline shell"
    )
    assert "!response.ok" in install, "the precache stores error responses"
    assert "allSettled" in install, (
        "cache.addAll rejects the whole batch on one failed request — a flaky "
        "connection at install time then leaves no offline mode at all"
    )


def test_an_offline_open_does_not_overwrite_the_metadata_it_just_recovered() -> None:
    """On the offline path, the metadata must not be overwritten by a null response or a live lookup."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    body = source[source.index("const res = await api(`/content/${contentId}`);") :]
    body = body[: body.index('document.querySelector(".player-title")')]
    assert "if (!data) {\n      data = res.data;" in body, (
        "res.data is assigned unconditionally, clobbering the offline "
        "metadata and then running songVersionOf against no network"
    )


def test_only_an_unreachable_server_falls_back_to_the_device() -> None:
    """A 404/409 is a real answer; only status 0 (never arrived) falls back to the device."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "res.status === 0 && playingFromDevice" in source, (
        "the offline fallback triggers on any failed response, so a track the "
        "server has deleted still opens from a stale device copy"
    )


def test_the_offline_banner_does_not_run_on_navigator_online_alone() -> None:
    """navigator.onLine reads true in a SW-served offline reload, so api() drives the banner."""
    source = CORE_JS.read_text()

    watch = _function_body(source, "watchConnection")
    assert "requestsFailing" in watch, (
        "watchConnection reads navigator.onLine alone, which is true on a "
        "cached page that is genuinely offline"
    )

    # Sliced to the next export: _function_body would find api()'s destructured parameter.
    api_body = source[source.index("export async function api(") :]
    api_body = api_body[: api_body.index("\nexport ")]
    assert "noteConnection(false)" in api_body, (
        "api() does not report a request that never arrived, so nothing "
        "tells the banner the connection is gone"
    )
    assert "noteConnection(true)" in api_body, (
        "api() does not report a request that came back, so the banner never "
        "comes down again"
    )


def test_an_unreachable_server_raises_the_banner_even_with_a_working_connection() -> None:
    """An unreachable host can hang rather than fail, so the probe needs its own timeout and runs on start."""
    source = CORE_JS.read_text()
    # Sliced by hand: probeConnection is module-private.
    probe = source[source.index("async function probeConnection(") :]
    probe = probe[: probe.index("\n}\n")]

    assert "PROBE_TIMEOUT_MS" in probe, (
        "probeConnection waits on /health with no clock — against a server "
        "that hangs rather than refuses, it concludes nothing"
    )
    assert "noteConnection(false)" in probe, (
        "probeConnection can only report a connection coming back, never one "
        "that was never there"
    )

    watch = _function_body(source, "watchConnection")
    assert "probeConnection()" in watch, (
        "nothing probes at startup, so an unreachable-but-silent server is "
        "only ever noticed if something else already raised the banner"
    )


def test_a_reachable_server_is_the_only_thing_that_lowers_the_banner() -> None:
    """`online` is untrustworthy, so it may only ask for the banner to be re-derived."""
    source = CORE_JS.read_text()
    watch = _function_body(source, "watchConnection")

    online_handler = watch[watch.index('addEventListener("online"') :]
    online_handler = online_handler[: online_handler.index("\n")]
    assert "noteConnection(true)" not in online_handler, (
        "the online event clears the offline state directly rather than "
        "letting a successful request prove it"
    )
    assert "syncConnectionBanner()" in online_handler, (
        "the online event does nothing at all — the banner will not even be "
        "re-derived when a connection comes back"
    )


def test_the_banner_is_re_derived_on_every_connection_report() -> None:
    """An early return left a banner raised by navigator.onLine alone stuck up."""
    body = _function_body(CORE_JS.read_text(), "noteConnection")
    # Comments explain the bug this guards, so they mention it by name.
    body = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )

    assert "return" not in body, (
        "noteConnection can return without re-rendering the banner, which is "
        "how a banner raised by navigator.onLine alone gets stuck up"
    )
    assert "syncConnectionBanner()" in body


def test_the_connection_probe_is_never_answered_from_the_cache() -> None:
    """A cached /health can only say "online", pinning the banner down while offline."""
    core = CORE_JS.read_text()
    worker = (JS_DIR / "sw.js").read_text()

    assert '"/health"' in core, "core.js no longer probes /health"
    prefixes = worker[worker.index("const API_PREFIXES") : worker.index("function isApiPath")]
    assert '"/health"' in prefixes, (
        "/health is not in the service worker's API_PREFIXES, so the "
        "connection probe can be answered from the cache"
    )


def test_a_hand_made_playlist_is_queued_by_id_not_by_kind() -> None:
    """A hand-made list has its own id-keyed route; the pinned route would 404."""
    source = (JS_DIR / "home" / "queue.js").read_text()

    # queueUrl is module-private, so it is sliced rather than read through _function_body.
    body = source[source.index("function queueUrl(") :]
    body = body[: body.index("\n}") + 2]
    assert "/content/queue/user-playlist/${source.id}" in body, (
        "queueUrl has no branch for a hand-made playlist, so Play all asks "
        "the pinned-playlist route for a kind that isn't one"
    )


def test_a_hand_made_playlist_carries_its_id_in_the_detail_url() -> None:
    """Without hasId, a user playlist would ask for a pinned playlist called "user-playlist"."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert 'kind === "user-playlist"' in source, (
        "hasId does not admit user-playlist, so its detail fetch drops the id"
    )
    core = CORE_JS.read_text()
    kinds = core[core.index("const ID_DETAIL_KINDS = [") :]
    kinds = kinds[: kinds.index("]")]
    assert "user-playlist" in kinds, (
        "classifyHash cannot parse #user-playlist/12, so a deep link or a "
        "reload lands on the unknown-hash branch"
    )


def test_the_playlist_module_does_not_import_the_panel_it_talks_to() -> None:
    """home/detail.js imports playlists.js, so this direction would be a cycle."""
    source = (JS_DIR / "home" / "playlists.js").read_text()

    assert "./detail.js" not in source, (
        "playlists.js imports detail.js, which already imports it — the "
        "events exist to keep this one-way"
    )
    assert 'export const PLAYLIST_CHANGED' in source
    assert 'export const PLAYLIST_DELETED' in source


def test_a_library_card_with_nothing_to_open_is_not_a_navigation() -> None:
    """The "New playlist" tile has .channel-card but no data-detail-kind."""
    source = (JS_DIR / "home" / "library.js").read_text()

    assert "card?.dataset.detailKind" in source, (
        "the Library grid opens a detail panel for any .channel-card, "
        "including ones with no kind to open"
    )


def test_a_superseded_prefetch_stops_pulling_the_track_down() -> None:
    """A superseded prefetch must be aborted, not left downloading a second copy of the track."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "let upcomingAbort = null;" in source
    assert "await fetch(`/content/${id}/stream`, { signal: controller.signal })" in source, (
        "the prefetch's stream fetch is no longer given an abort signal, so "
        "nothing can call it off once it has been superseded"
    )

    # Both places that supersede a prefetch: the handoff, and the queue naming a different successor.
    handoff = source[source.index("export async function openPlayer") : source.index("async function cacheUpcoming(")]
    assert "abortUpcomingFetch();" in handoff, (
        "openPlayer drops upcomingTrack without stopping the transfer behind "
        "it — the bytes keep coming for a Blob that will be revoked"
    )
    queued = source[source.index("async function cacheUpcoming(") : source.index("async function cacheUpcomingAudio(")]
    assert "abortUpcomingFetch();" in queued, (
        "a new prefetch no longer calls off the previous one, so two whole "
        "tracks can be in flight at once"
    )

    # Clear only its own controller: by the time the abort rejects, the next prefetch may have published one.
    audio = source[source.index("async function cacheUpcomingAudio(") :]
    assert "if (upcomingAbort === controller) upcomingAbort = null;" in audio, (
        "cacheUpcomingAudio clears upcomingAbort without checking it is still "
        "its own — a superseded call now disarms the live prefetch"
    )


def test_the_device_sync_gives_way_to_a_track_that_is_still_buffering() -> None:
    """The offline sync must not start or keep a save while the player is still buffering."""
    source = (JS_DIR / "home" / "device.js").read_text()

    check = source[
        source.index("function playbackNeedsTheConnection() {") : source.index("function keyFor(source)")
    ]
    assert "audio.paused" in check and "HAVE_FUTURE_DATA" in check, (
        "the gate no longer asks whether the element is starving — anything "
        "coarser either never runs the sync or never gets out of its way"
    )

    body = source[source.index("async function syncList(") : source.index("// One pass at a time")]
    gate = body.index("if (playbackNeedsTheConnection()) {")
    save = body.index("await saveTrack(")
    assert gate < save, "the sync starts a save before asking whether playback needs the connection"
    assert "signal: controller.signal" in body, (
        "saveTrack is no longer given an abort signal, so a whole track keeps "
        "coming down after playback has starved"
    )
    assert 'if (err?.name === "AbortError")' in body, (
        "an aborted save is being reported to the user as a failure — it is "
        "this module doing what it was told"
    )

    setup = source[source.index("export async function setupDeviceStorage() {") :]
    assert 'onPlayerEvent("waiting", () => saveAbort?.abort());' in setup, (
        "nothing calls the in-flight save off any more; `waiting` is the "
        "element saying it has run out, which is the exact signal"
    )


def test_the_prefetch_follows_its_download_on_the_shared_poll_ladder() -> None:
    """The prefetch uses player.js's elapsed-time poll ladder rather than a flat interval."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    player = (JS_DIR / "player.js").read_text()

    assert "export function nextPollDelay" in player, (
        "nextPollDelay is no longer exported, so the prefetch cannot share the "
        "ladder and will drift back to a grid of its own"
    )
    body = overlay[overlay.index("async function cacheUpcoming(") : overlay.index("async function cacheUpcomingAudio(")]
    assert "nextPollDelay(Date.now() - startedAt)" in body, (
        "the prefetch is back on a fixed poll interval — the exact arrangement "
        "player.js's own comment says it measured and abandoned"
    )
    # Elapsed-driven, so a slow response doesn't shift the schedule.
    assert "UPCOMING_POLL_BUDGET_MS" in body and "attempt < UPCOMING_POLL_LIMIT" not in body, (
        "the poll is bounded by a step count again, so a variable delay changes "
        "how long it watches for rather than how often it asks"
    )


def test_the_prefetch_asks_for_the_download_and_nothing_else() -> None:
    """The download request swaps server-side and returns the row, replacing three serial requests."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    body = overlay[overlay.index("async function cacheUpcoming(") : overlay.index("async function cacheUpcomingAudio(")]

    assert "songVersionOf" not in body, "the prefetch is making its own swap request again"

    # The row fetch only survives as the fallback for a 409 without a row, after the download.
    posted = body.index('await api(`/content/${id}/download`, { method: "POST" })')
    assert posted < body.index("api(`/content/${id}`)"), (
        "the prefetch is fetching the row ahead of the download again — the "
        "download's own answer already carries it"
    )
    assert "if (download.data?.content) {" in body, (
        "the prefetch no longer takes the row off the download's answer, so "
        "the fallback fetch is the only path left and nothing was saved"
    )

    # The published row must be the post-swap one, never the music video's.
    assert "data = { ...download.data.content };" in body, (
        "upcomingTrack is being built from something other than the download's "
        "own answer, which is the only post-swap row this path ever sees"
    )
    published = body.index("upcomingTrack = { id, data, objectUrl: null };")
    assert body.index("data = { ...download.data.content };") < published
    assert posted < published, "the row is published before the swap that produced it has landed"

    # The cold open renders from the row before any download, so it keeps its separate call.
    assert "await songVersionOf(data);" in overlay, (
        "the cold open lost its swap, so a first play shows the music video's "
        "title and cover under the song it actually plays"
    )


def test_the_next_track_is_sent_for_when_this_one_opens() -> None:
    """A stalled element emits no timeupdate, so the open itself sends for the next track."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    opened = source.index("export async function openPlayer(")
    setup = source.index("export function setupPlayerOverlay(")
    body = source[opened:setup]
    assert "prefetchUpcoming();" in body, (
        "opening a track no longer sends for the next one, so a track that "
        "stalls holds up its successor's preparation too"
    )
    # Before the current track's own preparation, which may take seconds.
    assert body.index("prefetchUpcoming();") < body.index("prepareAudio("), (
        "the next track is sent for only after this one's own download has "
        "been arranged, which is the wait it exists to run alongside"
    )

    registrations = source[setup:]
    assert "document.addEventListener(QUEUE_CHANGED, prefetchUpcoming);" in registrations, (
        '"Play all" opens its first track before it has a queue, so without '
        "this the first pair of tracks prefetches nothing"
    )
    assert 'onPlayerEvent("timeupdate", prefetchUpcoming);' in registrations
