The FastAPI and Flask examples and integration docs escape the request path before logging it, so a crafted path can't forge log lines.
