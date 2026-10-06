# ProxySQL Web UI test image (RHEL-compatible base, same Python as RHEL 9)
FROM almalinux:9

RUN dnf -y install python3 python3-pip && dnf clean all

WORKDIR /opt/proxysql-webui
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .
COPY docker/webui.ini /etc/proxysql-webui/config.ini
COPY docker/entrypoint.sh /entrypoint.sh

RUN sed -i 's/\r$//' /entrypoint.sh && chmod 0755 /entrypoint.sh \
 && useradd --system --home-dir /var/lib/proxysql-webui proxysql-webui \
 && mkdir -p /var/lib/proxysql-webui && chown proxysql-webui: /var/lib/proxysql-webui

USER proxysql-webui
ENV PROXYSQL_WEBUI_CONFIG=/etc/proxysql-webui/config.ini PATH=/opt/venv/bin:$PATH
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
