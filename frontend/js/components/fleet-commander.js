// Fleet Commander card — extracted from components.js during the R11 split.
// Imports shared helpers from ../utils/html.js and ./_shared.js.

import { escapeHTML, escapeHTMLAttr } from '../utils/html.js';
import { _formatTokenShort, formatHumanDelta, providerDisplayLabel, providerIconUrl } from './_shared.js';
import { formatCost, formatCurrency } from '../utils/format.js';
import { clusterPools, clusterModelLabel } from '../utils/quota.js';

// Forecast lookup key — must match _forecastSeriesKey in dashboard.js.
function _fcForecastKey(card) {
    return [
        card.provider_id || '',
        card.account_id || '',
        card.service_name || '',
        card.variant || '',
        card.model_id || '',
        card.window_type || '',
        card.unit_type || '',
    ].join('||');
}

const _FC_STATUS_COLOR = {
    exhausted: 'var(--crit)',
    near_limit: 'var(--crit)',
    risk: 'var(--crit)',
    warn: 'var(--warn)',
    decelerating: 'var(--info, #4a9eff)',
    ok: 'var(--good)',
    stable: 'var(--good)',
    insufficient_data: 'var(--text-dim)',
    low_resolution: 'var(--text-dim)',
};

export function buildFleetCommanderCard(entry, forecastMap, cumulativeMap) {
    const critical = entry.critical_gauge;
    if (!critical) return '';

    const secondary = entry.secondary_limits || [];
    const contributions = entry.sidecar_contributions || {};
    const allCards = [critical, ...secondary];

    // Quota-bearing cards (with pct_used or limit_value) become "pools".
    // Token-only enrichment cards are kept aside for the per-model strip and totals.
    const quotaCards = allCards.filter(c => _hasQuotaSignal(c));
    const isPayg = critical.is_unlimited || (!critical.limit_value && quotaCards.length === 0);

    const providerId = entry.provider_id || '';
    const accountId = entry.account_id || '';
    const provLabel = providerDisplayLabel(providerId);
    const accountLabel = critical.account_label || accountId || 'default';
    const tier = _firstNonEmpty(allCards.map(c => c.tier));
    const planText = tier || (isPayg ? 'PAYG' : '');

    const cardSidecars = new Set(allCards.map(c => c.sidecar_id).filter(Boolean));
    const sidecarCount = new Set([...Object.keys(contributions), ...cardSidecars]).size;
    const dataSource = critical.data_source || '';
    const inputSource = critical.input_source || '';
    const authorityLabel = _authorityLabel(dataSource, inputSource);

    const cumulative = cumulativeMap?.get?.(`${providerId}|${accountId}`) || null;

    const railHtml = _fcRail(providerId, provLabel, accountLabel, authorityLabel, planText, sidecarCount);
    const isVeloCard = critical.is_unlimited || (!critical.limit_value && critical.pct_used == null);
    const mainHtml = isVeloCard
        ? _fcVelocity(critical, forecastMap)
        : _fcPoolStack(quotaCards, forecastMap);

    const modelStripHtml = _fcModelsStrip(cumulative);
    const fuelDumpHtml = _fcFuelDump(contributions, cumulative);
    const cumeHtml = _fcCume(cumulative, isPayg, providerId);
    const podsHtml = _fcPods(secondary, contributions);

    const cKey = `${providerId}|${accountId}`;

    return `<article class="glass card fc"
            data-prov="${escapeHTMLAttr(providerId)}"
            data-acc="${escapeHTMLAttr(accountId)}"
            data-provider-id="${escapeHTMLAttr(providerId)}"
            data-card-key="${escapeHTMLAttr(cKey)}">
        ${railHtml}
        <div class="fc-main">
            ${mainHtml}
            ${modelStripHtml}
            ${fuelDumpHtml}
        </div>
        ${cumeHtml}
        ${podsHtml}
    </article>`;
}

function _hasQuotaSignal(card) {
    if (card.is_unlimited) return false;
    if (card.pct_used != null) return true;
    if (card.used_value != null && card.limit_value) return true;
    return false;
}

