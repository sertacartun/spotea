"""Guards on the shipped JavaScript that Python tests can still enforce.

There is no JS test runner in this project (no package.json, no node_modules)
and adding one — plus a browser — for a handful of assertions costs more than
it returns. So these are source-level guards, not behavioural tests: they
check that a specific, previously-shipped mistake cannot come back, and they
say so rather than pretending to execute anything.
"""

import re
from pathlib import Path

CORE_JS = Path("app/static/js/core.js")
JS_DIR = Path("app/static/js")


def _function_body(source: str, name: str) -> str:
    """The text of `export function <name>(...) { ... }`, brace-matched."""
    start = source.index(f"export function {name}(")
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
    """escapeHtml has to be safe in attribute position, not just in text.

    It was written as `div.textContent = str; return div.innerHTML`, which
    escapes `&`, `<` and `>` and nothing else — serializing a text node has no
    reason to touch quotes. Every caller interpolates the result into a
    double-quoted attribute (data-title, aria-label, title, src), so a YouTube
    title containing `"` closed the attribute and everything after it parsed
    as further attributes on the same element. Confirmed executable: an
    injected `onerror` on the card's <img> fired on render, with no
    interaction. Ordinary titles carry quotes constantly, so this also
    corrupted plain markup.
    """
    body = _function_body(CORE_JS.read_text(), "escapeHtml")

    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert entity in body, f"escapeHtml no longer escapes {char!r} to {entity}"

    # The specific implementation that was unsafe. textContent/innerHTML is a
    # reasonable-looking way to write this and is exactly what came back.
    assert "innerHTML" not in body, "escapeHtml is back to the textContent/innerHTML form, which leaves quotes intact"


def test_escape_html_is_the_only_escaper_used_in_markup() -> None:
    """No module builds attribute markup with a second, hand-rolled escaper.

    The fix above is only worth anything if there is one escaper to fix. A
    module that grows its own (or interpolates raw) puts the same hole back
    somewhere this test cannot see.
    """
    offenders: list[str] = []
    for path in sorted(JS_DIR.rglob("*.js")):
        source = path.read_text()
        # `="${...}"` interpolations inside a template literal, i.e. a value
        # landing in attribute position.
        for match in re.finditer(r'="\$\{([^}]+)\}"', source):
            expression = match.group(1)
            if "escapeHtml" in expression:
                continue
            # Numeric/enum-ish values that cannot carry a quote: `?? ""`
            # fallbacks over ids and durations, and String(bool) conversions.
            if re.fullmatch(r"[\w.?\s]*(\?\?\s*\"\")?", expression):
                continue
            offenders.append(f"{path}: {match.group(0)}")

    assert not offenders, "attribute interpolation without escapeHtml:\n" + "\n".join(offenders)


def test_service_worker_ignores_cross_origin_requests() -> None:
    """The worker must not intercept anything it doesn't serve itself.

    It used to handle every GET, including the no-cors `<img>` requests for
    remote artwork on i.ytimg.com. Passing an opaque response through its
    fetch/clone/cache.put path made those fail outright: measured against the
    live app, 23 of Explore's thumbnails failed with ERR_FAILED while the
    worker was registered and none did with it blocked. So uncached Explore
    artwork was broken in the installed PWA, and fine on the very first load
    before the worker activated — which is what made it look intermittent.
    """
    source = (JS_DIR / "sw.js").read_text()

    # Matched on the exact expressions rather than loose substrings — both
    # phrases also appear in the prose around them, and an earlier version of
    # this test happily found them in a comment.
    check = "url.origin !== self.location.origin"
    write = "cache.put(event.request"

    assert check in source, (
        "sw.js no longer compares the request's origin, so it is intercepting "
        "remote artwork again"
    )
    assert write in source, "sw.js's caching call moved; this guard needs updating"
    # The early return has to come before the caching path, or the check is
    # decoration.
    assert source.index(check) < source.index(write), (
        "the origin check must short-circuit before the cache write"
    )


def test_service_worker_api_prefixes_have_no_trailing_slash() -> None:
    """A prefix with a trailing slash (e.g. "/settings/") never matches the
    *bare* route ("/settings" has none of its own) — that exact bug shipped
    once already and meant GET /settings, GET /profiles and GET
    /recommendations were all getting cached instead of excluded, so a
    profile switch or creation could serve a stale, different profile's
    settings straight from the service worker cache. isApiPath's own
    path === prefix || path.startsWith(`${prefix}/`) check only works if the
    prefixes themselves stay bare."""
    source = (JS_DIR / "sw.js").read_text()

    match = re.search(r"const API_PREFIXES = \[(.*?)\];", source, re.DOTALL)
    assert match, "sw.js's API_PREFIXES list moved or was renamed; this guard needs updating"
    prefixes = re.findall(r'"([^"]+)"', match.group(1))
    assert prefixes, "found API_PREFIXES but no string entries in it"

    trailing_slash = [p for p in prefixes if p.endswith("/")]
    assert not trailing_slash, f"these API_PREFIXES entries have a trailing slash: {trailing_slash}"

    # The fetch handler has to actually call the boundary-aware matcher, not
    # a raw path.startsWith(prefix) loop over the (now-bare) prefixes —
    # otherwise a bare "/settings" would still slip past every prefix listed
    # here for the opposite reason (no prefix is a strict startsWith match of
    # an equal-length string).
    assert "isApiPath(url.pathname)" in source, (
        "sw.js's fetch handler no longer calls isApiPath — bare API routes "
        "can get cached again"
    )


