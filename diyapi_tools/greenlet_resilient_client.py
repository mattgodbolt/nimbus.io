# -*- coding: utf-8 -*-
"""
resilient_client.py

a class that manages a zeromq XREQ socket as a client,
to a resilient server
"""
from collections import deque, namedtuple
import logging
import time
import uuid

from gevent.queue import Queue
from gevent.coros import RLock
from gevent_zeromq import zmq

# our internal message format
_message_format = namedtuple("Message", "control body")
_ack_timeout = 10.0
_handshake_retry_interval = 60.0

_status_handshaking = 1
_status_connected = 2
_status_disconnected = 3

class GreelletResilientClient(object):
    """
    a class that manages a zeromq XREQ socket as a client
    """
    polling_interval = 3.0

    def __init__(
        self, 
        context, 
        server_address, 
        client_tag, 
        client_address,
        deliverator
    ):
        self._log = logging.getLogger("ResilientClient-%s" % (
            server_address, 
        ))

        self._xreq_socket = context.socket(zmq.XREQ)
        self._log.debug("connecting")
        self._xreq_socket.connect(server_address)

        # note that we treat pending_message as en extension of the queue
        # it should be locked whenever the queue is locked
        self._send_queue = deque()
        self._pending_message = None
        self._pending_message_start_time = None
        self._send_queue_lock = RLock()

        self._client_tag = client_tag
        self._client_address = client_address
        self._deliverator = deliverator

        self._status = _status_disconnected
        self._status_time = time.time()

        self._send_handshake()

    def register(self, pollster):
        pollster.register_read(
            self._xreq_socket, self._pollster_callback
        )

    def unregister(self, pollster):
        pollster.unregister(self._xreq_socket)

    def close(self):
        self._xreq_socket.close()

    def queue_message_for_send(self, message_control, data=None):

        if not "request-id" in message_control:
            message_control["request-id"] = uuid.uuid1().hex

        request_id = message_control["request-id"]
        delivery_channel = self._deliverator.add_request(request_id)

        message = _message_format(control=message_control, body=data)

        self._send_queue.lock.acquire()

        if self._status is _status_connected and self._pending_message is None:
            self._send_message(message)
            self._pending_message = message
            self._pending_message_start_time = time.time()
        else:
            self._send_queue.put(message)

        self._send_queue.lock.release()

        return delivery_channel

    def _send_handshake(self):
        self._log.info("sending handshake")
        message = {
            "message-type"      : "resilient-server-handshake",
            "request-id"        : uuid.uuid1().hex,
            "client-tag"        : self._client_tag,
            "client-address"    : self._client_address,
        }
        self._send_queue.lock.acquire()
        self._send_message(_message_format(control=message, body=None))
        self._pending_message = message
        self._pending_message_start_time = time.time()
        self._send_queue.lock.release()
        self._status = _status_handshaking
        self._status_time = time.time()

    def _pollster_callback(self, _active_socket, readable, writable):
        message = self._receive_message()     

        # if we get None, that means the socket would have blocked
        # go back and wait for more
        if message is None:
            return None

        self._send_queue.lock.acquire()
        try:
            if self._pending_message is None:
                self._log.error("Unexpected message: %s" % (message.control, ))
                return

            expected_request_id = self._pending_message.control["request-id"]
            if message["request-id"] != expected_request_id:
                self._log.error("unknown ack %s expecting %s" %(
                    message, self._pending_message 
                ))
                return

            # if we got and ack to a handshake request, we are connected
            if self._pending_message.control["message-type"] == \
                "resilient-server-handshake":
                assert self._status == _status_handshaking, self._status
                self._status = _status_connected
                self._status_time = time.time()

            self._pending_message = None
            self._pending_message_start_time = None

            try:
                message_to_send = self._send_queue.popleft()
            except IndexError:
                return

            self._send_message(message_to_send)
            self._pending_message = message
            self._pending_message_start_time = time.time()
        finally:
            self._send_queue.lock.release()

    def _send_message(self, message):
        self._log.info("sending message: %s" % (message.control, ))
        message.control["client-tag"] = self._client_tag
        if message.body is not None:
            self._xreq_socket.send_json(message.control, zmq.SNDMORE)
            if type(message.body) not in [list, tuple, ]:
                message = message._replace(body=[message.body, ])
            for segment in message.body[:-1]:
                self._xreq_socket.send(segment, zmq.SNDMORE)
            self._xreq_socket.send(message.body[-1])
        else:
            self._xreq_socket.send_json(message.control)

    def _receive_message(self):
        # we should only be receiving ack, so we don't
        # check for multipart messages
        try:
            return self._xreq_socket.recv_json(zmq.NOBLOCK)
        except zmq.ZMQError, instance:
            if instance.errno == zmq.EAGAIN:
                self._log.warn("socket would have blocked")
                return None
            raise

    def run(self, halt_event):
        """
        time_queue task to check for timeouts and retries
        """
        if halt_event.is_set():
            self._log.info("halt event is set")
            return []

        if self._status == _status_connected:
            self._send_queue.lock.acquire()
            if  self._pending_message is not None:
                elapsed_time = time.time() - self._pending_message_start_time
                if elapsed_time > _ack_timeout:
                    self._log.warn(
                        "timeout waiting ack: treating as disconnect %s" % (
                            self._pending_message,
                        )
                    )
                    self._status = _status_disconnected
                    self._status_time = time.time()
                    # put the message at the head of the send queue 
                    self._send_queue.appendleft(self._pending_message)
                    self._pending_message = None
                    self._pending_message_start_time = None
            self._send_queue.lock.release()
        elif self._status == _status_disconnected:
            elapsed_time = time.time() - self._status_time 
            if elapsed_time > _handshake_retry_interval:
                self._send_handshake()
        elif self._status == _status_handshaking:
            self._send_queue.lock.acquire()
            assert self._pending_message is not None
            elapsed_time = time.time() - self._pending_message_start_time
            if elapsed_time > _ack_timeout:
                self._log.warn("timeout waiting handshake ack")
                self._status = _status_disconnected
                self._status_time = time.time()
                self._pending_message = None
                self._pending_message_start_time = None
            self._send_queue.lock.release()
        else:
            self._log.error("unknown status '%s'" % (self._status, ))

        return [(self.run, time.time() + self.polling_interval, ), ]
