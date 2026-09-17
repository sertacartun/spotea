// Keeps tracks' bytes on the device. IndexedDB, not the Cache API: <audio> issues
// Range requests and Cache API keys on URL only, replaying the wrong range.

const DB_NAME = "spotea-offline";
const DB_VERSION = 2;

// Separate stores: IndexedDB materialises whole records, so listing titles from
// a combined store would load every Blob into memory.
const META_STORE = "tracks";
const BLOB_STORE = "blobs";

// Lists this device keeps ("favorites", "playlist:12"). A track record's `lists` names the
// ones holding it; a record without `lists` predates them and is only removed by hand.
const LISTS_STORE = "lists";

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
      if (!db.objectStoreNames.contains(LISTS_STORE)) db.createObjectStore(LISTS_STORE, { keyPath: "key" });
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

export async function savedTrackIds() {
  return (await listSaved()).map((record) => record.id);
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

/**
 * `signal` lets the sync give way to playback. Covers go via same-origin /image-proxy:
 * a cross-origin image Blob is opaque.
 */
export async function saveTrack(contentId, track = {}, { signal, list } = {}) {
  const id = Number(contentId);

  const res = await fetch(`/content/${id}/stream`, { signal });
  if (!res.ok) throw new Error(`Could not fetch this track (${res.status})`);
  const audio = await res.blob();

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
    lists: [list],
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

export async function keptLists() {
  if (!isSupported()) return [];
  try {
    return await transact(LISTS_STORE, "readonly", (tx) => promisify(tx.objectStore(LISTS_STORE).getAll()));
  } catch {
    return [];
  }
}

/**
 * `ids` (with the `videoIds` they were made from) only for lists the server can't name,
 * like an album: the device remembers what the list was when it was kept.
 */
export async function keepList(key, title, { ids = null, videoIds = null } = {}) {
  await transact(LISTS_STORE, "readwrite", (tx) => {
    tx.objectStore(LISTS_STORE).put({ key, title, ids, videoIds, keptAt: Date.now() });
  });
}

/**
 * Matches the device to a list's current tracks in one transaction: claims copies it
 * now holds, releases the rest, and deletes copies no kept list holds any more.
 * `ids` null means the list itself is gone (or unkept), so every copy is released.
 * Resolves to the ids of this list's tracks already on the device.
 */
export async function reconcileList(key, ids) {
  const wanted = ids ? new Set(ids.map(Number)) : new Set();
  return transact([META_STORE, BLOB_STORE, LISTS_STORE], "readwrite", async (tx) => {
    const metaStore = tx.objectStore(META_STORE);
    const records = await promisify(metaStore.getAll());
    const present = [];
    for (const record of records) {
      const holds = Array.isArray(record.lists) ? record.lists : null;
      if (wanted.has(record.id)) {
        present.push(record.id);
        if (!holds?.includes(key)) metaStore.put({ ...record, lists: [...(holds || []), key] });
        continue;
      }
      if (!holds?.includes(key)) continue;
      const rest = holds.filter((held) => held !== key);
      if (rest.length) {
        metaStore.put({ ...record, lists: rest });
      } else {
        metaStore.delete(record.id);
        tx.objectStore(BLOB_STORE).delete(record.id);
      }
    }
    if (!ids) tx.objectStore(LISTS_STORE).delete(key);
    return present;
  });
}