function _firstNonEmpty(values) {
    for (const v of values) if (v) return v;
    return '';
}

function _authorityLabel(dataSource, inputSource) {
    const tokens = new Set(
        (dataSource || '').toLowerCase().split(',').map(s => s.trim()).filter(Boolean)
    );
    const hasApi   = tokens.has('api') || tokens.has('oauth');
    const hasWeb   = tokens.has('web');
    const hasLocal = tokens.has('local');
    if (hasWeb)  return 'WEB · SCRAPED';
    if (hasApi)  return 'AUTHORITATIVE · API';
    if (hasLocal || (inputSource || '').toLowerCase().includes('sidecar')) return 'LOCAL · SIDECAR';
    return 'UNKNOWN';
}

function _fcRail(providerId, provLabel, accountLabel, authorityLabel, planText, sidecarCount) {
    const initial = (provLabel || '?').trim().charAt(0).toUpperCase();
    const provClass = providerId ? `c-${escapeHTMLAttr(providerId)}` : '';
    const iconUrl = providerIconUrl(providerId);
    const planPill = planText
        ? `<span class="pill"><b>${escapeHTML(planText)}</b></span>`
        : '';
    const sidecarPill = `<span class="pill"><b>${sidecarCount}</b>sidecar${sidecarCount === 1 ? '' : 's'}</span>`;
    return `<div class="fc-rail">
        <div class="who">
            <div class="plogo ${provClass}${iconUrl ? ' has-icon' : ''}">${iconUrl ? `<img class="plogo-img" src="${escapeHTMLAttr(iconUrl)}" alt="" loading="lazy" onerror="const p=this.parentElement;p.classList.remove('has-icon');p.innerHTML='${escapeHTMLAttr(initial)}'">` : escapeHTML(initial)}</div>
            <div class="stack">
                <div class="pname">${escapeHTML(provLabel)}</div>
                <div class="pacc">${escapeHTML(accountLabel)}</div>
            </div>
        </div>
        <span class="auth"><span class="d"></span>${escapeHTML(authorityLabel)}</span>
        <div class="meta-row">${planPill}${sidecarPill}</div>
    </div>`;
}

// Session windows aren't always 5h and don't tick on a fixed wall clock —
// Antigravity rotates per-model windows that can be 2-5h depending on first
// use. Show the bare window type for `session`; the live `resets <X>` countdown
// next to the bar carries the accurate per-model time.
const _FC_WINDOW_LABELS = {
    session: 'session',
    daily:   '24h fixed',
    weekly:  '7d fixed',
    monthly: '30d fixed',
    rolling: 'rolling',
};

function _fcWindowLabel(card) {
    const w = (card.window_type || '').toLowerCase();
    return _FC_WINDOW_LABELS[w] || w || 'rolling';
}

function _poolKindAndScope(card) {
    const variant = (card.variant || 'default').toLowerCase();
    const modelId = card.model_id || '';
    if (variant !== 'default' || modelId) {
        const scope = (modelId || variant).toString();
        return { kind: 'model', scope: `${scope.charAt(0).toUpperCase()}${scope.slice(1)} only` };
    }
    return { kind: 'shared', scope: 'All models' };
}

function _poolPct(card) {
    if (card.pct_used != null) return Math.max(0, Math.min(100, Math.round(card.pct_used)));
    if (card.used_value != null && card.limit_value) {
        return Math.max(0, Math.min(100, Math.round((card.used_value / card.limit_value) * 100)));
    }
    return 0;
}

function _glidePathPct(card) {
    if (!card.reset_at || !card.window_type || card.window_type === 'unknown') return null;
    const reset = new Date(card.reset_at);
    const now = new Date();
    const durations = { session: 5*3600000, daily: 86400000, weekly: 604800000, monthly: 2592000000 };
    const windowMs = durations[card.window_type];
    if (!windowMs) return null;
    const elapsed = windowMs - (reset - now);
    return Math.max(0, Math.min(100, (elapsed / windowMs) * 100));
}

function _poolStatus(usedPct) {
    if (usedPct >= 90) return 'crit';
    if (usedPct >= 70) return 'warn';
    return 'good';
}

