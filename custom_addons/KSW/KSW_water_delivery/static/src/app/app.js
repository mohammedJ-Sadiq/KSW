/* KSW Water Delivery — the driver's screen.
 *
 * Plain modules, no framework, no Odoo web client. The reason is offline: this
 * has to boot from the service worker's cache on a phone with the radio off, at
 * a customer site with no signal, and every kilobyte of framework is a kilobyte
 * that has to be cached correctly for that to work.
 *
 * The shape of the thing:
 *
 *   reference  — clients, their products and the agreed rates, pulled whenever
 *                there is a connection and kept in IndexedDB. This is what
 *                makes the screen work with no network: it is not asking the
 *                server who the clients are, it already knows.
 *   queue      — captures the driver has saved but the server has not yet
 *                acknowledged. Also IndexedDB, because it has to survive the
 *                tab closing, the worker being killed and the phone being
 *                restarted.
 *
 * Two things that are easy to get wrong and are not:
 *
 *   GPS DOES NOT NEED THE NETWORK. The satellites do not care that there is no
 *   signal. What the network provides is assistance data and cell/wifi
 *   positioning, which is why a fix takes 30-90 seconds offline instead of two
 *   seconds — so this uses watchPosition with a long window and lets accuracy
 *   converge, rather than a one-shot with a 15s timeout that would report
 *   failure for a phone that was about to succeed.
 *
 *   THE PRICE ON THIS SCREEN IS DISPLAY ONLY. The note is priced from the live
 *   register at the moment it is issued. A phone holding a three-day-old
 *   snapshot must never be able to invoice a stale rate — and when the two
 *   differ, the server says so on the capture instead of silently picking one.
 */

const APP_VERSION = new URL(import.meta.url).searchParams.get("v") || "dev";
const DB_NAME = "ksw-water";
const DB_VERSION = 1;
const STORE_REF = "reference";
const STORE_QUEUE = "queue";

// Offline, a first fix comes from the satellites alone. Give it room.
const GPS_WINDOW_MS = 90000;
const GPS_GOOD_ENOUGH_M = 25;

// ---------------------------------------------------------------------------
// IndexedDB — small enough to not want a library
// ---------------------------------------------------------------------------
let dbPromise = null;

function openDb() {
    if (!dbPromise) {
        dbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, DB_VERSION);
            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains(STORE_REF)) {
                    db.createObjectStore(STORE_REF, { keyPath: "id" });
                }
                if (!db.objectStoreNames.contains(STORE_QUEUE)) {
                    db.createObjectStore(STORE_QUEUE, { keyPath: "uuid" });
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }
    return dbPromise;
}

async function idb(store, mode, run) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode);
        const request = run(tx.objectStore(store));
        tx.onerror = () => reject(tx.error);
        tx.oncomplete = () => resolve(request && request.result);
    });
}

const dbPut = (store, value) => idb(store, "readwrite", (s) => s.put(value));
const dbGet = (store, key) => idb(store, "readonly", (s) => s.get(key));
const dbAll = (store) => idb(store, "readonly", (s) => s.getAll());
const dbDel = (store, key) => idb(store, "readwrite", (s) => s.delete(key));

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
    reference: null,
    refreshedAt: null,
    queue: [],
    recent: [],
    online: navigator.onLine,
    position: null,
    positionStatus: "idle",
    positionMessage: "",
    watchId: null,
    syncing: false,
    view: "new",
    form: null,
    banner: null,
    // Whether this phone can actually work with no signal. Checked and SHOWN,
    // never assumed: the failure mode that matters is a driver who thinks the
    // app is ready, drives to a site with no coverage and finds out there.
    ready: {
        secure: window.isSecureContext,
        swSupported: "serviceWorker" in navigator,
        swRegistered: false,
        swError: "",
        shellCached: false,
        dataCached: false,
    },
};

function offlineReady() {
    const r = state.ready;
    return r.secure && r.swRegistered && r.shellCached && r.dataCached;
}

