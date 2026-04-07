/**
 * Fetch all limits from the backend
 * @returns {Promise<{limits: Array<LimitCard>}>} Limits response
 * @throws {Error} Network, HTTP, or parsing errors with descriptive messages
 */
export async function fetchLimits() {
    try {
        const resp = await fetch('/api/limits');
        
        if (!resp.ok) {
            // Provide specific error messages for different HTTP status codes
            const errorMessages = {
                404: 'API endpoint not found',
                500: 'Server error - please try again',
                503: 'Server temporarily unavailable',
            };
            const message = errorMessages[resp.status] || `HTTP ${resp.status} error`;
            throw new Error(message);
        }
        
        // Check if response is valid JSON
        const contentType = resp.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            throw new Error('Invalid response format from server');
        }
        
        return await resp.json();
    } catch (err) {
        // Distinguish between different error types
        if (err instanceof TypeError) {
            // Network error (no internet, CORS issue, etc.)
            throw new Error('Network error - unable to reach server');
        } else if (err instanceof SyntaxError) {
            // JSON parse error
            throw new Error('Invalid data format received from server');
        }
        // Re-throw other errors
        throw err;
    }
}
