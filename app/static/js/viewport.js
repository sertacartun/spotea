// Measures the on-screen keyboard into --keyboard-inset: fixed bottom bars pin to the
// layout viewport, which iOS drags above the keys. Not using interactive-widget=resizes-content,
// since that makes this measurement read zero on Android.

// Keyboards are always taller than this; Safari's collapsing URL bar isn't (avoids flicker).
const KEYBOARD_MIN_HEIGHT = 120;

const TYPING_SELECTOR = 'input:not([type="range"]):not([type="checkbox"]):not([type="radio"]), textarea';

/** Text-size settings can change the header's height. */
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
  // Focus is the reliable yes/no signal; on iOS the measurement stops describing
  // a keyboard once the page scrolls.
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
    // Don't subtract viewport.offsetTop: it grows while scrolling with the keyboard open,
    // dropping the inset below the threshold so iOS dragged the bars above the keys.
    const covered = window.innerHeight - viewport.height;
    const inset = Math.max(0, Math.round(covered));
    document.documentElement.style.setProperty("--keyboard-inset", `${inset}px`);
    document.body.classList.toggle("is-keyboard-open", inset >= KEYBOARD_MIN_HEIGHT);
  }

  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  update();
}
