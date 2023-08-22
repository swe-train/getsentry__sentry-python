from copy import copy

import json
import time
import threading

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib import request

from sentry_sdk.utils import logger
from sentry_sdk.utils import json_dumps



DEFAULT_RESPONSE = """<!doctype html>
<html>
<head>
        <title>pipe</title>
</head>
<body>
        <pre id="output"></pre>
        <script type="text/javascript">
const Output = document.getElementById("output");
var EvtSource = new EventSource('/stream');
EvtSource.onmessage = function (event) {
        Output.appendChild(document.createTextNode(event.data));
        Output.appendChild(document.createElement("br"));
};
        </script>
</body>
</html>"""

# TODO(dcramer): this is all probably better done as a proper sidecar, which the SDK can then route to (via sockets, vs in-process)

EMPTY = object()

TERMINATOR = -1

EVENT = "event"
ENVELOPE = "envelope"

# TODO: add time-based expiration
class MessageBuffer(object):
    def __init__(self, size):
        self.size = size
        self.items = [EMPTY] * size
        self.write_pos = 0
        self.lock = threading.Lock()
        self.head = 0
        self.timeout = 60
        self.readers = {
            # ident: read_pos
        }

    def put(self, item):
        cur_time = time.time()
        with self.lock:
            self.items[self.write_pos % self.size] = (cur_time, item)
            self.write_pos += 1
            if self.head == self.write_pos:
                self.head += 1

            min_time = cur_time - self.timeout
            # adjust head based on timeout factor
            while self.head < self.write_pos:
                at_item = self.items[self.head % self.size]
                if at_item is EMPTY:
                    break
                if at_item[0] > min_time:
                    break
                self.head += 1

    def get(self):
        ident = threading.get_ident()
        read_pos = self.readers.setdefault(ident, self.head)
        item = self.items[read_pos % self.size]
        # TODO: could make this wait like a queue
        if item is EMPTY:
            return
        self.readers[ident] += 1
        return item[1]
    
    def __iter__(self):
        ident = threading.get_ident()
        read_pos = self.readers.get(ident, self.head)
        while True:
            item = self.items[read_pos % self.size]
            if item is EMPTY:
                break
            yield item[1]
            read_pos += 1
            self.readers[ident] = read_pos



class ProducerConsumer():
    def __init__(self):
        self.lock = threading.Condition()
        self.waiting = set()
        self.buffer = MessageBuffer(100)

    def consume(self):
        while True:
            with self.lock:
                try:
                    for item in self.buffer:
                        yield copy(item)
                except GeneratorExit:
                    break
                self.lock.wait(1000)

    def produce(self, item):
        with self.lock:
            self.buffer.put(item)
            self.lock.notify_all()


def make_handler(stream: ProducerConsumer):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == '/stream':
                # self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', '*')
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Content-type', 'text/event-stream')
                self.end_headers()

                try:
                    for item in stream.consume():
                        if item is TERMINATOR:
                            break
                        [payload_type, data] = item
                        # TODO: write id
                        # self.wfile.write(f'id: ')
                        self.wfile.write(f'event: {payload_type}\n'.encode())
                        for line in data.splitlines():
                            self.wfile.write(f'data: {line.rstrip()}\n'.encode())
                        self.wfile.write(b'\n')
                        self.wfile.flush()
                except Exception as e:
                    logger.exception(str(e))
                    raise
            else:
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                html = DEFAULT_RESPONSE
                self.wfile.write(html.encode())
        
        def do_OPTIONS(self):
            self.send_response(200)
            if self.path == '/stream':
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Allow-Headers', '*')
                self.send_header('Content-Type', 'application/json')
            self.end_headers()

        def do_POST(self):
            if self.path != '/stream':
                self.send_response(404)
                self.end_headers()
                return
            
            try:
                data = self.rfile.read(int(self.headers['Content-Length'])).decode()
                if self.headers.get("Content-Type") == "application/x-sentry-envelope":
                    payload_type = ENVELOPE
                else:
                    payload_type = EVENT
                stream.produce((payload_type, data))
            except Exception as e:
                logger.exception(str(e))
                self.send_response(500)
            else:
                self.send_response(204)

            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

    return Handler


class SpotlightHttpServer(object):
    def __init__(self, stream: ProducerConsumer, port: int):
        self.port = port
        self.stream = stream
        self.server = None

        self._lock = threading.Lock()
        self._thread = None
        self._active = True

    def stop(self):
        with self._lock:
            self._active = False
            if self.server is not None:
                self.server.shutdown()
            
            self.stream.produce(TERMINATOR)

            if self._thread is not None:
                while self._thread.is_alive():
                    logger.warning("[spotlight] Waiting for sidecar to shutdown")
                    time.sleep(100)
                self._thread = None
        print("[spotlight] HTTP server has stopped")

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise Exception
            
            if self.server is None:
                self.server = ThreadingHTTPServer(('', self.port), make_handler(self.stream), False)

            self._thread = threading.Thread(
                target=self.try_start,
                name="sentry-spotlight.HttpServer",
                daemon=True,
            )
            self._thread.start()
            logger.info(f"[spotlight] Sidecar starting on :{self.port}")

    def try_start(self):
        while self._active:
            with self._lock:
                try:
                    self.server.server_bind()
                except OSError as e:
                    if e.errno != 98:
                        raise
                self.server.server_activate()
                self.server.serve_forever()
                print(f"[spotlight] HTTP server is running on :{self.port}")
            time.sleep(1000)

    def wait(self):
        self._thread.join()

    def __del__(self):
        self.stop()


class SpotlightSidecar(object):
    def __init__(self, port):
        self.port = port
        self.stream = ProducerConsumer()
        
        self._lock = threading.Lock()
        self._http = None

    def ensure(self):
        with self._lock:
            if self._http is None:
                self._http = SpotlightHttpServer(self.stream, self.port)
                self._http.start()
    
    def wait(self):
        self._http.wait()

    def kill(self):
        with self._lock:
            if self._http:
                self._http.stop()
                self._http = None

    def capture_event(self, event):
        req =  request.Request(
            f"http://localhost:{self.port}/stream",
            data=json_dumps(event),
            headers={
                'Content-Type': 'application/json',
            },
        )
        try:
            request.urlopen(req)
        except Exception as e:
            logger.exception(str(e))


instance = None

def setup_spotlight(options):
    global instance

    if instance is None:
        instance = SpotlightSidecar(port=8969)
    
    instance.ensure()
    
    return instance


if __name__ == '__main__':
    from sentry_sdk.debug import configure_logger
    configure_logger()

    spotlight = setup_spotlight({})
    spotlight.wait()
