# Copyright 2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test AsyncIOMotorCursor."""
import asyncio
import sys
import unittest
from functools import partial
from unittest import SkipTest

import greenlet
from pymongo.errors import InvalidOperation, ExecutionTimeout
from pymongo.errors import OperationFailure
from motor import motor_asyncio

from test.utils import one, safe_get
from test.asyncio_tests import (asyncio_test, AsyncIOTestCase,
                                server_is_mongos, at_least, get_command_line)


class TestAsyncIOCursor(AsyncIOTestCase):
    def test_cursor(self):
        cursor = self.collection.find()
        self.assertTrue(isinstance(cursor, motor_asyncio.AsyncIOMotorCursor))
        self.assertFalse(cursor.started, "Cursor shouldn't start immediately")

    @asyncio_test
    def test_count(self):
        yield from self.make_test_data()
        coll = self.collection
        self.assertEqual(200, (yield from coll.find().count()))
        self.assertEqual(
            100,
            (yield from coll.find({'_id': {'$gt': 99}}).count()))

        where = 'this._id % 2 == 0 && this._id >= 50'
        self.assertEqual(75, (yield from coll.find().where(where).count()))

    @asyncio_test
    def test_fetch_next(self):
        yield from self.make_test_data()
        coll = self.collection
        # 200 results, only including _id field, sorted by _id.
        cursor = coll.find({}, {'_id': 1}).sort('_id').batch_size(75)

        self.assertEqual(None, cursor.cursor_id)
        self.assertEqual(None, cursor.next_object())  # Haven't fetched yet.
        i = 0
        while (yield from cursor.fetch_next):
            self.assertEqual({'_id': i}, cursor.next_object())
            i += 1
            # With batch_size 75 and 200 results, cursor should be exhausted on
            # the server by third fetch.
            if i <= 150:
                self.assertNotEqual(0, cursor.cursor_id)
            else:
                self.assertEqual(0, cursor.cursor_id)

        self.assertEqual(False, (yield from cursor.fetch_next))
        self.assertEqual(None, cursor.next_object())
        self.assertEqual(0, cursor.cursor_id)
        self.assertEqual(200, i)

    @asyncio_test
    def test_fetch_next_delete(self):
        coll = self.collection
        yield from coll.insert({})

        # Decref'ing the cursor eventually closes it on the server.
        cursor = coll.find()
        yield from cursor.fetch_next
        cursor_id = cursor.cursor_id
        retrieved = cursor.delegate._Cursor__retrieved
        del cursor
        yield from self.wait_for_cursor(coll, cursor_id, retrieved)

    @asyncio_test
    def test_fetch_next_without_results(self):
        coll = self.collection
        # Nothing matches this query.
        cursor = coll.find({'foo': 'bar'})
        self.assertEqual(None, cursor.next_object())
        self.assertEqual(False, (yield from cursor.fetch_next))
        self.assertEqual(None, cursor.next_object())
        # Now cursor knows it's exhausted.
        self.assertEqual(0, cursor.cursor_id)

    @asyncio_test
    def test_fetch_next_is_idempotent(self):
        # Subsequent calls to fetch_next don't do anything
        yield from self.make_test_data()
        coll = self.collection
        cursor = coll.find()
        self.assertEqual(None, cursor.cursor_id)
        yield from cursor.fetch_next
        self.assertTrue(cursor.cursor_id)
        self.assertEqual(101, cursor._buffer_size())
        yield from cursor.fetch_next  # Does nothing
        self.assertEqual(101, cursor._buffer_size())
        yield from cursor.close()

    @asyncio_test
    def test_fetch_next_exception(self):
        coll = self.collection
        cursor = coll.find()
        cursor.delegate._Cursor__id = 1234  # Not valid on server.

        with self.assertRaises(OperationFailure):
            yield from cursor.fetch_next

        # Avoid the cursor trying to close itself when it goes out of scope.
        cursor.delegate._Cursor__id = None

    @asyncio_test
    def test_each(self):
        yield from self.make_test_data()
        cursor = self.collection.find({}, {'_id': 1}).sort('_id')
        future = asyncio.Future(loop=self.loop)
        results = []

        def callback(result, error):
            if error:
                raise error

            if result is not None:
                results.append(result)
            else:
                # Done iterating.
                future.set_result(True)

        cursor.each(callback)
        yield from future
        expected = [{'_id': i} for i in range(200)]
        self.assertEqual(expected, results)

    @asyncio_test
    def test_to_list_argument_checking(self):
        # We need more than 10 documents so the cursor stays alive.
        yield from self.make_test_data()
        coll = self.collection
        cursor = coll.find()
        with self.assertRaises(ValueError):
            yield from cursor.to_list(-1)

        with self.assertRaises(TypeError):
            yield from cursor.to_list('foo')

    @asyncio_test
    def test_to_list_with_length(self):
        yield from self.make_test_data()
        coll = self.collection
        cursor = coll.find().sort('_id')

        def expected(start, stop):
            return [{'_id': i} for i in range(start, stop)]

        self.assertEqual(expected(0, 10), (yield from cursor.to_list(10)))
        self.assertEqual(expected(10, 100), (yield from cursor.to_list(90)))

        # Test particularly rigorously around the 101-doc mark, since this is
        # where the first batch ends
        self.assertEqual(expected(100, 101), (yield from cursor.to_list(1)))
        self.assertEqual(expected(101, 102), (yield from cursor.to_list(1)))
        self.assertEqual(expected(102, 103), (yield from cursor.to_list(1)))
        self.assertEqual([], (yield from cursor.to_list(0)))
        self.assertEqual(expected(103, 105), (yield from cursor.to_list(2)))

        # Only 95 docs left, make sure length=100 doesn't error or hang
        self.assertEqual(expected(105, 200), (yield from cursor.to_list(100)))
        self.assertEqual(0, cursor.cursor_id)
        yield from cursor.close()

    @asyncio_test
    def test_to_list_with_length_of_none(self):
        yield from self.make_test_data()
        collection = self.collection
        cursor = collection.find()
        docs = yield from cursor.to_list(None)  # Unlimited.
        count = yield from collection.count()
        self.assertEqual(count, len(docs))

    @asyncio_test
    def test_to_list_tailable(self):
        coll = self.collection
        cursor = coll.find(tailable=True)

        # Can't call to_list on tailable cursor.
        with self.assertRaises(InvalidOperation):
            yield from cursor.to_list(10)

    @asyncio_test
    def test_limit_zero(self):
        # Limit of 0 is a weird case that PyMongo handles specially, make sure
        # Motor does too. cursor.limit(0) means "remove limit", but cursor[:0]
        # or cursor[5:5] sets the cursor to "empty".
        coll = self.collection

        # make sure we do not have _id: 1
        yield from coll.remove({'_id': 1})

        yield from coll.insert({'_id': 1})
        resp = yield from coll.find()[:0].fetch_next
        self.assertEqual(False, resp)
        resp = yield from coll.find()[5:5].fetch_next
        self.assertEqual(False, resp)

        resp = yield from coll.find()[:0].to_list(length=1000)
        self.assertEqual([], resp)
        resp = yield from coll.find()[5:5].to_list(length=1000)
        self.assertEqual([], resp)

    @asyncio_test
    def test_cursor_explicit_close(self):
        yield from self.make_test_data()
        collection = self.collection
        cursor = collection.find()
        yield from cursor.fetch_next
        self.assertTrue(cursor.alive)
        yield from cursor.close()

        # Cursor reports it's alive because it has buffered data, even though
        # it's killed on the server
        self.assertTrue(cursor.alive)
        retrieved = cursor.delegate._Cursor__retrieved
        yield from self.wait_for_cursor(collection, cursor.cursor_id,
                                        retrieved)

    @asyncio_test
    def test_each_cancel(self):
        yield from self.make_test_data()
        loop = self.loop
        collection = self.collection
        results = []
        future = asyncio.Future(loop=self.loop)

        def cancel(result, error):
            if error:
                future.set_exception(error)

            else:
                results.append(result)
                loop.call_soon(canceled)
                return False  # Cancel iteration.

        def canceled():
            try:
                self.assertFalse(cursor.delegate._Cursor__killed)
                self.assertTrue(cursor.alive)

                # Resume iteration
                cursor.each(each)
            except Exception as e:
                future.set_exception(e)

        def each(result, error):
            if error:
                future.set_exception(error)
            elif result:
                pass
                results.append(result)
            else:
                # Complete
                future.set_result(None)

        cursor = collection.find()
        cursor.each(cancel)
        yield from future
        self.assertEqual((yield from collection.count()), len(results))

    @asyncio_test
    def test_each_close(self):
        yield from self.make_test_data()  # 200 documents.
        loop = self.loop
        collection = self.collection
        results = []
        future = asyncio.Future(loop=self.loop)

        def callback(result, error):
            if error:
                future.set_exception(error)

            else:
                results.append(result)
                if len(results) == 50:
                    # Prevent further calls.
                    cursor.close()
                    asyncio.Task(cursor.close(), loop=self.loop)

                    # Soon, finish this test. Leave a little time for further
                    # calls to ensure we've really canceled them by calling
                    # cursor.close().
                    loop.call_later(0.1, partial(future.set_result, None))

        cursor = collection.find()
        cursor.each(callback)
        yield from future
        self.assertGreater(150, len(results))

        # Let cursor finish closing.
        yield from asyncio.sleep(1, loop=self.loop)

    def test_cursor_slice_argument_checking(self):
        collection = self.collection

        for arg in '', None, {}, []:
            self.assertRaises(TypeError, lambda: collection.find()[arg])

        self.assertRaises(IndexError, lambda: collection.find()[-1])

    @asyncio_test
    def test_cursor_slice(self):
        # This is an asynchronous copy of PyMongo's test_getitem_slice_index in
        # test_cursor.py
        yield from self.make_test_data()
        coll = self.collection

        self.assertRaises(IndexError, lambda: coll.find()[-1])
        self.assertRaises(IndexError, lambda: coll.find()[1:2:2])
        self.assertRaises(IndexError, lambda: coll.find()[2:1])

        result = yield from coll.find()[0:].to_list(length=1000)
        self.assertEqual(200, len(result))

        result = yield from coll.find()[20:].to_list(length=1000)
        self.assertEqual(180, len(result))

        result = yield from coll.find()[99:].to_list(length=1000)
        self.assertEqual(101, len(result))

        result = yield from coll.find()[1000:].to_list(length=1000)
        self.assertEqual(0, len(result))

        result = yield from coll.find()[20:25].to_list(length=1000)
        self.assertEqual(5, len(result))

        # Any slice overrides all previous slices
        result = yield from coll.find()[20:25][20:].to_list(length=1000)
        self.assertEqual(180, len(result))

        result = yield from coll.find()[20:25].limit(0).skip(20).to_list(
            length=1000)
        self.assertEqual(180, len(result))

        result = yield from coll.find().limit(0).skip(20)[20:25].to_list(
            length=1000)
        self.assertEqual(5, len(result))

        result = yield from coll.find()[:1].to_list(length=1000)
        self.assertEqual(1, len(result))

        result = yield from coll.find()[:5].to_list(length=1000)
        self.assertEqual(5, len(result))

    @asyncio_test(timeout=30)
    def test_cursor_index(self):
        yield from self.make_test_data()
        coll = self.collection
        cursor = coll.find().sort([('_id', 1)])[0]
        yield from cursor.fetch_next
        self.assertEqual({'_id': 0}, cursor.next_object())

        self.assertEqual(
            [{'_id': 5}],
            (yield from coll.find().sort([('_id', 1)])[5].to_list(100)))

        # Only 200 documents, so 1000th doc doesn't exist. PyMongo raises
        # IndexError here, but Motor simply returns None.
        cursor = coll.find()[1000]
        self.assertFalse((yield from cursor.fetch_next))
        self.assertEqual(None, cursor.next_object())
        self.assertEqual([], (yield from coll.find()[1000].to_list(100)))

    @asyncio_test
    def test_cursor_index_each(self):
        yield from self.make_test_data()
        coll = self.collection

        results = set()
        futures = [asyncio.Future(loop=self.loop) for _ in range(3)]

        def each(result, error):
            if error:
                raise error

            if result:
                results.add(result['_id'])
            else:
                futures.pop().set_result(None)

        coll.find({}, {'_id': 1}).sort([('_id', 1)])[0].each(each)
        coll.find({}, {'_id': 1}).sort([('_id', 1)])[5].each(each)

        # Only 200 documents, so 1000th doc doesn't exist. PyMongo raises
        # IndexError here, but Motor simply returns None, which won't show up
        # in results.
        coll.find()[1000].each(each)

        yield from asyncio.gather(*futures, loop=self.loop)
        self.assertEqual(set([0, 5]), results)

    @asyncio_test
    def test_rewind(self):
        yield from self.collection.insert([{}, {}, {}])
        cursor = self.collection.find().limit(2)

        count = 0
        while (yield from cursor.fetch_next):
            cursor.next_object()
            count += 1
        self.assertEqual(2, count)

        cursor.rewind()
        count = 0
        while (yield from cursor.fetch_next):
            cursor.next_object()
            count += 1
        self.assertEqual(2, count)

        cursor.rewind()
        count = 0
        while (yield from cursor.fetch_next):
            cursor.next_object()
            break

        cursor.rewind()
        while (yield from cursor.fetch_next):
            cursor.next_object()
            count += 1

        self.assertEqual(2, count)
        self.assertEqual(cursor, cursor.rewind())

    @asyncio_test
    def test_del_on_main_greenlet(self):
        # Since __del__ can happen on any greenlet, cursor must be
        # prepared to close itself correctly on main or a child.
        yield from self.make_test_data()
        collection = self.collection
        cursor = collection.find()
        yield from cursor.fetch_next
        cursor_id = cursor.cursor_id
        retrieved = cursor.delegate._Cursor__retrieved
        del cursor
        yield from self.wait_for_cursor(collection, cursor_id, retrieved)

    @asyncio_test
    def test_del_on_child_greenlet(self):
        # Since __del__ can happen on any greenlet, cursor must be
        # prepared to close itself correctly on main or a child.
        yield from self.make_test_data()
        collection = self.collection
        cursor = [collection.find().batch_size(1)]
        yield from cursor[0].fetch_next
        cursor_id = cursor[0].cursor_id
        retrieved = cursor[0].delegate._Cursor__retrieved

        def f():
            # Last ref, should trigger __del__ immediately in CPython and
            # allow eventual __del__ in PyPy.
            del cursor[0]
            return

        greenlet.greenlet(f).switch()
        yield from self.wait_for_cursor(collection, cursor_id, retrieved)

    @asyncio_test
    def test_exhaust(self):
        if (yield from server_is_mongos(self.cx)):
            self.assertRaises(InvalidOperation,
                              self.db.test.find, exhaust=True)
            return

        self.assertRaises(TypeError, self.db.test.find, exhaust=5)

        cur = self.db.test.find(exhaust=True)
        self.assertRaises(InvalidOperation, cur.limit, 5)
        cur = self.db.test.find(limit=5)
        self.assertRaises(InvalidOperation, cur.add_option, 64)
        cur = self.db.test.find()
        cur.add_option(64)
        self.assertRaises(InvalidOperation, cur.limit, 5)

        yield from self.db.drop_collection("test")

        # Insert enough documents to require more than one batch.
        yield from self.db.test.insert([{} for _ in range(150)])

        client = self.asyncio_client(max_pool_size=1)
        # Ensure a pool.
        yield from client.db.collection.find_one()
        socks = client._get_primary_pool().sockets

        # Make sure the socket is returned after exhaustion.
        cur = client[self.db.name].test.find(exhaust=True)
        has_next = yield from cur.fetch_next
        self.assertTrue(has_next)
        self.assertEqual(0, len(socks))

        while (yield from cur.fetch_next):
            cur.next_object()

        self.assertEqual(1, len(socks))

        # Same as previous but with to_list instead of next_object.
        docs = yield from client[self.db.name].test.find(exhaust=True).to_list(
            None)
        self.assertEqual(1, len(socks))
        self.assertEqual(
            (yield from self.db.test.count()),
            len(docs))

        # If the Cursor instance is discarded before being
        # completely iterated we have to close and
        # discard the socket.
        sock = one(socks)
        cur = client[self.db.name].test.find(exhaust=True).batch_size(1)
        has_next = yield from cur.fetch_next
        self.assertTrue(has_next)
        self.assertEqual(0, len(socks))
        if 'PyPy' in sys.version:
            # Don't wait for GC or use gc.collect(), it's unreliable.
            cur.close()

        cursor_id = cur.cursor_id
        retrieved = cur.delegate._Cursor__retrieved
        cur = None

        yield from asyncio.sleep(0.1, loop=self.loop)

        # The exhaust cursor's socket was discarded, although another may
        # already have been opened to send OP_KILLCURSORS.
        self.assertNotIn(sock, socks)
        self.assertTrue(sock.closed)


