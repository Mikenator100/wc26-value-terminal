FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY datalayer ./datalayer
ENV LEDGER_DB=/data/bets.db FEED_PATH=/data/feed.json PORT=8000
EXPOSE 8000
# the ledger lives on a mounted volume so logged bets persist across deploys
CMD ["gunicorn", "-b", "0.0.0.0:8000", "datalayer.service:app"]
