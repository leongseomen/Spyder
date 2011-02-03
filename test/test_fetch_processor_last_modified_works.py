#
# Copyright (c) 2010 Daniel Truemper truemped@googlemail.com
#
# test_fetch_processor_last_modified_works.py 17-Jan-2011
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
# under the License.
#
#

import os
import os.path
import time
from datetime import datetime
import random

import unittest

import tornado
import tornado.httpserver
import tornado.web

import zmq
from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream

from spyder.core.constants import ZMQ_SPYDER_MGMT_WORKER
from spyder.core.constants import ZMQ_SPYDER_MGMT_WORKER_QUIT
from spyder.core.constants import ZMQ_SPYDER_MGMT_WORKER_QUIT_ACK
from spyder.core.messages import DataMessage, MgmtMessage
from spyder.core.messages import serialize_date_time
from spyder.core.mgmt import ZmqMgmt
from spyder.core.worker import AsyncZmqWorker
from spyder.core.settings import Settings
from spyder.processor.fetcher import FetchProcessor
from spyder.thrift.gen.ttypes import CrawlUri


class ZmqTornadoIntegrationTest(unittest.TestCase):

    def setUp(self):

        # create the io_loop
        self._io_loop = IOLoop.instance()

        # and the context
        self._ctx = zmq.Context(1)

        # setup the mgmt sockets
        self._setup_mgmt_sockets()

        # setup the data sockets
        self._setup_data_sockets()

        # setup the management interface
        self._mgmt = ZmqMgmt( self._mgmt_sockets['worker_sub'],
            self._mgmt_sockets['worker_pub'], io_loop=self._io_loop)
        self._mgmt.start()
        self._mgmt.add_callback(ZMQ_SPYDER_MGMT_WORKER, self.on_mgmt_end)

    def tearDown(self):
        # stop the mgmt
        self._mgmt.stop()

        # close all sockets
        for socket in self._mgmt_sockets.itervalues():
            socket.close()
        for socket in self._worker_sockets.itervalues():
            socket.close()

        # terminate the context
        self._ctx.term()

    def _setup_mgmt_sockets(self):

        self._mgmt_sockets = dict()

        # adress for the communication from master to worker(s)
        mgmt_master_worker = 'inproc://master/worker/coordination/'

        # connect the master with the worker
        # the master is a ZMQStream because we are sending msgs from the test
        sock = self._ctx.socket(zmq.PUB)
        sock.bind(mgmt_master_worker)
        self._mgmt_sockets['master_pub'] = ZMQStream(sock, self._io_loop)
        # the worker stream is created inside the ZmqMgmt class
        self._mgmt_sockets['worker_sub'] = self._ctx.socket(zmq.SUB)
        self._mgmt_sockets['worker_sub'].setsockopt(zmq.SUBSCRIBE, "")
        self._mgmt_sockets['worker_sub'].connect(mgmt_master_worker)

        # adress for the communication from worker(s) to master
        mgmt_worker_master = 'inproc://worker/master/coordination/'

        # connect the worker with the master
        self._mgmt_sockets['worker_pub'] = self._ctx.socket(zmq.PUB)
        self._mgmt_sockets['worker_pub'].bind(mgmt_worker_master)
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, "")
        sock.connect(mgmt_worker_master)
        self._mgmt_sockets['master_sub'] = ZMQStream(sock, self._io_loop)

    def _setup_data_sockets(self):

        self._worker_sockets = dict()

        # address for master -> worker communication
        data_master_worker = 'inproc://master/worker/pipeline/'

        sock = self._ctx.socket(zmq.PUSH)
        sock.bind(data_master_worker)
        self._worker_sockets['master_push'] = ZMQStream(sock, self._io_loop)
        self._worker_sockets['worker_pull'] = self._ctx.socket(zmq.PULL)
        self._worker_sockets['worker_pull'].connect(data_master_worker)

        # address for worker -> master communication
        data_worker_master = 'inproc://worker/master/pipeline/'

        self._worker_sockets['worker_pub'] = self._ctx.socket(zmq.PUB)
        self._worker_sockets['worker_pub'].bind(data_worker_master)
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, "")
        sock.connect(data_worker_master)
        self._worker_sockets['master_sub'] = ZMQStream(sock, self._io_loop)

    def on_mgmt_end(self, _msg):
        self._io_loop.stop()


class SimpleFetcherTestCase(ZmqTornadoIntegrationTest):

    port = 8085

    def setUp(self):
        ZmqTornadoIntegrationTest.setUp(self)

        self._path = os.path.join(os.path.dirname(__file__), "static")
        application = tornado.web.Application([
            (r"/(.*)", tornado.web.StaticFileHandler, {"path": self._path}),
        ])
        self._server = tornado.httpserver.HTTPServer(application, io_loop =
                self._io_loop)
        self._server.listen(self.port)

    def tearDown(self):
        ZmqTornadoIntegrationTest.tearDown(self)
        self._server.stop()

    def test_fetching_last_modified_works(self):

        settings = Settings()
        fetcher = FetchProcessor(settings, io_loop=self._io_loop)

        worker = AsyncZmqWorker( self._worker_sockets['worker_pull'],
            self._worker_sockets['worker_pub'],
            self._mgmt,
            fetcher,
            self._io_loop)
        worker.start()

        mtimestamp = datetime.fromtimestamp(os.stat(os.path.join(self._path,
                        "robots.txt")).st_mtime)
        mtime = serialize_date_time(mtimestamp)
        curi = CrawlUri(url="http://localhost:%s/robots.txt" % self.port,
                effective_url="http://127.0.0.1:%s/robots.txt" % self.port,
                req_header = { "Last-Modified" :
                    mtime }
                )

        msg = DataMessage()
        msg.identity = "me"
        msg.curi = curi

        def assert_expected_result_and_stop(raw_msg):
            msg = DataMessage(raw_msg)
            self.assertEqual(304, msg.curi.status_code)
            self.assertEqual("", msg.curi.content_body)
            death = MgmtMessage(topic=ZMQ_SPYDER_MGMT_WORKER,
                    data=ZMQ_SPYDER_MGMT_WORKER_QUIT)
            self._mgmt_sockets['master_pub'].send_multipart(death.serialize())

        self._worker_sockets['master_sub'].on_recv(assert_expected_result_and_stop)

        self._worker_sockets['master_push'].send_multipart(msg.serialize())

        self._io_loop.start()


if __name__ == '__main__':
    unittest.main()
