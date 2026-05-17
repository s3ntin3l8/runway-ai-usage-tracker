// Fleet Commander card — extracted from components.js during the R11 split.
// Imports shared helpers from ../utils/html.js and ./_shared.js.

import { escapeHTML, escapeHTMLAttr } from '../utils/html.js';
import { _formatTokenShort, formatHumanDelta, providerDisplayLabel } from './_shared.js';

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
        ? _fcVelocity(critical)
        : _fcPoolStack(quotaCards, forecastMap);

    // Per-model and fuel-dump: prefer server-aggregated window_aggregations.longest
    // which covers the provider's actual quota window (weekly, daily, etc.).
    const winAgg = entry.window_aggregations?.longest || null;

    // Per-model and fuel-dump source from the longest-window pool's default card —
    // the canonical "all-models" view for that account (e.g. weekly/default for Claude).
    const primaryCard = _pickPrimaryPoolCard(allCards) || critical;
    const modelStripHtml = _fcModelsStrip(primaryCard, winAgg, cumulative);
    const fuelDumpHtml = _fcFuelDump(primaryCard, contributions, winAgg, cumulative);
    const cumeHtml = _fcCume(cumulative, isPayg, providerId);
    const podsHtml = _fcPods(secondary, contributions, primaryCard, winAgg);

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
    const planPill = planText
        ? `<span class="pill"><b>${escapeHTML(planText)}</b></span>`
        : '';
    const sidecarPill = `<span class="pill"><b>${sidecarCount}</b>sidecar${sidecarCount === 1 ? '' : 's'}</span>`;
    return `<div class="fc-rail">
        <div class="who">
            <div class="plogo ${provClass}">${escapeHTML(initial)}</div>
            <div class="stack">
                <div class="pname">${escapeHTML(provLabel)}</div>
                <div class="pacc">${escapeHTML(accountLabel)}</div>
            </div>
        </div>
        <span class="auth"><span class="d"></span>${escapeHTML(authorityLabel)}</span>
        <div class="meta-row">${planPill}${sidecarPill}</div>
    </div>`;
}

