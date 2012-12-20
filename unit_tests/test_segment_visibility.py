# -*- coding: utf-8 -*-
"""
test_segment_visibility.py

see Ticket #67 Continue implementation of segment visibility subsystem

To run this, first create test user and database:
 sudo -u postgres createuser -P nimbusio_node_user_test
 sudo -u postgres createdb -O nimbusio_node_user_test nimbusio_node.test

To work with the generated debug output files, I generally edit them directly
in vim.  You can run them on the command line like this, or just use piping
straight from vim.
 sudo -u postgres psql nimbusio_node.test < /tmp/debug.sql

"""


from collections import Counter
import logging
import os
import os.path
import subprocess
import sys
try:
    import unittest2 as unittest
except ImportError:
    import unittest

import psycopg2
import psycopg2.extensions
psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)
from psycopg2.extras import RealDictConnection

from tools.database_connection import get_node_database_dsn

from tools.process_util import identify_program_dir
from tools.database_connection import _node_database_name, \
    _node_database_user, \
    get_node_connection 

from segment_visibility.sql_factory import collectable_archive, \
    list_versions, \
    list_keys, \
    version_for_key, \
    mogrify

_write_debug_sql = int(os.environ.get("WRITE_DEBUG_SQL", "0"))

_node_name = "test"
_database_password = "test_password"
_database_host = os.environ.get("NIMBUSIO_NODE_DATABASE_HOST", "localhost")
_database_port = int(os.environ.get("NIMBUSIO_NODE_DATABASE_PORT", "5432"))

_test_collection_id = 1
_test_key = 'key-10'
_test_prefix = 'key-1'
_test_unified_id = None    # define later
_test_no_such_unified_id = 1

def _initialize_logging_to_stderr():
    from tools.standard_logging import _log_format_template
    log_level = logging.DEBUG
    handler = logging.StreamHandler(stream=sys.stderr)
    formatter = logging.Formatter(_log_format_template)
    handler.setFormatter(formatter)
    logging.root.addHandler(handler)
    logging.root.setLevel(log_level)

def _install_schema_and_test_data():
    log = logging.getLogger("_install_schema")
    database_name = _node_database_name(_node_name)
    user_name = _node_database_user(_node_name)

    sql_path = identify_program_dir("sql")
    schema_path = os.path.join(sql_path, "nimbusio_node.sql")

    env = {"PGPASSWORD" : _database_password};
    args = ["/usr/bin/psql", 
            "-h", _database_host,
            "-p", str(_database_port),
            "-d", database_name, 
            "-U", user_name,
            "-f", schema_path]
    log.debug(args)

    process = subprocess.Popen(args, env=env)
    process.wait()
    assert process.returncode == 0, process.returncode

    test_data_path = os.path.join(sql_path, "test_gc.sql")
    env = {"PGPASSWORD" : _database_password};
    args = ["/usr/bin/psql", 
            "-h", _database_host,
            "-p", str(_database_port),
            "-d", database_name, 
            "-U", user_name,
            "-f", test_data_path]
    log.debug(args)

    process = subprocess.Popen(args, env=env)
    process.wait()
    assert process.returncode == 0, process.returncode

