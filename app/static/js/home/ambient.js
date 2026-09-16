import { onFragmentsSwapped } from "../fragments.js";

// Cover as Canvas — the ambient wash behind Home's hero.
//
// The colour comes from the cover that is already on screen. Every cover in
// the app is proxied through /image-proxy, so the image is same-origin, the
// canvas it is drawn into is not tainted, and this costs no extra request, no
// new CSP origin and no dependency.
//
// A cover cannot be used as a page colour as it comes. Album art is routinely
// near-white, fully saturated, or almost black, and each of those applied
// straight to the page either washes the chrome out or does nothing visible
// at all. So the sample is converted to OKLCH — a perceptual space, where
// clamping lightness actually clamps how light the result *looks* — squeezed
// into a narrow band, and only then written back as a custom property.
//
// That clamp is the whole treatment. The hue is taken from the record and
// nothing else is: the tint changes with the music, the contrast does not.

// Rendering the cover down to this before reading pixels. Large enough that a
// small colour block on an otherwise dark sleeve survives, small enough that
// the whole read is a fraction of a frame.
const SAMPLE_SIZE = 24;

// The band the tint is allowed to occupy, in OKLCH. Lightness is compressed
// rather than clipped, so a bright sleeve and a dark one still differ — just
// far less than they do in the artwork. Chroma is capped outright: past this
// the wash stops reading as light in a room and starts reading as a colour
// the app chose.
//
// The ceiling is what the numbers below are for. At L 0.34 / C 0.085 the
// worst hue on the wheel still leaves --text-secondary at 6.4:1 and
// --text-primary at 9.7:1 on the wash, and the play button 3.9:1 against it —
// so no cover, however bright or saturated, can push anything in the hero
// under its threshold. Raising either constant means re-checking that.
const L_BASE = 0.21;
const L_RANGE = 0.13;
const C_BASE = 0.02;
const C_SCALE = 0.6;
const C_MAX = 0.085;

// The second, dimmer wash in the top-right corner. Same hue, pulled down so
// the two gradients read as one light source rather than two.
const SOFT_L_FACTOR = 0.62;
const SOFT_C_FACTOR = 0.7;

function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function linearToSrgb(c) {
  return c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055;
}

function linearRgbToOklab(r, g, b) {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function oklabToRgb255(L, a, bb) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * bb) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * bb) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * bb) ** 3;
  const rgb = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  // Clipping per channel rather than a proper gamut mapping: everything that
  // reaches here has already been held to C_MAX, which is well inside sRGB at
  // these lightnesses, so there is nothing left for a smarter mapping to save.
  return rgb.map((v) => Math.round(Math.min(1, Math.max(0, linearToSrgb(v))) * 255));
}

/**
 * The dominant colour of an image, as OKLCH.
 *
 * Averaged in OKLab rather than in sRGB — a plain sRGB mean of a colourful
 * sleeve comes back mud — and the a/b axes are weighted by each pixel's own
 * chroma, so a mostly-black cover with one saturated band reports that band
 * instead of reporting black. Lightness stays an unweighted mean, since that
 * is genuinely how bright the sleeve is.
 *
 * Returns null when the image cannot be read (not decoded yet, zero-sized, or
 * a canvas the browser refuses to hand back).
 */
function sampleOklch(img) {
  const canvas = document.createElement("canvas");
  canvas.width = SAMPLE_SIZE;
  canvas.height = SAMPLE_SIZE;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;

  let data;
  try {
    ctx.drawImage(img, 0, 0, SAMPLE_SIZE, SAMPLE_SIZE);
    data = ctx.getImageData(0, 0, SAMPLE_SIZE, SAMPLE_SIZE).data;
  } catch {
    // A tainted canvas throws here. It should not happen while covers are
    // same-origin, but a thrown SecurityError must not take Home down with
    // it — the page is perfectly usable on the default tint.
    return null;
  }

  let sumL = 0;
  let sumA = 0;
  let sumB = 0;
  let chromaWeight = 0;
  let count = 0;

  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 8) continue; // fully transparent padding
    const [L, a, b] = linearRgbToOklab(
      srgbToLinear(data[i] / 255),
      srgbToLinear(data[i + 1] / 255),
      srgbToLinear(data[i + 2] / 255),
    );
    const chroma = Math.hypot(a, b);
    sumL += L;
    sumA += a * chroma;
    sumB += b * chroma;
    chromaWeight += chroma;
    count += 1;
  }

  if (!count) return null;

  const meanL = sumL / count;
  // A genuinely greyscale sleeve has no hue to report; it gets chroma 0 and
  // the wash becomes a neutral lift, which is the honest answer for it.
  const meanA = chromaWeight > 0 ? sumA / chromaWeight : 0;
  const meanB = chromaWeight > 0 ? sumB / chromaWeight : 0;

  return { L: meanL, C: Math.hypot(meanA, meanB), h: Math.atan2(meanB, meanA) };
}

function toChannels({ L, C, h }, dim) {
  const clampedL = (L_BASE + L_RANGE * Math.min(1, Math.max(0, L))) * (dim ? SOFT_L_FACTOR : 1);
  const clampedC = Math.min(C_MAX, C_BASE + C * C_SCALE) * (dim ? SOFT_C_FACTOR : 1);
  return oklabToRgb255(clampedL, Math.cos(h) * clampedC, Math.sin(h) * clampedC).join(" ");
}