function _poolLabel(card) {
    const w = (card.window_type || '').toLowerCase();
    const variant = (card.variant || 'default').toLowerCase();
    const wTitle = w ? w.charAt(0).toUpperCase() + w.slice(1) : 'Limit';
    if (variant !== 'default' && variant !== 'unknown') {
        const vLabel = card.model_id || variant;
        return `${wTitle} ${vLabel.charAt(0).toUpperCase()}${vLabel.slice(1)}`;
    }
    return wTitle;
}

function _resetText(card) {
    return card.reset_at ? formatHumanDelta(new Date(card.reset_at)) : '—';
}

/**
 * Merge a cluster of same-quota cards into a single pool descriptor.
 * Singletons pass through unchanged (cluster.length === 1).
 */
function _mergeCluster(cluster) {
    const rep = cluster[0]; // representative card — all share the same quota state
    const isMulti = cluster.length > 1;
    const w = (rep.window_type || '').toLowerCase();
    const wTitle = w ? w.charAt(0).toUpperCase() + w.slice(1) : 'Limit';
    return {
        card:  { ...rep, _clusterCards: cluster },
        used:  _poolPct(rep),
        glide: _glidePathPct(rep),
        label: isMulti ? wTitle : _poolLabel(rep),
        kind:  isMulti ? 'shared' : _poolKindAndScope(rep).kind,
        scope: isMulti ? clusterModelLabel(cluster) : _poolKindAndScope(rep).scope,
    };
}

function _fcPoolStack(quotaCards, forecastMap) {
    const clusters = clusterPools(quotaCards);
    const pools = clusters
        .map(cluster => _mergeCluster(cluster))
        .sort((a, b) => b.used - a.used);

    const head = pools[0];
    const headStatus = _poolStatus(head.used);

    const rows = pools.map((p, i) => {
        const status = _poolStatus(p.used);
        const isHead = i === 0;
        const glideHtml = p.glide != null
            ? `<div class="pglide" style="left:${p.glide.toFixed(1)}%" title="Glide-path target ${Math.round(p.glide)}%: where you should be if pacing usage evenly across this window"></div>`
            : '';
        const glideAhead = p.glide != null && p.used > p.glide + 4;
        const glideBehind = p.glide != null && p.used < p.glide - 4;
        const fc = forecastMap?.get(_fcForecastKey(p.card));
        const forecastHtml = fc?.projected_pct != null
            ? `<div class="pforecast" style="left:${Math.min(fc.projected_pct, 100).toFixed(1)}%" title="Forecast: ${fc.projected_pct.toFixed(1)}% by end of window"></div>`
            : '';
        const fcBadge = (() => {
            if (!fc || fc.projected_pct == null) return '';
            const color = _FC_STATUS_COLOR[fc.status] || 'var(--text-dim)';
            const proj = fc.projected_pct.toFixed(1);
            let hit = '';
            if ((fc.status === 'risk' || fc.status === 'exhausted') && fc.projected_limit_hit_at) {
                const d = new Date(fc.projected_limit_hit_at);
                const dateStr = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
                const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                hit = ` · hits 100% ${dateStr} ${timeStr}`;
            }
            return ` <span style="color:${color};font-weight:600;">→ ${proj}%</span> <span style="color:${color};font-size:9px;letter-spacing:0.08em;">${(fc.status || '').toUpperCase()}</span>${hit ? `<span style="color:var(--text-dim);font-size:9px;">${escapeHTML(hit)}</span>` : ''}`;
        })();
        const glideFootHtml = p.glide == null ? (fcBadge ? `<div class="pfoot">${fcBadge}</div>` : '')
            : `<div class="pfoot">glide-path target <b>${Math.round(p.glide)}%</b>${
                glideAhead ? ` <span class="ahead">↑ ${Math.round(p.used - p.glide)}% ahead of pace</span>`
                : glideBehind ? ` <span class="ontrack">✓ ${Math.round(p.glide - p.used)}% under pace</span>`
                : ` <span class="ontrack">✓ on glide path</span>`
            }${fcBadge}</div>`;
        return `<div class="fc-pool-row h-${status} ${status === 'crit' ? 'crit-row' : ''}">
            <span class="pidx">${String(i + 1).padStart(2, '0')}</span>
            <div class="pmid">
                <div class="phead">
                    <span class="plab">${escapeHTML(p.label)}</span>
                    <span class="pscope">${escapeHTML(p.scope)} · ${escapeHTML(_fcWindowLabel(p.card))}</span>
                    <span class="pbadge ${p.kind}">${p.kind === 'shared' ? 'Shared' : 'Model'}</span>
                </div>
                <div class="pmeter">
                    <div class="pused" style="width:${p.used}%"></div>
                    ${glideHtml}
                    ${forecastHtml}
                </div>
                ${glideFootHtml}
            </div>
            <div class="pright">
                <span class="ppct">${p.used}<em>%</em></span>
                <span class="preset">resets <b>${escapeHTML(_resetText(p.card))}</b></span>
            </div>
        </div>`;
    }).join('');

    return `<div>
        <div class="fc-pools-head">
            <span class="label">Quota pools · ${pools.length}</span>
            <span class="meta">most constraining <b>${escapeHTML(head.label)}</b> · ${head.used}%</span>
        </div>
        <div class="fc-pools-stack">${rows}</div>
    </div>`;
}

