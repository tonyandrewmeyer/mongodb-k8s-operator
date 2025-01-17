#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess
import time
from uuid import uuid4

import pytest
from lightkube import AsyncClient
from lightkube.resources.core_v1 import Pod
from pymongo import MongoClient
from pytest_operator.plugin import OpsTest

from .ha_tests.helpers import (
    deploy_and_scale_application,
    relate_mongodb_and_application,
)
from .helpers import (
    APP_NAME,
    METADATA,
    TEST_DOCUMENTS,
    UNIT_IDS,
    audit_log_line_sanity_check,
    check_if_test_documents_stored,
    check_or_scale_app,
    generate_collection_id,
    get_address_of_unit,
    get_app_name,
    get_leader_id,
    get_mongo_cmd,
    get_password,
    get_secret_content,
    get_secret_id,
    primary_host,
    run_mongo_op,
    secondary_mongo_uris_with_sync_delay,
    set_password,
)

LOG_PATH = "/var/log/mongodb/"

logger = logging.getLogger(__name__)


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    app_name = await get_app_name(ops_test)
    if app_name:
        return await check_or_scale_app(ops_test, app_name, len(UNIT_IDS))

    app_name = APP_NAME
    # build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")
    resources = {"mongodb-image": METADATA["resources"]["mongodb-image"]["upstream-source"]}
    await ops_test.model.deploy(
        charm,
        resources=resources,
        application_name=app_name,
        num_units=len(UNIT_IDS),
        series="jammy",
    )

    # issuing dummy update_status just to trigger an event
    await ops_test.model.set_config({"update-status-hook-interval": "10s"})

    # TODO: remove raise_on_error when we move to juju 3.5 (DPE-4996)
    await ops_test.model.wait_for_idle(
        apps=[app_name], status="active", raise_on_blocked=True, timeout=1000, raise_on_error=False
    )
    assert ops_test.model.applications[app_name].units[0].workload_status == "active"

    # effectively disable the update status from firing
    await ops_test.model.set_config({"update-status-hook-interval": "60m"})


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
@pytest.mark.parametrize("unit_id", UNIT_IDS)
async def test_application_is_up(ops_test: OpsTest, unit_id: int):
    address = await get_address_of_unit(ops_test, unit_id=unit_id)
    response = MongoClient(address, directConnection=True).admin.command("ping")
    assert response["ok"] == 1


@pytest.mark.group(1)
async def test_application_primary(ops_test: OpsTest):
    """Tests existence of primary and verifies the application is running as a replica set.

    By retrieving information about the primary this test inherently tests password retrieval.
    """
    rs_status = await run_mongo_op(ops_test, "rs.status()")
    assert rs_status.succeeded, "mongod had no response for 'rs.status()'"

    primary = [
        member["name"] for member in rs_status.data["members"] if member["stateStr"] == "PRIMARY"
    ][0]

    assert primary, "mongod has no primary on deployment"

    number_of_primaries = 0
    for member in rs_status.data["members"]:
        if member["stateStr"] == "PRIMARY":
            number_of_primaries += 1

    assert number_of_primaries == 1, "more than one primary in replica set"


@pytest.mark.group(1)
async def test_audit_log(ops_test: OpsTest) -> None:
    """Test that audit log was created and contains actual audit data."""
    mongodb_application_name = await get_app_name(ops_test)
    audit_log_path = "/var/log/mongodb/audit.log"

    for unit in ops_test.model.applications[mongodb_application_name].units:
        audit_log = subprocess.check_output(
            f"JUJU_MODEL={ops_test.model_full_name} juju ssh  --container mongod {unit.name}  'cat {audit_log_path}'",
            stderr=subprocess.PIPE,
            shell=True,
            universal_newlines=True,
        )

        for line in audit_log.splitlines():
            if not len(line):
                continue
            item = json.loads(line)
            # basic sanity check
            assert audit_log_line_sanity_check(
                item
            ), "Audit sanity log check failed for first line"


