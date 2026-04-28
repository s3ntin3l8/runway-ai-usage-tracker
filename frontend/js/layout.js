/**
 * Pure helpers for applying a user-defined dashboard layout to live card data.
 * No DOM, no network — easy to reason about and to unit-test later.
 */

/**
 * Stable identity of a card within its provider.
 * Tuple = (account_id, service_name, variant, model_id, window_type).
 * @param {object} card - LimitCard dict from /api/v1/usage/limits
 * @returns {string} e.g. "default|Claude||sonnet|weekly"
 */
export function cardKey(card) {
    return [
        card.account_id ?? '',
        card.service_name ?? '',
        card.variant ?? '',
        card.model_id ?? '',
        card.window_type ?? '',
    ].join('|');
}

/**
 * Build an ordered list from a mix of pinned and unpinned items.
 * Pinned items appear first, in the order they appear in `orderKeys`.
 * Unpinned items keep their input order (which callers may have pre-sorted).
 * Keys in `orderKeys` that don't exist in `items` are silently dropped.
 *
 * @template T
 * @param {Array<T>} items
 * @param {(item: T) => string} keyOf
 * @param {Array<string>} orderKeys
 * @returns {Array<T>}
 */
export function applyOrder(items, keyOf, orderKeys) {
    const byKey = new Map(items.map(i => [keyOf(i), i]));
    const pinned = [];
    const seen = new Set();
    for (const k of orderKeys) {
        if (byKey.has(k) && !seen.has(k)) {
            pinned.push(byKey.get(k));
            seen.add(k);
        }
    }
    const unpinned = items.filter(i => !seen.has(keyOf(i)));
    return [...pinned, ...unpinned];
}

/**
 * Read the current provider order from the DOM.
 * @param {HTMLElement} gridEl - container with direct children carrying [data-provider-id]
 * @returns {Array<string>}
 */
export function extractProviderOrder(gridEl) {
    if (!gridEl) return [];
    return [...gridEl.querySelectorAll('[data-provider-id]')].map(
        el => el.dataset.providerId
    );
}

/**
 * Read the current card order for a specific container.
 * @param {HTMLElement} containerEl - container with children carrying [data-card-key]
 * @returns {Array<string>}
 */
export function extractCardOrder(containerEl) {
    if (!containerEl) return [];
    return [...containerEl.querySelectorAll('[data-card-key]')].map(
        el => el.dataset.cardKey
    );
}
