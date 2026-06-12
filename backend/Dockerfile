# build from the REPO ROOT so the frontend is in context:
#   docker build -f backend/Dockerfile .
# (docker-compose.yml sets this up.)

FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY backend/datalayer ./datalayer
# hand-collected prop-price CSVs (PROPS_CSV env points at one)
COPY backend/props ./props
# the built terminal, served by the Flask app at / (same origin as the API)
COPY --from=frontend /fe/dist ./static
ENV LEDGER_DB=/data/bets.db FEED_PATH=/data/feed.json STATIC_DIR=/app/static PORT=8000
EXPOSE 8000
# the ledger lives on a mounted volume so logged bets persist across deploys
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--threads", "4", "datalayer.service:app"]
