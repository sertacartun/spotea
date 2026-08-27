// What the on-screen keyboard does to a fixed bottom bar.
//
// The bottom navigation and the mini player are `position: fixed; bottom: 0`,
// which pins them to the *layout* viewport. Opening the keyboard doesn't
// shrink that viewport — it shrinks the *visual* one and leaves the layout
// viewport alone, so the bar keeps sitting at a bottom edge that is now
// somewhere behind the keyboard. iOS resolves the contradiction by dragging
// the bar up into the middle of the screen, right above the keys, where it
// covers the content you were typing towards.
//
// So the keyboard is measured here instead: how much of the layout viewport
// it covers goes into --keyboard-inset, and the CSS uses that both to get the
// bottom furniture out of the way and to give the page that much extra room
// to scroll into, so every element can still be brought above the keys.
//
// Deliberately not paired with `interactive-widget=resizes-content` in the
// viewport meta: that flag makes Android's browser resize the layout viewport
// itself, which fixes the drag but leaves the bar parked directly on top of
// the keyboard — and makes the measurement below read zero, so this code
// would stop running on exactly one platform. One behaviour on both is worth
// more than a native-feeling half.

import { reportPlayback } from "./player.js";

// Whether this is the installed app rather than a browser tab. The difference
// matters below: in a browser, the space between the screen and the layout
// viewport is the browser's own chrome, and anything moved into it disappears
// behind a toolbar. An installed app has no chrome, so the same gap can only
// be viewport geometry.
const STANDALONE =
  window.matchMedia?.("(display-mode: standalone)").matches === true ||
  window.navigator.standalone === true;

// An on-screen keyboard is always taller than this. Safari's collapsing URL
// bar is not, and neither is the accessory strip on its own — without a floor
// the class would flicker on every scroll.
const KEYBOARD_MIN_HEIGHT = 120;

// Anything focusable that summons a keyboard. Ranges, checkboxes and buttons
// don't, and treating them as if they did would hide the bottom bar every
// time the volume slider was touched.
const TYPING_SELECTOR = 'input:not([type="range"]):not([type="checkbox"]):not([type="radio"]), textarea';

/**
 * Publishes the app header's rendered height as --app-header-height.
 *
 * On a phone the header is `position: sticky; top: 0`, so anything else that
 * wants to pin below it (Explore's search field and its tab strip) needs to
 * know how tall it is — and it isn't a constant: the logo row's height comes
 * from its own padding and font size, both of which a browser's text-size
 * setting can change under us.
 *
 * Measured rather than hardcoded for exactly that reason, and re-measured on
 * resize through a ResizeObserver rather than a window listener, so a change
 * in the header itself is caught too.
 */
export function installHeaderOffset() {
  const header = document.querySelector(".app-header-sticky");
  if (!header) return;

  const publish = () => {
    document.documentElement.style.setProperty(
      "--app-header-height",
      `${Math.round(header.getBoundingClientRect().height)}px`
    );
  };

  publish();
  if (!("ResizeObserver" in window)) return;
  new ResizeObserver(publish).observe(header);
}

export function installKeyboardInset() {
  // The second, independent signal that a keyboard is up. The measurement
  // below is the one that says *how tall* it is, and it has to be — nothing
  // reports that — but on iOS it is not reliable as a yes/no: the moment the
  // page is scrolled with the keyboard open the numbers stop describing a
  // keyboard at all (see update). Focus does not have that problem. On a
  // phone, a text field with focus means a keyboard, full stop.
  //
  // Bound whether or not visualViewport exists, since this half needs
  // nothing from it.
  const setTyping = (on) => document.body.classList.toggle("is-typing", on);
  document.addEventListener("focusin", (event) => {
    if (event.target.matches?.(TYPING_SELECTOR)) setTyping(true);
  });
  document.addEventListener("focusout", (event) => {
    if (event.target.matches?.(TYPING_SELECTOR)) setTyping(false);
  });

  const viewport = window.visualViewport;
  if (!viewport) return;

  function update() {
    // `innerHeight - height`, and nothing else. This used to subtract
    // viewport.offsetTop as well, on the reasoning that the offset is the
    // part of the gap scrolled off the top rather than covered by keys — but
    // the keyboard is still there while the offset grows, so scrolling the
    // page with it open walked the answer down towards zero. Past
    // KEYBOARD_MIN_HEIGHT the class came off and the bottom bar and mini
    // player reappeared, dragged by iOS into the middle of the screen right
    // above the keys: exactly the failure this file exists to prevent, only
    // now triggered by scrolling instead of by typing.
    const covered = window.innerHeight - viewport.height;
    const inset = Math.max(0, Math.round(covered));
    document.documentElement.style.setProperty("--keyboard-inset", `${inset}px`);
    document.body.classList.toggle("is-keyboard-open", inset >= KEYBOARD_MIN_HEIGHT);
  }

  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  update();
}


/**
 * One beacon on boot with the geometry the bottom bar is laid out against.
 *
 * Everything here is invisible from the server and unreproducible off the
 * device — which is the whole reason /debug/playback exists (see
 * routers/debug.py). It has already earned its keep once: the installed app
 * reports a 932pt screen against an 873pt viewport, and knowing that the
 * missing 59pt is the status bar at the top rather than anything at the
 * bottom is what stopped a second round of guessing at the bar's placement.
 * Sent once per app open, so a household install adds a line a day.
 */
export function reportViewportGeometry() {
  // After load, and a beat after that: the numbers this is about only mean
  // something once the page has been laid out, and the document's height —
  // which is the whole suspicion — is the last of them to settle.
  window.addEventListener("load", () => setTimeout(sendGeometry, 500));
}

/** What env(safe-area-inset-*) actually resolves to here. There is no way to
 *  read one directly, so each is measured off an element sized by it. */
function safeAreaInsets() {
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;top:0;left:0;width:1px;visibility:hidden;pointer-events:none;";
  document.body.appendChild(probe);
  const read = (edge) => {
    probe.style.height = `env(safe-area-inset-${edge}, 0px)`;
    return Math.round(probe.getBoundingClientRect().height);
  };
  const insets = `${read("top")}/${read("right")}/${read("bottom")}/${read("left")}`;
  probe.remove();
  return insets;
}

function sendGeometry() {
  const vv = window.visualViewport;
  const tabs = document.querySelector(".tabs");
  const rect = tabs?.getBoundingClientRect();
  reportPlayback("viewport-geometry", {
    insets: safeAreaInsets(),
    tabs: rect ? `${Math.round(rect.top)}..${Math.round(rect.bottom)} h${Math.round(rect.height)}` : "none",
    tabsPad: tabs ? getComputedStyle(tabs).paddingBottom : "none",
    standalone: STANDALONE,
    screen: `${window.screen?.width}x${window.screen?.height}`,
    inner: `${window.innerWidth}x${window.innerHeight}`,
    visual: vv ? `${Math.round(vv.width)}x${Math.round(vv.height)}@${Math.round(vv.offsetTop)}` : "none",
    doc: document.documentElement.scrollHeight,
    dpr: window.devicePixelRatio,
  });
}