def test_report_playback_only_sends_the_unexpected_events() -> None:
    """4 beacons were measured per track played under the old blanket policy
    — "now-playing", "play-requested" and a successful "playing" for starting
    a track, "track-ended" for finishing it — and three of those four say
    nothing, since every track produces them whether or not anything went
    wrong.

    "track-ended" was cut with them and has since been put back, because it
    is not like the other three: it carries `next` and `prepared` alongside
    the page's visibility, and that is precisely what separates "the queue was
    empty" from "the page stopped running". Without it a track that simply
    stopped dead leaves no trace at all on the server — one real failure took
    several rounds of guesswork for exactly that reason. One beacon per track
    is the price.

    Pinned as an exact set rather than "at least these" so a future change to
    the allowlist is a deliberate edit here, not a silent one in player.js."""
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
    }

    # The allowlist alone proves nothing if reportPlayback doesn't actually
    # enforce it — this is the guard clause that turns "defined" into "used".
    # Checked as one contiguous block (not via _function_body's brace
    # matching) because reportPlayback's own `detail = {}` default parameter
    # contains a brace pair that helper isn't parameter-list-aware enough to
    # skip past.
    assert (
        "export function reportPlayback(event, detail = {}) {\n"
        "  if (!REPORTED_EVENTS.has(event)) return;"
    ) in source, (
        "REPORTED_EVENTS is defined but reportPlayback no longer checks "
        "against it — every event is being sent again"
    )


def test_the_volume_slider_is_gated_on_ios_as_well_as_on_the_write_taking() -> None:
    """iOS routes playback volume to the hardware buttons, so the slider does
    nothing there and is hidden. That used to be decided by feature detection
    alone — write a volume, read it back — on the reasoning that the
    restriction is per-browser rather than per-OS.

    That is measurably wrong on a modern iPhone, confirmed from the device
    (iOS 18.7, Safari 26.6): writing 0.5 and reading it back returns 0.5, so
    the detection reports "settable" over a slider that does nothing. Apple's
    documentation still claims reading always returns 1; it has stopped being
    true. Nothing readable separates the two cases any more, hence the second,
    user-agent gate — and hence this test, because "just feature-detect it" is
    exactly the tidy-looking change that would put the dead control back on
    every iPhone."""
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
    """wireScrollers() runs again after every fragment swap (Home/Library
    rows get replaced wholesale), and it used to create a brand new
    ResizeObserver *and* a brand new `window` "mouseup" listener for every
    row, every single time — neither was ever torn down. Measured live: 5 of
    each at boot, 105 of each after 20 refreshes. The fix is structural
    (module-scope singletons, not per-row), so this checks the structure
    rather than actually leaking memory in a browser this suite can't run."""
    source = (JS_DIR / "home" / "scrollers.js").read_text()

    mouseup_registrations = source.count('addEventListener("mouseup"')
    assert mouseup_registrations == 1, (
        f"expected exactly one window mouseup listener, found {mouseup_registrations} "
        "— a per-row registration inside wireScrollers is back"
    )
    # The one registration that exists must be at module scope (outside
    # wireScrollers' body), or "exactly one" would just mean it moved rather
    # than stopped repeating.
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


def test_downloads_modal_actions_refresh_its_own_list() -> None:
    """Clearing all downloads or removing one both run from *inside* the open
    Downloads modal — since refreshFragments() alone no longer touches that
    modal's list (see the sweep test above), those two actions have to opt
    back in explicitly, or a user's own action wouldn't appear to do
    anything until they closed and reopened the modal."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert source.count("{ alsoDownloads: true }") == 2, (
        "expected exactly two confirmedAction calls (clear-storage, "
        "remove-download) to opt into refreshing the open modal's own list"
    )


def test_refresh_fragments_default_sweep_does_not_include_the_downloads_body() -> None:
    """/partials/downloads was 86.5KB — the Downloads modal's full item list —
    behind a modal that's closed the vast majority of the time, and
    refreshFragments() (called after every save/favorite/play) used to
    refetch it every single time regardless. See fragments.js's
    refreshDownloadsBody for where that list is fetched instead."""
    source = (JS_DIR / "fragments.js").read_text()
    fragments_block = source[source.index("const FRAGMENTS") : source.index("];") + 2]

    assert "downloads-body" not in fragments_block, (
        "FRAGMENTS' default sweep includes downloads-body again — the "
        "expensive modal list is back to being refetched on every action"
    )
    assert "refreshDownloadsBody" in source, (
        "the on-demand downloads-body refresh (called on #open-downloads and "
        "from settings.js's in-modal actions) is gone"
    )


def test_initial_tab_is_never_restored_from_local_storage() -> None:
    """Opening the app fresh starts on Home.

    The pre-paint script used to fall back to a localStorage copy of the last
    tab whenever the URL carried no hash — which is every PWA launch, every
    bookmark, and every reload done by profiles.js. Creating a profile from
    Settings → Manage profiles therefore handed the brand-new profile the
    Settings tab with the onboarding wizard sitting on top of it. The hash is
    still written on every tab switch (home/tabs.js), so a reload or a deep
    link keeps its tab; a fresh open with no hash is meant to mean Home.
    """
    index = Path("app/templates/index.html").read_text()
    tabs = (JS_DIR / "home" / "tabs.js").read_text()

    assert "spotea-active-tab" not in index, (
        "index.html's pre-paint script reads the remembered tab again — a "
        "fresh open no longer starts on Home"
    )
    assert "spotea-active-tab" not in tabs, "home/tabs.js is writing the remembered tab again"
    # The word itself still appears in the comment explaining why it's gone.
    assert "localStorage." not in tabs, "home/tabs.js is back to persisting the active tab"


def test_opening_explore_never_shows_a_loading_placeholder() -> None:
    """The shelves are fetched in the background at boot and re-checked
    quietly on every later visit. Entering the tab used to swap a spinner in
    over them — worst right after onboarding, where the interest list had
    just changed and the re-check was a full rebuild (several live YouTube
    searches) with the user watching it. An unchanged batch isn't re-rendered
    at all, since replacing every card with an identical copy flashes every
    thumbnail for nothing."""
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
    """A song result carries the artist's channel id most of the time but not
    always — the fallback yt-dlp search (routers/explore.py's
    search_video_feeds) doesn't reliably report one on a flat entry. Rendering
    the name as a button anyway gives a control that opens nothing, which is
    worse than plain text."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    body = source[source.index("function artistNameHtml(") :]
    body = body[: body.index("\n}\n")]

    assert "if (!name || !item.channel_id) return name;" in body, (
        "artistNameHtml no longer falls back to plain text for a result with "
        "no channel id — the name renders as a button that opens nothing"
    )


def test_the_artist_link_is_handled_before_the_card_it_sits_inside() -> None:
    """The name sits inside a .rec-card, and that whole card is a play
    target. Whichever branch runs first wins, so putting the artist check
    after the card's would make clicking the artist start the song."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    listener = source[source.index('body.addEventListener("click"') :]
    listener = listener[: listener.index("\n  });")]

    assert listener.index('closest(".artist-link")') < listener.index('closest(".rec-card")'), (
        "the .rec-card branch is checked before .artist-link — clicking an "
        "artist's name plays the song instead of opening their page"
    )


def test_a_channel_result_opens_the_artist_route() -> None:
    """The fix for an artist whose YouTube channel is mostly vlogs: the
    search result and the shelf card both go to yt-artist, and the server
    decides whether that id is an artist or falls back to the channel's
    uploads (see services/remote_detail.py). Sending them to yt-channel
    instead skips that decision and shows the vlogs again."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert 'openDetail("yt-channel"' not in source, (
        "a channel result opens yt-channel directly again — an artist's "
        "track list is never reached, only their uploads"
    )
    assert source.count('openDetail("yt-artist", channelCard.dataset.channelId') == 1
    assert source.count('openDetail("yt-artist", row.dataset.channelId') == 1


