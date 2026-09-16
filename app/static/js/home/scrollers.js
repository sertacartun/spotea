// Row -> ResizeObserver. Swapped-out rows would leak observers, so detached rows are pruned
// lazily via document.contains(): Explore also wires rows that never go through a swap.
const wiredRows = new Map();

function pruneDetachedObservers() {
  for (const [row, observer] of wiredRows) {
    if (document.contains(row)) continue;
    observer.disconnect();
    wiredRows.delete(row);
  }
}

// Only isDown is reset on mouseup. `dragged` stays per-row: mouseup fires before click,
// so resetting it here would defeat the click suppression below.
let activeDragReset = null;

window.addEventListener("mouseup", () => {
  activeDragReset?.();
  activeDragReset = null;
});

/** Idempotent. */
export function wireScrollers() {
  pruneDetachedObservers();

  // Native overflow handles horizontal gestures. Don't map the vertical wheel to
  // horizontal scroll: it froze page scrolling whenever the cursor was over a row.
  document.querySelectorAll(".shelf-row, .channel-row").forEach((row) => {
    if (row.dataset.scrollerReady) return;
    row.dataset.scrollerReady = "true";

    // Grab cursor and the "See more" link only when the row actually overflows.
    const seeMore = row.closest(".shelf")?.querySelector(".shelf-see-more");
    const updateScrollable = () => {
      const isScrollable = row.scrollWidth > row.clientWidth + 1;
      row.classList.toggle("is-scrollable", isScrollable);
      if (seeMore) seeMore.hidden = !isScrollable;
    };
    updateScrollable();
    const observer = new ResizeObserver(updateScrollable);
    observer.observe(row);
    wiredRows.set(row, observer);

    // Links/images are natively draggable, which would swallow the mousemove events.
    row.addEventListener("dragstart", (event) => event.preventDefault());

    let isDown = false;
    let dragged = false;
    let startX = 0;
    let startScroll = 0;

    function resetDrag() {
      isDown = false;
      row.classList.remove("dragging");
    }

    row.addEventListener("mousedown", (event) => {
      if (!row.classList.contains("is-scrollable")) return;
      isDown = true;
      dragged = false;
      startX = event.pageX;
      startScroll = row.scrollLeft;
      row.classList.add("dragging");
      activeDragReset = resetDrag;
    });

    row.addEventListener("mouseleave", () => {
      resetDrag();
      if (activeDragReset === resetDrag) activeDragReset = null;
    });

    row.addEventListener("mousemove", (event) => {
      if (!isDown) return;
      const delta = event.pageX - startX;
      if (Math.abs(delta) > 5) dragged = true;
      row.scrollLeft = startScroll - delta;
      event.preventDefault();
    });

    row.addEventListener(
      "click",
      (event) => {
        if (dragged) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
  });
}