@pytest.mark.group(1)
async def test_log_rotate(ops_test: OpsTest) -> None:
    """Test that log are being rotated."""
    # deploy test application for writing data
    application_name = await deploy_and_scale_application(ops_test)
    mongodb_application_name = await get_app_name(ops_test)
    await relate_mongodb_and_application(ops_test, mongodb_application_name, application_name)

    # Note: this timeout out depends on max log size
    # which is defined in "src/config.py::Config.MAX_LOG_SIZE"
    logrotate_timeout = 60
    audit_log_path = "/var/log/mongodb/"

    for unit in ops_test.model.applications[mongodb_application_name].units:
        log_files = subprocess.check_output(
            f"JUJU_MODEL={ops_test.model_full_name} juju ssh  --container mongod {unit.name}  'ls {LOG_PATH}'",
            stderr=subprocess.PIPE,
            shell=True,
            universal_newlines=True,
        )

        log_not_rotated = "audit.log.1.gz" not in log_files
        assert (
            log_not_rotated
        ), f"Found rotated log in {log_files}, should not have already rotated."

    application_unit = ops_test.model.applications[application_name].units[0]
    start_writes_action = await application_unit.run_action("start-continuous-writes")
    await start_writes_action.wait()

    # before logs get rotated, they must reach the maximum log file size. Wait 10 minutes for
    # enough writes to occur and for data to be written
    time.sleep(600)

    clear_writes_action = await application_unit.run_action("clear-continuous-writes")
    await clear_writes_action.wait()
    time.sleep(logrotate_timeout)  # Just to make sure that logroate will run

    for unit in ops_test.model.applications[mongodb_application_name].units:
        log_files = subprocess.check_output(
            f"JUJU_MODEL={ops_test.model_full_name} juju ssh  --container mongod {unit.name}  'ls {audit_log_path}'",
            stderr=subprocess.PIPE,
            shell=True,
            universal_newlines=True,
        )

        log_rotated = "audit.log.1" in log_files
        assert log_rotated, f"Could not find rotated log in {log_files}"

        audit_log_exists = "audit.log" in log_files
        assert audit_log_exists, f"Could not find audit.log log in {log_files}"

        # wait for some logs to be collected
        time.sleep(10)

        audit_log_content = subprocess.check_output(
            f"JUJU_MODEL={ops_test.model_full_name} juju ssh  --container mongod {unit.name}  'cat {audit_log_path}audit.log'",
            stderr=subprocess.PIPE,
            shell=True,
            universal_newlines=True,
        )
        audit_log_lines = audit_log_content.strip().split("\n")
        assert len(audit_log_lines) > 0, "New audit logs have not been written after log rotation."


@pytest.mark.group(1)
async def test_monitor_user(ops_test: OpsTest) -> None:
    """Test verifies that the monitor user can perform operations such as 'rs.conf()'."""
    app_name = await get_app_name(ops_test)
    unit = ops_test.model.applications[app_name].units[0]
    password = await get_password(ops_test, unit_id=0, username="monitor")
    addresses = [await get_address_of_unit(ops_test, unit_id) for unit_id in UNIT_IDS]
    hosts = ",".join(addresses)
    mongo_uri = f"mongodb://monitor:{password}@{hosts}/admin?"

    admin_mongod_cmd = await get_mongo_cmd(ops_test, unit.name)
    admin_mongod_cmd += f" {mongo_uri} --eval 'rs.conf()'"
    complete_command = f"ssh --container mongod {unit.name} {admin_mongod_cmd}"
    return_code, _, stderr = await ops_test.juju(*complete_command.split())
    assert return_code == 0, f"command rs.conf() on monitor user does not work, error: {stderr}"


@pytest.mark.group(1)
async def test_only_leader_can_set_while_all_can_read_password_secret(ops_test: OpsTest) -> None:
    """Test verifies that only the leader can set a password, while all units can read it."""
    # Setting existing password
    app_name = await get_app_name(ops_test)
    leader_id = await get_leader_id(ops_test, app_name=app_name)
    non_leaders = []
    all_units = []

    for unit in ops_test.model.applications[app_name].units:
        unit_id = int(unit.entity_id.split("/")[-1])
        all_units.append(unit_id)
        if unit_id == leader_id:
            continue
        non_leaders.append(unit_id)

    new_password = "blablabla"
    # get previous password
    old_password = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )
    # attempt to set password from non-leader
    await set_password(
        ops_test,
        unit_id=non_leaders[0],
        username="monitor",
        password=new_password,
        app_name=app_name,
    )
    # get password after attempt to set it up with non-leader
    password1 = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )
    # password should be the same as before
    assert password1 == old_password

    # setting new password with leader
    await set_password(
        ops_test, unit_id=leader_id, username="monitor", password=new_password, app_name=app_name
    )

    # validate that all unit return new password
    for unit_id in all_units:
        password2 = await get_password(
            ops_test, unit_id=unit_id, username="monitor", app_name=app_name
        )
        assert password2 == new_password

    # return password back to old
    await set_password(
        ops_test,
        unit_id=non_leaders[0],
        username="monitor",
        password=old_password,
        app_name=app_name,
    )