def test_the_scroller_module_has_no_import_cycle() -> None:
    """Drag-to-scroll moved out of home/library.js so home/detail.js could
    wire the artist profile's shelves after a panel swap. Importing it back
    from library.js would recreate the cycle the move exists to avoid —
    library.js already imports openDetail from detail.js."""
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
    """A cached fragment and a freshly fetched one are the same markup, so
    anything one needs wired the other does too. They used to diverge."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert source.count("  afterPanelSwap();") == 2, (
        "a swap path skips afterPanelSwap — its shelves won't drag-scroll, "
        "or its shuffle button won't match the current preference"
    )


def test_a_release_card_opens_by_browse_id() -> None:
    """An album carries an audioPlaylistId and a single doesn't, so the
    browse id is the only identifier that works for both — see
    music.ArtistRelease."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert 'openDetail("yt-release", card.dataset.releaseId)' in source
    # Reached from both places a release card is rendered: the artist profile
    # (inside #detail-panel) and Home's "New releases" shelf.
    assert source.count("openReleaseCard(") == 3


def test_a_single_is_resolved_before_any_history_is_pushed() -> None:
    """A one-track release plays instead of opening a panel (see
    routers/partials.py's remote_release_fragment), so it must not leave a
    history entry pointing at a panel that was never shown. The only way to
    guarantee that is to ask what the release is before pushing."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    resolve = source.index("await resolveRelease(id)")
    push = source.index("history.pushState")
    assert resolve < push, "resolveRelease must run before openDetail pushes history"


def test_playing_a_standalone_track_sets_a_one_track_queue() -> None:
    """Nothing playRemoteVideo opens came out of a list, so there is no rest
    of it to queue. Left to itself, queue.js's noteCurrent would clear the
    queue instead and the queue panel would go blank — which on desktop is a
    permanently open panel showing nothing while a track plays."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    play = source.index("export async function playRemoteVideo")
    set_queue = source.index('setQueue({ kind: "single" }, [data.content_id])', play)
    open_player = source.index("openPlayer(data.content_id)", play)
    # Order matters: noteCurrent runs off openPlayer and drops any queue the
    # new track isn't in, so the queue has to exist first.
    assert set_queue < open_player


def test_following_someone_does_not_navigate_anywhere() -> None:
    """Follow follows, and that is all it does.

    It used to jump straight to the artist's profile, which made a search
    result's Follow button and the result row itself do the same thing — and
    the button you press when you already know who you're adding is exactly
    the one that must not take you anywhere. The row still opens the profile.
    """
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert "event.detail.browseId" not in source


def test_the_follow_event_carries_what_the_server_decided() -> None:
    """Off the response, not off the request: the caller sends a channel URL
    and the server says whose page that turned out to be."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    assert "browseId: data.artist.browse_id || null" in source


def test_the_first_sync_counts_as_an_artist_still_filling_in() -> None:
    """"syncing" is the only phase there is: the server puts an artist in it
    before fetching anything, and Library's card polls on exactly that."""
    initial_sync = Path("app/services/initial_sync.py").read_text()
    library = (JS_DIR / "home" / "library.js").read_text()

    assert 'ACTIVE_PHASES = frozenset({"syncing"})' in initial_sync
    assert "/artists/syncing" in library


