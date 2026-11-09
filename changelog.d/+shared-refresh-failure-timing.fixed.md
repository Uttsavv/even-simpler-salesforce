Concurrent callers whose requests were in flight when a token refresh failed now reliably share that failure (the refresh generation is captured before each request is sent).
