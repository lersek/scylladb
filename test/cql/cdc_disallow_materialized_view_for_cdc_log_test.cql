-- Error messages contain a keyspace name. Make the output stable.
-- CDC and tablets are not working together yet, turn them off.
CREATE KEYSPACE ks
    WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 1} AND
    tablets = {'enabled': false};

create table ks.tb1 (pk int primary key, v int) with cdc = {'enabled': true};

create materialized view ks.tb1_mv as select * from ks.tb1_scylla_cdc_log where v is not null primary key (v);
DROP KEYSPACE ks;
