import asyncio
import datetime
import json
import logging
import os
import ssl
import sys
import threading

import websockets

from aiortc import RTCIceCandidate, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

logger = logging.getLogger(__name__)
BYE = object()


def object_from_string(message_str):
    message = json.loads(message_str)
    if message["type"] in ["answer", "offer"]:
        return RTCSessionDescription(**message)
    elif message["type"] == "candidate" and message["candidate"]:
        candidate = candidate_from_sdp(message["candidate"].split(":", 1)[1])
        candidate.sdpMid = message["id"]
        candidate.sdpMLineIndex = message["label"]
        return candidate
    elif message["type"] == "bye":
        return BYE


def object_to_string(obj):
    if isinstance(obj, RTCSessionDescription):
        message = {"sdp": obj.sdp, "type": obj.type}
    elif isinstance(obj, RTCIceCandidate):
        message = {
            "candidate": "candidate:" + candidate_to_sdp(obj),
            "id": obj.sdpMid,
            "label": obj.sdpMLineIndex,
            "type": "candidate",
        }
    else:
        assert obj is BYE
        message = {"type": "bye"}
    return json.dumps(message, sort_keys=True)


def debug(*args) -> None:
    print(datetime.datetime.now().time().isoformat(), threading.current_thread().name, *args, file=sys.stderr)


class SimpleWebsocketSignaling:
    server = "wss://zhuker.video:8443"
    our_id = "4241"
    remote_id = "4240"

    def __init__(self, server, our_id, remote_id, request_offer: bool):
        self.server = server
        self.request_offer = request_offer
        self.our_id = our_id
        self.remote_id = remote_id
        self.wsloop = None
        self.wsconn = None

    async def connect(self):
        await self.websocket_connect()

    async def close(self):
        debug("close")
        if self.wsconn is not None:
            await self.wsconn.close()
            self.wsconn = None

    async def receive(self):
        recv = await self.wsconn.recv()
        debug("<", recv)
        return object_from_string(recv)

    async def send(self, descr):
        await self.send_ws_msg(object_to_string(descr))

    async def websocket_connect(self):
        if self.server.startswith("wss://"):
            sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
            self.wsconn = await websockets.connect(self.server, ssl=sslctx, ping_interval=5)
        else:
            self.wsconn = await websockets.connect(self.server, ping_interval=5)
        await self.send_ws_msg(f'HELLO {self.our_id}')
        msg = await self.wsconn.recv()
        if "HELLO" != msg:
            raise Exception("expected HELLO. got unhandled response " + msg)

        if self.request_offer:
            await self.send_ws_msg(f'SESSION {self.remote_id}')
            msg = await self.wsconn.recv()
            if 'SESSION_OK' == msg:
                await self.send_ws_msg(f'OFFER_REQUEST')
            else:
                raise Exception("expected SESSION_OK. got unhandled response " + msg)
        else:
            msg = await self.wsconn.recv()
            if 'OFFER_REQUEST' == msg:
                debug("offer requested")
            else:
                raise Exception("unhandled response " + msg)

    async def send_ws_msg(self, txt: str):
        debug(f'> {txt}')
        await self.wsconn.send(txt)

    async def websocket_recvloop(self):
        assert self.wsconn
        async for message in self.wsconn:
            debug(f'< {message}')
            if 'HELLO' == message:
                debug('registered with server as peer', self.our_id)
                await self.send_ws_msg(f'SESSION {self.remote_id}')
            elif 'SESSION_OK' == message:
                debug('waiting for offer')
                await self.send_ws_msg(f'OFFER_REQUEST')

class CopyAndPasteSignaling:
    def __init__(self):
        self._read_pipe = sys.stdin
        self._read_transport = None
        self._reader = None
        self._write_pipe = sys.stdout

    async def connect(self):
        loop = asyncio.get_event_loop()
        self._reader = asyncio.StreamReader(loop=loop)
        self._read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._reader), self._read_pipe
        )

    async def close(self):
        if self._reader is not None:
            await self.send(BYE)
            self._read_transport.close()
            self._reader = None

    async def receive(self):
        print("-- Please enter a message from remote party --")
        data = await self._reader.readline()
        print()
        return object_from_string(data.decode(self._read_pipe.encoding))

    async def send(self, descr):
        print("-- Please send this message to the remote party --")
        self._write_pipe.write(object_to_string(descr) + "\n")
        self._write_pipe.flush()
        print()


