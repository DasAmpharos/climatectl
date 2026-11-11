# Grafana with everything the climatectl dashboard needs baked in: the SQLite data source
# plugin (pinned, so nothing is downloaded at startup), the data source and the dashboard.
# Build from deploy/grafana: docker build -t climatectl-grafana deploy/grafana
FROM grafana/grafana:13.2.4

ARG SQLITE_PLUGIN_VERSION=4.0.6
USER root
RUN grafana cli --pluginsDir /opt/grafana-plugins \
        plugins install frser-sqlite-datasource "${SQLITE_PLUGIN_VERSION}" \
    && chmod -R a+rX /opt/grafana-plugins
COPY provisioning /etc/grafana/climatectl/provisioning
COPY dashboards /etc/grafana/climatectl/dashboards
# The same user as the logger, so both can use the database's WAL files in the shared volume.
RUN chown -R 10001:10001 /var/lib/grafana
ENV CLIMATECTL_DB=/data/climatectl.db \
    GF_PATHS_PLUGINS=/opt/grafana-plugins \
    GF_PATHS_PROVISIONING=/etc/grafana/climatectl/provisioning \
    GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/etc/grafana/climatectl/dashboards/house.json \
    GF_PLUGINS_PREINSTALL_DISABLED=true \
    GF_USERS_ALLOW_SIGN_UP=false \
    GF_ANALYTICS_REPORTING_ENABLED=false \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false \
    GF_NEWS_NEWS_FEED_ENABLED=false
USER 10001
