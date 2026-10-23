// Accessible modal dialogs: a stack (Escape closes only the top one), focus
// moved into the dialog on open, Tab kept inside it, and focus returned to
// whatever opened it on close.

const stack = [];   // [{ overlay, returnTo, onClose }]

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(overlay) {
  return Array.from(overlay.querySelectorAll(FOCUSABLE)).filter((n) => n.offsetParent !== null);
}

function isOpen(overlay) {
  return stack.some((m) => m.overlay === overlay);
}

export function openModal(overlay, { onClose } = {}) {
  if (!overlay) return;
  if (isOpen(overlay)) {
    // Re-opening (e.g. a different log): bring it to the top, keep return focus.
    const ix = stack.findIndex((m) => m.overlay === overlay);
    const [entry] = stack.splice(ix, 1);
    entry.onClose = onClose;
    stack.push(entry);
    return;
  }
  stack.push({ overlay, returnTo: document.activeElement, onClose });
  overlay.hidden = false;
  const dialog = overlay.querySelector('[role="dialog"]') || overlay;
  const first = focusables(overlay)[0];
  (first || dialog).focus({ preventScroll: true });
}

export function closeModal(overlay) {
  const ix = stack.findIndex((m) => m.overlay === overlay);
  if (ix < 0) return;
  const [entry] = stack.splice(ix, 1);
  overlay.hidden = true;
  if (entry.onClose) entry.onClose();
  if (entry.returnTo && document.contains(entry.returnTo)) entry.returnTo.focus({ preventScroll: true });
}

function closeTopModal() {
  if (stack.length) closeModal(stack[stack.length - 1].overlay);
}

export function initModals() {
  document.addEventListener("keydown", (e) => {
    if (!stack.length) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeTopModal();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(stack[stack.length - 1].overlay);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  // Clicking the dimmed backdrop (not the dialog) closes that modal.
  document.querySelectorAll(".modal-overlay").forEach((overlay) => {
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(overlay); });
  });
}