class MotorCursorMaxTimeMSTest(AsyncIOTestCase):
    def setUp(self):
        super(MotorCursorMaxTimeMSTest, self).setUp()
        self.loop.run_until_complete(self.maybe_skip())

    def tearDown(self):
        self.loop.run_until_complete(self.disable_timeout())
        super(MotorCursorMaxTimeMSTest, self).tearDown()

    @asyncio.coroutine
    def maybe_skip(self):
        if not (yield from at_least(self.cx, (2, 5, 3, -1))):
            raise SkipTest("maxTimeMS requires MongoDB >= 2.5.3")

        cmdline = yield from get_command_line(self.cx)
        if '1' != safe_get(cmdline, 'parsed.setParameter.enableTestCommands'):
            if 'enableTestCommands=1' not in cmdline['argv']:
                raise SkipTest("testing maxTimeMS requires failpoints")

    @asyncio.coroutine
    def enable_timeout(self):
        yield from self.cx.admin.command("configureFailPoint",
                                         "maxTimeAlwaysTimeOut",
                                         mode="alwaysOn")

    @asyncio.coroutine
    def disable_timeout(self):
        self.cx.admin.command("configureFailPoint",
                              "maxTimeAlwaysTimeOut",
                              mode="off")

    @asyncio_test
    def test_max_time_ms_query(self):
        # Cursor parses server timeout error in response to initial query.
        yield from self.enable_timeout()
        cursor = self.collection.find().max_time_ms(100000)
        with self.assertRaises(ExecutionTimeout):
            yield from cursor.fetch_next

        cursor = self.collection.find().max_time_ms(100000)
        with self.assertRaises(ExecutionTimeout):
            yield from cursor.to_list(10)

        with self.assertRaises(ExecutionTimeout):
            yield from self.collection.find_one(max_time_ms=100000)

    @asyncio_test(timeout=60)
    def test_max_time_ms_getmore(self):
        # Cursor handles server timeout during getmore, also.
        yield from self.collection.insert({} for _ in range(200))
        try:
            # Send initial query.
            cursor = self.collection.find().max_time_ms(100000)
            yield from cursor.fetch_next
            cursor.next_object()

            # Test getmore timeout.
            yield from self.enable_timeout()
            with self.assertRaises(ExecutionTimeout):
                while (yield from cursor.fetch_next):
                    cursor.next_object()

            yield from cursor.close()

            # Send another initial query.
            yield from self.disable_timeout()
            cursor = self.collection.find().max_time_ms(100000)
            yield from cursor.fetch_next
            cursor.next_object()

            # Test getmore timeout.
            yield from self.enable_timeout()
            with self.assertRaises(ExecutionTimeout):
                yield from cursor.to_list(None)

            # Avoid 'IOLoop is closing' warning.
            yield from cursor.close()
        finally:
            # Cleanup.
            yield from self.disable_timeout()
            yield from self.collection.remove()

    @asyncio_test
    def test_max_time_ms_each_query(self):
        # Cursor.each() handles server timeout during initial query.
        yield from self.enable_timeout()
        cursor = self.collection.find().max_time_ms(100000)
        future = asyncio.Future(loop=self.loop)

        def callback(result, error):
            if error:
                future.set_exception(error)
            elif not result:
                # Done.
                future.set_result(None)

        with self.assertRaises(ExecutionTimeout):
            cursor.each(callback)
            yield from future

    @asyncio_test(timeout=30)
    def test_max_time_ms_each_getmore(self):
        # Cursor.each() handles server timeout during getmore.
        yield from self.collection.insert({} for _ in range(200))
        try:
            # Send initial query.
            cursor = self.collection.find().max_time_ms(100000)
            yield from cursor.fetch_next
            cursor.next_object()

            future = asyncio.Future(loop=self.loop)

            def callback(result, error):
                if error:
                    future.set_exception(error)
                elif not result:
                    # Done.
                    future.set_result(None)

            yield from self.enable_timeout()
            with self.assertRaises(ExecutionTimeout):
                cursor.each(callback)
                yield from future

            yield from cursor.close()
        finally:
            # Cleanup.
            yield from self.disable_timeout()
            yield from self.collection.remove()


if __name__ == '__main__':
    unittest.main()