@pytest.mark.group(1)
async def test_reset_and_get_password_secret_same_as_cli(ops_test: OpsTest) -> None:
    """Test verifies that we can set and retrieve the correct password using Juju 3.x secrets."""
    app_name = await get_app_name(ops_test)
    new_password = str(uuid4())

    # Re=setting existing password
    leader_id = await get_leader_id(ops_test, app_name=app_name)
    result = await set_password(
        ops_test, unit_id=leader_id, username="monitor", password=new_password, app_name=app_name
    )

    secret_id = result["secret-id"].split("/")[-1]

    # Getting back the pw programmatically
    password = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )

    #
    # No way to retrieve a secet by label for now (https://bugs.launchpad.net/juju/+bug/2037104)
    # Therefore we take advantage of the fact, that we only have ONE single secret a this point
    # So we take the single member of the list
    # NOTE: This would BREAK if for instance units had secrets at the start...
    #
    secret_id = await get_secret_id(ops_test, app_or_unit=app_name)

    # Getting back the pw from juju CLI
    content = await get_secret_content(ops_test, secret_id)

    assert password == new_password
    assert content["monitor-password"] == password


@pytest.mark.group(1)
async def test_empty_password(ops_test: OpsTest) -> None:
    """Test that the password can't be set to an empty string."""
    app_name = await get_app_name(ops_test)
    leader_id = await get_leader_id(ops_test, app_name=app_name)

    password1 = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )
    await set_password(
        ops_test, unit_id=leader_id, username="monitor", password="", app_name=app_name
    )
    password2 = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )

    # The password remained unchanged
    assert password1 == password2


@pytest.mark.group(1)
async def test_no_password_change_on_invalid_password(ops_test: OpsTest) -> None:
    """Test that in general, there is no change when password validation fails."""
    app_name = await get_app_name(ops_test)
    leader_id = await get_leader_id(ops_test)
    password1 = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )

    # The password has to be minimum 3 characters
    await set_password(
        ops_test, unit_id=leader_id, username="monitor", password="ca" * 1000000, app_name=app_name
    )
    password2 = await get_password(
        ops_test, unit_id=leader_id, username="monitor", app_name=app_name
    )

    # The password didn't change
    assert password1 == password2


@pytest.mark.group(1)
async def test_scale_up(ops_test: OpsTest):
    """Tests juju add-unit functionality.

    Verifies that when a new unit is added to the MongoDB application that it is added to the
    MongoDB replica set configuration.
    """
    app_name = await get_app_name(ops_test)
    # add two units and wait for idle
    await ops_test.model.applications[app_name].scale(scale_change=2)
    # TODO: Remove the `raise_on_error` when we move to juju 3.5 (DPE-4996)
    await ops_test.model.wait_for_idle(
        apps=[app_name],
        status="active",
        timeout=1000,
        wait_for_exact_units=5,
        raise_on_error=False,
    )
    num_units = len(ops_test.model.applications[app_name].units)
    assert num_units == 5

    # grab juju hosts
    juju_hosts = [
        f"mongodb-k8s-{unit_id}.mongodb-k8s-endpoints:27017" for unit_id in range(num_units)
    ]

    # connect to replica set uri and get replica set members
    rs_status = await run_mongo_op(ops_test, "rs.status()")
    assert rs_status.succeeded, "mongod had no response for 'rs.status()'"

    mongodb_hosts = [member["name"] for member in rs_status.data["members"]]

    # verify that the replica set members have the correct units
    assert set(mongodb_hosts) == set(juju_hosts), (
        "hosts for mongodb: "
        + str(set(mongodb_hosts))
        + " and juju: "
        + str(set(juju_hosts))
        + " don't match"
    )


@pytest.mark.group(1)
async def test_scale_down(ops_test: OpsTest):
    """Tests juju remove-unit functionality.

    This test verifies:
    1. multiple units can be removed while still maintaining a majority (ie remove a minority)
    2. Replica set hosts are properly updated on unit removal
    """
    app_name = await get_app_name(ops_test)
    # add two units and wait for idle
    await ops_test.model.applications[app_name].scale(scale_change=-2)
    # TODO: Remove the `raise_on_error` when we move to juju 3.5 (DPE-4996)
    await ops_test.model.wait_for_idle(
        apps=[app_name],
        status="active",
        timeout=1000,
        wait_for_exact_units=3,
        raise_on_error=False,
    )
    num_units = len(ops_test.model.applications[app_name].units)
    assert num_units == 3

    # grab juju hosts
    juju_hosts = [
        f"mongodb-k8s-{unit_id}.mongodb-k8s-endpoints:27017" for unit_id in range(num_units)
    ]

    # connect to replica set uri and get replica set members
    rs_status = await run_mongo_op(ops_test, "rs.status()")
    mongodb_hosts = [member["name"] for member in rs_status.data["members"]]

    # verify that the replica set members have the correct units
    assert set(mongodb_hosts) == set(juju_hosts), (
        "hosts for mongodb: "
        + str(set(mongodb_hosts))
        + " and juju: "
        + str(set(juju_hosts))
        + " don't match"
    )

    # verify that the set maintains a primary
    primary = [
        member["name"] for member in rs_status.data["members"] if member["stateStr"] == "PRIMARY"
    ][0]

    assert primary in juju_hosts, "no primary after scaling down"