const _FC_WINDOW_LABELS = {
    session: '5h rolling',
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

function _fcPoolStack(quotaCards, _forecastMap) {
    const pools = quotaCards.map(card => ({
        card,
        used: _poolPct(card),
        glide: _glidePathPct(card),
        label: _poolLabel(card),
        ...(_poolKindAndScope(card)),
    })).sort((a, b) => b.used - a.used);

    const head = pools[0];
    const headStatus = _poolStatus(head.used);

    const rows = pools.map((p, i) => {
        const status = _poolStatus(p.used);
        const isHead = i === 0;
        const glideHtml = p.glide != null
            ? `<div class="pglide" style="left:${p.glide.toFixed(1)}%" title="glide-path target ${Math.round(p.glide)}%"></div>`
            : '';
        const glideAhead = p.glide != null && p.used > p.glide + 4;
        const glideBehind = p.glide != null && p.used < p.glide - 4;
        const glideFootHtml = p.glide == null ? ''
            : `<div class="pfoot">glide-path target <b>${Math.round(p.glide)}%</b>${
                glideAhead ? ` <span class="ahead">↑ ${Math.round(p.used - p.glide)}% ahead of pace</span>`
                : glideBehind ? ` <span class="ontrack">✓ ${Math.round(p.glide - p.used)}% under pace</span>`
                : ` <span class="ontrack">✓ on glide path</span>`
            }</div>`;
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
            <div class="pct-label">${used}%</div>
        </div>
        <div class="fc-gauge-foot" style="margin-top:6px">
            <span><b>${escapeHTML(usedAbs)}</b> used</span>
            ${glideTarget}
            ${aheadHtml}
        </div>
    </div>`;
}

function _fcVelocity(card) {
    const spend = card.used_value != null
        ? `$${Number(card.used_value).toFixed(2)}`
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
            <div class="v">—</div>
            <div class="s">no quota ceiling</div>
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
function _pickPrimaryPoolCard(allCards) {
    const candidates = allCards.filter(c => {
        const v = (c.variant || 'default').toLowerCase();
        const m = (c.model_id || '').toLowerCase();
        return v === 'default' && (!m || m === 'default');
    });
    if (!candidates.length) return null;
    const score = c => {
        const w = (c.window_type || '').toLowerCase();
        const total = Number(c.token_usage?.total ?? 0);
        const hasModels = c.by_model && Object.keys(c.by_model).length > 0 ? 1 : 0;
        return [(_WINDOW_RANK[w] ?? 0), hasModels, total];
    };
    let best = candidates[0];
    let bestScore = score(best);
    for (const c of candidates.slice(1)) {
        const s = score(c);
        for (let i = 0; i < s.length; i++) {
            if (s[i] > bestScore[i]) { best = c; bestScore = s; break; }
            if (s[i] < bestScore[i]) break;
        }
    }
    return best;
}

function _fcModelsStrip(primaryCard, winAgg, cumulative) {
    // Source order: calendar-month rollup → window aggregation → card's by_model.
    // Calendar-month aligns with the modal's "Model mix · this month" view and
    // doesn't go nearly empty when a provider's billing window has just reset
    // (e.g. OpenCode monthly).
    const monthKey = `month_${new Date().toISOString().slice(0, 7)}`;
    const monthByModel = cumulative?.[monthKey]?.by_model
        && Object.keys(cumulative[monthKey].by_model).length > 0
        ? cumulative[monthKey].by_model : null;
    const winByModel = winAgg?.by_model && Object.keys(winAgg.by_model).length > 0
        ? winAgg.by_model : null;

    let entries;
    let periodLabel;
    if (monthByModel) {
        entries = Object.entries(monthByModel).map(([name, data]) => ({
            name,
            tokens: (Number(data?.tokens_input ?? 0)
                + Number(data?.tokens_output ?? 0)
                + Number(data?.tokens_cache_read ?? 0)
                + Number(data?.tokens_cache_create ?? 0)
                + Number(data?.tokens_reasoning ?? 0)),
        })).filter(e => e.tokens > 0);
        periodLabel = 'Per-model contribution · this month';
    } else if (winByModel) {
        entries = Object.entries(winByModel).map(([name, data]) => ({
            name,
            tokens: (Number(data?.tokens_input ?? 0)
                + Number(data?.tokens_output ?? 0)
                + Number(data?.tokens_cache_read ?? 0)
                + Number(data?.tokens_cache_create ?? 0)
                + Number(data?.tokens_reasoning ?? 0)),
        })).filter(e => e.tokens > 0);
        periodLabel = winAgg?.window_type
            ? `Per-model contribution · this ${winAgg.window_type}`
            : 'Per-model contribution';
    } else {
        const bm = primaryCard?.by_model || {};
        entries = Object.entries(bm).map(([name, data]) => ({
            name,
            tokens: Number(data?.tokens?.total ?? data?.tokens ?? data?.total ?? 0),
        })).filter(e => e.tokens > 0);
        periodLabel = 'Per-model contribution';
    }

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

function _fcFuelDump(primaryCard, contributions, winAgg, cumulative) {
    // Source order matches _fcModelsStrip: calendar-month → window aggregation → contributions.
    const monthKey = `month_${new Date().toISOString().slice(0, 7)}`;
    const monthBySidecar = cumulative?.[monthKey]?.by_sidecar
        && Object.keys(cumulative[monthKey].by_sidecar).length > 0
        ? cumulative[monthKey].by_sidecar : null;
    const monthTotal = monthBySidecar
        ? Number(cumulative[monthKey]?.tokens_input ?? 0)
            + Number(cumulative[monthKey]?.tokens_output ?? 0)
            + Number(cumulative[monthKey]?.tokens_cache_read ?? 0)
            + Number(cumulative[monthKey]?.tokens_cache_create ?? 0)
            + Number(cumulative[monthKey]?.tokens_reasoning ?? 0)
        : 0;
    const winByAgg = winAgg?.by_sidecar && Object.keys(winAgg.by_sidecar).length > 0
        ? winAgg.by_sidecar : null;

    const windowTotal = monthBySidecar
        ? monthTotal
        : winAgg?.token_usage?.total != null
            ? Number(winAgg.token_usage.total)
            : Number(primaryCard?.token_usage?.total ?? 0);

    const _sumTokenFields = d => (Number(d?.tokens_input ?? 0) + Number(d?.tokens_output ?? 0)
        + Number(d?.tokens_cache_read ?? 0) + Number(d?.tokens_cache_create ?? 0)
        + Number(d?.tokens_reasoning ?? 0));

    let segs;
    if (monthBySidecar) {
        segs = Object.entries(monthBySidecar)
            .map(([sid, data]) => ({ sid, value: _sumTokenFields(data) }))
            .filter(s => s.value > 0)
            .sort((a, b) => b.value - a.value);
    } else if (winByAgg) {
        segs = Object.entries(winByAgg)
            .map(([sid, data]) => ({ sid, value: _sumTokenFields(data) }))
            .sort((a, b) => b.value - a.value);
    } else {
        const entries = Object.entries(contributions || {});
        if (!entries.length || !windowTotal) {
            return `<div class="fc-dump">
                <div class="fc-dump-head">
                    <span>Fuel-dump · sidecar contribution</span>
                    <span class="total">${windowTotal ? '+' + _formatTokenShort(windowTotal) : '— no sidecar telemetry —'}</span>
                    ${entries.length ? '<span class="toggle" data-toggle="pods">▾ wingmen</span>' : ''}
                </div>
            </div>`;
        }
        // Per-sidecar weights from cumulative_usage (month). We don't have per-window
        // per-sidecar data, so we use month ratios to apportion the windowed total
        // across sidecars — accurate when one sidecar dominates, approximate otherwise.
        const _tokensOf = units =>
            Number(units?.tokens_input ?? units?.tokens_total ?? units?.total ?? 0);
        const weights = entries.map(([sid, units]) => ({ sid, w: _tokensOf(units) }));
        const sumW = weights.reduce((s, x) => s + x.w, 0);
        segs = weights.map(({ sid, w }) => ({
            sid,
            value: sumW ? windowTotal * (w / sumW) : windowTotal / weights.length,
        })).sort((a, b) => b.value - a.value);
    }

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

    const now = new Date();
    const yearKey = `year_${now.getUTCFullYear()}`;
    const monthKey = `month_${now.toISOString().slice(0, 7)}`;

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
            <span class="label">Yearly · ${now.getUTCFullYear()}</span>
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

function _fcPods(secondaryCards, contributions, primaryCard, winAgg) {
    // Prefer window_aggregations.by_sidecar for per-pod token amounts.
    const winByAgg = winAgg?.by_sidecar && Object.keys(winAgg.by_sidecar).length > 0
        ? winAgg.by_sidecar : null;

    // Collect sidecar IDs from whichever source has data.
    const sids = winByAgg
        ? Object.keys(winByAgg)
        : Object.keys(contributions || {});
    if (!sids.length) return '';

    const _winSideTok = d => (Number(d?.tokens_input ?? 0) + Number(d?.tokens_output ?? 0)
        + Number(d?.tokens_cache_read ?? 0) + Number(d?.tokens_cache_create ?? 0)
        + Number(d?.tokens_reasoning ?? 0));

    // Legacy path: use month-ratio apportionment when no winAgg by_sidecar.
    const windowTotal = Number(primaryCard?.token_usage?.total ?? 0);
    const _tokensOf = units => Number(units?.tokens_input ?? units?.tokens_total ?? 0);
    const sumW = winByAgg ? 0 : sids.reduce((s, sid) => s + _tokensOf(contributions[sid] || {}), 0);

    const pods = sids.map(sid => {
        let windowed, cost;
        if (winByAgg) {
            const data = winByAgg[sid] || {};
            windowed = _winSideTok(data);
            cost = Number(data.cost_usd ?? 0);
        } else {
            const units = contributions[sid] || {};
            const monthTokens = _tokensOf(units);
            windowed = windowTotal && sumW ? windowTotal * (monthTokens / sumW) : monthTokens;
            cost = Number(units.cost_usd ?? 0);
        }
        const matching = secondaryCards.find(c => c.sidecar_id === sid);
        const sname = matching?.service_name || sid;

        const deltaText = windowed ? `+${_formatTokenShort(windowed)}` : '—';
        const costText = cost ? `$${cost.toFixed(2)}` : '—';

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