function newForm() {
    return {
        partner: null,
        product: null,
        qty: 1,
        uom: "product",
        signedBy: "",
        signature: "",
        note: "",
        showAll: false,
        search: "",
    };
}

function uuid() {
    if (crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------
async function callJson(route, params) {
    const response = await fetch(route, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: params || {} }),
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.error) {
        const data = payload.error.data || {};
        const error = new Error(data.message || payload.error.message || "Server error");
        error.name = data.name || "ServerError";
        throw error;
    }
    return payload.result;
}

async function refreshReference() {
    const reference = await callJson("/water/reference");
    await dbPut(STORE_REF, { id: "current", reference, at: new Date().toISOString() });
    state.reference = reference;
    state.refreshedAt = new Date().toISOString();
    state.ready.dataCached = Boolean(reference.clients && reference.clients.length);
}

async function loadRecent() {
    try {
        state.recent = await callJson("/water/recent", { limit: 20 });
    } catch (error) {
        // Not worth a banner: the recent list is a convenience and the queue
        // is the thing that matters.
    }
}

// ---------------------------------------------------------------------------
// The queue
// ---------------------------------------------------------------------------
async function enqueue(capture) {
    await dbPut(STORE_QUEUE, capture);
    state.queue = await dbAll(STORE_QUEUE);
}

/**
 * Send everything the server has not acknowledged.
 *
 * Every capture carries a uuid generated on the phone at the moment it was
 * saved, and the server is idempotent on it. That is the whole duplicate
 * story: a response lost on a flaky edge of coverage is indistinguishable from
 * a send that never arrived, so the phone resends — and the second attempt
 * finds the first capture instead of issuing a second note for one load.
 *
 * A capture leaves the phone only when the server says it owns it ('settled').
 * That includes a capture the server could not issue, because a record exists
 * there that a dispatcher can see and retry — keeping a copy here that nothing
 * will ever retry is how duplicates get made.
 */
async function flushQueue({ quiet = false } = {}) {
    if (state.syncing || !navigator.onLine) {
        return;
    }
    const pending = await dbAll(STORE_QUEUE);
    if (!pending.length) {
        state.queue = [];
        return;
    }
    state.syncing = true;
    render();
    try {
        // `sent_at` is the phone's clock right now, so the server can tell a
        // capture that sat in the queue for six hours (normal, and the point)
        // from a phone whose clock is simply wrong (not normal, and worth
        // knowing before anyone reads `captured_at` as evidence).
        const sentAt = new Date().toISOString().slice(0, 19).replace("T", " ");
        const { results } = await callJson("/water/sync", {
            captures: pending.map((c) => ({ ...c.payload, sent_at: sentAt })),
        });
        let held = 0;
        let failed = 0;
        for (const result of results) {
            if (result.settled) {
                await dbDel(STORE_QUEUE, result.uuid);
            }
            if (result.status === "held") held += 1;
            if (result.status === "failed" || result.status === "error") failed += 1;
        }
        state.queue = await dbAll(STORE_QUEUE);
        if (!quiet) {
            const sent = results.filter((r) => r.settled).length;
            let message = `${sent} delivery${sent === 1 ? "" : "s"} sent.`;
            if (held) {
                message += ` ${held} waiting for a dispatcher to check the location.`;
            }
            if (failed) {
                message += ` ${failed} could not be issued — the office has been given them.`;
            }
            banner(held || failed ? "warn" : "ok", message);
        }
        await loadRecent();
    } catch (error) {
        if (error.name === "odoo.http.SessionExpiredException") {
            banner("warn", "Your session has expired. Open the app with a connection and sign in — nothing is lost.");
        } else if (!quiet) {
            banner("warn", `Could not send yet: ${error.message}. It stays on the phone.`);
        }
    } finally {
        state.syncing = false;
        render();
    }
}

