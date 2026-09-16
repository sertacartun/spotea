// Keeps tracks' bytes on the device. IndexedDB, not the Cache API: <audio> issues
// Range requests and Cache API keys on URL only, replaying the wrong range.

const DB_NAME = "spotea-offline";
const DB_VERSION = 1;

// Separate stores: IndexedDB materialises whole records, so listing titles from
// a combined store would load every Blob into memory.
const META_STORE = "tracks";
const BLOB_STORE = "blobs";

export function isSupported() {
  return typeof indexedDB !== "undefined";
}

// A device preference, not an account one. Lives here because the player reads
// it too (a prefetched track's bytes are kept when it's on).
const OFFLINE_PREF_KEY = "spotea-offline-playback";

export function offlinePlaybackOn() {
  try {
    return localStorage.getItem(OFFLINE_PREF_KEY) === "1";
  } catch {
    return false;
  }
}

export function rememberOfflinePlayback(on) {
  try {
    if (on) localStorage.setItem(OFFLINE_PREF_KEY, "1");
    else localStorage.removeItem(OFFLINE_PREF_KEY);
  } catch {
    /* Nothing to remember it with; the switch still works for this session. */
  }
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
    // Another tab holds the old DB open; fail rather than hang forever.
    request.onblocked = () => reject(new Error("Offline storage is busy in another tab"));
  }).catch((err) => {
    // Never memoise a rejection, or one transient failure poisons the page.
    dbPromise = null;
    throw err;
  });
  return dbPromise;
}

function promisify(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transact(storeNames, mode, work) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeNames, mode);
        let result;
        // Resolve on commit, not on the request: quota errors surface at commit.
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

/** A refusal is reported, not thrown. */
export async function requestPersistence() {
  if (!navigator.storage?.persist) return false;
  try {
    if (await navigator.storage.persisted()) return true;
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}

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
  // ids from the same read, so the total and the rows it covers can't disagree.
  return { count: records.length, bytes, quota, ids: records.map((record) => record.id) };
}

export async function listSaved() {
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

/** Caller revokes (offerPrefetchedAudio does). */
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

/** Caller revokes. */
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

/** `signal` lets the sync give way to playback. */
export async function saveTrack(contentId, track = {}, { signal } = {}) {
  const id = Number(contentId);

  const res = await fetch(`/content/${id}/stream`, { signal });
  if (!res.ok) throw new Error(`Could not fetch this track (${res.status})`);
  const audio = await res.blob();

  return storeTrack(id, audio, track);
}

/**
 * Keeps bytes the page already holds (the prefetch), so the sync doesn't refetch
 * them. Covers go via same-origin /image-proxy: a cross-origin image Blob is opaque.
 */
export async function storeTrack(contentId, audio, track = {}) {
  const id = Number(contentId);

  // Not given the abort signal: the audio is already down, don't discard it over artwork.
  let cover = null;
  if (track.coverUrl) {
    try {
      const coverRes = await fetch(track.coverUrl);
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
    // Audio only, to match the server's per-item size.
    size: audio.size,
    savedAt: Date.now(),
  };

  try {
    await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
      tx.objectStore(BLOB_STORE).put({ id, audio, cover });
      tx.objectStore(META_STORE).put(meta);
    });
  } catch (err) {
    // A half-written pair can't play or can't be deleted from the UI; sweep this id
    // either way, since a quota failure can follow an earlier successful write.
    await deleteTrack(id).catch(() => {});
    if (err?.name === "QuotaExceededError") {
      throw new Error("No room left on this device. Remove a few saved songs and try again.");
    }
    throw err;
  }

  return meta;
}

export async function deleteTrack(contentId) {
  const id = Number(contentId);
  await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
    tx.objectStore(BLOB_STORE).delete(id);
    tx.objectStore(META_STORE).delete(id);
  });
}

export async function clearAll() {
  await transact([META_STORE, BLOB_STORE], "readwrite", (tx) => {
    tx.objectStore(BLOB_STORE).clear();
    tx.objectStore(META_STORE).clear();
  });
}
