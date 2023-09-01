#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
Kelkoo Tina exporter for Prometheus
@author: Benjamin Falque / Matthias Bosc
"""
import argparse
import asyncio
import copy
import inspect
import logging
import time

from typing import Dict

from prometheus_client.core import GaugeMetricFamily
from prometheus_client import start_http_server, REGISTRY

import aiohttp
import yaml

class SessionError(Exception):
    pass

class Tina:
    """
    This is the class where all the stuff around Tina is done in background
    Connection, session management, agents, jobs and reporting fetch, etc

    Once everything has been fetched, a copy is push into a dict (tina.data)
    that the Prometheus collector will fetch when the /metrics page is accessed
    """
    def __init__(self):
        """Defines the base strings, first"""
        # the object that will store the sessions
        self.session: Dict[str, aiohttp.ClientSession] = {}
        # the value code corresponding to a severity level
        self.severity_map = {
            "SEVERITY_NONE": 0,
            "SEVERITY_MINOR": 1,
            "SEVERITY_MAJOR": 2,
            "SEVERITY_CRITICAL": 3
        }
        # the value message corresponding to a severity level
        self.severity_message_map = {
            "SEVERITY_NONE": "info",
            "SEVERITY_MINOR": "warning",
            "SEVERITY_MAJOR": "error",
            "SEVERITY_CRITICAL": "critical"
        }
        self.catalog_heartbeat = {}
        # the dict where we will place processed values
        # that the collector will scrape
        self.data = {}
        # temporary results dictionary
        self.results = {}
        # Parse the global config
        self.parse_config()
        # grab the expected metrics and labels for later comparison
        self.build_metrics_list()


    def parse_config(self) -> None:
        """Get the config from the yaml file"""
        with open(args.config_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            self.config = config.get("catalogs")
            self.exporter_port = config.get("exporter_port")

        # no longer needed
        del config

    def build_metrics_list(self):
        """build the reference labelset"""
        # dictionary that hold a metric_name: [metrics_labels...] reference
        self.metrics_reference = {}
        # labels list that we don't use to compare metrics / values
        self.non_label_values = ["catalog", "value"]

        # get the data from the Collector
        tina_collector = TinaCollector()
        tina_collector.build_metrics()

        for metric_name, metric_values in tina_collector.metrics.items():
            self.metrics_reference.update({
                metric_name: [x for x in metric_values._labelnames if x not in self.non_label_values]
            })
            self.metrics_reference.get(metric_name).append(
                "value"
            )
        del tina_collector

    async def copy_data(self):
        """makes the fresh data available for the Collector"""
        self.data = copy.deepcopy(self.results)
        # get rid of old data
        self.results.clear()

    async def get_session(self, catalog: str, fqdn: str):
        """
        Get the Tina Token
        Instead of storing the session.id in a variable and use it as a GET parameter,
        we use a single, reusable and persistant aiohttp.clientSession()
        object that stores the token in the session
        """
        payload = {
            "lang": "en",
            "catalog": catalog,
            "server": fqdn,
            'user': self.config.get(fqdn).get(catalog).get("user"),
            'password': self.config.get(fqdn).get(catalog).get("password")
        }

        try:
            # if a session already exists, close it
            # otherwise, active and pending connections (TIME_WAIT and ESTABLISHED) won't be closed
            # and re-connection will be more difficult
            if isinstance(self.session.get(catalog), aiohttp.ClientSession):
                self.session.get(catalog).close()

            # instantiate the aiohttp.ClientSession object that will be reused later
            session = aiohttp.ClientSession(
                base_url=self.config.get(fqdn).get(catalog).get("uri"),
                raise_for_status=True
            )
            # send the post request to authenticate the user
            async with session.put(url="/tina/api/users/authenticate", json=payload, timeout=5, ssl=False) as response:

                if (await response.json()).get('authenticated') == "AUTHENTICATED":
                    # we're OK
                    self.catalog_heartbeat[catalog] = {
                        "success": True,
                        "message": "ok"
                    }
                    # appends the Auth token to the aiohttp.ClientSession object (so it's stored, gotcha?)
                    session.headers.update({
                        "Accept": "application/json",
                        "Authorization": f"Bearer {(await response.json()).get('token')}"
                    })
                    # returns the object
                    return session

                error_message = f"Authentication failed for catalog {catalog}: {(await response.json())}"

        except aiohttp.ClientError as err:
            error_message = f"Didn't receive a 200 in {inspect.stack()[0][3]}() for catalog {catalog}: {err}"
        except Exception as err:
            error_message = f"Error in {inspect.stack()[1][3]}() for catalog {catalog}: {err}"

        # the session has failed, closing it
        await session.close()

        logger.error(error_message)
        raise SessionError(error_message)

    async def check_session(self, catalog: str, fqdn: str) -> None:
        """
        Does an health-check on the session object and check for the token validity
        1) checks if the object is an instance of aiohttp.ClientSession (1st connection)
        2) checks is the token is valid with a real-world check (API call to list catalogs)
        """
        #for catalog in self.catalogs:
        if not isinstance(self.session.get(catalog), aiohttp.ClientSession):
            logger.debug("First connection for catalog %s", catalog)
            self.session[catalog] = await self.get_session(catalog, fqdn)

        try:
            await self.get_catalog_infos(catalog, check=True)
        except Exception:
            logger.debug("Session lost, trying to reconnect")
            self.session[catalog] = await self.get_session(catalog, fqdn)
        else:
            self.catalog_heartbeat[catalog] = {
                "success": True,
                "message": "ok"
            }

    async def call_tina_api(self, catalog: str, endpoint: str, method: str = "GET", params: Dict = None):
        """Common function to call the tina API"""

        if not params:
            params = {}

        if method == "GET":
            request = await self.session.get(catalog).get(url=f"/tina/api/{endpoint}", params=params, timeout=60, ssl=False)
        else:
            request = await self.session.get(catalog).post(url=f"/tina/api/{endpoint}", json=params, timeout=60, ssl=False)

        request.raise_for_status()
        response = await request.json()
        request.close()

        if "error" in response:
            raise Exception(f"Received a 200 but with an error: {response}")

        return response


    async def get_agents(self, catalog: str):
        """fetch all the agents (hosts + applications)"""
        agents = await self.call_tina_api(catalog=catalog, endpoint="agents")

        for agent in agents:
            self.results.get(catalog).get("tina_agents_status").append({
                "agent_name": agent.get("name"),
                "category": agent.get("category").lower(),
                "comment": agent.get("comment", "").capitalize(),
                "enabled": agent.get("isEnabled"),
                "is_server": agent.get("isServer"),
                "type": agent.get("osType").get("name") if agent.get("category").lower() == "host" else agent.get("applicationType").get("name"),
                "server_name": self.results.get(catalog).get("server_name"),
                "version": agent.get("softwareVersion", ""),
                "value": await self.get_severity_status_code(agent.get("highestAlarm")),
            })

            # register the server version for comparison with agents
            if agent.get("category").lower() == "host" and agent.get("isServer"):
                self.results.get(catalog).get("servers_version").update({
                    agent.get("name"): agent.get("softwareVersion")
                })

        for host in self.results.get(catalog).get("tina_agents_status"):
            if host.get("category") == "host" and not host.get("is_server"):
                server_version = self.results.get(catalog).get("servers_version").get(self.results.get(catalog).get("server_name"))
                # 2 : agent_version = server_version
                # 1 : agent_version < server_version
                # 0 : agent_version > server_version
                self.results.get(catalog).get("tina_host_version_status").append({
                    "agent_name": host.get("agent_name"),
                    "value": 2 if host.get("version") == server_version else int(host.get("version") < server_version)
                })


    async def get_alarms(self, catalog: str) -> None:
        """fetch all the alarms"""
        alarms = await self.call_tina_api(catalog=catalog, endpoint="alarms")

        for alarm in alarms:
            self.results.get(catalog).get("tina_alarms").append({
                "cause": alarm.get("cause"),
                "count": alarm.get("count"),
                "level": await self.get_severity_status_message(alarm.get("severity")),
                "message": alarm.get("message"),
                "name": alarm.get("associatedObjectName"),
                "value": await self.get_severity_status_code(alarm.get("severity"))
            })

    async def get_backup_selections(self, catalog: str) -> None:
        """fetch all the catalog_infos"""
        backups_selections = await self.call_tina_api(catalog=catalog, endpoint="backup-selections")

        for backup in backups_selections:
            for strategy in backup.get("strategyList"):
                self.results.get(catalog).get("tina_backup_selections").append({
                    "agent_name": backup.get("agentName"),
                    "path": backup.get("path"),
                    "strategy": strategy.replace("STRATEGY_", ""),
                    "value": 1 if str(backup.get("secured")) == "true" else 0
                })

                self.results.get(catalog).get("tina_backup_selections_secured_volume_bytes").append({
                    "agent_name": backup.get("agentName"),
                    "path": backup.get("path"),
                    "strategy": strategy.replace("STRATEGY_", ""),
                    "value": backup.get("securedVolume"),
                })

    async def get_backup_status(self, catalog: str) -> None:
        """fetch all the backups statuses"""
        payload = {
            "sinceDays": 7
        }
        backups_status = await self.call_tina_api(catalog=catalog, endpoint="reporting/backup-status", params=payload)

        for bckp_status in backups_status:
            for strat_backup in bckp_status.get("strategyStats"):
                if "nextIncrBackupDate" in strat_backup and "lastCompletedBackupJobPublicId" in strat_backup:
                    self.results.get(catalog).get("jobs_list").append(strat_backup.get("lastCompletedBackupJobPublicId"))
                if "nextFullBackupDate" in strat_backup and "lastCompletedFullBackupJobPublicId" in strat_backup:
                    self.results.get(catalog).get("jobs_list").append(strat_backup.get("lastCompletedFullBackupJobPublicId"))

    async def get_catalog_infos(self, catalog: str, check: bool = False) -> None:
        """fetch all the catalog infos"""
        catalog_infos = await self.call_tina_api(catalog=catalog, endpoint="current-catalog/info")

        if not check:
            self.results.get(catalog).get("tina_catalog_size_bytes").append({
                "server_name": catalog_infos.get("serverName"),
                "value": catalog_infos.get("catalogSize")
            })
            self.results.get(catalog).get("tina_catalog_max_size_bytes").append({
                "server_name": catalog_infos.get("serverName"),
                "value": catalog_infos.get("maxCatalogSize")
            })
            self.results.get(catalog).get("tina_catalog_used_size_bytes").append({
                "server_name": catalog_infos.get("serverName"),
                "value": catalog_infos.get("usedCatalogSize")
            })
            self.results.get(catalog).get("tina_catalog_object_count").append({
                "server_name": catalog_infos.get("serverName"),
                "value": catalog_infos.get("objectCount")
            })

    async def get_discovered_hosts(self, catalog: str) -> None:
        """fetch all the Agents Discovered"""
        discovered_hosts = await self.call_tina_api(catalog=catalog, endpoint="agents/hosts/discovered")

        for discovered_host in discovered_hosts:
            self.results.get(catalog).get("tina_discovered_hosts").append({
                "name": discovered_host.get("name"),
                "os_type": discovered_host.get("osTypeName"),
                "version": discovered_host.get("protocolVersion"),
                "value": 1
            })

    async def get_cartridges_for_pool(self, catalog: str, pool: str) -> None:
        """fetch all the cartridges for a given pool"""
        cartridges = await self.call_tina_api(catalog=catalog, endpoint=f"pools/{pool}/cartridges")

        for cartridge in cartridges:
            if not "vtl" in cartridge.get("location").lower():

                if cartridge.get("publicId") not in self.results.get(catalog).get("cartridges_integrity_status_list"):
                    self.results.get(catalog).get("cartridges_integrity_status_list").update({
                        cartridge.get("publicId"): [cartridge.get("name")]
                    })
                else:
                    logger.debug("%s already in integ", cartridge.get("publicId"))

                base_values = {
                    "barcode": cartridge.get("barcode"),
                    "cartridge_name": cartridge.get("name"),
                    "location": cartridge.get("location")
                }

                self.results.get(catalog).get("tina_cartridges").append({
                    **base_values,
                    "comment": cartridge.get("comment"),
                    "filling_status": cartridge.get("fillingStatus"),
                    "has_retention": cartridge.get("hasRetention"),
                    "retention_type": cartridge.get("retentionType"),
                    "status": cartridge.get("status"),
                    "value": 1
                })

                self.results.get(catalog).get("tina_cartridges_volume_bytes").append({
                    **base_values,
                    "value": cartridge.get("volume")
                })
                self.results.get(catalog).get("tina_cartridges_written_volume_bytes").append({
                    **base_values,
                    "value": cartridge.get("writtenVolume")
                })

    async def get_cartridge_integrity_status(self, catalog: str, public_id: Dict) -> None:
        """fetch the integrity status for a given cartridge"""

        if public_id != {}:
            payload = {
                "publicIds": [
                    *list(public_id.keys())
                ]
            }

            result = await self.call_tina_api(catalog=catalog, endpoint="cartridges/integrity-status", method="POST", params=payload)

            for integrity_status in result:
                self.results.get(catalog).get("tina_cartridges_integrity_status").append({
                    "allowed_to_be_recycle": integrity_status.get("integrityStatus").get("allowedToBeRecycle"),
                    "cartridge_name": self.results.get(catalog).get("cartridges_integrity_status_list").get(integrity_status.get("cartId"))[0],
                    "backup_integrity": integrity_status.get("integrityStatus").get("backupIntegrity"),
                    "in_retention_time": integrity_status.get("integrityStatus").get("inRetentionTime"),
                    "recycling_forceable": integrity_status.get("integrityStatus").get("recyclingForceable"),
                    "value": 1
                })

    async def _process_job(self, catalog: str, job: Dict = None):
        base_values = {
            "agent_name": job.get("agentName"),
            "server_name": job.get("hostName"),
            "strategy": job.get("stratName").replace("STRATEGY_", ""),
            "type": "incr" if job.get("isIncr") else "full"
        }

        self.results.get(catalog).get("tina_backup_last_status").append({
            **base_values,
            "category": job.get("xsi:type").replace("job", ""),
            "value": -1 if bool(job.get("isActive")) else await self.get_severity_status_code(job.get("highestAlarm")),
        })
        self.results.get(catalog).get("tina_backup_last_average_rate_bytes").append({
            **base_values,
            "value": job.get("averageRate", -1)
        })
        self.results.get(catalog).get("tina_backup_last_duration").append({
            **base_values,
            "value": job.get("duration", -1)
        })
        self.results.get(catalog).get("tina_backup_last_processed_objects").append({
            **base_values,
            "value": job.get("processedObjects", -1)
        })
        self.results.get(catalog).get("tina_backup_last_processed_volume_bytes").append({
            **base_values,
            "value": job.get("processedVolume", -1)
        })

    async def get_running_jobs(self, catalog: str) -> None:
        """fetch all the running jobs"""
        payload = {
            "category": "inProgress",
            "content": "FULL"
        }
        jobs = await self.call_tina_api(catalog=catalog, endpoint="jobs", params=payload)

        for job in jobs:
            await self._process_job(catalog, job)


    async def get_job_by_id(self, catalog: str, job_id: int) -> None:
        """fetch a job by its id"""
        job = await self.call_tina_api(catalog=catalog, endpoint=f"jobs/{job_id}")

        await self._process_job(catalog, job)

    async def get_pools(self, catalog: str) -> None:
        """fetch all the pools"""
        pools = await self.call_tina_api(catalog=catalog, endpoint="pools")

        for pool in pools:
            base_values =  {
                "category": pool.get("category").lower(),
                "name": pool.get("name"),
                "label": pool.get("label", "")
            }

            self.results.get(catalog).get("tina_pools").append({
                **base_values,
                "comment": pool.get("comment", ""),
                "value": 1
            })
            self.results.get(catalog).get("tina_pools_nb_cartridges").append({
                **base_values,
                "value": pool.get("nbCartridges", -1)
            })
            self.results.get(catalog).get("tina_pools_nb_drives").append({
                **base_values,
                "value": pool.get("nbDrives", -1)
            })

            # if the pool has cartridges, append it to the list of pools to check
            if pool.get("nbCartridges") is not None and pool.get("nbCartridges") > 0:
                self.results.get(catalog).get("cartridges_list").append([
                    pool.get("publicId"), pool.get("name")
                ])

    async def get_storages(self, catalog: str) -> None:
        """fetch all the strategies for a given catalog"""
        storages = await self.call_tina_api(catalog=catalog, endpoint="storages")

        for storage in storages:
            self.results.get(catalog).get("tina_storages").append({
                "category": storage.get("category").lower(),
                "storage_name": storage.get("name"),
                "type": storage.get("type", ""),
                "value": 1
            })
            self.results.get(catalog).get("tina_storages_nb_drives").append({
                "category": storage.get("category").lower(),
                "storage_name": storage.get("name"),
                "type": storage.get("type", ""),
                "value": storage.get("nbDrives", -1)
            })

            if "driveList" in storage:
                for drive_list in storage.get("driveList"):

                    if drive_list.get("publicId") not in self.results.get(catalog).get("drives"):
                        # add .. so it will not be tested again
                        self.results.get(catalog).get("drives").append(drive_list.get("publicId"))

                        base_values = {
                            "cartridge_name": drive_list.get("cartdridgeName", ""),
                            "category": drive_list.get("category"),
                            "name": drive_list.get("name"),
                        }

                        self.results.get(catalog).get("tina_drives").append({
                            **base_values,
                            "status": drive_list.get("status"),
                            "type": drive_list.get("type"),
                            "value": 1,
                        })
                        self.results.get(catalog).get("tina_drives_volume_read_bytes").append({
                            **base_values,
                            "value": drive_list.get("volumeReadInByte", -1),
                        })
                        self.results.get(catalog).get("tina_drives_volume_written_bytes").append({
                            **base_values,
                            "value": drive_list.get("volumeWritenInByte", -1),
                        })

                        if "libraryId" in drive_list and [drive_list.get("libraryId"), storage.get("name")] not in self.results.get(catalog).get("libraries"):
                            self.results.get(catalog).get("libraries").append([
                                drive_list.get("libraryId"), storage.get("name")
                            ])

    async def get_library_content(self, catalog: str, public_id: str) -> None:
        """fetch the content of a given library"""
        library_content = await self.call_tina_api(catalog=catalog, endpoint=f"storages/library/{public_id}/content")

        if "libraryLocationList" in library_content:
            for content in library_content.get("libraryLocationList"):

                if "cartridge" in content:
                    if not "vtl" in content.get("cartridge").get("location").lower():
                        # get the integrity from the cartridge
                        if content.get("cartridge").get("publicId") not in self.results.get(catalog).get("cartridges_integrity_status_list"):
                            self.results.get(catalog).get("cartridges_integrity_status_list").update({
                                content.get("cartridge").get("publicId"): [content.get("cartridge").get("name")]
                            })
                        else:
                            logger.debug("%s already in integ", content.get("cartridge").get("publicId"))


                        base_values = {
                            "barcode": content.get("cartridge").get("barcode"),
                            "cartridge_name": content.get("cartridge").get("name"),
                            "location": content.get("cartridge").get("location")
                        }

                        self.results.get(catalog).get("tina_library_content").append({
                            **base_values,
                            "comment": content.get("cartridge").get("comment"),
                            "filling_status": content.get("cartridge").get("fillingStatus"),
                            "has_retention": content.get("cartridge").get("hasRetention"),
                            "retention_type": content.get("cartridge").get("retentionType"),
                            "status": content.get("cartridge").get("status"),
                            "type": content.get("type"),
                            "value": 1
                        })
                        self.results.get(catalog).get("tina_library_content_volume_bytes").append({
                            **base_values,
                            "value": content.get("cartridge").get("volume")
                        })
                        self.results.get(catalog).get("tina_library_content_written_volume_bytes").append({
                            **base_values,
                            "value": content.get("cartridge").get("writtenVolume")
                        })

    async def get_strategies(self, catalog: str) -> None:
        """fetch all the strategies for a given catalog"""
        strategies = await self.call_tina_api(catalog=catalog, endpoint="strategies")

        for strategy in strategies:
            self.results.get(catalog).get("tina_strategies").append({
                "agent_name": strategy.get("agentName"),
                "category": strategy.get("category").lower(),
                "full_pool_name": strategy.get("fullPoolName"),
                "full_schedule_active": strategy.get("fullScheduleActive"),
                "incr_pool_name": strategy.get("incrPoolName"),
                "incr_schedule_active": strategy.get("incrScheduleActive"),
                "strategy": strategy.get("name").replace("STRATEGY_", ""),
                "value": 1
            })


    async def get_severity_status_code(self, severity: str) -> int:
        """Get the "exit code" of a given severity message"""
        if severity not in self.severity_map:
            return -1

        return self.severity_map.get(severity)

    async def get_severity_status_message(self, severity: str) -> str:
        """Get the "exit code" level of a given severity message"""
        if severity not in self.severity_message_map:
            return "unknown"

        return self.severity_message_map.get(severity)

    async def process_request(self, semaphore, fqdn, catalogs) -> None:
        """
        The function where almost everything is done and formatted

        TODO update and clean data structure, use a clean class
        """

        start = time.perf_counter()

        async with semaphore:
            for catalog in catalogs:
                c_start = time.perf_counter()
                self.results.update({
                    catalog: {
                        "cartridges_list": [],
                        "cartridges_integrity_status_list": {},
                        "drives": [],
                        "jobs_list": [],
                        "servers_version": {},
                        "libraries": [],
                        "server_name": fqdn,
                        "storages": {}
                    }
                })

                for metric_key in self.metrics_reference:
                    self.results.get(catalog).update({
                        metric_key: []
                    })

                try:
                    # checks that the session and the server are still alive
                    await self.check_session(catalog, fqdn)

                    # The asyncio http Pool has a default value of 100,
                    # so limiting the concurrency to 15 requests
                    # We don't want to overload the server
                    item_semaphore = asyncio.Semaphore(15)

                    async with item_semaphore:
                        await asyncio.gather(
                            self.get_pools(catalog),
                            self.get_storages(catalog),
                            self.get_backup_status(catalog),
                        )

                        await asyncio.gather(
                            self.get_agents(catalog),
                            self.get_alarms(catalog),
                            self.get_backup_selections(catalog),
                            self.get_catalog_infos(catalog),
                            self.get_discovered_hosts(catalog),
                            self.get_running_jobs(catalog),
                            self.get_strategies(catalog),
                            *{self.get_job_by_id(catalog, job_id) for job_id in self.results.get(catalog).get("jobs_list")},
                            *{self.get_cartridges_for_pool(catalog, pool_id[0]) for pool_id in self.results.get(catalog).get("cartridges_list")},
                            *{self.get_library_content(catalog, storage_id[0]) for storage_id in self.results.get(catalog).get("libraries")}
                        )

                        await self.get_cartridge_integrity_status(catalog, self.results.get(catalog).get("cartridges_integrity_status_list"))


                    logger.debug("%ss elapsed for catalog %s", round(time.perf_counter() - c_start, 2), catalog)
                    # add the scrape duration for performance-checking purpose
                    self.results.get(catalog).get("tina_catalog_scrape_duration").append({
                        "server_name": fqdn,
                        "value": time.perf_counter() - c_start
                    })

                except SessionError as err:
                    self.catalog_heartbeat[catalog] = {
                        "success": False,
                        "message": str(err)
                    }
                    logger.error("Error during authentication for catalog %s: %s", catalog, err)
                except aiohttp.ClientError as err:
                    self.catalog_heartbeat[catalog] = {
                        "success": False,
                        "message": f"Didn't receive a 200 in {inspect.stack()[0][3]}() for catalog {catalog}: {err}"
                    }
                    logger.error("Didn't receive a 200 in %s() for catalog %s: %s", inspect.stack()[0][3], catalog, err)
                except Exception as err:
                    self.catalog_heartbeat[catalog] = {
                        "success": False,
                        "message": f"Error in {inspect.stack()[0][3]}() for catalog {catalog}: {err}"
                    }
                    logger.error("Error in %s() for catalog %s: %s", inspect.stack()[0][3], catalog, err)

        logger.info("%ss elapsed for host %s", round(time.perf_counter() - start, 2), fqdn)

class TinaCollector():
    """
    Class only used by the Prometheus HTTP Server
    It is registered and the collect() method is called each time the web page is accessed (GET /metrics)
    """
    def __init__(self):
        self.metrics: Dict[str, GaugeMetricFamily] = {}

    def build_metrics(self) -> None:
        """
        The metrics are inside a dict to make it easier to yield them
        """
        self.metrics = {
            "tina_agents_status": GaugeMetricFamily(
                name="tina_agents_status",
                documentation="Tina Agents status flag (-1=UNKNOWN, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)",
                labels=[
                    "catalog", "agent_name", "category", "comment", "enabled",
                    "is_server", "type", "server_name", "version"
                ]
            ),
            "tina_alarms": GaugeMetricFamily(
                name="tina_alarms",
                documentation="Tina Alarms (-1=UNKNOWN, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)",
                labels=[
                    "catalog", "cause", "count", "level", "message", "name"
                ]
            ),
            "tina_backup_selections": GaugeMetricFamily(
                name="tina_backup_selections",
                documentation="Tina backup selections with a constant '1' value",
                labels=[
                    "catalog", "agent_name", "path", "strategy"
                ]
            ),
            "tina_backup_selections_secured_volume_bytes": GaugeMetricFamily(
                name="tina_backup_selections_secured_volume_bytes",
                documentation="Tina backup selections secured volume in bytes",
                labels=[
                    "catalog", "agent_name", "path", "strategy"
                ]
            ),
            "tina_backup_last_status": GaugeMetricFamily(
                name="tina_backup_last_status",
                documentation="Tina backup last status (-2=UNKNOWN, -1=RUNNING, 0=OK, 1=MINOR, 2=MAJOR, 3=CRITICAL)",
                labels=[
                    "catalog", "agent_name", "category", "server_name", "strategy", "type"
                ]
            ),
            "tina_backup_last_average_rate_bytes": GaugeMetricFamily(
                name="tina_backup_last_average_rate_bytes",
                documentation="Tina backup last average volume (unit: bytes)",
                labels=[
                    "catalog", "agent_name", "server_name", "strategy", "type"
                ]
            ),
            "tina_backup_last_duration": GaugeMetricFamily(
                name="tina_backup_last_duration",
                documentation="Tina backup last duration (unit: seconds)",
                labels=[
                    "catalog", "agent_name", "server_name", "strategy", "type"
                ]
            ),
            "tina_backup_last_processed_objects": GaugeMetricFamily(
                name="tina_backup_last_processed_objects",
                documentation="Tina backup last processed_objects",
                labels=[
                    "catalog", "agent_name", "server_name", "strategy", "type"
                ]
            ),
            "tina_backup_last_processed_volume_bytes": GaugeMetricFamily(
                name="tina_backup_last_processed_volume_bytes",
                documentation="Tina backup last processed volume (unit: bytes)",
                labels=[
                    "catalog", "agent_name", "server_name", "strategy", "type"
                ]
            ),
            "tina_cartridges": GaugeMetricFamily(
                name="tina_cartridges",
                documentation="Tina cartridges with a constant '1' value",
                labels=[
                    "catalog", "barcode", "cartridge_name", "comment", "filling_status",
                    "has_retention", "location", "retention_type", "status"
                ]
            ),
            "tina_cartridges_integrity_status": GaugeMetricFamily(
                name="tina_cartridges_integrity_status",
                documentation="integrity status of a cartridge with a constant '1' value",
                labels=[
                    "catalog", "allowed_to_be_recycle", "cartridge_name", "backup_integrity",
                    "in_retention_time", "recycling_forceable"
                ]
            ),
            "tina_cartridges_volume_bytes": GaugeMetricFamily(
                name="tina_cartridges_volume_bytes",
                documentation="Tina cartridge volume (unit: bytes)",
                labels=[
                    "catalog", "barcode", "cartridge_name", "location",
                ]
            ),
            "tina_cartridges_written_volume_bytes": GaugeMetricFamily(
                name="tina_cartridges_written_volume_bytes",
                documentation="Tina cartridge written volume (unit: bytes)",
                labels=[
                    "catalog", "barcode", "cartridge_name", "location",
                ]
            ),
            "tina_catalog_size_bytes": GaugeMetricFamily(
                name="tina_catalog_size_bytes",
                documentation="Tine catalog size (unit: bytes)",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_catalog_max_size_bytes": GaugeMetricFamily(
                name="tina_catalog_max_size_bytes",
                documentation="Tina catalog max size (unit: bytes)",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_catalog_used_size_bytes": GaugeMetricFamily(
                name="tina_catalog_used_size_bytes",
                documentation="Tina catalog used size (unit: bytes)",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_catalog_object_count": GaugeMetricFamily(
                name="tina_catalog_object_count",
                documentation="Tina catalog objectcount",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_catalog_scrape_duration": GaugeMetricFamily(
                name="tina_catalog_scrape_duration",
                documentation="Scrape duration of the catalog (unit: seconds)",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_catalog_status": GaugeMetricFamily(
                name="tina_catalog_status",
                documentation="status of the catalog (0=FAILURE / 1=OK)",
                labels=[
                    "catalog", "server_name"
                ]
            ),
            "tina_discovered_hosts": GaugeMetricFamily(
                name="tina_discovered_hosts",
                documentation="Tina discovered hosts with a constant '1' value",
                labels=[
                    "catalog", "name", "os_type", "version"
                ]
            ),
            "tina_drives": GaugeMetricFamily(
                name="tina_drives",
                documentation="Tina drives with a constant '1' value",
                labels=[
                    "catalog", "cartridge_name", "category", "name", "status", "type"
                ]
            ),
            "tina_drives_volume_read_bytes": GaugeMetricFamily(
                name="tina_drives_volume_read_bytes",
                documentation="Volume read for a drive (unit: bytes)",
                labels=[
                    "catalog", "cartridge_name", "category", "name"
                ]
            ),
            "tina_drives_volume_written_bytes": GaugeMetricFamily(
                name="tina_drives_volume_written_bytes",
                documentation="Volume written for a drive (unit: bytes)",
                labels=[
                    "catalog", "cartridge_name", "category", "name"
                ]
            ),
            "tina_host_version_status": GaugeMetricFamily(
                name="tina_host_version_status",
                documentation="Tina Host version status (0=OK, 1=Outdated, 2=Newer)",
                labels=[
                    "catalog", "agent_name"
                ]
            ),
            "tina_library_content": GaugeMetricFamily(
                name="tina_library_content",
                documentation="Tina library content with a constant '1' value",
                labels=[
                    "catalog", "barcode", "cartridge_name", "comment", "filling_status",
                    "has_retention", "location", "retention_type", "status", "type"
                ]
            ),
            "tina_library_content_volume_bytes": GaugeMetricFamily(
                name="tina_library_content_volume_bytes",
                documentation="Tina cartridge volume",
                labels=[
                    "catalog", "barcode", "cartridge_name", "location",
                ]
            ),
            "tina_library_content_written_volume_bytes": GaugeMetricFamily(
                name="tina_library_content_written_volume_bytes",
                documentation="Tina cartridge written volume",
                labels=[
                    "catalog", "barcode", "cartridge_name", "location",
                ]
            ),
            "tina_pools": GaugeMetricFamily(
                name="tina_pools",
                documentation="Tina Pools with a constant '1' value",
                labels=[
                    "catalog", "category", "comment", "name", "label"
                ]
            ),
            "tina_pools_nb_cartridges": GaugeMetricFamily(
                name="tina_pools_nb_cartridges",
                documentation="Tina Pools number of cartridges",
                labels=[
                    "catalog", "category", "name", "label"
                ]
            ),
            "tina_pools_nb_drives": GaugeMetricFamily(
                name="tina_pools_nb_drives",
                documentation="Tina Pools number of drives",
                labels=[
                    "catalog", "category", "name", "label"
                ]
            ),
            "tina_storages": GaugeMetricFamily(
                name="tina_storages",
                documentation="Tina Storages list with a constant '1' value",
                labels=[
                    "catalog", "category", "storage_name", "type"
                ]
            ),
            "tina_storages_nb_drives": GaugeMetricFamily(
                name="tina_storages_nb_drives",
                documentation="Tina Storages number of drives",
                labels=[
                    "catalog", "category", "storage_name", "type"
                ]
            ),
            "tina_strategies": GaugeMetricFamily(
                name="tina_strategies",
                documentation="Tina strategies with a constant '1' value",
                labels=[
                    "catalog", "agent_name", "category", "full_pool_name", "full_schedule_active",
                    "incr_pool_name", "incr_schedule_active", "strategy",
                ]
            )
        }

    def collect(self):
        """
        The method called by the Prometheus HTTP server itself

        This method is also called when the class is registered,
        add a describe() method if this must be avoided !
        """
        self.build_metrics()

        for catalog, catalog_values in tina.data.items():
            # catalog heartbeat
            self.metrics.get("tina_catalog_status").add_metric(labels=[
                catalog,
                catalog_values.get("server_name")
            ],
            value=int(tina.catalog_heartbeat.get(catalog).get("success")))

            if int(tina.catalog_heartbeat.get(catalog).get("success")):
                # automatically filled metrics
                for metric_key, values in catalog_values.items():
                    if metric_key.startswith("tina_"):
                        for metric_value in values:
                            if metric_value.get("value") is not None:
                                if sorted(metric_value) == sorted(tina.metrics_reference.get(metric_key)):
                                    self.metrics.get(metric_key).add_metric(labels=[
                                        catalog,
                                        *[str(metric_value.get(k)) for k in tina.metrics_reference.get(metric_key) if k not in tina.metrics_reference],
                                    ],
                                    value=float(metric_value.get("value")))
                                else:
                                    logger.warning("There is something wrong with the metric %s", metric_key)
                            else:
                                logger.warning("No value for the metric %s", metric_key)

        for metric in self.metrics.values():
            yield metric


async def main():
    """run the servers processing concurrently"""
    max_concurrency = 3
    semaphore = asyncio.Semaphore(max_concurrency)

    while True:
        # create 1 task by server
        tasks = [tina.process_request(semaphore, fqdn, catalogs) for fqdn, catalogs in tina.config.items()]
        # execute them in //
        await asyncio.gather(*tasks)
        # make the data available for the Collector
        await tina.copy_data()
        # next execution in 300 seconds
        await asyncio.sleep(300)

if __name__ == "__main__":
   # parsing cli arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loglevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="The log level to use"
    )
    parser.add_argument(
        "--config",
        default=None,
        dest="config_file",
        help="Tina exporter config file location"
    )
    args = parser.parse_args()

    # set up logging
    loglevel = logging.getLevelName(args.loglevel)
    logging.basicConfig(
        level=loglevel,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger('tina-exporter')

    # instantiate the Tina class
    tina = Tina()

    # Register our collector
    REGISTRY.register(TinaCollector())
    # start the built-in webserver
    start_http_server(port=int(tina.exporter_port), registry=REGISTRY)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