def test_every_module_import_resolves() -> None:
    """The regression this exists for: a rename left home/remote.js exporting
    neither playRemoteVideo nor playRemoteList while home/explore.js and
    home/detail.js still imported both. One unresolved import fails the whole
    module graph, so *nothing* on the page was wired — no tabs, no menu, no
    play button — and every server-side test still passed.

    There is no JS runner here to catch that by executing it, so this parses
    the import/export graph instead: every named import has to be exported by
    the file it names, and that file has to exist.
    """
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
    """Following along is a timeupdate consumer and nothing more. The
    player's iOS behaviour is load-bearing and easy to break from outside —
    an unrelated module calling pause() is exactly the bug that cost days
    (see player.js's notes on background playback)."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    for forbidden in (".play()", ".pause()", ".src =", ".load()", "new Audio"):
        assert forbidden not in source, f"lyrics.js touches the audio element: {forbidden}"
    assert 'onPlayerEvent("timeupdate"' in source


def test_lyrics_are_not_fetched_until_the_tab_is_opened() -> None:
    """A miss is two live YouTube requests and most tracks have none, so
    fetching on play would spend the request budget on answers nobody asked
    for. The only paths that may call load() are selecting the tab and, once
    selected, the track changing underneath it."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    # Every call site of load() sits after a check that the lyrics tab is the
    # selected one.
    for match in re.finditer(r"^\s*(?:else )?(?:if \([^)]*\) )?load\(", source, re.M):
        before = source[: match.start()]
        assert 'selected !== "lyrics"' in before or "isLyrics" in before, (
            "load() is reachable without the Lyrics tab being selected"
        )


