
# tina_exporter

## Description

Prometheus exporter for [Atempo Tina](https://www.atempo.com/products/tina-time-navigator-enterprise-solution-for-backup-and-recovery/) (Time Navigator).
It is designed to run against the Tina API and can expose metrics from multiple catalogs connected to multiple servers.

## Compatibility

* Tina 4.8
* Python 3.7 or higher

## Known limitations

* This is a very early version, configuration, metrics and labels will likely change with future releases
* Grafana dashboards and prometheus alerting rules with follow as well

## Installation

### Docker
```bash
docker build -t tina_exporter .
```
### pip

```bash
pip install -r requirements.txt
```

### Package manager

On debian-based systems
```bash
sudo apt update && sudo apt install python3-prometheus_client python3-aiohttp python3-pyyaml
```

On RHEL-based systems
```bash
sudo dnf install python3-prometheus_client python3-aiohttp python3-pyyaml
```

## Configuration

Create the config file
```bash
touch tina.yaml
```
Edit the file according to your needs.
Ex:
```yaml
exporter_port: 19199

catalogs:
  <tina_server_name_1>:
    <catalog_name_1>:
      uri: https://<fqdn>:port
      user: <user>
      password: <password>
    <catalog_name_2>:
      uri: https://<fqdn>:port
      user: <user>
      password: <password>
  <tina_server_name_2>:
    <catalog_name_3>:
      uri: https://<fqdn>:port
      user: <user>
      password: <password>
```

## Usage

### Docker

```bash
docker run -d -p 19199:19199 -v ./tina.yaml:/config/tina.yaml:ro tina_exporter
```

### command-line

```
python3 exporter.py --config tina.yaml
```

#### example systemd service file

```systemd
[Unit]
Description=start Tina exporter for prometheus
After=network.target
 
[Service]
User=<your user>
ExecStart=python3 /path/to/your/exporter.py --config /path/to/your/tina_config.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Metrics exposed
|Metric name|Type|Labels|Description|
|---|---|---|---|
tina_agents_status|Gauge|catalog, agent_name, category, comment, enabled, is_server, type, server_name, version|Tina Agents status flag (-1=UNKNOWN, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)|
tina_alarms|Gauge|catalog, cause, count, level, message, name|Tina Alarms (-1=UNKNOWN, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)|
tina_backup_selections|Gauge|catalog, agent_name, path, strategy|Tina backup selections with a constant '1' value|
tina_backup_selections_secured_volume_bytes|Gauge|catalog, agent_name, path, strategy|Tina backup selections secured volume in bytes|
tina_backup_last_status|Gauge|catalog, agent_name, category, server_name, strategy, type|Tina backup last status (-2=UNKNOWN, -1=RUNNING, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)|
tina_backup_last_average_rate_bytes|Gauge|catalog, agent_name, server_name, strategy, type|Tina backup last average volume (unit: bytes)|
tina_backup_last_duration|Gauge|catalog, agent_name, server_name, strategy, type|Tina backup last duration (unit: seconds)|
tina_backup_last_processed_objects|Gauge|catalog, agent_name, server_name, strategy, type|Tina backup last processed_objects|
tina_backup_last_processed_volume_bytes|Gauge|catalog, agent_name, server_name, strategy, type|Tina backup last processed volume (unit: bytes)|
tina_cartridges|Gauge|catalog, barcode, cartridge_name, comment, filling_status, has_retention, location, retention_type, status|Tina cartridges with a constant '1' value|
tina_cartridges_integrity_status|Gauge|catalog, allowed_to_be_recycle, cartridge_name, backup_integrity, in_retention_time, recycling_forceable|integrity status of a cartridge with a constant '1' value|
tina_cartridges_volume_bytes|Gauge|catalog, barcode, cartridge_name, location|Tina cartridge volume (unit: bytes)|
tina_cartridges_written_volume_bytes|Gauge|catalog, barcode, cartridge_name, location|Tina cartridge written volume (unit: bytes)|
tina_catalog_size_bytes|Gauge|catalog, server_name|Tine catalog size (unit: bytes)|
tina_catalog_max_size_bytes|Gauge|catalog, server_name|Tina catalog max size (unit: bytes)|
tina_catalog_used_size_bytes|Gauge|catalog, server_name|Tina catalog used size (unit: bytes)|
tina_catalog_object_count|Gauge|catalog, server_name|Tina catalog objectcount|
tina_catalog_scrape_duration|Gauge|catalog, server_name|Scrape duration of the catalog (unit: seconds)|
tina_catalog_status|Gauge|catalog, server_name|status of the catalog (0=FAILURE / 1=OK)|
tina_discovered_hosts|Gauge|catalog, name, os_type, version|Tina discovered hosts with a constant '1' value|
tina_drives|Gauge|catalog, cartridge_name, category, name, status, type|Tina drives with a constant '1' value|
tina_drives_volume_read_bytes|Gauge|catalog, cartridge_name, category, name|Volume read for a drive (unit: bytes)|
tina_drives_volume_written_bytes|Gauge|catalog, cartridge_name, category, name|Volume written for a drive (unit: bytes)|
tina_host_version_status|Gauge|catalog, agent_name|Tina Host version status (0=OK, 1=Outdated, 2=Newer)|
tina_library_content|Gauge|catalog, barcode, cartridge_name, comment, filling_status, has_retention, location, retention_type, status, type|Tina library content with a constant '1' value|
tina_library_content_volume_bytes|Gauge|catalog, barcode, cartridge_name, location|Tina cartridge volume|
tina_library_content_written_volume_bytes|Gauge|catalog, barcode, cartridge_name, location|Tina cartridge written volume|
tina_pools|Gauge|catalog, category, comment, name, label|Tina Pools with a constant '1' value|
tina_pools_nb_cartridges|Gauge|catalog, category, name, label|Tina Pools number of cartridges|
tina_pools_nb_drives|Gauge|catalog, category, name, label|Tina Pools number of drives|
tina_storages|Gauge|catalog, category, storage_name, type|Tina Storages list with a constant '1' value|
tina_storages_nb_drives|Gauge|catalog, category, storage_name, type|Tina Storages number of drives|
tina_strategies|Gauge|catalog, agent_name, category, full_pool_name, full_schedule_active, incr_pool_name, incr_schedule_active, strategy|Tina strategies with a constant '1' value|