class TcpSocketSignaling:
    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._server = None
        self._reader = None
        self._writer = None

    async def connect(self):
        pass

    async def _connect(self, server):
        if self._writer is not None:
            return

        if server:
            connected = asyncio.Event()

            def client_connected(reader, writer):
                self._reader = reader
                self._writer = writer
                connected.set()

            self._server = await asyncio.start_server(
                client_connected, host=self._host, port=self._port
            )
            await connected.wait()
        else:
            self._reader, self._writer = await asyncio.open_connection(
                host=self._host, port=self._port
            )

    async def close(self):
        if self._writer is not None:
            await self.send(BYE)
            self._writer.close()
            self._reader = None
            self._writer = None
        if self._server is not None:
            self._server.close()
            self._server = None

    async def receive(self):
        await self._connect(False)
        try:
            data = await self._reader.readuntil()
        except asyncio.IncompleteReadError:
            return
        return object_from_string(data.decode("utf8"))

    async def send(self, descr):
        await self._connect(True)
        data = object_to_string(descr).encode("utf8")
        self._writer.write(data + b"\n")


class UnixSocketSignaling:
    def __init__(self, path):
        self._path = path
        self._server = None
        self._reader = None
        self._writer = None

    async def connect(self):
        pass

    async def _connect(self, server):
        if self._writer is not None:
            return

        if server:
            connected = asyncio.Event()

            def client_connected(reader, writer):
                self._reader = reader
                self._writer = writer
                connected.set()

            self._server = await asyncio.start_unix_server(
                client_connected, path=self._path
            )
            await connected.wait()
        else:
            self._reader, self._writer = await asyncio.open_unix_connection(self._path)

    async def close(self):
        if self._writer is not None:
            await self.send(BYE)
            self._writer.close()
            self._reader = None
            self._writer = None
        if self._server is not None:
            self._server.close()
            self._server = None
            os.unlink(self._path)

    async def receive(self):
        await self._connect(False)
        try:
            data = await self._reader.readuntil()
        except asyncio.IncompleteReadError:
            return
        return object_from_string(data.decode("utf8"))

    async def send(self, descr):
        await self._connect(True)
        data = object_to_string(descr).encode("utf8")
        self._writer.write(data + b"\n")


def add_signaling_arguments(parser):
    """
    Add signaling method arguments to an argparse.ArgumentParser.
    """
    parser.add_argument(
        "--signaling",
        "-s",
        choices=["copy-and-paste", "tcp-socket", "unix-socket", "websocket"],
    )
    parser.add_argument(
        "--signaling-url", default="", help="Signaling url (eg wss://server:443)"
    )
    parser.add_argument(
        "--our-id", default="", help="Signaling id of this host (websocket only)"
    )
    parser.add_argument(
        "--remote-id", default="", help="Signaling id of remote host to connect to (websocket only)"
    )
    parser.add_argument(
        "--signaling-host", default="127.0.0.1", help="Signaling host (tcp-socket only)"
    )
    parser.add_argument(
        "--signaling-port", default=1234, help="Signaling port (tcp-socket only)"
    )
    parser.add_argument(
        "--signaling-path",
        default="aiortc.socket",
        help="Signaling socket path (unix-socket only)",
    )


def create_signaling(args):
    """
    Create a signaling method based on command-line arguments.
    """
    if args.signaling == "tcp-socket":
        return TcpSocketSignaling(args.signaling_host, args.signaling_port)
    elif args.signaling == "unix-socket":
        return UnixSocketSignaling(args.signaling_path)
    elif args.signaling == "websocket":
        return SimpleWebsocketSignaling(args.signaling_url, args.our_id, args.remote_id, args.role == "answer")
    else:
        return CopyAndPasteSignaling()