function _fcCriticalGauge(card, _forecastMap) {
    if (card.is_unlimited || (!card.limit_value && card.pct_used == null)) {
        return _fcVelocity(card);
    }
    const used = _poolPct(card);
    const glide = _glidePathPct(card);
    const status = _poolStatus(used);
    const ahead = glide != null && used > glide + 4;
    const behind = glide != null && used < glide - 4;
    const usedAbs = card.used_value != null
        ? `${_formatTokenShort(card.used_value)} ${card.unit_type || ''}`
        : `${used}%`;
    const aheadHtml = glide == null ? ''
        : ahead ? `<span class="ahead">↑ ${Math.round(used - glide)}% ahead of pace</span>`
        : behind ? `<span class="ontrack">✓ ${Math.round(glide - used)}% under pace</span>`
        : `<span class="ontrack">✓ on glide path</span>`;
    const glideHtml = glide != null
        ? `<div class="glide" style="left:${glide.toFixed(1)}%" title="ideal pace by elapsed time"></div>`
        : '';
    const gaugeFc = _forecastMap?.get(_fcForecastKey(card));
    const gaugeForecastHtml = gaugeFc?.projected_pct != null
        ? `<div class="forecast" style="left:${Math.min(gaugeFc.projected_pct, 100).toFixed(1)}%" title="Forecast: ${gaugeFc.projected_pct.toFixed(1)}% by end of window"></div>`
        : '';
    const glideTarget = glide != null
        ? `<span>glide-path target <b>${Math.round(glide)}%</b></span>`
        : '';
    return `<div>
        <div class="fc-gauge-head">
            <span class="label">Critical gauge · most restrictive</span>
            <span class="name">${escapeHTML(card.service_name || _poolLabel(card))}</span>
            <span class="reset">resets <b>${escapeHTML(_resetText(card))}</b></span>
        </div>
        <div class="fc-gauge h-${status} ${ahead ? 'behind' : ''}" style="margin-top:8px">
            <div class="ticks"></div>
            <div class="used" style="width:${used}%"></div>
            ${glideHtml}
            ${gaugeForecastHtml}
            <div class="pct-label">${used}%</div>
        </div>
        <div class="fc-gauge-foot" style="margin-top:6px">
            <span><b>${escapeHTML(usedAbs)}</b> used</span>
            ${glideTarget}
            ${aheadHtml}
        </div>
    </div>`;
}

