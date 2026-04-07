# Future Ideas & Improvements

## Claude Collector
- [ ] **Claude OAuth Token Refreshing**: Implement automatic token refreshing via `refreshToken` in `~/.claude/.credentials.json` if the primary `accessToken` is expired (typically expires after a few hours/days). Note: This would require writing back to the credentials file, which needs careful handling of file permissions and potential race conditions.