// ---------------------------------------------------------------------------
// Position
// ---------------------------------------------------------------------------
/**
 * The satellites work with no signal; the network only makes the fix faster.
 * So this keeps watching rather than asking once: offline the first fix is
 * 30-90 seconds and arrives coarse before it arrives good, and a one-shot with
 * a short timeout would report "could not get your location" for a phone that
 * was thirty seconds from a perfect answer.
 */
function startWatching() {
    if (!window.isSecureContext || !navigator.geolocation) {
        state.positionStatus = "unavailable";
        state.positionMessage = "Location needs a secure (https) connection.";
        render();
        return;
    }
    if (state.watchId !== null) {
        navigator.geolocation.clearWatch(state.watchId);
    }
    state.positionStatus = "locating";
    state.positionMessage = "Finding your location…";
    render();

    const started = Date.now();
    state.watchId = navigator.geolocation.watchPosition(
        (position) => {
            const accuracy = position.coords.accuracy || 0;
            state.position = {
                lat: position.coords.latitude,
                lon: position.coords.longitude,
                accuracy,
                at: new Date().toISOString(),
            };
            state.positionStatus = "located";
            state.positionMessage = `Location recorded (±${Math.round(accuracy)} m)`;
            // Stop once it is good enough, or once the window is up with
            // whatever we have. A coarse fix is still worth far more than none.
            if (accuracy && accuracy <= GPS_GOOD_ENOUGH_M) {
                navigator.geolocation.clearWatch(state.watchId);
                state.watchId = null;
            } else if (Date.now() - started > GPS_WINDOW_MS) {
                navigator.geolocation.clearWatch(state.watchId);
                state.watchId = null;
            }
            render();
        },
        (error) => {
            if (error.code === 1) {
                state.positionStatus = "denied";
                state.positionMessage =
                    "Location permission refused. On iPhone check Settings ▸ Privacy & Security ▸ Location Services.";
            } else {
                state.positionStatus = "failed";
                state.positionMessage =
                    "No fix yet. Step out of the cab, away from the tank, and give it a minute.";
            }
            render();
        },
        { enableHighAccuracy: true, timeout: GPS_WINDOW_MS, maximumAge: 0 }
    );
}

const EARTH_RADIUS_M = 6371000;
function distanceM(lat1, lon1, lat2, lon2) {
    const toRad = (d) => (d * Math.PI) / 180;
    const p1 = toRad(lat1);
    const p2 = toRad(lat2);
    const dp = toRad(lat2 - lat1);
    const dl = toRad(lon2 - lon1);
    const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a));
}

/**
 * The same radius rule the server applies, run here as well.
 *
 * Not a duplicate of the guard — the server's is still the one that decides.
 * This one exists because of WHEN it can run: the driver is standing at the
 * gate with the client's coordinates already in his pocket, so if he has the
 * wrong client selected he can fix it now. Told three hours later at the depot,
 * the same information is only good for an apology.
 */
function locationProblem(form) {
    const rules = state.reference && state.reference.rules;
    if (!rules || rules.location_rule !== "enforce" || !form.partner) {
        return null;
    }
    if (!state.position) {
        return "No position yet. This branch issues notes at the client's site.";
    }
    if (!form.partner.lat || !form.partner.lon) {
        return null; // nobody has located this client yet — it stays reachable
    }
    const metres = distanceM(
        state.position.lat, state.position.lon, form.partner.lat, form.partner.lon
    );
    if (metres > (rules.radius_m || 300)) {
        return `You are about ${Math.round(metres)} m from ${form.partner.name}; this branch allows ${rules.radius_m} m.`;
    }
    return null;
}

