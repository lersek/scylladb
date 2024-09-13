#
# Copyright (C) 2024-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import pytest
import logging
import asyncio

from test.pylib.manager_client import ManagerClient
from test.pylib.rest_client import inject_error_one_shot
from test.topology.conftest import skip_mode

logger = logging.getLogger(__name__)

async def disable_autocompaction_across_keyspaces(manager: ManagerClient, server_ip_addr: str, *keyspace_list):
    """Disable autocompaction that might interfere with testing"""

    logger.info("Disabling autocompaction across keyspaces")
    for ks in (*keyspace_list, "system", "system_schema"):
        await manager.api.disable_autocompaction(server_ip_addr, ks)

@pytest.mark.asyncio
@pytest.mark.parametrize("consider_only_existing_data", [True, False])
@skip_mode('release', 'error injections are not supported in release mode')
async def test_major_compaction_consider_only_existing_data(manager: ManagerClient, consider_only_existing_data):
    """
    Test compactions drop tombstones when consider_only_existing_data is enabled.
    1. Create a single node cluster.
    2. Write some keys and then delete a few to create tombstones.
    3. Start major compaction with consider_only_existing_data=true but make it wait
        through error injection right after it has collected the sstables for compaction.
    4. Insert the deleted keys with backdated data into memtables and flush one
        of them into a new sstable that will not be a part of the compaction.
    5. Resume the major compaction and let it complete.
    6. Verify the results.
       - If consider_only_existing_data is False, the tombstones should not be purged and the backdated rows should not be visible
       - If consider_only_existing_data is True, the tombstones should be purged and the backdated rows should be visible
    """
    logger.info("Bootstrapping cluster")
    server = (await manager.servers_add(1))[0]

    logger.info("Creating table")
    ks = "test_consider_only_existing_data"
    cf = "t1"
    cql = manager.get_cql()
    await cql.run_async(f"CREATE KEYSPACE {ks} WITH replication = {{'class': 'NetworkTopologyStrategy', 'replication_factor': 1}}")
    await cql.run_async(f"CREATE TABLE {ks}.{cf} (pk int PRIMARY KEY) WITH tombstone_gc = {{'mode': 'immediate'}}")
    await disable_autocompaction_across_keyspaces(manager, server.ip_addr, ks)

    logger.info("Populating table")
    await asyncio.gather(*[cql.run_async(f"INSERT INTO {ks}.{cf} (pk) VALUES ({k});") for k in range(20)])
    await asyncio.gather(*[cql.run_async(f"DELETE FROM {ks}.{cf} WHERE pk = {k};") for k in range(10)])
    await manager.api.keyspace_flush(server.ip_addr, ks, cf)

    # let a second pass, so that the tombstones are eligible for gc
    await asyncio.sleep(1)

    # error injection to make compaction wait after collecting sstables
    injection = "major_compaction_wait"
    injection_handler = await inject_error_one_shot(manager.api, server.ip_addr, injection)

    logger.info("Start major compaction")
    log = await manager.server_open_log(server.server_id)
    mark = await log.mark()
    compaction_task = asyncio.create_task(manager.api.keyspace_compaction(server.ip_addr, ks, cf, consider_only_existing_data=consider_only_existing_data))
    # wait for the injection to pause the compaction
    await log.wait_for("major_compaction_wait: waiting", from_mark=mark, timeout=30)

    # insert new backdated rows with deleted keys and flush them
    # into a new sstable that will not be part of the major compaction
    logger.info("Insert backdated data into the table")
    await asyncio.gather(*[cql.run_async(f"INSERT INTO {ks}.{cf} (pk) VALUES ({k}) USING TIMESTAMP 1;") for k in range(5)])
    await manager.api.keyspace_flush(server.ip_addr, ks, cf)

    # insert few more rows with deleted keys with backdated data into memtable
    await asyncio.gather(*[cql.run_async(f"INSERT INTO {ks}.{cf} (pk) VALUES ({k}) USING TIMESTAMP 1;") for k in range(5, 10)])

    # resume compaction
    await injection_handler.message()
    await compaction_task

    # evict cache to make backdated data visible for consider_only_existing_data mode
    if consider_only_existing_data:
        await manager.api.drop_sstable_caches(server.ip_addr)

    logger.info("Verify major compaction results")
    expected_count = 1 if consider_only_existing_data else 0
    for k in range(10):
        assert len(await cql.run_async(f"SELECT * FROM {ks}.{cf} WHERE pk = {k}")) == expected_count

@pytest.mark.asyncio
@pytest.mark.parametrize("compaction_flush_all_tables_before_major_seconds", [0, 2, 10])
async def test_major_compaction_flush_all_tables(manager: ManagerClient, compaction_flush_all_tables_before_major_seconds):
    """
    1. Start server with configured compaction_flush_all_tables_before_major_seconds value
    2. Create table and insert few rows
    3. Run major compaction and verify if all tables were flushed
       - if compaction_flush_all_tables_before_major_seconds == 0, expect no flush to happen
       - if compaction_flush_all_tables_before_major_seconds == 2 or 10, expect all tables to be flushed
    4. Sleep for 2 seconds
    3. Run major compaction again and verify if all tables were flushed
       - if compaction_flush_all_tables_before_major_seconds == 0, expect no flush to happen
       - if compaction_flush_all_tables_before_major_seconds == 2, expect all tables to be flushed as 2 seconds have elapsed already
       - if compaction_flush_all_tables_before_major_seconds == 10, expect no flush to happen as only 2 seconds have elapsed
    """
    logger.info("Bootstrapping cluster")
    cfg = {'compaction_flush_all_tables_before_major_seconds' : compaction_flush_all_tables_before_major_seconds}
    server = (await manager.servers_add(1, config=cfg, cmdline=['--smp=1']))[0]

    logger.info("Creating table")
    ks = "test_flush_all_tables"
    cf = "t1"
    cql = manager.get_cql()
    await cql.run_async(f"CREATE KEYSPACE {ks} WITH replication = {{'class': 'NetworkTopologyStrategy', 'replication_factor': 1}}")
    await cql.run_async(f"CREATE TABLE {ks}.{cf} (pk int PRIMARY KEY)")
    await disable_autocompaction_across_keyspaces(manager, server.ip_addr, ks)

    logger.info("Populating table")
    await asyncio.gather(*[cql.run_async(f"INSERT INTO {ks}.{cf} (pk) VALUES ({k});") for k in range(256)])
    await manager.api.keyspace_flush(server.ip_addr, ks, cf)
    log = await manager.server_open_log(server.server_id)

    async def check_all_table_flush_in_major_compaction(expect_all_table_flush: bool):
        mark = await log.mark()

        logger.info("Start major compaction")
        await manager.api.keyspace_compaction(server.ip_addr, ks, cf)

        flush_log = await log.grep("Forcing new commitlog segment and flushing all tables", from_mark=mark)
        assert len(flush_log) == (1 if expect_all_table_flush else 0)

    # all tables should be flushed the first time unless compaction_flush_all_tables_before_major_seconds == 0
    await check_all_table_flush_in_major_compaction(compaction_flush_all_tables_before_major_seconds != 0)

    if compaction_flush_all_tables_before_major_seconds == 2:
        # let 2 seconds pass before trying again
        await asyncio.sleep(compaction_flush_all_tables_before_major_seconds)

    # for the second time, all tables should be flushed only if
    # compaction_flush_all_tables_before_major_seconds == 2 as only 2 seconds have passed
    await check_all_table_flush_in_major_compaction(compaction_flush_all_tables_before_major_seconds == 2)