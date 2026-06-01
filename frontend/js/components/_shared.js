// Shared helpers re-exported by components.js and consumed directly by
// the split-out card builders (fleet-commander.js, etc.) so we can break
// up the historical monolith without creating import cycles.

export function _formatTokenShort(val) {
    if (!val) return '0';
    if (val >= 1000000) return (val/1000000).toFixed(2) + 'M';
    if (val >= 1000) return (val/1000).toFixed(0) + 'K';
    return val.toString();
}

export function formatHumanDelta(targetDate) {
    const now = new Date();
    const diffMs = targetDate - now;
    const seconds = Math.floor(diffMs / 1000);

    if (seconds < 0) return 'Just now';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) {
        const hours = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
    }
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
}

const _DI = 'https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg';
const _LH = 'https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@latest/icons';

const _PROV_MAP = {
    anthropic:   { label: 'Anthropic · Claude', key: 'claude',      init: 'CL', icon:      `${_DI}/claude-ai.svg` },
    chatgpt:     { label: 'OpenAI · ChatGPT',   key: 'chatgpt',     init: 'AI', icon:      `${_DI}/openai.svg` },
    gemini:      { label: 'Google · Gemini',    key: 'gemini',      init: 'GM', icon:      `${_DI}/google-gemini.svg` },
    github:      { label: 'GitHub · Copilot',   key: 'copilot',     init: 'GH', icon:      `${_DI}/github-copilot.svg` },
    opencode:      { label: 'Opencode',       key: 'opencode',    init: 'OC', iconLight: `${_DI}/opencode-light.svg`,  iconDark: `${_DI}/opencode-dark.svg` },
    'opencode-free': { label: 'Opencode Free', key: 'opencode',    init: 'OF', iconLight: `${_DI}/opencode-light.svg`,  iconDark: `${_DI}/opencode-dark.svg` },
    zai:         { label: 'Z.AI',                key: 'zai',         init: 'ZI', icon:      `${_DI}/z-ai.svg` },
    kimi_api:    { label: 'Kimi',                key: 'kimi',        init: 'KM', icon:      `${_DI}/kimi-ai.svg` },
    kimi_coding: { label: 'Kimi Coding',         key: 'kimi',        init: 'KC', icon:      `${_DI}/kimi-ai.svg` },
    kimi_k2:     { label: 'Kimi K2',             key: 'kimi',        init: 'K2', icon:      `${_DI}/kimi-ai.svg` },
    minimax:     { label: 'MiniMax',             key: 'minimax',     init: 'MM', iconLight: `${_DI}/minimax-light.svg`,  iconDark: `${_DI}/minimax-dark.svg` },
    openrouter:  { label: 'OpenRouter',          key: 'openrouter',  init: 'OR', icon:      `${_DI}/open-router.svg`,    iconDark: `${_DI}/open-router-dark.svg` },
    ollama:      { label: 'Ollama',              key: 'ollama',      init: 'OL', icon:      `${_DI}/ollama.svg` },
    antigravity: { label: 'Antigravity',         key: 'antigravity', init: 'AG', icon:      `${_LH}/antigravity-color.svg` },
};

export function providerDisplayLabel(providerId) {
    return _PROV_MAP[providerId]?.label || providerId || 'Other';
}

/** Returns the CDN URL for a provider's icon, theme-aware. Returns null when no icon is defined. */
export function providerIconUrl(providerId) {
    const prov = _PROV_MAP[providerId];
    if (!prov || (!prov.icon && !prov.iconLight && !prov.iconDark)) return null;
    const isLight = document.documentElement.dataset.theme === 'light';
    if (isLight) return prov.iconLight ?? prov.icon ?? null;
    return prov.iconDark ?? prov.icon ?? null;
}