function _fcVelocity(card, forecastMap) {
    const spend = card.used_value != null
        ? formatCurrency(Number(card.used_value), card.currency || 'USD')
        : '—';
    return `<div class="fc-velo">
        <span class="payg-tag">PAYG · No quota</span>
        <div class="cell">
            <div class="k">Current spend</div>
            <div class="v">${escapeHTML(spend)}</div>
            <div class="s">${card.unit_type ? `<b>${escapeHTML(card.unit_type)}</b>` : 'rolling'}</div>
        </div>
        <div class="cell">
            <div class="k">Tokens</div>
            <div class="v">${card.token_usage?.total ? _formatTokenShort(card.token_usage.total) : '—'}</div>
            <div class="s">total</div>
        </div>
        <div class="cell forecast">
            <div class="k">Forecast · EoM</div>
            <div class="v">${(() => {
                const fc = forecastMap?.get(_fcForecastKey(card));
                if (!fc || fc.projected_pct == null) return '—';
                const color = _FC_STATUS_COLOR[fc.status] || 'var(--text-dim)';
                return `<span style="color:${color};">${fc.projected_pct.toFixed(1)}%</span>`;
            })()}</div>
            <div class="s">${(() => {
                const fc = forecastMap?.get(_fcForecastKey(card));
                if (!fc) return 'no quota ceiling';
                return (fc.status || 'no data').toUpperCase();
            })()}</div>
        </div>
    </div>`;
}

const _MODEL_HUES = {
    sonnet: 28, opus: 280, haiku: 200, design: 320,
    gpt: 160, chatgpt: 160, codex: 60,
    gemini: 220, flash: 200, pro: 220, 'flash-lite': 180,
    glm: 200, default: 60,
};

function _modelHue(name) {
    const n = (name || '').toLowerCase();
    for (const k of Object.keys(_MODEL_HUES)) {
        if (n.includes(k)) return _MODEL_HUES[k];
    }
    return _MODEL_HUES.default;
}

// Window ranking: longer windows win as the canonical pool view.
const _WINDOW_RANK = {
    monthly: 5, prepaid: 5, biweekly: 4, weekly: 4, daily: 3, session: 2, rolling: 1, unknown: 0,
};

/**
 * Pick the card that best represents the longest pool window for an account:
 * variant="default", model_id="" (i.e. all-models view), longest window_type, with
 * the most token data. Returns null if no candidate has token_usage.
 */

function _fcModelsStrip(cumulative) {
    // Always use the calendar-month cumulative rollup so the split is
    // independent of the provider's quota reset date.
    const monthKey = cumulative?.current_month_key;
    const monthByModel = cumulative?.[monthKey]?.by_model
        && Object.keys(cumulative[monthKey].by_model).length > 0
        ? cumulative[monthKey].by_model : null;

    if (!monthByModel) return '';

    const entries = Object.entries(monthByModel).map(([name, data]) => ({
        name,
        tokens: (Number(data?.tokens_input ?? 0)
            + Number(data?.tokens_output ?? 0)
            + Number(data?.tokens_cache_read ?? 0)
            + Number(data?.tokens_cache_create ?? 0)
            + Number(data?.tokens_reasoning ?? 0)),
    })).filter(e => e.tokens > 0);
    const periodLabel = 'Per-model contribution · this month';

    if (entries.length === 0) return '';

    const total = entries.reduce((s, e) => s + e.tokens, 0) || 1;
    entries.sort((a, b) => b.tokens - a.tokens);

    const bar = entries.map(e => {
        const share = (e.tokens / total) * 100;
        const hue = _modelHue(e.name);
        return `<i style="flex:${share.toFixed(2)};background:oklch(0.62 0.16 ${hue})"
                   title="${escapeHTMLAttr(e.name)} · ${_formatTokenShort(e.tokens)} · ${share.toFixed(0)}%"></i>`;
    }).join('');

    const list = entries.map(e => {
        const share = Math.round((e.tokens / total) * 100);
        const hue = _modelHue(e.name);
        return `<span class="it">
            <span class="sw" style="background:oklch(0.62 0.16 ${hue})"></span>
            <span class="nm">${escapeHTML(e.name)}</span>
            <span class="tk">${_formatTokenShort(e.tokens)}</span>
            <span class="pc">${share}%</span>
        </span>`;
    }).join('');

    return `<div class="fc-models-strip">
        <div class="h">
            <span class="label">${periodLabel}</span>
            <span class="note">no individual ceiling · feeds pools above</span>
        </div>
        <div class="fc-models-bar">${bar}</div>
        <div class="fc-models-list">${list}</div>
    </div>`;
}