// ---------------------------------------------------------------------------
// Saving
// ---------------------------------------------------------------------------
async function saveCapture() {
    const form = state.form;
    const reference = state.reference;
    if (!form.partner) {
        return banner("warn", "Pick the client first.");
    }
    if (!form.product) {
        return banner("warn", "Pick what you delivered.");
    }
    if (!(form.qty > 0)) {
        return banner("warn", "The quantity has to be more than zero.");
    }
    if (reference.rules.signature_required && !form.signature) {
        return banner("warn", "The customer has to sign before the note can be issued.");
    }
    if (reference.rules.signature_required && !form.signedBy.trim()) {
        return banner("warn", "Record the name of the person who signed.");
    }

    const problem = locationProblem(form);
    if (problem && !window.confirm(`${problem}\n\nSave anyway? A dispatcher will have to check it before it can be invoiced.`)) {
        return;
    }

    const usual = form.partner.usual;
    const capture = {
        uuid: uuid(),
        savedAt: new Date().toISOString(),
        clientName: form.partner.name,
        payload: {
            uuid: null, // filled below — one uuid, in both places
            partner_id: form.partner.id,
            product_id: form.product.id,
            entered_qty: form.qty,
            entered_uom: form.uom,
            vehicle_id: reference.vehicle ? reference.vehicle.id : false,
            signature: form.signature || false,
            signed_by: form.signedBy.trim() || false,
            signature_origin: "on_site",
            note: form.note.trim() || false,
            off_route: !usual,
            gps_latitude: state.position ? state.position.lat : 0,
            gps_longitude: state.position ? state.position.lon : 0,
            gps_accuracy: state.position ? state.position.accuracy : 0,
            // The phone's own clock. The server records its own arrival time
            // separately and keeps the gap, because offline capture moves this
            // one under the driver's control.
            captured_at: new Date().toISOString().slice(0, 19).replace("T", " "),
            cached_price: form.product.price,
            app_version: APP_VERSION,
        },
    };
    capture.payload.uuid = capture.uuid;

    await enqueue(capture);
    state.form = newForm();
    state.view = "queue";
    banner("ok", navigator.onLine ? "Saved. Sending…" : "Saved on the phone. It will send itself when you have signal.");
    render();
    flushQueue({ quiet: true });
}

// ---------------------------------------------------------------------------
// Can this phone actually work offline?
// ---------------------------------------------------------------------------
/**
 * Registering the worker is the ONE thing that makes offline possible, so its
 * failure is reported rather than swallowed.
 *
 * Two ways it fails that look identical to a driver and are completely
 * different to fix:
 *
 *  - The page is on a plain-http address. `navigator.serviceWorker` is not
 *    merely blocked, it does not exist, so there is no error to catch — the
 *    code just quietly skips. This is what an ordinary LAN address like
 *    http://192.168.1.207:8070 does.
 *  - The page is on https but the certificate is not trusted on THIS device.
 *    `isSecureContext` is true (the origin is https) and geolocation works
 *    after tapping through the warning — but the browser still refuses to
 *    register a worker on an origin with a certificate error. Clicking through
 *    the interstitial does not lift it.
 */
async function registerWorker() {
    const r = state.ready;
    r.secure = window.isSecureContext;
    r.swSupported = "serviceWorker" in navigator;
    if (!r.secure || !r.swSupported) {
        return;
    }
    try {
        await navigator.serviceWorker.register("/water/sw.js", { scope: "/water/" });
        // `.ready` never resolves if the worker fails to activate, so it is
        // raced rather than awaited: boot must not hang behind it. Not being
        // ready yet is a legitimate state the readiness panel can report --
        // silently never finishing booting is not.
        await Promise.race([
            navigator.serviceWorker.ready,
            new Promise((resolve) => setTimeout(resolve, 8000)),
        ]);
        r.swRegistered = Boolean(navigator.serviceWorker.controller)
            || Boolean(await navigator.serviceWorker.getRegistration("/water/"));
        r.swError = "";
    } catch (error) {
        r.swRegistered = false;
        r.swError = error.message || String(error);
    }
}

/**
 * Asks the caches and the database what is actually there, rather than
 * trusting that an earlier step succeeded. The shell precache adds each file
 * individually and tolerates a failure, so "the worker registered" does not by
 * itself mean "the app will start with the radio off".
 */
