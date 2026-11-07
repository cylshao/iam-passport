FROM python:3.13-alpine

RUN apk add --no-cache tzdata && adduser -D -u 1000 app && mkdir /data && chown app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates templates

USER app
EXPOSE 8930
CMD ["python", "app.py"]