function _fcFuelDump(contributions, cumulative) {
    // Always use calendar-month data so sidecar attribution is independent of
    // the provider's quota reset date. Prefer the cumulative endpoint's
    // by_sidecar; fall back to sidecar_contributions from the fleet endpoint
    // (same underlying query, different fetch path).
    const monthKey = cumulative?.current_month_key;
    const monthBySidecar = cumulative?.[monthKey]?.by_sidecar
        && Object.keys(cumulative[monthKey].by_sidecar).length > 0
        ? cumulative[monthKey].by_sidecar : null;

    const _sumTokenFields = d => (Number(d?.tokens_input ?? 0) + Number(d?.tokens_output ?? 0)
        + Number(d?.tokens_cache_read ?? 0) + Number(d?.tokens_cache_create ?? 0)
        + Number(d?.tokens_reasoning ?? 0));

    const sidecarSource = monthBySidecar
        || (Object.keys(contributions || {}).length ? contributions : null);

    const segs = sidecarSource
        ? Object.entries(sidecarSource)
            .map(([sid, data]) => ({ sid, value: _sumTokenFields(data) }))
            .filter(s => s.value > 0)
            .sort((a, b) => b.value - a.value)
        : [];

    const windowTotal = segs.reduce((s, e) => s + e.value, 0);

    if (!segs.length || !windowTotal) {
        return `<div class="fc-dump">
            <div class="fc-dump-head">
                <span>Fuel-dump · sidecar contribution</span>
                <span class="total">— no sidecar telemetry —</span>
            </div>
        </div>`;
    }

    const totalText = `+${_formatTokenShort(windowTotal)}`;

    const dumpBar = segs.map((s, i) => {
        const pct = Math.max(4, (s.value / windowTotal) * 100);
        const hue = (i * 47 + 60) % 360;
        const short = (s.sid || '').split(/[-.]/)[0];
        return `<i style="flex:${pct.toFixed(2)};background:oklch(0.62 0.15 ${hue} / 0.55);color:oklch(0.95 0.02 ${hue})"
                   title="${escapeHTMLAttr(s.sid)} · ${_formatTokenShort(s.value)}">
            <span style="opacity:.9">${escapeHTML(short)}</span>
        </i>`;
    }).join('');

    return `<div class="fc-dump">
        <div class="fc-dump-head">
            <span>Fuel-dump · sidecar contribution</span>
            <span class="total">${escapeHTML(totalText)}</span>
            <span class="toggle" data-toggle="pods">▾ wingmen</span>
        </div>
        <div class="fc-dump-bar">${dumpBar}</div>
    </div>`;
}

function _bucketTokens(bucket) {
    if (!bucket || typeof bucket !== 'object') return 0;
    if (bucket.tokens_total != null) return Number(bucket.tokens_total);
    let sum = 0;
    for (const [k, v] of Object.entries(bucket)) {
        if (k.startsWith('tokens_') && typeof v === 'number') sum += v;
    }
    return sum;
}

function _bucketCost(bucket) {
    if (!bucket || typeof bucket !== 'object') return 0;
    return Number(bucket.cost_usd ?? 0);
}

function _formatCost(usd) {
    if (!usd) return '';
    if (usd >= 1000) return `$${(usd / 1000).toFixed(1)}K`;
    if (usd >= 100)  return `$${usd.toFixed(0)}`;
    return `$${usd.toFixed(2)}`;
}

