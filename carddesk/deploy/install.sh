#!/bin/bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
archive=/home/deploy/ai-carddesk-release.tar.gz
release=/opt/ai-carddesk/releases/$(date -u +%Y%m%dT%H%M%SZ)
id carddesk >/dev/null 2>&1 || useradd --system --home /var/lib/ai-carddesk --shell /usr/sbin/nologin carddesk
install -d -m 750 -o carddesk -g carddesk /var/lib/ai-carddesk
install -d -m 755 "$release" /var/www/ai-carddesk-acme
tar -xzf "$archive" -C "$release"
chown -R root:root "$release"
chmod -R a+rX "$release"
if [ ! -d /opt/ai-carddesk/venv ]; then python3 -m venv /opt/ai-carddesk/venv; fi
/opt/ai-carddesk/venv/bin/pip install --disable-pip-version-check -r "$release/requirements.txt"
ln -sfn "$release" /opt/ai-carddesk/current
install -m 644 "$release/deploy/ai-carddesk.service" /etc/systemd/system/ai-carddesk.service
install -m 644 "$release/deploy/ai-carddesk-worker.service" /etc/systemd/system/ai-carddesk-worker.service
systemctl daemon-reload
systemctl enable ai-carddesk.service ai-carddesk-worker.service
systemctl restart ai-carddesk.service
for step in $(seq 1 15); do
    curl -fsS http://127.0.0.1:8808/api/health >/dev/null && break
    sleep 1
done
curl -fsS http://127.0.0.1:8808/api/health
systemctl restart ai-carddesk-worker.service
if [ ! -f /etc/letsencrypt/live/ai.mychangliapp.cn/fullchain.pem ]; then
    install -m 644 "$release/deploy/nginx-http.conf" /etc/nginx/sites-available/ai-carddesk
    ln -sfn /etc/nginx/sites-available/ai-carddesk /etc/nginx/sites-enabled/ai-carddesk
    nginx -t
    systemctl reload nginx
    certbot certonly --webroot -w /var/www/ai-carddesk-acme -d ai.mychangliapp.cn --cert-name ai.mychangliapp.cn --non-interactive
fi
install -m 644 "$release/deploy/nginx.conf" /etc/nginx/sites-available/ai-carddesk
ln -sfn /etc/nginx/sites-available/ai-carddesk /etc/nginx/sites-enabled/ai-carddesk
nginx -t
systemctl reload nginx
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\n/usr/sbin/nginx -t && /bin/systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/ai-carddesk-reload
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/ai-carddesk-reload
systemctl is-active ai-carddesk.service ai-carddesk-worker.service
install -m 644 "$release/deploy/ai-carddesk-backup.service" /etc/systemd/system/ai-carddesk-backup.service
install -m 644 "$release/deploy/ai-carddesk-backup.timer" /etc/systemd/system/ai-carddesk-backup.timer
systemctl daemon-reload
systemctl enable --now ai-carddesk-backup.timer
systemctl start ai-carddesk-backup.service