@pytest.mark.group(1)
async def test_replication_primary_reelection(ops_test: OpsTest):
    """Tests removal of Mongodb primary and the reelection functionality.

    Verifies that after the primary server gets removed,
    a successful reelection happens.
    """
    # retrieve the status of the replica set
    rs_status = await run_mongo_op(ops_test, "rs.status()")
    assert rs_status.succeeded, "mongod had no response for 'rs.status()'"

    # get the primary host from the rs_status response
    primary = primary_host(rs_status.data)
    assert primary, "no primary set"

    replica_name = primary.split(".")[0]

    # Deleting the primary pod using kubectl
    k8s_client = AsyncClient(namespace=ops_test.model_name)
    await k8s_client.delete(Pod, name=replica_name)
    # the median time in which a reelection event happens is after around 12 seconds
    # setting the double to be on the safe side
    time.sleep(24)

    # retrieve the status of the replica set
    rs_status = await run_mongo_op(ops_test, "rs.status()")
    assert rs_status.succeeded, "mongod had no response for 'rs.status()'"

    # get the new primary host after reelection
    new_primary = primary_host(rs_status.data)
    assert new_primary, "no new primary set"
    assert new_primary != primary


@pytest.mark.group(1)
async def test_replication_data_consistency(ops_test: OpsTest):
    """Test the data consistency between the primary and secondaries.

    Verifies that after writing data to the primary the data on
    the secondaries match.
    """
    app_name = await get_app_name(ops_test)
    # generate a collection id
    collection_id = generate_collection_id()

    # Create a database and a collection (lazily)
    create_collection = await run_mongo_op(
        ops_test, f'db.createCollection("{collection_id}")', suffix=f"?replicaSet={app_name}"
    )
    assert create_collection.succeeded and create_collection.data["ok"] == 1
    # Store a few test documents
    insert_many_docs = await run_mongo_op(
        ops_test,
        f"db.{collection_id}.insertMany({TEST_DOCUMENTS})",
        suffix=f"?replicaSet={app_name}",
    )
    assert insert_many_docs.succeeded and len(insert_many_docs.data["insertedIds"]) == 2
    # attempt ensuring that the replication happened on all secondaries
    # 24sec is an arbitrary number that worked well locally in a couple of tests
    # 12 sec being the median time for primary reelection, so I randomly chose a factor
    time.sleep(24)

    # query the primary only
    set_primary_read_pref = await run_mongo_op(
        ops_test,
        'db.getMongo().setReadPref("primary")',
        suffix=f"?replicaSet={app_name}",
        expecting_output=False,
    )
    assert set_primary_read_pref.succeeded
    await check_if_test_documents_stored(ops_test, collection_id)

    # query only from the secondaries
    set_secondary_read_pref = await run_mongo_op(
        ops_test,
        'db.getMongo().setReadPref("secondary")',
        suffix=f"?replicaSet={app_name}",
        expecting_output=False,
    )
    assert set_secondary_read_pref.succeeded
    await check_if_test_documents_stored(ops_test, collection_id)

    # query the secondaries by targeting units
    rs_status = await run_mongo_op(ops_test, "JSON.stringify(rs.status())", stringify=False)
    assert rs_status.succeeded, "mongod had no response for 'rs.status()'"

    # get the secondaries ordered ASC by the least amount of data sync delay
    # compared to the primary, so that we can attempt to delay the documents
    # query until after the said delay is elapsed (using time.sleep)
    secondaries = await secondary_mongo_uris_with_sync_delay(ops_test, rs_status.data)

    # verify that each secondary contains the data
    synced_secondaries_count = 0
    for secondary in secondaries:
        time.sleep(secondary["delay"] + 2)  # probably useless, but attempting
        try:
            await check_if_test_documents_stored(
                ops_test, collection_id, mongo_uri=secondary["uri"]
            )
        except Exception:
            # there may need some time to finish replicating to this specific secondary
            continue

        synced_secondaries_count += 1

    logger.info(
        f"{synced_secondaries_count}/{len(secondaries)} secondaries fully synced with primary."
    )
    assert synced_secondaries_count > 0