async function checkReadiness() {
    const r = state.ready;
    try {
        const names = await caches.keys();
        const shell = names.find((n) => n.startsWith("ksw-water-shell-"));
        if (shell) {
            const cache = await caches.open(shell);
            r.shellCached = Boolean(await cache.match("/water/"));
        } else {
            r.shellCached = false;
        }
    } catch (error) {
        r.shellCached = false;
    }
    r.dataCached = Boolean(state.reference && state.reference.clients.length);
    render();
}

function readinessMessage() {
    const r = state.ready;
    if (!r.secure) {
        // Built from the address the driver is actually on, not a hardcoded
        // hostname: the dev box publishes an mDNS name that Android Chrome
        // often cannot resolve, so whatever he typed to get here is the thing
        // that works for him. Only the scheme and the port are wrong.
        return {
            kind: "bad",
            title: "This address cannot work offline.",
            detail:
                "Offline storage needs a secure (https) address. Open " +
                `https://${location.hostname}:8443/water/ instead, ` +
                "then add THAT to your home screen.",
        };
    }
    if (!r.swSupported) {
        return {
            kind: "bad",
            title: "This browser cannot work offline.",
            detail: "Use Chrome on Android or Safari on iPhone.",
        };
    }
    if (!r.swRegistered) {
        return {
            kind: "bad",
            title: "Offline storage was refused by the browser.",
            detail:
                "This usually means the site certificate is not trusted on this " +
                "phone. Install and trust the KSW Dev Root CA, then reopen the app. " +
                (r.swError ? `(${r.swError})` : ""),
        };
    }
    if (!r.shellCached) {
        return {
            kind: "warn",
            title: "Still preparing for offline.",
            detail: "Stay on a connection for a few more seconds, then reopen the app.",
        };
    }
    if (!r.dataCached) {
        return {
            kind: "warn",
            title: "Your client list has not loaded.",
            detail: "Tap Prepare for offline while you still have a connection.",
        };
    }
    return {
        kind: "good",
        title: "Ready to work offline.",
        detail: `${state.reference.clients.length} clients stored on this phone.`,
    };
}

async function prepareForOffline() {
    if (!navigator.onLine) {
        return banner("warn", "You need a connection to prepare for offline.");
    }
    banner("ok", "Preparing…");
    await registerWorker();
    try {
        await refreshReference();
    } catch (error) {
        banner("warn", `Could not load your clients: ${error.message}`);
    }
    await checkReadiness();
    banner(offlineReady() ? "ok" : "warn", readinessMessage().title);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
let bannerTimer = null;
function banner(kind, message) {
    state.banner = { kind, message };
    render();
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => {
        state.banner = null;
        render();
    }, 6000);
}

const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

function visibleClients() {
    const reference = state.reference;
    if (!reference) {
        return [];
    }
    const form = state.form;
    const needle = form.search.trim().toLowerCase();
    return reference.clients
        .filter((c) => (form.showAll || c.usual) && (!needle || c.name.toLowerCase().includes(needle)))
        .slice(0, 60);
}

function render() {
    const root = document.getElementById("ksw-app");
    if (!state.reference) {
        root.innerHTML = `
            <div class="ksw-boot">
                <h1>Water Delivery</h1>
                <p>${state.online
                    ? "Loading your clients…"
                    : "This phone has never loaded its client list. Open the app once with a connection."}</p>
                <div class="ksw-ready ksw-ready-${esc(readinessMessage().kind)}">
                    <strong>${esc(readinessMessage().title)}</strong>
                    <div>${esc(readinessMessage().detail)}</div>
                </div>
            </div>`;
        return;
    }
    root.innerHTML = `
        ${renderTop()}
        ${state.banner ? `<div class="ksw-banner ksw-${esc(state.banner.kind)}">${esc(state.banner.message)}</div>` : ""}
        ${renderReadiness()}
        <main class="ksw-main">${state.view === "new" ? renderForm() : renderQueue()}</main>
        ${renderTabs()}`;
    wire();
}

/**
 * Always on screen, not tucked into a settings page. A driver has to be able to
 * see at the depot whether this phone will work at the next site, because that
 * is the only place he can still do something about it.
 */
