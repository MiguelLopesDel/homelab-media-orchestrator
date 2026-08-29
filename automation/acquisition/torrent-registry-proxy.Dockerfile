FROM python:3.13-alpine

# The proxy has no shell access to the media files or qBittorrent configuration.
# cryptography is used solely to encrypt reusable download references at rest.
RUN pip install --no-cache-dir cryptography
