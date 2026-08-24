// Keeping a track's actual bytes on the device, so it plays with no network
// at all.
//
// The server already downloads everything you play (see routers/content.py's
// _run_download) — that is what the Downloads modal lists. But those files
// live on the instance's disk, which is no help on a train: reaching them
// still needs a request to reach the instance. This module is the second
// hop, from the instance to the phone.
//
// Why IndexedDB rather than the Cache API, which the service worker already
// uses: the <audio> element issues Range requests while seeking, and the
// Cache API keys purely on URL with no notion of Range — a cached response
// for one byte range gets replayed for a request asking for a different one
// (which is exactly why sw.js refuses to cache /content/* at all). Handing
// the element a Blob sidesteps ranges entirely: the bytes are already in the
// page, so seeking is a memory operation and no request is made for it.
//
// The playback path this feeds is not new. player.js already accepts bytes
// instead of a URL, for the prefetch that pulls the *next* track down while
// the current one plays (see offerPrefetchedAudio). A saved track is offered
// through that same door, so nothing about how audio actually starts changes
// here.

const DB_NAME = "spotea-offline";
const DB_VERSION = 1;

// Metadata and bytes are deliberately in separate stores, keyed by the same
// content id. Listing what's saved (the Downloads modal, the device total)
// must not pay for the audio: IndexedDB materialises whole records, so a
// getAll() over a single combined store would pull every saved song's Blob
// into memory just to render a list of titles.
const META_STORE = "tracks";
const BLOB_STORE = "blobs";

/** Whether this browser can store anything at all. */
export function isSupported() {
  return typeof indexedDB !== "undefined";
}

let dbPromise = null;

function openDb() {
  if (!isSupported()) return Promise.reject(new Error("IndexedDB unavailable"));
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(META_STORE)) db.createObjectStore(META_STORE, { keyPath: "id" });
      if (!db.objectStoreNames.contains(BLOB_STORE)) db.createObjectStore(BLOB_STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    // Another tab running a newer version of the app is holding the old
    // database open. Failing here is better than hanging forever on a
    // request that will never fire either callback.
    request.onblocked = () => reject(new Error("Offline storage is busy in another tab"));
  }).catch((err) => {
    // Never leave a rejected promise memoised — a transient failure (private
    // browsing, a blocked upgrade the user then resolved) would otherwise
    // poison every later call for the lifetime of the page.
    dbPromise = null;
    throw err;
  });
  return dbPromise;
}

/** Promise-wraps one IDBRequest. */
function promisify(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** Runs `work` inside a transaction that resolves when it actually commits. */
function transact(storeNames, mode, work) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeNames, mode);
        let result;
        // Resolving on the request's own onsuccess would report a write as
        // done before the transaction commits, so a quota failure raised at
        // commit time would surface after the caller had already told the
        // user it was saved.
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error("Offline write aborted"));
        Promise.resolve(work(tx)).then(
          (value) => {
            result = value;
          },
          (err) => {
            reject(err);
            tx.abort();
          }
        );
      })
  );
}

/**
 * Asks the browser not to evict what we store.
 *
 * Without this, everything here is "best effort" storage the browser is free
 * to throw away under disk pressure — which for a feature whose whole point
 * is that the songs are still there when you have no signal is the one
 * failure that matters. Granted silently on an installed PWA in most
 * browsers; a refusal is not an error, just a weaker guarantee, so this
 * reports rather than throws.
 */
