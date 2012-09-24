# -*- coding: utf-8 -*-
"""
delete_collection_view.py

A View to delete a collection for a user
"""
import httplib
import json
import logging

import flask

from tools.greenlet_database_util import GetConnection
from tools.collection import compute_default_collection_name
from tools.customer_key_lookup import CustomerKeyConnectionLookup

from web_collection_manager.connection_pool_view import ConnectionPoolView
from web_collection_manager.authenticator import authenticate

rules = ["/customers/<username>/collections/<collection_name>", ]
endpoint = "delete_collection"

def _delete_collection(cursor, customer_id, collection_name):
    """
    mark the collection as deleted
    """
    log = logging.getLogger("_delete_collection")
    cursor.execute("""
        select id
        from nimbusio_central.collection
        where customer_id = %s and name = %s""", 
        [customer_id, collection_name, ])
    result = cursor.fetchone()
    if result is None:
        log.warn("attempt to delete unknown collection {0}".format(
            collection_name))
        return False
    (row_id, ) = result

    deleted_name = "".join(["__deleted__{0}__".format(row_id), 
                            collection_name, ])
    log.debug("renaming deleted collection to  {0}".format(deleted_name))
    cursor.execute("""
        update nimbusio_central.collection
        set deletion_time = current_timestamp,
            name = %s
        where id = %s
        """, [deleted_name, row_id])

    return True

class DeleteCollectionView(ConnectionPoolView):
    methods = ["DELETE", "POST", ]

    def dispatch_request(self, username, collection_name):
        log = logging.getLogger("DeleteCollectionView")
        log.info("user_name = {0}, collection_name = {1}".format(
            username, collection_name))

        assert flask.request.method == "DELETE" or \
            (flask.request.method == "POST" \
             and flask.request.args["action"] == "delete"), \
                (flask.request.method, flask.request.args, )

        with GetConnection(self.connection_pool) as connection:

            customer_key_lookup = \
                CustomerKeyConnectionLookup(self.memcached_client,
                                            connection)
            customer_id = authenticate(customer_key_lookup,
                                       username,
                                       flask.request)
            if customer_id is None:
                flask.abort(httplib.UNAUTHORIZED)

            # you can't delete your default collection
            default_collection_name = compute_default_collection_name(username)
            if collection_name == default_collection_name:
                log.warn("attempt to delete default collection {0}".format(
                    default_collection_name))
                flask.abort(httplib.METHOD_NOT_ALLOWED)

            # TODO: can't delete a collection that contains keys

            cursor = connection.cursor()
            try:
                deleted = _delete_collection(cursor, 
                                             customer_id, 
                                             collection_name)
            except Exception:
                cursor.close()
                connection.rollback()
                raise

        cursor.close()

        # Ticket #39 collection manager allows authenticated users to set 
        # versioning property on collections they don't own
        if not deleted:
            connection.rollback()
            flask.abort(httplib.FORBIDDEN)

        connection.commit()

        # Ticket #33 Make Nimbus.io API responses consistently JSON
        collection_dict = {"success" : True}
        return flask.Response(json.dumps(collection_dict, 
                                         sort_keys=True, 
                                         indent=4), 
                              status=httplib.OK,
                              content_type="application/json")

view_function = DeleteCollectionView.as_view(endpoint)

