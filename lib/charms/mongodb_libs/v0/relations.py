# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""In this class we manage client database relations.

This class creates user and database for each application relation
and expose needed information for client connection via fields in
external relation.
"""

import re
import logging
from typing import Optional, Set

from charms.mongodb_libs.v0.helpers import generate_password
from charms.mongodb_libs.v0.mongodb import (
    MongoDBConfiguration,
    MongoDBConnection,
)
from ops.framework import Object
from ops.model import Relation
from ops.charm import RelationBrokenEvent
from pymongo.errors import PyMongoError

# The unique Charmhub library identifier, never change it
LIBID = "1057f353503741a98ed79309b5be7e32"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version.
LIBPATCH = 0

logger = logging.getLogger(__name__)
REL_NAME = "database"


class MongoDBClientRelation(Object):
    """In this class we manage client database relations."""

    def __init__(self, charm):
        """Manager of MongoDB client relations."""
        super().__init__(charm, "client-relations")
        self.charm = charm
        self.framework.observe(self.charm.on[REL_NAME].relation_joined, self._on_relation_event)
        self.framework.observe(self.charm.on[REL_NAME].relation_changed, self._on_relation_event)
        self.framework.observe(self.charm.on[REL_NAME].relation_broken, self._on_relation_event)

    def _on_relation_event(self, event):
        """Handle relation joined events.

        When a new relation joins the :class:`MongoProvider` sets relation
        data, that the related charm can use for accessing the MongoDB
        database.
        """
        if not self.charm.unit.is_leader():
            return
        if "db_initialised" not in self.charm.app_data:
            return

        departed_relation_id = None
        if type(event) is RelationBrokenEvent:
            departed_relation_id = event.relation.id

        try:
            self.reconcile(departed_relation_id)
        except PyMongoError as e:
            logger.error("Deferring _on_relation_event since: error=%r", e)
            event.defer()
            return

    def reconcile(self, departed_relation_id: Optional[int]):
        """Forced reconciliation of all current relations."""
        with MongoDBConnection(self.charm.mongodb_config) as mongo:
            database_users = mongo.get_users()
            relation_users = self._get_users_from_relations(departed_relation_id)

            for username in database_users - relation_users:
                logger.info("Remove relation user: %s", username)
                mongo.drop_user(username)

            for username in relation_users - database_users:
                config = self._get_config(username)
                if config.database is None:
                    # We need to wait for moment when provider library
                    # set the database name into the relation.
                    continue
                logger.info("Create relation user: %s on %s", config.username, config.database)
                mongo.create_user(config)
                self._set_relation(config)

            for username in relation_users.intersection(database_users):
                config = self._get_config(username)
                logger.info("Update relation user: %s on %s", config.username, config.database)
                mongo.update_user(config)

            if not self.charm.model.config["auto-delete"]:
                return

            database_dbs = mongo.get_databases()
            relation_dbs = self._get_databases_from_relations(departed_relation_id)
            for database in database_dbs - relation_dbs:
                logger.info("Drop database: %s", database)
                mongo.drop_database(database)

    def _get_config(self, username: str) -> MongoDBConfiguration:
        """Construct config object for future user creation."""
        relation = self._get_relation(username)
        return MongoDBConfiguration(
            replset=self.charm.app.name,
            database=self._get_relation_database(relation),
            username=username,
            password=generate_password(),
            hosts=self.charm.mongodb_config.hosts,
            roles=self._get_relation_roles(relation),
        )

    def _set_relation(self, config: MongoDBConfiguration):
        """Save all output fields into application relation."""
        relation = self._get_relation(config.username)
        if relation is None:
            return None
        relation.data[self.charm.app]["username"] = config.username
        relation.data[self.charm.app]["password"] = config.password
        relation.data[self.charm.app]["database"] = config.password
        relation.data[self.charm.app]["endpoints"] = ",".join(config.hosts)
        relation.data[self.charm.app]["replset"] = config.replset
        relation.data[self.charm.app]["uris"] = config.uri

    @staticmethod
    def _get_username(relation_id: str) -> str:
        """Construct username."""
        return f"relation-{relation_id}"

    def _get_users_from_relations(self, departed_relation_id: Optional[int]):
        """Return usernames for all relations."""
        relations = self.model.relations[REL_NAME]
        return set([
            self._get_username(relation.id)
            for relation in relations
            if relation.id != departed_relation_id
        ])

    def _get_databases_from_relations(self, departed_relation_id: Optional[int]) -> Set[str]:
        """Return database names from all relations."""
        relations = self.model.relations[REL_NAME]
        return set([
            self._get_relation_database(relation)
            for relation in relations
            if relation.id != departed_relation_id
        ])

    def _get_relation(self, username: str) -> Relation:
        """Parse relation ID from a username and return Relation object."""
        match = re.match(r"^relation-(\d+)$", username)
        # We generated username in `_get_users_from_relations`
        # func and passed it into this function later.
        # It means the username here MUST match to regex.
        assert match is not None, "No relation match"
        relation_id = int(match.group(1))
        return self.model.get_relation(REL_NAME, relation_id)

    def _get_relation_database(self, relation: Relation) -> str:
        """Return database name from relation."""
        return relation.data[self.charm.app].get("database", None)

    def _get_relation_roles(self, relation: Relation) -> Set[str]:
        """Return additional user roles from relation if specified or return None."""
        roles = relation.data[self.charm.app].get("extra-user-roles", "default")
        return set(roles.split(","))