export async function requestPersistence() {
  if (!navigator.storage?.persist) return false;
  try {
    if (await navigator.storage.persisted()) return true;
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}

/**
 * What the device is holding, and what the browser will let it hold.
 *
 * `quota` is the browser's own figure for the whole origin and is a long way
 * from a promise — it is advisory, differs per browser, and on iOS is both
 * smaller and less predictable than elsewhere. It is shown so a full device
 * is legible rather than mysterious, not because it can be relied on.
 */
export async function deviceUsage() {
  const records = await listSaved();
  const bytes = records.reduce((total, record) => total + (record.size || 0), 0);
  let quota = null;
  if (navigator.storage?.estimate) {
    try {
      quota = (await navigator.storage.estimate()).quota ?? null;
    } catch {
      quota = null;
    }
  }
  // The ids ride along rather than being a second call. Everything that shows
  // a total also has to tick the rows it covers, and two reads of the same
  // store can disagree — a save landing between them renders a total that
  // counts a track the toggles say isn't saved.
  return { count: records.length, bytes, quota, ids: records.map((record) => record.id) };
}

/** Every saved track's metadata, newest first. Never touches the audio. */
async function listSaved() {
  if (!isSupported()) return [];
  try {
    const records = await transact(META_STORE, "readonly", (tx) =>
      promisify(tx.objectStore(META_STORE).getAll())
    );
    return records.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  } catch {
    return [];
  }
}

/**
 * What was stored alongside a saved track's audio: enough to render it with
 * no server to ask.
 *
 * This is the whole reason the metadata is kept at all. A device that can
 * play the bytes but cannot say what the song is called is not usable
 * offline, and GET /content/{id} — where every other surface gets a title
 * from — is exactly what is unreachable at the moment it matters.
 */
export async function readTrackMeta(contentId) {
  if (!isSupported()) return null;
  try {
    return (
      (await transact(META_STORE, "readonly", (tx) =>
        promisify(tx.objectStore(META_STORE).get(Number(contentId)))
      )) || null
    );
  } catch {
    return null;
  }
}

/**
 * A blob: URL for the saved audio, or null when this track isn't saved.
 *
 * The caller owns the URL and must revoke it — which for the playback path
 * means handing it to player.js's offerPrefetchedAudio, whose existing
 * ownership rules already cover revoking it whether or not it gets used.
 */
export async function openTrackUrl(contentId) {
  if (!isSupported()) return null;
  try {
    const record = await transact(BLOB_STORE, "readonly", (tx) =>
      promisify(tx.objectStore(BLOB_STORE).get(Number(contentId)))
    );
    if (!record?.audio) return null;
    return URL.createObjectURL(record.audio);
  } catch {
    return null;
  }
}

/** A blob: URL for the saved cover art, or null. Caller revokes. */
export async function openCoverUrl(contentId) {
  if (!isSupported()) return null;
  try {
    const record = await transact(BLOB_STORE, "readonly", (tx) =>
      promisify(tx.objectStore(BLOB_STORE).get(Number(contentId)))
    );
    if (!record?.cover) return null;
    return URL.createObjectURL(record.cover);
  } catch {
    return null;
  }
}

/**
 * Pulls a track's bytes down and keeps them.
 *
 * `track` is what the Downloads modal already knows about the row — enough
 * to render it again with no server to ask, which is the point: a device
 * that can play the audio but can't say what the song is called is not
 * usable offline.
 *
 * The cover goes through /image-proxy rather than YouTube's CDN directly.
 * Not a detail: a cross-origin fetch of an image is opaque, and an opaque
 * Blob is unreadable — it would store bytes that could never be displayed.
 * The proxy is same-origin, so its response is a real one (and it is also
 * the only way the app renders covers at all — see main.py's image_proxy).
 */
export async function saveTrack(contentId, track = {}) {
  const id = Number(contentId);

  const res = await fetch(`/content/${id}/stream`);
  if (!res.ok) throw new Error(`Could not fetch this track (${res.status})`);
  const audio = await res.blob();

  let cover = null;
  if (track.coverUrl) {
    try {
      const coverRes = await fetch(track.coverUrl);
      // A cover that won't come is worth losing; the song isn't. Anything
      // that fails here leaves cover null and the record still saveable.
      if (coverRes.ok) cover = await coverRes.blob();
    } catch {
      cover = null;
    }
  }

  const meta = {
    id,
    title: track.title || "",
    artist: track.artist || "",
    duration: track.duration ?? null,
    coverUrl: track.coverUrl || null,
    mime: audio.type || "application/octet-stream",
    // The audio only. The cover is a rounding error next to it and counting
    // it would make this figure disagree with the server's own per-item size
    // for no benefit.
    size: audio.size,
    savedAt: Date.now(),
  };

  try {
    await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
      tx.objectStore(BLOB_STORE).put({ id, audio, cover });
      tx.objectStore(META_STORE).put(meta);
    });
  } catch (err) {
    // A half-written pair is worse than nothing: metadata with no audio
    // renders a song that cannot play, and audio with no metadata is
    // invisible to every listing and so can never be deleted from the UI.
    // The transaction above is atomic across both stores, so reaching here
    // means neither landed — but a quota failure can also strike after an
    // earlier successful write, so sweep this id either way.
    await deleteTrack(id).catch(() => {});
    if (err?.name === "QuotaExceededError") {
      throw new Error("No room left on this device. Remove a few saved songs and try again.");
    }
    throw err;
  }

  return meta;
}

/** Forgets one track's bytes and its metadata. */
export async function deleteTrack(contentId) {
  const id = Number(contentId);
  await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
    tx.objectStore(BLOB_STORE).delete(id);
    tx.objectStore(META_STORE).delete(id);
  });
}

/** Forgets everything. */
export async function clearAll() {
  await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
    tx.objectStore(BLOB_STORE).clear();
    tx.objectStore(META_STORE).clear();
  });
}