function _fcCume(cumulative, _isPayg, providerId) {
    if (!cumulative) {
        const subParts = providerId === 'github'
            ? ['quota-based', 'no usage events']
            : ['awaiting', 'sidecar deltas'];
        return `<div class="fc-cume fc-cume-empty">
            <div class="row">
                <span class="label">No cumulative data</span>
                <span class="sub">
                    <span>${subParts[0]}</span><span class="sub-sep">·</span><span>${subParts[1]}</span>
                </span>
            </div>
        </div>`;
    }

    // Period keys resolved server-side in the user's timezone (stamped onto
    // the entry in dashboard.js) so "This period" rolls over on the local
    // calendar, not at UTC midnight.
    const monthKey = cumulative.current_month_key;
    const yearKey = cumulative.current_year_key;
    const yearLabel = (yearKey || '').replace('year_', '');

    const month = cumulative[monthKey] || {};
    const year = cumulative[yearKey] || {};
    const lifetime = cumulative.lifetime || {};

    const monthTok = _bucketTokens(month);
    const yearTok = _bucketTokens(year);
    const lifeTok = _bucketTokens(lifetime);

    const monthCost = _bucketCost(month);
    const yearCost = _bucketCost(year);
    const lifeCost = _bucketCost(lifetime);

    // Always render the cost sub (with em-dash when missing) so every populated
    // row is the same 3-line height — keeps the tray visually consistent next
    // to providers without cost data (e.g. opencode-free).
    const _costSub = (usd) => escapeHTML(_formatCost(usd) || '—');

    return `<div class="fc-cume">
        <div class="row">
            <span class="label">This period</span>
            <span class="v">${monthTok ? _formatTokenShort(monthTok) : '—'}<em>${monthTok ? 'tok' : ''}</em></span>
            <span class="sub">${_costSub(monthCost)}</span>
        </div>
        <hr/>
        <div class="row">
            <span class="label">Yearly · ${yearLabel}</span>
            <span class="v" style="font-size:16px">${yearTok ? _formatTokenShort(yearTok) : '—'}</span>
            <span class="sub">${_costSub(yearCost)}</span>
        </div>
        <div class="row">
            <span class="label">Lifetime</span>
            <span class="v" style="font-size:16px">${lifeTok ? _formatTokenShort(lifeTok) : '—'}</span>
            <span class="sub">${_costSub(lifeCost)}</span>
        </div>
    </div>`;
}

function _osGlyph(label) {
    const s = (label || '').toLowerCase();
    if (s.includes('mac') || s.includes('darwin')) return '';
    if (s.includes('linux')) return '🐧';
    if (s.includes('win'))   return '⊞';
    if (s.includes('ipad') || s.includes('ios'))    return '◌';
    return '⌂';
}

function _fcPods(secondaryCards, contributions) {
    // Always use calendar-month contributions for per-pod token amounts.
    const sids = Object.keys(contributions || {});
    if (!sids.length) return '';

    const _sumTok = d => (Number(d?.tokens_input ?? 0) + Number(d?.tokens_output ?? 0)
        + Number(d?.tokens_cache_read ?? 0) + Number(d?.tokens_cache_create ?? 0)
        + Number(d?.tokens_reasoning ?? 0));

    const pods = sids.map(sid => {
        const units = contributions[sid] || {};
        const windowed = _sumTok(units);
        const cost = Number(units.cost_usd ?? 0);
        const matching = secondaryCards.find(c => c.sidecar_id === sid);
        const sname = matching?.service_name || sid;

        const deltaText = windowed ? `+${_formatTokenShort(windowed)}` : '—';
        const costText = cost ? formatCost(cost) : '—';

        return `<div class="pod" data-sidecar="${escapeHTMLAttr(sid)}">
            <div class="h">
                <span class="hdot"></span>
                <span class="os">${_osGlyph(sname)}</span>
                <span class="name">${escapeHTML(sid)}</span>
            </div>
            <div class="stats">
                <span class="delta">${escapeHTML(deltaText)}</span>
                <span>cost <b>${escapeHTML(costText)}</b></span>
            </div>
        </div>`;
    }).join('');

    return `<div class="fc-pods">
        <div class="fc-pods-head">
            <span class="t"><b>WINGMEN</b>${sids.length} sidecar${sids.length === 1 ? '' : 's'} feeding this account</span>
            <span class="ln"></span>
            <span class="t" style="color:var(--text-dim)">delta this period · since last push</span>
        </div>
        <div class="fc-pods-grid">${pods}</div>
    </div>`;
}