function renderReadiness() {
    const message = readinessMessage();
    if (message.kind === "good") {
        return `<div class="ksw-ready ksw-ready-good">
            <span>&#10003; ${esc(message.title)} ${esc(message.detail)}</span>
        </div>`;
    }
    return `<div class="ksw-ready ksw-ready-${esc(message.kind)}">
        <strong>${esc(message.title)}</strong>
        <div>${esc(message.detail)}</div>
        <button id="ksw-prepare" class="ksw-secondary">Prepare for offline</button>
    </div>`;
}

function renderTop() {
    const reference = state.reference;
    const queued = state.queue.length;
    return `
        <header class="ksw-top">
            <div class="ksw-top-row">
                <strong>${esc(reference.driver.name)}</strong>
                <span class="ksw-chip ${state.online ? "ksw-on" : "ksw-off"}">
                    ${state.online ? "Online" : "Offline"}
                </span>
            </div>
            <div class="ksw-top-row ksw-sub">
                <span>${esc(reference.vehicle ? reference.vehicle.name : "No tanker assigned")}</span>
                ${queued ? `<span class="ksw-chip ksw-warnchip">${queued} waiting to send</span>` : ""}
            </div>
        </header>`;
}

function renderTabs() {
    const queued = state.queue.length;
    return `
        <nav class="ksw-tabs">
            <button data-view="new" class="${state.view === "new" ? "active" : ""}">New Delivery</button>
            <button data-view="queue" class="${state.view === "queue" ? "active" : ""}">
                Sent &amp; Waiting${queued ? ` (${queued})` : ""}
            </button>
        </nav>`;
}

function renderForm() {
    const form = state.form;
    const rules = state.reference.rules;
    const problem = locationProblem(form);
    const clients = visibleClients();

    return `
        <section class="ksw-gps ksw-gps-${esc(state.positionStatus)}">
            <div>${esc(state.positionMessage || "")}</div>
            ${state.positionStatus !== "located"
                ? `<button id="ksw-locate" class="ksw-secondary">Record my location</button>`
                : ""}
        </section>

        <label class="ksw-label">Client</label>
        ${form.partner
            ? `<div class="ksw-picked">
                    <span>${esc(form.partner.name)}</span>
                    <button id="ksw-clear-client" class="ksw-link">change</button>
               </div>`
            : `<input id="ksw-search" class="ksw-input" placeholder="Type a client name"
                      value="${esc(form.search)}" autocomplete="off"/>
               <ul class="ksw-list">
                   ${clients.map((c) => `
                       <li><button data-client="${c.id}" class="${c.usual ? "" : "ksw-off-route"}">
                           ${esc(c.name)}${c.usual ? "" : " · off your usual route"}
                       </button></li>`).join("")}
                   ${clients.length ? "" : `<li class="ksw-empty">No match.</li>`}
               </ul>
               <label class="ksw-check">
                   <input type="checkbox" id="ksw-show-all" ${form.showAll ? "checked" : ""}/>
                   Client not listed — show every priced client
               </label>`}

        ${problem ? `<div class="ksw-warnbox">${esc(problem)} It will be issued and held for a dispatcher.</div>` : ""}

        ${form.partner ? renderProduct() : ""}

        ${form.partner && rules.signature_required ? `
            <label class="ksw-label">Signed by</label>
            <input id="ksw-signed-by" class="ksw-input" value="${esc(form.signedBy)}"
                   placeholder="Name of the person signing"/>
            <label class="ksw-label">Signature</label>
            <canvas id="ksw-sign" class="ksw-sign"></canvas>
            <button id="ksw-clear-sign" class="ksw-link">clear signature</button>` : ""}

        ${form.partner ? `
            <label class="ksw-label">Remark</label>
            <input id="ksw-note" class="ksw-input" value="${esc(form.note)}"
                   placeholder="Anything the client should see"/>
            <button id="ksw-save" class="ksw-primary">Save Delivery</button>` : ""}`;
}

