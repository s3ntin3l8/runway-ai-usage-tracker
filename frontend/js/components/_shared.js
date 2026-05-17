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

const _PROV_MAP = {
    anthropic:      { label: 'Anthropic · Claude', key: 'claude',      init: 'CL' },
    openai:         { label: 'OpenAI · ChatGPT',   key: 'chatgpt',     init: 'AI' },
    google_gemini:  { label: 'Google · Gemini',    key: 'gemini',      init: 'GM' },
    github_copilot: { label: 'GitHub · Copilot',   key: 'copilot',     init: 'GH' },
    opencode:       { label: 'Opencode',            key: 'opencode',    init: 'OC' },
    zai:            { label: 'Z.AI',                key: 'zai',         init: 'ZI' },
    kimi_api:       { label: 'Kimi',                key: 'kimi',        init: 'KM' },
    kimi_coding:    { label: 'Kimi Coding',         key: 'kimi',        init: 'KC' },
    kimi_k2:        { label: 'Kimi K2',             key: 'kimi',        init: 'K2' },
    minimax:        { label: 'MiniMax',             key: 'minimax',     init: 'MM' },
    openrouter:     { label: 'OpenRouter',          key: 'openrouter',  init: 'OR' },
    ollama:         { label: 'Ollama',              key: 'ollama',      init: 'OL' },
    antigravity:    { label: 'Antigravity',         key: 'antigravity', init: 'AG' },
};

export function providerDisplayLabel(providerId) {
    return _PROV_MAP[providerId]?.label || providerId || 'Other';
}
