/**
 * In-tab alerting for new proposals.
 *
 * A proposal expires in about two minutes. If nobody looks at the tab in that
 * window it dies unread, so the queue needs to be able to interrupt — the
 * whole human-in-the-loop model assumes a human who knows there is something
 * to decide.
 *
 * ## Why this is not Web Push
 *
 * The dashboard is served over plain HTTP on a LAN address, by design (see
 * the LAN-only constraint). Service workers and the Push API require a
 * **secure context** — HTTPS or localhost — so neither can register here, and
 * a service worker that silently fails to install is worse than none: the
 * dashboard would look like it could alert you when the tab is closed, and it
 * cannot.
 *
 * So everything in this file works with no service worker and no permission
 * prompt, and works only while a tab is open:
 *
 * - **Audio** — a short synthesised tone via WebAudio. No asset to fetch, so
 *   nothing to fail against the CSP or a cold cache.
 * - **Favicon badge** — a dot drawn onto a canvas, so a background tab shows
 *   the count in the tab strip.
 * - **Title prefix** — `(2) kalshi-copilot`, which is what actually gets seen
 *   in a narrow tab where the favicon is all that renders.
 *
 * Getting alerts with the tab closed needs TLS on the dashboard first. That
 * is an operator decision (self-signed cert vs. a local CA), not something to
 * paper over here.
 *
 * ## Audio autoplay
 *
 * Browsers refuse to start an AudioContext before the page has been
 * interacted with. That is not a bug to work around — it is why
 * {@link unlockAudio} exists and is wired to the first click. Until then the
 * visual badges still work.
 */

let audioContext: AudioContext | null = null;
let unlocked = false;

/** The count currently shown on the favicon and in the title. */
let badgeCount = 0;
let baseTitle = "";
let baseFavicon: string | null = null;

/**
 * Prepare the AudioContext on a user gesture.
 *
 * Must be called from within a real event handler — creating the context
 * outside one leaves it suspended, and resuming it later is exactly what
 * browsers block.
 */
export function unlockAudio(): void {
  if (unlocked) return;
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctor) return;
    audioContext = new Ctor();
    void audioContext.resume();
    unlocked = true;
  } catch {
    // No audio is survivable; the badges carry the signal on their own.
    unlocked = false;
  }
}

/**
 * Two short tones — a notification, not an alarm.
 *
 * Deliberately quiet and brief. This fires whenever a detector finds
 * something, which on a busy scan can be several times a minute, and an
 * alert that is unpleasant gets muted, which defeats the point.
 */
export function ping(): void {
  if (!audioContext) return;
  const ctx = audioContext;
  const now = ctx.currentTime;

  [880, 1320].forEach((frequency, index) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = frequency;

    const start = now + index * 0.12;
    // Ramped rather than switched: an abrupt gain change clicks.
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(0.08, start + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.1);

    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.12);
  });
}

/**
 * Show `count` pending decisions on the favicon and in the tab title.
 *
 * Passing 0 restores both. Idempotent, so it is safe to call on every poll.
 */
export function setBadge(count: number): void {
  if (count === badgeCount) return;
  badgeCount = count;

  if (!baseTitle) baseTitle = document.title;
  document.title = count > 0 ? `(${count}) ${baseTitle}` : baseTitle;

  drawFavicon(count);
}

const BADGE_LINK_ID = "proposal-badge-favicon";

/**
 * The `<link rel=icon>` to write the badge into, creating it if needed.
 *
 * Captures the original href on the way past. That original may legitimately
 * be the empty string — this app ships no favicon — which is why the "have we
 * looked yet" sentinel is `null` and not `""`. Conflating the two leaves the
 * badge stuck on after the queue empties, since restoring `""` is a no-op and
 * the injected data URI survives.
 */
function faviconLink(): HTMLLinkElement {
  let link = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
  if (baseFavicon === null) baseFavicon = link?.getAttribute("href") ?? "";
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    link.id = BADGE_LINK_ID;
    document.head.appendChild(link);
  }
  return link;
}

/** Draw the count as a dot on a 32px canvas and install it as the favicon. */
function drawFavicon(count: number): void {
  const link = faviconLink();

  if (count <= 0) {
    if (baseFavicon) {
      link.href = baseFavicon;
    } else {
      // There was no favicon before us, so there is nothing to restore to —
      // the only way back to the default is to remove the element we added.
      link.remove();
    }
    return;
  }

  const size = 32;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.fillStyle = "#12161c";
  ctx.fillRect(0, 0, size, size);

  ctx.fillStyle = "#f5a623";
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 2, 0, Math.PI * 2);
  ctx.fill();

  // Past 9 the exact number stops mattering — the queue is capped at
  // max_pending_proposals anyway, and two digits at 32px are unreadable.
  ctx.fillStyle = "#12161c";
  ctx.font = "bold 20px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(count > 9 ? "9+" : String(count), size / 2, size / 2 + 1);

  link.href = canvas.toDataURL("image/png");
}

/**
 * Alert on newly arrived proposals.
 *
 * Takes the ids currently pending and the ids from the previous poll, and
 * returns the next `seen` set. Comparing ids rather than counts matters: one
 * proposal expiring while another arrives leaves the count unchanged, and
 * that is exactly a moment worth a ping.
 *
 * `seen` is `null` on the first poll — not an empty set, which would be
 * indistinguishable from "the queue was empty last time" and would swallow
 * the ping for the first proposal of the session.
 */
export function alertOnNew(
  pendingIds: number[],
  seen: Set<number> | null,
): Set<number> {
  setBadge(pendingIds.length);

  // A page load is not an event worth announcing, however full the queue is.
  if (seen !== null && pendingIds.some((id) => !seen.has(id))) ping();

  return new Set(pendingIds);
}