function renderProduct() {
    const form = state.form;
    const products = form.partner.products;
    const capacity = state.reference.vehicle ? state.reference.vehicle.capacity_m3 : 0;
    const canTrips = capacity > 0 && form.product && form.product.uom === "m³";
    const delivering = form.uom === "trip" ? form.qty * capacity : form.qty;

    return `
        <label class="ksw-label">Product</label>
        ${products.length === 1
            ? `<div class="ksw-picked ksw-locked"><span>${esc(products[0].name)}</span></div>`
            : `<select id="ksw-product" class="ksw-input">
                   <option value="">Choose…</option>
                   ${products.map((p) => `
                       <option value="${p.id}" ${form.product && form.product.id === p.id ? "selected" : ""}>
                           ${esc(p.name)}
                       </option>`).join("")}
               </select>`}

        <label class="ksw-label">Quantity</label>
        <div class="ksw-row">
            <input id="ksw-qty" class="ksw-input" type="number" inputmode="decimal"
                   step="0.01" min="0" value="${esc(form.qty)}"/>
            ${canTrips
                ? `<select id="ksw-uom" class="ksw-input ksw-narrow">
                       <option value="product" ${form.uom === "product" ? "selected" : ""}>m³</option>
                       <option value="trip" ${form.uom === "trip" ? "selected" : ""}>Trips</option>
                   </select>`
                : ""}
        </div>
        ${form.product ? `
            <div class="ksw-hint">
                ${form.uom === "trip"
                    ? `Delivering ${esc(delivering)} ${esc(form.product.uom)} (${esc(capacity)} per trip). `
                    : ""}
                Agreed rate ${esc(form.product.price)} / ${esc(form.product.uom)} —
                the office prices the note from today's register.
            </div>` : ""}`;
}