def test_the_pinned_panel_breakpoint_matches_the_stylesheet() -> None:
    """The panel counts as open on a wide screen so the existing load and
    refresh paths keep working (see overlay.js's setQueueOpen). If the two
    breakpoints drift, the panel is either invisible-but-loading or
    visible-but-empty."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert 'matchMedia("(min-width: 900px)")' in overlay
    assert "@media (min-width: 900px)" in css


def test_the_desktop_panel_does_not_decide_how_tall_the_card_is() -> None:
    """The tab strip has to sit level with the player's Collapse link, and it
    only does while the card's height comes from the player column alone. So
    the panel's list is taken out of flow and the panel's own in-flow content
    is just the tabs.

    The rule this replaced was `max-height: min(680px, 100%)`, which quietly
    stopped capping anything once the card no longer had a definite height —
    a percentage max-height against an auto-height parent computes to none.
    Measured before the fix: a 40-track queue stretched the card to 1211px
    and pushed Collapse 272px below the tabs."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    desktop = css[css.index("@media (min-width: 900px) {") :]
    start = desktop.index(".queue-panel-inner {")
    inner = desktop[start : desktop.index("}", start)]

    assert "position: relative" in inner
    assert "overflow: hidden" in inner
    # The cap that cannot work here must not come back.
    panel_start = desktop.index(".queue-panel {")
    assert "max-height" not in desktop[panel_start : desktop.index("}", panel_start)]
    # And the list itself has to be the thing that scrolls.
    assert '.queue-panel-inner > [role="tabpanel"] {' in desktop


def test_the_overlay_centres_the_card_safely() -> None:
    """`safe center`, never plain `center`: a centred flex item taller than
    its scroll container overflows in both directions and its top becomes
    unreachable at any scroll position — which is why the base rule says
    stretch. `safe` falls back to start exactly then."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert "align-items: safe center" in css
    overlay = css[css.index(".player-overlay {") :]
    assert "align-items: center;" not in overlay[: overlay.index("}")]


def test_a_remote_track_row_says_it_is_clickable() -> None:
    """.track-link is an <a href> in _content_row.html but a <button> in
    _remote_track_row.html, and a button's default cursor is an arrow. Every
    row on a chart, album, mood or artist-release page is the remote one, so
    without this they were the only tracks in the app that gave no sign of
    being clickable, beside local rows that looked identical and did.

    Asserted on button.track-link specifically: that block exists to strip
    button chrome so the row reads as a link, and the cursor is the piece of
    chrome it originally missed. A plain .track-link rule would also pass
    here while leaving the button's own default in place."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    start = css.index("button.track-link {")
    assert "cursor: pointer" in css[start : css.index("}", start)]


def test_the_desktop_panel_is_not_centred_against_the_card() -> None:
    """The tab strip is the panel's first element, so a panel sized to its
    own contents drifts up and down as the queue fills and empties, taking
    Queue and Lyrics with it. Stretching pins them where a full panel would
    have put them, which is the only place a tab strip may be."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    desktop = css[css.index("@media (min-width: 900px) {") :]
    panel = desktop[desktop.index(".queue-panel {") : desktop.index("}", desktop.index(".queue-panel {"))]
    assert "align-self: stretch" in panel
    assert "align-self: center" not in panel


def test_moods_is_the_first_shelf_in_explore() -> None:
    """It is the only shelf that needs nothing followed and nothing typed,
    so on a library that has just been created it is the difference between
    Explore being somewhere to start and Explore being a heading over
    nothing."""
    source = (JS_DIR / "home" / "explore.js").read_text()
    shelves = source[source.index("const shelves = ["): source.index("].join(\"\")")]

    calls = re.findall(r"(moodsShelfHtml|shelfHtml)\(", shelves)
    assert calls[0] == "moodsShelfHtml", f"Moods is not first — order is {calls}"


def test_the_moods_shelf_does_not_promise_genres() -> None:
    """ytmusicapi fails to parse 25 of YouTube Music's 40 mood categories and
    they are every entry under Genres (see music.MOOD_SECTION), so only the
    moods section is listed. The old "Moods & genres" heading offered Rock
    and Jazz and then showed neither."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert "Moods &amp; genres" not in source
    assert '<h3 class="shelf-title">Moods</h3>' in source


def test_there_is_no_second_module_editing_interests() -> None:
    """home/onboarding.js was a whole second copy of this — its own chip
    query, its own selected-set, its own PUT /settings — for a panel that
    showed the same partial. Both jobs are one overlay and one module now."""
    assert not (JS_DIR / "home" / "onboarding.js").exists()

    for path in JS_DIR.rglob("*.js"):
        if path.name == "settings.js":
            continue
        source = path.read_text()
        assert ".genre-chip" not in source, f"{path.name} still reaches for the chips"


def test_settings_reads_the_picker_rather_than_a_second_copy_of_it() -> None:
    """Which chips are on is server-rendered into aria-pressed (see
    interests.interest_chips). Shipping the same fact a second time — as JSON
    on the element, which is how the free-text editor did it — would only be
    something to keep in step."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert 'getAttribute("aria-pressed")' in source
    assert "dataset.interests" not in source
    # The free-text editor's own machinery, gone rather than left unreachable.
    for gone in ("interests-input", "interests-form", "interest-chip-remove", "renderInterests"):
        assert gone not in source, gone


def test_the_first_run_releases_the_overlay_when_it_is_done() -> None:
    """The same element is a locked first run and, later in the same session,
    what Settings' "Manage interests" opens. Every way out consults
    data-required at the moment of the click (see core.js's isRequired), so
    removing the attribute is what hands the overlay back — without it,
    reopening it from Settings afterwards would trap the user in a screen
    they had already finished with, until they reloaded the page."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert 'removeAttribute("data-required")' in source

    core = (JS_DIR / "core.js").read_text()
    # A wiring-time flag can't express "locked for part of a session", which
    # is the shape this actually needs.
    assert "dismissible" not in core, "a fixed flag is what this replaced"
    assert core.count("isRequired(overlay)") >= 2  # the Escape path and the click paths


def test_the_interests_modal_outranks_the_generic_one() -> None:
    """.modal-interests and .modal are both a single class, so neither
    out-specifies the other and whichever comes last in the file wins.

    This block used to sit up with the Settings rules, ~1500 lines above
    .modal — so its max-width lost to .modal's 360px and the declaration sat
    there doing nothing, which is not something reading either rule reveals.
    Anything .modal also sets has to be declared below it.
    """
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert css.index("\n.modal {") < css.index("\n.modal-interests {")


def test_the_first_run_is_fullscreen_and_the_settings_one_is_not() -> None:
    """Both modes are the same element, so the difference has to live in a
    selector rather than in a class the template picks. Scoped to
    [data-required] — an id-plus-attribute selector, which outranks .modal
    wherever it sits in the file, unlike the class-only rules around it.

    The fullscreen treatment deliberately doesn't apply in both: Settings'
    picker has no Continue row and about half a screen of chips, so a
    full-height panel there was mostly empty space below them.
    """
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    fullscreen = css[css.index("#interests-overlay[data-required] .modal-interests {") :][:400]
    assert "height: 100%" in fullscreen
    assert "max-width: none" in fullscreen
    assert "modal-overlay-full" not in css, "the class this replaced applied to both modes"


def test_lyrics_ignores_its_own_smooth_scroll() -> None:
    """Following along stalled for six seconds after every line change.

    The panel leaves the list alone for MANUAL_SCROLL_GRACE_MS after a manual
    scroll, and the automatic scroll fires the very same `scroll` event as it
    animates — dozens of times — so each line change armed the grace period
    against the next one. On a phone that was long enough for the current
    line to slide off the bottom of a short panel before the list caught up.
    """
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    assert "autoScrolling = true" in source
    assert "!autoScrolling" in source, "the scroll listener has to check it"
    assert "scrollend" in source, "and hand the list back as soon as it settles"


def test_opening_the_lyrics_tab_jumps_to_the_line_being_sung() -> None:
    """The tab switch resets the scroll to the top, and the only thing that
    moves it afterwards reacts to the line *changing* — so opening this tab
    mid-verse left the reader at the start of the song until the next line
    came round."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    assert "syncActiveLine({ force: true })" in source


def test_the_keyboard_is_measured_without_the_scroll_offset() -> None:
    """`innerHeight - height`, and nothing else.

    Subtracting visualViewport.offsetTop as well walked the answer towards
    zero as the page was scrolled with the keyboard open; past
    KEYBOARD_MIN_HEIGHT the class came off and iOS dragged the bottom bar and
    mini player into the middle of the screen, right above the keys.
    """
    source = (JS_DIR / "viewport.js").read_text()

    assert "const covered = window.innerHeight - viewport.height;" in source
    # In the note explaining why, not in the arithmetic.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    assert "viewport.offsetTop" not in code


def test_a_focused_text_field_hides_the_bottom_furniture_on_its_own() -> None:
    """The second, independent signal. The measurement says how tall the
    keyboard is — nothing else reports that — but focus is what says one is
    up at all, and unlike the geometry it can't be talked out of it by a
    scroll."""
    source = (JS_DIR / "viewport.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert '"focusin"' in source and '"focusout"' in source
    assert 'classList.toggle("is-typing"' in source
    assert "body.is-typing .tabs" in css
    assert "body.is-typing .mini-player" in css


def test_a_toggle_that_is_on_outranks_a_stuck_hover() -> None:
    """A touch browser applies :hover on tap and never sends the mouseleave
    that clears it, so shuffle stayed hovered after being pressed. At two
    classes the on-state tied with .btn-transport:hover and lost outright to
    .btn-quiet-icon:hover, which is further down the file — so pressing
    shuffle repainted it to the hover colour on both buttons that carry it.
    """
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert ".btn-transport.btn-shuffle.is-on" in css
    assert ".btn-quiet-icon.btn-shuffle.is-on" in css
    assert "\n.btn-shuffle.is-on," not in css, "two classes is not enough — see the docstring"


def test_the_pinned_playlists_hero_badge_is_smaller_than_an_artists() -> None:
    """Both fill the same slot in _detail_hero.html, and they started at the
    same size. An artist's hero is a photograph and a photograph at 96px is a
    portrait; a single white glyph on a saturated disc at 96px is louder than
    the playlist's own name beside it.

    Scoped to .channel-hero-avatar so the library grid's 44px badge, which is
    deliberately sized to match the avatars beside it, is left alone.
    """
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert ".channel-hero-avatar.channel-card-icon {" in css
    assert ".channel-card-avatar.channel-card-icon" not in css, (
        "the grid badge matches the artist avatars beside it on purpose"
    )


def test_the_players_artist_line_opens_the_artist() -> None:
    """The one place in the app where you look straight at an artist's name
    with no way to reach them. Announced rather than called: home/detail.js
    owns openDetail and already imports the overlay, so importing it back
    would be a cycle."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    detail = (JS_DIR / "home" / "detail.js").read_text()

    assert "OPEN_ARTIST" in overlay and "OPEN_ARTIST" in detail
    # Collapsed, not closed: closing stops the music.
    assert "collapsePlayer();" in overlay[overlay.index("artistPageId") :]


def test_a_song_search_result_plays_from_anywhere_on_the_row() -> None:
    """A track in a list is something you tap. The play button stays for
    keyboard and screen-reader use, and the artist link inside the row is
    still checked first so it isn't swallowed."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    handler = source[source.index('videoResults.addEventListener("click"') :][:900]
    assert handler.index(".artist-link") < handler.index(".video-search-result")
    assert "playRemoteVideo(row.dataset" in handler


def test_repeat_all_does_not_wrap_a_single_track_queue() -> None:
    """`1 % 1` is 0, so the wrap-around modulo turned a one-track queue's
    next and previous into the track already playing — turning repeat on lit
    both skip buttons up and pressing either restarted it."""
    source = (JS_DIR / "home" / "queue.js").read_text()

    assert "if (state.order.length < 2) return null;" in source


def test_the_queue_is_dragged_closed_by_its_own_top_edge() -> None:
    """It used to be the artwork and the title — the two elements furthest
    from the panel being dismissed. The tab strip is the sheet's top edge and
    sits directly above what moves."""
    js = (JS_DIR / "home" / "overlay.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert 'panel.querySelector(".panel-tabs")' in js
    assert ".player-overlay.is-queue-open .panel-tabs {\n  touch-action: none;" in css
    assert ".player-overlay.is-queue-open .player-art,\n.player-overlay.is-queue-open .player-meta {\n  touch-action: none;" not in css


def test_a_drag_does_not_also_switch_the_tab_it_started_on() -> None:
    """Pointer events fire first and the click lands after the panel has
    already closed, so without this a pull-to-close that began on LYRICS also
    selected it.

    A time window, not a "swallow the next click" flag. A touch drag doesn't
    always produce a click, and the flag then stayed armed until the next tap
    — a real one — was eaten instead. Caught in a browser test: the tab
    tapped after a pull-to-close silently didn't switch.
    """
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "closedByDragAt" in source
    assert "CLICK_AFTER_DRAG_MS" in source
    assert "dataset.dragged" not in source, "the flag this replaced ate real taps"


def test_explores_search_field_and_tabs_are_one_sticky_block() -> None:
    """Two stacked sticky elements would mean hardcoding the strip's offset to
    the field's rendered height. On a phone the app header is sticky too, so
    this pins below it — measured, not assumed (see installHeaderOffset)."""
    css = (JS_DIR.parent / "css" / "style.css").read_text()
    viewport = (JS_DIR / "viewport.js").read_text()

    head = css[css.index(".explore-search-head {") :][:260]
    assert "position: sticky" in head
    assert "top: var(--app-header-height);" in css
    assert "--app-header-height" in viewport
    assert "ResizeObserver" in viewport


def test_a_music_video_row_is_swapped_for_the_song_before_it_plays() -> None:
    """Explore's playlists are video playlists almost end to end, and a video
    entry has a 16:9 still for a cover, no lyrics and a different recording.
    Resolved for the track actually being played — and for the one prefetched
    behind it, before its download, since the download fetches whatever
    video_id the row names."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    assert "songVersionOf" in source
    assert "is_music_video" in source
    # The whole function, not a fixed slice of it: this used to read the
    # first 1400 characters and broke on a comment being added above the
    # download, which says nothing about the ordering it is checking.
    prefetch = source[source.index("async function cacheUpcoming(contentId) {") :]
    prefetch = prefetch[: prefetch.index("\n}\n")]
    assert prefetch.index("songVersionOf") < prefetch.index("/download")


def test_start_playback_does_not_reload_the_track_already_loaded() -> None:
    """Assigning `src` runs the media element's load algorithm, which resets
    the playback position to the beginning — including when the URL assigned
    is the one already loaded. So an unconditional `audio.src = streamUrl`
    turns any second call to startPlayback into a silent restart from 0:00.

    That is how a real session lost a queue. iOS reports a page as visible
    again when the screen merely *wakes* at the lock screen, which re-runs
    prepareAudio's visibility check-in; it re-entered startPlayback for the
    track already playing, reset it to 0:00, and — the phone still being
    locked — left it pinned there. A track pinned at 0 never reaches its end,
    so it never advances, and a home-screen PWA that has stopped making sound
    is frozen by iOS seconds later, so nothing was left running to recover.
    """
    source = (JS_DIR / "player.js").read_text()

    # What "already loaded" is decided from moved (see
    # test_the_loaded_track_is_not_identified_by_comparing_urls); that it is
    # decided at all, and that it gates the assignment, has not.
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
    """The watchdog used to bail out on `!audio.paused || currentTime > 0`,
    which let through the worse of the two silent states: a play() the browser
    accepted and then never produced a frame for leaves the element unpaused
    and pinned at 0 indefinitely — "playing" on the lock screen with nothing
    coming out of it. Being at the start is the symptom; whether the element
    admits to being paused is not part of it."""
    source = (JS_DIR / "player.js").read_text()

    assert "if (!audio.paused || audio.currentTime > 0) return;" not in source, (
        "the stall watchdog treats an unpaused element as healthy again — the "
        "state that stopped a real session is invisible to it"
    )
    assert "if (audio.currentTime > 0) return;" in source, "the stall watchdog no longer checks the position"


def test_the_prefetch_guard_is_set_only_once_the_prefetch_goes_out() -> None:
    """`prefetchedFor` is a once-per-track guard, so setting it before the
    queue had been consulted made it permanent for that track: a queue that
    was momentarily empty at this one second — or a track pinned at 0, so that
    the check ran at the same instant every time — never got a second
    chance."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    # Matched as contiguous text rather than by comparing indexes: peekNextId
    # is called from the transport sync earlier in this same file, so a plain
    # source.index() finds that one and compares the wrong pair.
    assert "prefetchedFor = playing;\n    const upcoming = peekNextId();" not in source, (
        "prefetchedFor is marked before peekNextId() is consulted — a track "
        "whose queue was momentarily empty never prefetches again"
    )
    assert "const upcoming = peekNextId();\n    if (upcoming == null) return;" in source, (
        "the prefetch no longer bails out before marking the guard"
    )


def test_the_stall_watchdog_does_not_interfere_with_a_loading_element() -> None:
    """Widening the watchdog to catch an unpaused element pinned at 0 was
    right; repairing that case was not. `readyState: 1` on an unpaused element
    does not mean stuck, it means "no data yet" — a backgrounded PWA opening
    audio sits there for seconds with a play() already in flight.

    A version of this called `audio.load()` to unstick it. Measured on a real
    device: three stalls, three `AbortError`s from the play() it aborted, and
    the last track never played again — it died 2 seconds after the call. The
    pending play() of a backgrounded iOS page is the whole audio grant, so
    interrupting a load is worse than waiting for one."""
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
    """A locked phone whose screen comes on makes no request of its own, so
    "it advances with the screen off but not while the screen is awake on the
    lock screen" is invisible in the server log — there is nothing to line the
    failure up against. Gated on a loaded track so it stays quiet outside
    playback."""
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
    """Prefetching used to mean "tell the server to download it", and stopped
    there — nothing pulled a byte of the next track into the page, so every
    handoff still opened a network fetch at the one moment it could least
    afford to. Measured over nine auto-advances that all had their next track
    already on the server's disk: 0.28-1.43s just to get the /stream request
    out, and six playback-stalled beacons, every one of them readyState 1
    (metadata in hand, not one sample of audio) three seconds after play().

    So the bytes are fetched during the previous track and the element is
    handed an object URL for them. If either half of that goes away the wait
    comes back, and it comes back invisibly — everything still works, just
    slowly, which is exactly how it went unnoticed the first time."""
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
    """`audio.currentSrc.endsWith(dataset.stream)` was how two places asked
    "is this track the one loaded?", and it cannot answer that any more: a
    prefetched track is handed a blob: URL, which carries no content id, so
    the comparison says "not loaded" for the track playing right now.

    Both readers break loudly if that comes back. startPlayback would
    reassign src on a track already playing — which resets it to 0:00, the
    whole of the screen-wake bug — and overlay's `ended` handler would take
    the finished track for an outgoing one and stop advancing the queue."""
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
    """An object URL pins its Blob in memory until revoked, and these are
    whole audio files. There are three ways one stops being needed — the
    queue moves off it before the handoff, the element replaces it with the
    next track, the player is closed — and only the middle one is on the
    happy path, so the other two are the ones that would quietly leak."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    player = (JS_DIR / "player.js").read_text()

    minted = overlay.count("URL.createObjectURL(") + player.count("URL.createObjectURL(")
    assert minted == 1, (
        f"object URLs are minted in {minted} places — one owner was the point, "
        "since every one of them has to be revoked somewhere"
    )

    # Dropped without ever being played: the queue moved on mid-fetch, or the
    # open went to a different track than the one prefetched.
    assert "if (upcomingTrack?.id !== id) {\n    URL.revokeObjectURL(objectUrl);" in overlay, (
        "a prefetch superseded while its bytes were in flight leaks its Blob"
    )
    assert "} else if (upcomingTrack?.objectUrl) {" in overlay, (
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
    """It used to wait for 8 seconds of the current track before pulling the
    next one down, so that skipping quickly through a queue didn't start a
    download per track passed over. That put the cost on the wrong person:
    press Next inside those 8 seconds — which is most of the time anyone
    presses it — and nothing had been prefetched, so the press paid for the
    metadata round trip, the live song-version search behind it, and the whole
    download before a single thing on screen changed.

    A track skipped past before it plays a frame still prefetches nothing,
    because no timeupdate ever fires for it — that part never needed a
    threshold."""
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


def test_the_upcoming_track_is_cached_before_its_download_is_asked_for() -> None:
    """Everything openPlayer would otherwise have to do itself — the metadata
    fetch and songVersionOf's live search — is already done by this point, so
    publishing it here rather than after the download POST is what makes a
    Next press landing mid-prefetch instant on screen instead of repeating all
    of it.

    The write after the POST has to re-check that the entry is still ours: the
    handoff can take it while that request is in flight, and writing to it
    then resurrects a cache entry for the track that is already playing."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    body = source[source.index("async function cacheUpcoming(contentId) {") :]
    body = body[: body.index("\n}\n")]

    published = body.index("upcomingTrack = { id, data: { ...resolved }, objectUrl: null };")
    posted = body.index('await api(`/content/${id}/download`, { method: "POST" })')
    assert published < posted, (
        "the upcoming track is only cached after its download POST returns — a "
        "Next press before then repeats the metadata fetch and the live search"
    )
    assert "if (upcomingTrack?.id !== id) return;" in body[posted:], (
        "the post-download write doesn't re-check that the entry is still ours"
    )


def test_a_press_that_has_to_ask_the_server_says_so_immediately() -> None:
    """With nothing prefetched, openPlayer awaits a round trip (and possibly a
    live song-version search) before it writes a single thing to the DOM, so
    the card went on showing the previous track the whole time and the press
    read as ignored.

    Gated on a track already being open, and undone when the fetch fails —
    otherwise a cold open surfaces an empty card, and a failed one leaves the
    transport disabled with the outgoing track still playing and no way to
    pause it."""
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
    """`prepared` only covers the next track's metadata; whether the src swap
    ran against a blob or against the network was invisible, and the
    2026-08-23 stall had to be settled from the *absence* of a /stream line
    in the server's access log. `buffered` states it outright."""
    source = (JS_DIR / "home" / "overlay.js").read_text()

    # rindex: the repeat-"one" branch has its own, earlier track-ended call
    # (with `repeat: "one"` instead of a handoff), and the handoff's is last.
    ended = source[source.rindex('reportPlayback("track-ended"') :]
    ended = ended[: ended.index(");")]
    assert "buffered: Boolean(upcomingTrack?.objectUrl)" in ended, (
        "track-ended no longer reports whether the handoff had the audio in memory"
    )


def test_lock_screen_taps_leave_a_trace() -> None:
    """A lock-screen control that "did nothing" has two very different causes:
    our handler ran and its play() was refused, or iOS had already frozen the
    page and the handler never ran at all. Only a beacon *from inside the
    handler* can tell them apart — a tap with no beacon is the frozen-page
    case. Every transport action the OS can send must therefore report."""
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
    """Whether this device has the Audio Session API, and whether the session
    was "interrupted" at the moment something went wrong, both matter exactly
    when a beacon fires — and a field costs no extra beacons. `visibility`
    rides along the same way and for the same reason."""
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
    """The spec makes a playbackRate of zero a TypeError — paused is
    playbackState's job — so the old `playbackRate: rendering ? 1 : 0` always
    threw for a non-rendering element and the catch swallowed it. The shipped
    behaviour ("no update at all while silent") was right; the code now states
    it instead of stumbling into it, and a finite-duration guard covers the
    other input setPositionState throws on."""
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
    """WebKit's Audio Session API is the one channel a web page has to tell
    iOS "I am a music app" — the category kept running with the screen locked
    and not muted by the silent switch — rather than leaving the OS to infer
    it per play(). Safari-only and experimental, so feature-detected and
    allowed to fail."""
    source = (JS_DIR / "player.js").read_text()

    assert '"audioSession" in navigator' in source
    assert 'navigator.audioSession.type = "playback"' in source, (
        "the audio session type is no longer declared"
    )


def test_unchanged_metadata_is_republished_until_a_held_publish_lands() -> None:
    """Two competing needs. A track change publishes its metadata during the
    silent gap before playback, which iOS may silently drop — so the publish
    on `playing` (the one moment iOS provably holds the session) must NOT be
    skipped just because the values look identical; a Dynamic Island stuck on
    the previous track is the bug that re-publish fixed. But `playing` also
    fires after every buffering hitch and resume, and re-publishing identical
    values then makes iOS rebuild the Now Playing card for nothing. The cache
    therefore only short-circuits once a held-session publish has landed."""
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
    """When the last track runs out there is nothing left to control, and iOS
    freezes the page shortly after the audio stops — a Now Playing card left
    up is dead weight, and the thing that lingers on the Dynamic Island after
    the app is closed. Replaying in-app re-publishes on `playing`."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()

    assert "if (next == null) clearNowPlayingMetadata();" in overlay, (
        "a queue running out no longer clears the OS Now Playing surface"
    )
    # closePlayer must clear through the same helper — a hand-rolled clear
    # there would leave player.js's publish cache thinking the old metadata
    # is still up, so re-opening the same track would publish nothing.
    body = overlay[overlay.index("export function closePlayer(") :]
    body = body[: body.index("\n}")]
    assert "clearNowPlayingMetadata();" in body, (
        "closePlayer clears mediaSession by hand (or not at all) instead of "
        "through clearNowPlayingMetadata, desyncing the publish cache"
    )


def test_the_background_handoff_happens_before_the_cliff() -> None:
    """`ended` is a cliff for a backgrounded page: the moment nothing renders,
    iOS starts freezing it — measured 2026-08-23 18:14, a handoff with the
    next track's bytes already in memory and play() already called still sat
    at readyState 1 for 14 seconds until the screen woke. The only reliable
    side of the cliff is the near one, so in the background the swap happens
    while the outgoing track is still rendering."""
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

    # The `ended` path must survive as the fallback for everything the early
    # handoff declines: foreground playback, a missing blob, a paused element.
    ended = source[source.rindex('reportPlayback("track-ended"') :]
    assert "playFromQueue(next);" in ended, (
        "the ended handler no longer advances — the early handoff is now the "
        "only path forward and every case it declines just stops"
    )
