FROM node:20-alpine

# Install Python, BeautifulSoup, and utilities
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-beautifulsoup4 \
    py3-lxml \
    curl \
    ca-certificates

WORKDIR /usr/src/app

# Copy application source
COPY app/ .

# Copy cron configuration
COPY cron/root /etc/crontabs/root
RUN chmod 0644 /etc/crontabs/root

# Install Node dependencies (currently none, but keep for future packages)
RUN npm install --production

# Prime cached data during build (best-effort)
RUN python3 scripts/fetch_weather.py || true
RUN python3 scripts/fetch_news.py --category all || true
RUN python3 scripts/fetch_news.py --category indland || true
RUN python3 scripts/fetch_news.py --category udland || true
RUN python3 scripts/fetch_news.py --category kultur || true
RUN python3 scripts/fetch_news.py --category debat || true

ENV NODE_ENV=production
ENV PORT=8081

EXPOSE 8081

RUN chmod +x start.sh

CMD ["sh", "start.sh"]