/**
 * Light one ambient field from the artwork inside its own hero.
 *
 * Writes to whichever of the two layers is not currently showing, then flips
 * `data-live` so CSS cross-fades to it.
 */
function paintField(field) {
  const art = field.parentElement?.querySelector("[data-ambient-source]");
  if (!art) return;

  const paint = () => {
    const sample = sampleOklch(art);
    if (!sample) return;

    const next = field.dataset.live === "a" ? "b" : "a";
    field.style.setProperty(`--tint-${next}`, toChannels(sample, false));
    field.style.setProperty(`--tint-${next}-soft`, toChannels(sample, true));
    field.dataset.live = next;
  };

  if (art.complete && art.naturalWidth > 0) {
    paint();
    return;
  }
  // decode() rejects on a broken image; the tint simply stays at its default,
  // which is the page colour, so there is nothing to handle.
  art.decode().then(paint, () => {});
}

/**
 * Light every ambient field currently in the document.
 *
 * Home has one and an open artist/album page has another, so this is written
 * as a sweep rather than as one hard-coded pair of ids. Called on load, after
 * every fragment swap, and after the detail panel swaps its own markup in, so
 * it has to be safe to run any number of times — it holds no state beyond the
 * attribute already on each element.
 */
export function applyAmbientTint() {
  document.querySelectorAll(".ambient-field").forEach(paintField);
  measureWashReach();
}

// A hero's wash has to start at the very top of the screen, behind the
// transparent header, and end just below the hero. CSS can only anchor both
// edges to one box, and that box is the hero, so the distance up to the top of
// the page used to be written into style.css as a guess (-132px on Home,
// -152px on an artist page). A guess is wrong the moment anything above the
// hero changes height: on an iPhone the header gains the Dynamic Island's
// safe-area inset, and an artist page has a back link Home doesn't, which
// left the top ~40px of the screen unlit there.
//
// So it is measured instead — the hero's distance from the top of the
// document, written to --wash-reach on the field, which style.css subtracts
// from. The player overlay's field fills its own box and is not a hero wash.
const HERO_WASH = ".home-hero-ambient, .detail-hero-ambient";

function measureWashReach() {
  document.querySelectorAll(HERO_WASH).forEach((field) => {
    const hero = field.offsetParent;
    // A hidden panel has no layout; keep the last value rather than write 0,
    // and measure again when the panel's own resize says it is showing.
    if (!hero) return;
    // rect.top + scrollY rather than offsetTop: offsetTop is relative to the
    // hero's own offsetParent, not the document. The sum also holds during
    // iOS overscroll, where the two move in opposite directions.
    const reach = hero.getBoundingClientRect().top + window.scrollY;
    field.style.setProperty("--wash-reach", `${Math.round(reach)}px`);
  });
}

// What can move a hero down the page: something above its panel changing
// size (body), or something above it inside the panel — a back link, a
// banner, or the panel itself going from hidden to shown (each .tab-panel).
// Those five sections are never replaced, so observing them holds on to no
// markup that a fragment swap throws away.
function watchWashReach() {
  const observer = new ResizeObserver(measureWashReach);
  observer.observe(document.body);
  document.querySelectorAll(".tab-panel").forEach((panel) => observer.observe(panel));
}

// The sticky header sits directly over the top of the wash. Left opaque it
// draws a hard band across it; left transparent forever it would put the logo
// over whatever a shelf happens to scroll underneath. So it is transparent
// while a page with a hero — Home, or an open artist/playlist — is resting at
// the very top, and opaque as soon as it is scrolled at all.
//
// This only answers "is the page at the top". Whether the panel on screen has
// a hero is style.css's question, asked with :has() against the active panel.
// It used to be asked here as "does .home-hero exist", which is true while an
// artist page is open over a lit Home, and false on an artist page opened from
// a Home with no hero yet — the wrong panel either way.
//
// This used to observe the hero itself, offset by the header's height. That
// answered a different question than it looked like it did: the hero stayed
// intersecting until its *bottom* edge cleared the header, so the bar went on
// carrying no fill for the hero's whole height — a couple of hundred pixels
// during which shelf rows scrolled under an unfilled bar and the logo sat on
// top of them. The threshold that matters is "has this moved at all", so what
// is observed now is a 1px probe at the very top of the document (see
// index.html) rather than the hero.
//
// Still an IntersectionObserver rather than a scroll handler: this is a
// threshold question, and a scroll listener would answer it on every frame to
// say "no" almost every time.
//
// Measured: the fill lands at two pixels of scroll, not one. At exactly one,
// the probe's bottom edge is flush with the top of the viewport, and an
// observer still calls an edge-touching rect intersecting. Two pixels is
// nobody's scroll gesture, so this is the threshold either way.
let pageTopObserver = null;

function watchPageTop() {
  pageTopObserver?.disconnect();
  const probe = document.querySelector(".page-top-probe");
  if (!probe) {
    delete document.documentElement.dataset.heroLit;
    return;
  }
  pageTopObserver = new IntersectionObserver(
    ([entry]) => {
      if (entry.isIntersecting) document.documentElement.dataset.heroLit = "";
      else delete document.documentElement.dataset.heroLit;
    },
    { threshold: 0 },
  );
  pageTopObserver.observe(probe);
}

export function setupAmbientTint() {
  applyAmbientTint();
  watchPageTop();
  watchWashReach();
  onFragmentsSwapped(() => {
    applyAmbientTint();
  });
}
