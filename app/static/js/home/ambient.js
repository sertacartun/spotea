import { onFragmentsSwapped } from "../fragments.js";

// Ambient wash tinted from the on-screen cover. Covers are same-origin via
// /image-proxy, so the canvas isn't tainted and no new CSP origin is needed.

const SAMPLE_SIZE = 24;

// OKLCH band: at L 0.34 / C 0.085 the worst hue keeps hero text >= 6.4:1 and
// the play button 3.9:1 on the wash. Raising any constant means re-checking.
const L_BASE = 0.21;
const L_RANGE = 0.13;
const C_BASE = 0.02;
const C_SCALE = 0.6;
const C_MAX = 0.085;

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
  // Per-channel clipping is enough: C_MAX keeps everything inside sRGB here.
  return rgb.map((v) => Math.round(Math.min(1, Math.max(0, linearToSrgb(v))) * 255));
}

/**
 * Dominant colour as OKLCH. Averaged in OKLab (sRGB means come back mud), with
 * a/b weighted by chroma so one saturated band on a dark sleeve wins over black.
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
    // A tainted canvas throws; Home must survive on the default tint.
    return null;
  }

  let sumL = 0;
  let sumA = 0;
  let sumB = 0;
  let chromaWeight = 0;
  let count = 0;

  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 8) continue;
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
  const meanA = chromaWeight > 0 ? sumA / chromaWeight : 0;
  const meanB = chromaWeight > 0 ? sumB / chromaWeight : 0;

  return { L: meanL, C: Math.hypot(meanA, meanB), h: Math.atan2(meanB, meanA) };
}

function toChannels({ L, C, h }, dim) {
  const clampedL = (L_BASE + L_RANGE * Math.min(1, Math.max(0, L))) * (dim ? SOFT_L_FACTOR : 1);
  const clampedC = Math.min(C_MAX, C_BASE + C * C_SCALE) * (dim ? SOFT_C_FACTOR : 1);
  return oklabToRgb255(clampedL, Math.cos(h) * clampedC, Math.sin(h) * clampedC).join(" ");
}

/** Writes to the hidden layer, then flips `data-live` so CSS cross-fades to it. */
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
  art.decode().then(paint, () => {});
}

/** Must stay idempotent: runs on load, after fragment swaps and detail panel swaps. */
export function applyAmbientTint() {
  document.querySelectorAll(".ambient-field").forEach(paintField);
  measureWashReach();
}

// The hero wash must reach the top of the page, and the distance varies
// (safe-area inset, back link), so it is measured into --wash-reach, not hard-coded in CSS.
const HERO_WASH = ".home-hero-ambient, .detail-hero-ambient";

function measureWashReach() {
  document.querySelectorAll(HERO_WASH).forEach((field) => {
    const hero = field.offsetParent;
    // A hidden panel has no layout; keep the last value rather than write 0.
    if (!hero) return;
    // Not offsetTop (relative to offsetParent); this sum also holds during iOS overscroll.
    const reach = hero.getBoundingClientRect().top + window.scrollY;
    field.style.setProperty("--wash-reach", `${Math.round(reach)}px`);
  });
}

// These sections are never replaced by fragment swaps, so observing them holds no stale markup.
function watchWashReach() {
  const observer = new ResizeObserver(measureWashReach);
  observer.observe(document.body);
  document.querySelectorAll(".tab-panel").forEach((panel) => observer.observe(panel));
}

// Header is transparent only while the page rests at the top. Observes a 1px
// probe, not the hero, which stays intersecting until its bottom clears the header.
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
