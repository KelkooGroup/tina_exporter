FROM python:3.11-slim

LABEL org.opencontainers.image.authors="Benjamin Falque <benjamin.falque@kelkoogroup.com>, Matthias Bosc <matthias.bosc@kelkoogroup.com>"
LABEL org.opencontainers.image.url="https://github.com/kelkoogroup/tina_exporter"

WORKDIR /usr/src/app

EXPOSE 19199
VOLUME ["/config"]

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py ./

CMD ["python3", "./exporter.py", "--config", "/config/tina.yaml"]