class TestSegmentVisibility(unittest.TestCase):
    """
    test segment visibility subsystem
    """
    def setUp(self):
        log = logging.getLogger("setUp")
        log.debug("installing schema and test data")
        _install_schema_and_test_data()
        log.debug("creating database connection")
        self._connection = RealDictConnection(
            get_node_database_dsn(_node_name, 
                                  _database_password, 
                                  _database_host, 
                                  _database_port))
        log.debug("setup done")

    def tearDown(self):
        log = logging.getLogger("tearDown")        
        log.debug("teardown starts")
        if hasattr(self, "_connection"):
            self._connection.close()
            delattr(self, "_connection")
        log.debug("teardown done")

    def _retrieve_collectables(self, versioned):
        """
        check that none of these rows appear in any other result.
        check that the rows from other results are not included here.
        """
        sql_text = collectable_archive(_test_collection_id, 
                                       versioned=versioned, 
                                       key=_test_key, 
                                       unified_id=None)

        args = {"collection_id" : _test_collection_id,
                "key"           : _test_key,
                "unified_id"    : None}

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        rows = cursor.fetchall()
        cursor.close()

        return set([(r["key"], r["unified_id"], ) for r in rows])

    def test_no_such_collectable(self):
        """
        test retrieving garbage collectable segments
        """
        log = logging.getLogger("test_no_such_collectable")

        sql_text = collectable_archive(_test_collection_id, 
                                       versioned=True, 
                                       key=_test_key, 
                                       unified_id=_test_no_such_unified_id)

        args = {"collection_id" : _test_collection_id,
                "key"           : _test_key,
                "unified_id"    : _test_no_such_unified_id}

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        rows = cursor.fetchall()
        cursor.close()
        self.assertEqual(len(rows), 0, rows)

    def test_list(self):
        """
        test listing keys and versions of keys
        """
        log = logging.getLogger("test_list")

        versioned = False
        sql_text = list_versions(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix) 

        args = {"collection_id" : _test_collection_id,
                "versioned"     : versioned,
                "prefix"        : _test_prefix, }

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        unversioned_rows = cursor.fetchall()
        cursor.close()

        collectable_set = self._retrieve_collectables(versioned)
        test_set = set([(r["key"], r["unified_id"], ) for r in unversioned_rows])
        collectable_intersection = test_set & collectable_set
        self.assertEqual(len(collectable_intersection), 0, 
                         collectable_intersection)

        # check that there's no more than one row per key for a non-versioned 
        # collection
        # check that every row begins with prefix
        unversioned_key_counts = Counter()
        for row in unversioned_rows:
            unversioned_key_counts[row["key"]] += 1
            self.assertTrue(row["key"].startswith(_test_prefix))
        for key, value in unversioned_key_counts.items():
            self.assertEqual(value, 1, (key, value))

        versioned = True
        sql_text = list_versions(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix)

        args = {"collection_id" : _test_collection_id,
                "prefix"        : _test_prefix, }

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        versioned_rows = cursor.fetchall()
        cursor.close()

        collectable_set = self._retrieve_collectables(versioned)
        test_set = set([(r["key"], r["unified_id"], ) for r in versioned_rows])
        collectable_intersection = test_set & collectable_set
        self.assertEqual(len(collectable_intersection), 0, 
                         collectable_intersection)
        
        versioned_key_counts = Counter()
        for row in versioned_rows:
            versioned_key_counts[row["key"]] += 1
            self.assertTrue(row["key"].startswith(_test_prefix))

        # check that there's >= as many rows now as above.
        for key, value in versioned_key_counts.items():
            self.assertTrue(value >= versioned_key_counts[key], (key, value))

        versioned = False
        sql_text = list_keys(_test_collection_id, 
                             versioned=versioned, 
                             prefix=_test_prefix)

        args = {"collection_id" : _test_collection_id,
                "prefix"        : _test_prefix, }

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        key_unversioned_rows = cursor.fetchall()
        cursor.close()

        collectable_set = self._retrieve_collectables(versioned)
        test_set = set([(r["key"], r["unified_id"], ) for r in key_unversioned_rows])
        collectable_intersection = test_set & collectable_set
        self.assertEqual(len(collectable_intersection), 0, 
                         collectable_intersection)

        # check that the list keys result is the same as list_versions in the
        # unversioned case above (although there could be extra columns.)
        key_unversioned_counts = Counter()
        for row in key_unversioned_rows:
            key_unversioned_counts[row["key"]] += 1
            self.assertTrue(row["key"].startswith(_test_prefix))
        for key, value in key_unversioned_counts.items():
            self.assertEqual(value, 1, (key, value))
        for key_row, version_row in zip(key_unversioned_rows, unversioned_rows):
            self.assertEqual(key_row["key"], version_row["key"])
            self.assertEqual(key_row["unified_id"], version_row["unified_id"])

        versioned = True
        sql_text = list_versions(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix)

        args = {"collection_id" : _test_collection_id,
                "prefix"        : _test_prefix, }

        cursor = self._connection.cursor()
        cursor.execute(sql_text, args)
        key_versioned_rows = cursor.fetchall()
        cursor.close()

        collectable_set = self._retrieve_collectables(versioned)
        test_set = set([(r["key"], r["unified_id"], ) for r in key_versioned_rows])
        collectable_intersection = test_set & collectable_set
        self.assertEqual(len(collectable_intersection), 0, 
                         collectable_intersection)

        key_versioned_counts = Counter()
        for row in key_versioned_rows:
            key_versioned_counts[row["key"]] += 1
            self.assertTrue(row["key"].startswith(_test_prefix))

    @unittest.skip("isolate test")
    def test_limits_and_markers(self):
        """
        check that the limits and markers work correctly. 
        perhaps take the result with limit=None, and run a series of queries 
        with limit=1 for each of those rows, checking results.
        """
        log = logging.getLogger("test_limits_and_markers")

        for versioned in [True, False]:
            sql_text = list_keys(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix)

            args = {"collection_id" : _test_collection_id,
                    "prefix"        : _test_prefix, }

            cursor = self._connection.cursor()
            cursor.execute(sql_text, args)
            baseline_rows = cursor.fetchall()
            cursor.close()

            key_marker = None
            for row in baseline_rows:
                sql_text = list_keys(_test_collection_id, 
                                     versioned=versioned, 
                                     prefix=_test_prefix,
                                     key_marker=key_marker,
                                     limit=1)

                args = {"collection_id" : _test_collection_id,
                        "prefix"        : _test_prefix, 
                        "key_marker"    : key_marker,
                        "limit"         : 1}

                cursor = self._connection.cursor()
                cursor.execute(sql_text, args)
                test_row = cursor.fetchone()
                cursor.close()
                
                self.assertEqual(test_row["key"], row["key"], 
                                 (test_row["key"], row["key"]))
                self.assertEqual(test_row["unified_id"], row["unified_id"], 
                                 (test_row["unified_id"], row["unified_id"]))

                key_marker = test_row["key"]

        for versioned in [True, False]:
            sql_text = list_versions(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix)

            args = {"collection_id" : _test_collection_id,
                    "prefix"        : _test_prefix, }

            cursor = self._connection.cursor()
            cursor.execute(sql_text, args)
            baseline_rows = cursor.fetchall()
            cursor.close()

            key_marker = None
            version_marker = None
            for row in baseline_rows:
                sql_text = list_versions(_test_collection_id, 
                                     versioned=versioned, 
                                     prefix=_test_prefix,
                                     key_marker=key_marker,
                                     version_marker=version_marker,
                                     limit=1)

                args = {"collection_id" : _test_collection_id,
                        "prefix"        : _test_prefix, 
                        "key_marker"    : key_marker,
                        "version_marker": version_marker,
                        "limit"         : 1}

                cursor = self._connection.cursor()
                cursor.execute(sql_text, args)
                test_row = cursor.fetchone()
                cursor.close()
                
                log.info("{0}, {1}".format(test_row["key"], row["key"]))
                log.debug(sql_text)

                self.assertEqual(test_row["key"], row["key"], 
                                 (versioned, test_row["key"], row["key"]))
                self.assertEqual(test_row["unified_id"], row["unified_id"], 
                                 (test_row["unified_id"], row["unified_id"]))

                key_marker = test_row["key"]
                version_marker = test_row["unified_id"]

    def test_version_for_key(self):
        """
        version_for_key 
        """
        log = logging.getLogger("test_version_for_key")

        # check that for every row in list_keys, calling version_for_key with
        # unified_id=None should return the same row, regardless of it being 
        # versioned or not.
        for versioned in [True, False]:
            sql_text = list_keys(_test_collection_id, 
                                 versioned=versioned, 
                                 prefix=_test_prefix)

            args = {"collection_id" : _test_collection_id,
                    "prefix"        : _test_prefix, }

            cursor = self._connection.cursor()
            cursor.execute(sql_text, args)
            baseline_rows = cursor.fetchall()
            cursor.close()

            for row in baseline_rows:
                sql_text = version_for_key(_test_collection_id, 
                                           versioned=versioned, 
                                           key=row["key"])

                args = {"collection_id" : _test_collection_id,
                        "key"           : row["key"]} 

                cursor = self._connection.cursor()
                if _write_debug_sql:
                    with open("/tmp/debug.sql", "w") as debug_sql_file:
                        debug_sql_file.write(mogrify(sql_text, args))
                cursor.execute(sql_text, args)
                test_rows = cursor.fetchall()
                cursor.close()

                # 2012-12-20 dougfort -- list_keys and list_versions only
                # retrieve one conjoined part, but version_for_key retrieves
                # all conjoined parts. So we may have more than one row here.
                self.assertTrue(len(test_rows) > 0) 
                for test_row in test_rows:
                    self.assertEqual(test_row["key"], row["key"], 
                                     (test_row["key"], row["key"]))
                    self.assertEqual(test_row["unified_id"], row["unified_id"], 
                                     (test_row["unified_id"], row["unified_id"]))

        # check that these return empty
        for versioned in [True, False]:
            sql_text = version_for_key(_test_collection_id, 
                                       versioned=versioned, 
                                       key=_test_key,
                                       unified_id=_test_no_such_unified_id)

            args = {"collection_id" : _test_collection_id,
                    "key"           : row["key"],
                    "unified_id"    : _test_no_such_unified_id} 

            cursor = self._connection.cursor()
            cursor.execute(sql_text, args)
            test_rows = cursor.fetchall()
            cursor.close()
            self.assertEqual(len(test_rows), 0, test_rows)

if __name__ == "__main__":
    _initialize_logging_to_stderr()
    unittest.main()