function renderQueue() {
    const queue = state.queue;
    const recent = state.recent;
    return `
        ${queue.length ? `
            <h2 class="ksw-h2">Waiting to send</h2>
            <ul class="ksw-cards">
                ${queue.map((c) => `
                    <li class="ksw-card ksw-pending">
                        <strong>${esc(c.clientName)}</strong>
                        <span>${esc(c.payload.entered_qty)} ${c.payload.entered_uom === "trip" ? "trip(s)" : ""}</span>
                        <span class="ksw-sub">Saved ${esc(c.savedAt.slice(0, 16).replace("T", " "))}</span>
                    </li>`).join("")}
            </ul>
            <button id="ksw-sync" class="ksw-primary" ${state.online ? "" : "disabled"}>
                ${state.syncing ? "Sending…" : state.online ? "Send now" : "No signal yet"}
            </button>` : `<p class="ksw-empty">Nothing waiting to send.</p>`}

        <h2 class="ksw-h2">Recently issued</h2>
        <ul class="ksw-cards">
            ${recent.length ? recent.map((n) => `
                <li class="ksw-card ${n.held ? "ksw-held" : ""}">
                    <strong>${esc(n.name)}</strong>
                    <span>${esc(n.client)}</span>
                    <span class="ksw-sub">
                        ${esc(n.qty)} · ${esc(n.total)}
                        ${n.offline ? " · offline" : ""}
                        ${n.held ? " · waiting for a dispatcher" : ""}
                    </span>
                </li>`).join("") : `<li class="ksw-empty">Nothing yet.</li>`}
        </ul>`;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function on(id, event, handler) {
    const element = document.getElementById(id);
    if (element) {
        element.addEventListener(event, handler);
    }
}

function wire() {
    document.querySelectorAll("[data-view]").forEach((button) =>
        button.addEventListener("click", () => {
            state.view = button.dataset.view;
            render();
        })
    );
    document.querySelectorAll("[data-client]").forEach((button) =>
        button.addEventListener("click", () => {
            const id = Number(button.dataset.client);
            const client = state.reference.clients.find((c) => c.id === id);
            state.form.partner = client;
            // One product is a question with one answer: choose it rather than
            // offering it. This is the same rule the Odoo wizard follows.
            state.form.product = client.products.length === 1 ? client.products[0] : null;
            state.form.signedBy = state.form.signedBy || client.name;
            render();
        })
    );

    on("ksw-search", "input", (event) => {
        state.form.search = event.target.value;
        const active = document.activeElement === event.target;
        render();
        if (active) {
            const input = document.getElementById("ksw-search");
            input.focus();
            input.setSelectionRange(input.value.length, input.value.length);
        }
    });
    on("ksw-show-all", "change", (event) => {
        state.form.showAll = event.target.checked;
        render();
    });
    on("ksw-clear-client", "click", () => {
        state.form = newForm();
        render();
    });
    on("ksw-product", "change", (event) => {
        const id = Number(event.target.value);
        state.form.product = state.form.partner.products.find((p) => p.id === id) || null;
        render();
    });
    on("ksw-qty", "input", (event) => {
        state.form.qty = parseFloat(event.target.value) || 0;
    });
    on("ksw-uom", "change", (event) => {
        state.form.uom = event.target.value;
        render();
    });
    on("ksw-signed-by", "input", (event) => {
        state.form.signedBy = event.target.value;
    });
    on("ksw-note", "input", (event) => {
        state.form.note = event.target.value;
    });
    on("ksw-save", "click", saveCapture);
    on("ksw-sync", "click", () => flushQueue());
    on("ksw-locate", "click", startWatching);
    on("ksw-prepare", "click", prepareForOffline);
    on("ksw-clear-sign", "click", () => {
        state.form.signature = "";
        render();
    });
    wireSignature();
}

function wireSignature() {
    const canvas = document.getElementById("ksw-sign");
    if (!canvas) {
        return;
    }
    const ratio = window.devicePixelRatio || 1;
    canvas.width = canvas.offsetWidth * ratio;
    canvas.height = 180 * ratio;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#111";
    if (state.form.signature) {
        const image = new Image();
        image.onload = () => ctx.drawImage(image, 0, 0, canvas.offsetWidth, 180);
        image.src = `data:image/png;base64,${state.form.signature}`;
    }

    let drawing = false;
    const point = (event) => {
        const rect = canvas.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    canvas.addEventListener("pointerdown", (event) => {
        drawing = true;
        canvas.setPointerCapture(event.pointerId);
        const p = point(event);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
    });
    canvas.addEventListener("pointermove", (event) => {
        if (!drawing) return;
        event.preventDefault();
        const p = point(event);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
    });
    const stop = () => {
        if (!drawing) return;
        drawing = false;
        state.form.signature = canvas.toDataURL("image/png").split(",")[1];
    };
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointerleave", stop);
    canvas.addEventListener("pointercancel", stop);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
    state.form = newForm();
    const stored = await dbGet(STORE_REF, "current");
    if (stored) {
        state.reference = stored.reference;
        state.refreshedAt = stored.at;
    }
    state.queue = await dbAll(STORE_QUEUE);
    render();

    // Not awaited: the driver gets a usable screen immediately and the
    // readiness panel fills itself in. A slow or stuck worker must never be
    // the reason the app does not appear.
    registerWorker().then(checkReadiness);

    window.addEventListener("online", () => {
        state.online = true;
        render();
        // Refresh what the phone knows BEFORE flushing: a rate that changed
        // while he was out of coverage should be on the screen the next time
        // he uses it, not a surprise on the note.
        refreshReference().catch(() => {});
        flushQueue({ quiet: false });
    });
    window.addEventListener("offline", () => {
        state.online = false;
        render();
    });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            // iOS has no Background Sync, so reopening the app IS the sync
            // trigger. Cheap enough to do unconditionally.
            flushQueue({ quiet: true });
        }
    });

    startWatching();

    if (navigator.onLine) {
        try {
            await refreshReference();
        } catch (error) {
            if (!state.reference) {
                banner("warn", `Could not load your clients: ${error.message}`);
            }
        }
        render();
        await flushQueue({ quiet: true });
        await loadRecent();
        render();
    }
}

boot();
