// Match credential inventory accounts to this provider detail account. The ``default`` account
// holds the credentials not tied to one account (the server's env var, an unscoped pasted key), so
// it shows on every pane of the provider; any other account matches only its own pane.
export const matchesAccount = (id: string, accountId: string) => id === accountId || id === 'default';
